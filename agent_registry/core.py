# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# agent_registry/core.py
import json
import copy
import os
import re
from contextlib import contextmanager
from pathlib import Path
IS_WINDOWS = os.name == 'nt'
from threading import Lock
from typing import List, Dict, Tuple, Optional, Any

from a2a.types import AgentCard
from google.protobuf.json_format import MessageToDict, Parse
from loguru import logger

from agent_registry import status as status_policy
from agent_registry.errors import SemanticSearchUnavailable, authoritative_store_unavailable
from agent_registry.model.tag import Tag
from agent_registry.config import PERSISTENCE_FILE, PERSISTENCE_METADATA_FILE, USE_VECTORDB, COLLECTION_NAME, \
    PERSISTENCE_CONF, PERSISTENCE_MODE
from agent_registry.persistence import StorageRegistry, StorageBackend
from agent_registry.persistence.base import AgentRecord
from agent_registry.prompts import build_agent_selection_prompt
from agent_registry.broadcast import get_event_bus
from agent_registry.broadcast.events import EventType
from common.llm import get_llm_instance, get_embed_instance
from common.util.app_config import get_root_path
from common.vector_db.vector_db_client.config.vector_db_client_registry import get_or_create_vectordb_tool_instance
from common.vector_db.vector_db_client.config.vector_db_config import VectorDBType, get_vectordb_config_by_type


def make_agent_key(name: str, organization: str) -> Tuple[str, str]:
    """Create a normalized key for indexing."""
    return name.strip(), organization.strip()


def make_agent_id(name: str, organization: str) -> str:
    """Create a delimited ID for vector database indexing."""
    return f"{name}::{organization}"


#: Only cards in this status are discoverable. Every public surface (list,
#: exact query, semantic query, change feed, webhook delivery) applies this one
#: rule, and a card only produces public change events while it holds it.
#: Defined in agent_registry.status and re-exported here for existing callers.
DISCOVERABLE_STATUS = status_policy.DISCOVERABLE_STATUS
PENDING_STATUS = status_policy.PENDING_STATUS
is_discoverable_status = status_policy.is_discoverable_status


# Storage backends that share the SQL implementation, plus the JSON-file default.
SQL_PERSISTENCE_MODES = ('postgresql', 'sqlite', 'gauss', 'mysql')
FILE_PERSISTENCE_MODE = 'file'
KNOWN_PERSISTENCE_MODES = (FILE_PERSISTENCE_MODE,) + SQL_PERSISTENCE_MODES


def validate_persistence_mode(mode: str) -> str:
    """Return the normalized persistence mode, or raise for an unknown one.

    An unknown `persistence.mode` used to fall through to the file backend, so a
    typo silently served the operator a different store than the one configured.
    """
    normalized = str(mode or '').strip().lower()
    if normalized not in KNOWN_PERSISTENCE_MODES:
        raise ValueError(
            f"Unknown persistence.mode '{mode}'. "
            f"Supported: {', '.join(KNOWN_PERSISTENCE_MODES)}"
        )
    return normalized


class RegistryCore:
    """
    Core registry that stores AgentCard instances with (name, organization) as unique key.
    Provides registration, update, deletion, exact search, and LLM-based fuzzy search.
    Supports persistence to a JSON file, PostgreSQL, or vectordb.
    """

    def __init__(self, persistence_file: str = PERSISTENCE_FILE,
                 persistence_metadata_file: str = PERSISTENCE_METADATA_FILE,
                 use_vectordb: bool = USE_VECTORDB,
                 persistence_mode: str = PERSISTENCE_MODE, persistence_conf: dict = PERSISTENCE_CONF):
        self._llm = None
        self._llm_lock = Lock()
        self.use_vectordb = use_vectordb
        self.persistence_mode = validate_persistence_mode(persistence_mode)
        self.persistence_conf = persistence_conf
        self.storage: Optional[StorageBackend] = None
        self._lock = Lock()
        # Set while a unit of work is open: event failures must then roll the
        # mutation back instead of being swallowed (see _publish_event).
        self._event_must_succeed = False
        # Events persisted inside the open unit of work, notified after commit.
        self._deferred_events = None

        if use_vectordb:
            self.vectordb = get_or_create_vectordb_tool_instance(get_vectordb_config_by_type(VectorDBType.Milvus))
            self.embedding_tool = get_embed_instance()
        elif self.persistence_mode in SQL_PERSISTENCE_MODES:
            self.storage = StorageRegistry.get_backend(self.persistence_mode, self.persistence_conf)
            logger.info(f"Registry initialized with {self.persistence_mode} storage")
        else:
            # 'file' is the only remaining known mode once the mode is validated.
            data_path = Path(get_root_path()) / "data"
            data_path.mkdir(parents=True, exist_ok=True)
            if not IS_WINDOWS:
                os.chmod(data_path, 0o700)
            file_storage_conf = {
                'file.path': str(data_path / persistence_file),
                'metadata.file': str(data_path / persistence_metadata_file),
                'tags.file': str(data_path / "tags.json"),
            }
            self.storage = StorageRegistry.get_backend('file', file_storage_conf)
            logger.info(f"Registry initialized with file storage at {data_path}")

    @property
    def llm(self):
        """Return the chat model, resolving it on first use.

        The model definition lives in a local models.yaml, so resolving it in the
        constructor would stop the service from starting when a host has none.
        Deferring the lookup keeps registration and exact search usable, and the
        caller that needs the model reports the missing definition instead.
        """
        if self._llm is None:
            with self._llm_lock:
                if self._llm is None:
                    self._llm = get_llm_instance()
        return self._llm

    def require_authoritative_store(self, operation: str) -> None:
        """Public form of the guard for surfaces that must fail *before* answering.

        A streaming endpoint (SSE) commits its 200 response as soon as it starts
        yielding, so a missing-store error raised inside the generator can no
        longer become a 503. Such a surface calls this up front instead.
        """
        self._require_authoritative_store(operation)

    def _require_authoritative_store(self, operation: str) -> None:
        """Refuse to guess when ``use_vectordb=true`` removed the record store.

        A vector index is a search projection, not a record store: it has no
        unique keys, no transactions and no durable approval/ownership state. The
        registry's own API is defined in terms of that state, so this mode must
        fail loudly instead of answering "no results" to a question it cannot
        answer (see R4 in the 10-03 review).
        """
        if self.use_vectordb:
            raise authoritative_store_unavailable(operation)

    def _uses_vectordb(self, use_vectordb: Optional[bool]) -> bool:
        """Whether this call takes the vector path.

        The instance flag is what decides if a record store exists at all, so it
        is the default; the module-level config value only seeds the constructor.
        An explicit argument from a caller still wins. Before this, a call that
        omitted the argument fell back to the *config* while the instance had been
        built without storage, which turned a coherent "no record store" into an
        AttributeError on `self.storage` (or, worse, a silent empty result).
        """
        return self.use_vectordb if use_vectordb is None else use_vectordb

    def initialize(self):
        """Initialize storage backend for file or PostgreSQL mode."""
        if not self.use_vectordb and not self.storage:
            self.storage = StorageRegistry.get_backend(self.persistence_mode, self.persistence_conf)
            logger.info(f"Registry initialized with {self.persistence_mode} storage")

    def close(self):
        """Close storage backend connection."""
        if self.storage:
            self.storage.close()

    @staticmethod
    def _make_key(name: str, organization: str) -> Tuple[str, str]:
        """Create a normalized key for indexing."""
        return make_agent_key(name, organization)

    def register(self, agent: AgentCard, use_vectordb: Optional[bool] = None, owner: Optional[str] = None) -> bool:
        """
        Register a new agent. Returns True if successful, False if duplicate.
        Raises ValueError if agent lacks required fields (name, provider.organization).
        """
        return self.register_with_status(agent, initial_status='published', use_vectordb=use_vectordb, owner=owner)

    def register_with_status(self, agent: AgentCard, initial_status: str = 'published',
                             use_vectordb: Optional[bool] = None, owner: Optional[str] = None) -> bool:
        """
        Register a new agent with specified initial status.

        A card that starts as 'registered' (pending approval) is not
        discoverable, so it must not produce a public change event: subscribers
        would otherwise learn about unpublished cards. The approval transition
        emits the "now discoverable" event (see update_status).
        """
        with self._lock:
            use_vectordb = self._uses_vectordb(use_vectordb)
            if use_vectordb:
                entity_str = json.dumps(MessageToDict(agent, preserving_proto_field_name=True))
                embedding = self.embedding_tool.embed(agent.description)
                id = self._make_id(agent.name, agent.provider.organization)
                insert_entity = {"embedding": embedding, "id": id, "name": agent.name,
                                 "description": agent.description,
                                 "organization": agent.provider.organization,
                                 "agent_card": entity_str, "status": initial_status, "owner": owner}
                insert_data = {"collection_name": COLLECTION_NAME, "entity": insert_entity}
                result = self.vectordb.insert_entity(insert_data)
            else:
                with self._atomic_write():
                    result = self.storage.create(agent, owner=owner, status=initial_status)
                    if result:
                        logger.info(
                            f"Registered agent: {agent.name} (org={agent.provider.organization}, status={initial_status}, owner={owner})")
                    if result:
                        self._publish_discoverability_event(
                            EventType.AGENT_REGISTERED, agent.name, agent.provider.organization,
                            visible=is_discoverable_status(initial_status),
                            card_data=MessageToDict(agent, preserving_proto_field_name=True))
                return result
            if result:
                self._publish_discoverability_event(
                    EventType.AGENT_REGISTERED, agent.name, agent.provider.organization,
                    visible=is_discoverable_status(initial_status),
                    card_data=MessageToDict(agent, preserving_proto_field_name=True))
            return result

    def find_exact(self, name: Optional[str] = None, organization: Optional[str] = None,
                   use_vectordb: Optional[bool] = None) -> List[AgentCard]:
        """
        Exact search based on name, organization.
        All parameters are optional; if multiple are given, they are combined with AND.
        """
        use_vectordb = self._uses_vectordb(use_vectordb)
        if use_vectordb:
            if name is not None and organization is not None:
                query_data = {"collection_name": COLLECTION_NAME, "key": "id",
                              "value": self._make_id(name, organization)}
            elif name is not None:
                query_data = {"collection_name": COLLECTION_NAME, "key": "name", "value": name}
            elif organization is not None:
                query_data = {"collection_name": COLLECTION_NAME, "key": "organization", "value": organization}
            else:
                entities = self.vectordb.get_all_entities({"collection_name": COLLECTION_NAME})
                result = []
                for agent_dict in entities:
                    agent_card_json = agent_dict.get("agent_card", "{}")
                    result.append(Parse(agent_card_json, AgentCard()))
                return result
            return self.vectordb.query_by_key(query_data)
        else:
            if name and organization:
                record = self.storage.find_by_key(name, organization)
                return [record.agent_card] if record else []
            elif name:
                return self.storage.find_by_name(name)
            elif organization:
                return self.storage.find_by_organization(organization)
            return self.storage.find_all()

    def get_agents(self, use_vectordb: Optional[bool] = None):
        """Map of (name, organization) keys.

        Index-aware in vector-only mode on purpose: registration uses this for
        duplicate detection, and registration is what that mode still supports.
        """
        use_vectordb = self._uses_vectordb(use_vectordb)
        if use_vectordb:
            entities = self.vectordb.get_all_entities({"collection_name": COLLECTION_NAME})
            result = {}
            for agent_dict in entities:
                key = make_agent_key(agent_dict.get("name", ""), agent_dict.get("organization", ""))
                result[key] = True
            return result
        else:
            agents = self.storage.find_all()
            result = {}
            for agent in agents:
                key = make_agent_key(agent.name, agent.provider.organization)
                result[key] = True
            return result

    def update(self, name: str, organization: str, agent_data: Dict[str, Any],
               use_vectordb: Optional[bool] = None, owner: Optional[str] = None) -> bool:
        """
        Update an existing agent. The primary key (name, organization) cannot be changed.
        Owner permission must be verified by the caller before invoking.
        Return True if successful, False if not found.
        """
        # An update must announce what changed to the change feed, which needs the
        # authoritative status; the index alone cannot decide visibility.
        self._require_authoritative_store("updating an agent")
        if (agent_data.get('name') != name
                or agent_data.get('provider', {}).get('organization') != organization):
            raise ValueError('Cannot change primary key(name or organization) during update.')
        with self._lock:
            use_vectordb = self._uses_vectordb(use_vectordb)
            if use_vectordb:
                entity_str = json.dumps(agent_data)
                embedding = self.embedding_tool.embed(agent_data["description"])
                key = self._make_id(agent_data["name"], agent_data["provider"]["organization"])
                insert_entity = {"id": key, "embedding": embedding, "name": agent_data["name"],
                                 "description": agent_data["description"],
                                 "organization": agent_data["provider"]["organization"], "agent_card": entity_str,
                                 "owner": owner}
                update_data = {"collection_name": COLLECTION_NAME, "entity": insert_entity}
                result = self.vectordb.update_entity(update_data)
                logger.info(f"Updated agent in vectordb: {name}({organization}, owner={owner})")
                return result
            with self._atomic_write():
                visible = is_discoverable_status(self._stored_status(name, organization))
                result = self.storage.update(name, organization, agent_data, owner=owner)
                logger.info(f"Updated agent: {name}({organization}, owner={owner})")
                if result:
                    self._publish_discoverability_event(
                        EventType.AGENT_UPDATED, name, organization, visible, card_data=agent_data)
            return result

    def deregister(self, name: str, organization: str, use_vectordb: Optional[bool] = None,
                   owner: Optional[str] = None) -> bool:
        """
        Remove an agent. Returns True if deleted, False if not found.
        Owner permission must be verified by the caller before invoking.
        """
        # A removal that is not announced would leave every subscriber serving a
        # deregistered card forever.
        self._require_authoritative_store("deregistering an agent")
        with self._lock:
            use_vectordb = self._uses_vectordb(use_vectordb)
            if use_vectordb:
                delete_data = {"collection_name": COLLECTION_NAME, "id": self._make_id(name, organization)}
                result = self.vectordb.delete_entity(delete_data)
                logger.info(f"Deregistered agent from vectordb: {name}({organization}, owner={owner})")
                return result
            health_cleanup_deferred = False
            with self._atomic_write():
                # Read visibility before the row disappears: a pending card that is
                # removed was never announced, so it must not produce a public
                # removal event either.
                visible = is_discoverable_status(self._stored_status(name, organization))
                result = self.storage.delete(name, organization, owner=owner)
                logger.info(f"Deregistered agent: {name}({organization}, owner={owner})")
                if result:
                    self._publish_discoverability_event(
                        EventType.AGENT_DEREGISTERED, name, organization, visible)
                    if getattr(self.storage, "supports_transactions", False):
                        health_cleanup_deferred = self.storage.add_commit_hook(
                            lambda: self._remove_health_state(name, organization)
                        )
            if result and not health_cleanup_deferred:
                # Own transaction committed (or a non-transactional backend).
                self._remove_health_state(name, organization)
            return result

    def _select_agents_by_llm(self, task: str, agents_info: List[dict], top_n: int) -> list:
        """Use LLM to select the most relevant agents, returning list of (org, name) tuples."""
        try:
            prompt = build_agent_selection_prompt(task, json.dumps(agents_info, ensure_ascii=False, indent=2),
                                                  top_n=top_n)
            _, selected_str = self.llm.ask_llm(prompt)
            selected = self._parse_llm_json_response(selected_str)
            if not isinstance(selected, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get('name'), str)
                    or not item['name'] or not isinstance(item.get('organization', ''), str)
                    for item in selected):
                raise ValueError('Invalid semantic selection response')
            return [(item.get("organization", ""), item["name"]) for item in selected
                    if isinstance(item, dict) and "name" in item]
        except Exception as e:
            logger.error("LLM agent selection failed: {}", type(e).__name__)
            raise SemanticSearchUnavailable('Semantic search is temporarily unavailable') from e

    def _parse_llm_json_response(self, text: str) -> list:
        text = text.strip()
        if not text:
            raise ValueError('Empty semantic selection response')
        m = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        start = text.find('[')
        end = text.rfind(']')
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        return json.loads(text)

    def _build_agents_info(self, agents: List) -> List[dict]:
        result = []
        for agent in agents:
            if isinstance(agent, dict):
                result.append({
                    "name": agent.get("name", ""),
                    "description": agent.get("description", ""),
                    "organization": agent.get("organization", ""),
                })
            else:
                skills_list = [s.name for s in agent.skills] if agent.skills else []
                result.append({
                    "name": agent.name,
                    "description": agent.description,
                    "organization": agent.provider.organization,
                    "skills": skills_list,
                })
        return result

    def _embed_for_search(self, text: str) -> list:
        """Embed a search query, reporting a model failure as 503.

        The embedding service is the same model dependency as the selection call,
        so a failure here is "semantic search is temporarily unavailable" (503) —
        not an unhandled 500. Write paths deliberately do not use this helper: a
        failed index write means the card was not registered at all, which is a
        server error rather than a retryable search outage.
        """
        try:
            return self.embedding_tool.embed(text)
        except Exception as e:
            logger.error("Embedding failed: {}", type(e).__name__)
            raise SemanticSearchUnavailable('Semantic search is temporarily unavailable') from e

    def retrieve_by_task(self, task: str, top_n: int, use_vectordb: Optional[bool] = None) -> List[AgentCard]:
        """
        Fuzzy retrieve using LLM to match task description with agent capabilities.
        Returns a list of candidate agents(could be empty).

        Only discoverable (published) cards are candidates: a pending card must
        not be selectable, and it must not be visible to the selection model
        either, since the prompt would disclose it.
        """
        if not task:
            return []
        # Candidate statuses would come from the index itself, so selection could
        # surface a card the record store never approved.
        self._require_authoritative_store("semantic search over approved cards")

        use_vectordb = self._uses_vectordb(use_vectordb)
        if use_vectordb:
            retrieve_entity = {"collection_name": COLLECTION_NAME,
                               "embedding": self._embed_for_search(task),
                               "top_n": top_n}
            retrieve_results = [agent for agent in self.vectordb.retrieve_entity(retrieve_entity)
                                if is_discoverable_status(agent.get("status"))]
            if not retrieve_results:
                # Nothing passed the status filter: asking the model to choose from
                # an empty candidate list only invites a hallucinated pick (and turns
                # an empty result into a 503 when the model is down). Both branches
                # now agree that "no candidates" means "no matches".
                return []
            agents_info = self._build_agents_info(retrieve_results)
            selected_pairs = self._select_agents_by_llm(task, agents_info, top_n)
            result = [agent for agent in retrieve_results
                      if (agent.get("organization", ""), agent["name"]) in selected_pairs]
        else:
            agents = self.storage.find_all(status=DISCOVERABLE_STATUS) if self.storage else []
            if not agents:
                return []
            agents_info = self._build_agents_info(agents)
            selected_pairs = self._select_agents_by_llm(task, agents_info, top_n)
            result = [agent for agent in agents
                      if (agent.provider.organization, agent.name) in selected_pairs]
            # The model may name a card that was unpublished between candidate
            # collection and selection; re-check the authoritative status.
            result = [agent for agent in result
                      if is_discoverable_status(self._stored_status(agent.name, agent.provider.organization))]

        logger.info("LLM selected {} agents", len(result))
        return result

    def get_by_key(self, name: str, organization: str, use_vectordb: Optional[bool] = None) -> Optional[AgentCard]:
        """Search a single agent by exact name and organization."""
        use_vectordb = self._uses_vectordb(use_vectordb)
        if use_vectordb:
            query_data = {"collection_name": COLLECTION_NAME, "key": "id", "value": self._make_id(name, organization)}
            result = self.vectordb.query_by_key(query_data)
            if len(result) > 0:
                agent_data = result[0]
                agent_card_json = agent_data.get("agent_card", "{}")
                return Parse(agent_card_json, AgentCard())
            else:
                return None
        else:
            record = self.storage.find_by_key(name, organization)
            return record.agent_card if record else None

    def get_by_key_with_owner(self, name: str, organization: str, owner: Optional[str] = None,
                              use_vectordb: Optional[bool] = None) -> Optional[AgentRecord]:
        """Search a single agent by exact name and organization, returns AgentRecord with owner."""
        use_vectordb = self._uses_vectordb(use_vectordb)
        if use_vectordb:
            query_data = {"collection_name": COLLECTION_NAME, "key": "id", "value": self._make_id(name, organization)}
            result = self.vectordb.query_by_key(query_data)
            if len(result) > 0:
                agent_data = result[0]
                stored_owner = agent_data.get("owner")
                agent_card_json = agent_data.get("agent_card", "{}")
                return AgentRecord(
                    agent_card=Parse(agent_card_json, AgentCard()),
                    owner=stored_owner
                )
            else:
                return None
        else:
            return self.storage.find_by_key(name, organization, owner=owner)

    def find_by_owner(self, owner: str, use_vectordb: Optional[bool] = None) -> List[AgentRecord]:
        """Find all agents belonging to a specific owner."""
        # Ownership is an authorization input: an index that lost a card would
        # silently answer "this owner has no agents".
        self._require_authoritative_store("listing an owner's cards")
        use_vectordb = self._uses_vectordb(use_vectordb)
        if use_vectordb:
            query_data = {"collection_name": COLLECTION_NAME, "key": "owner", "value": owner}
            results = self.vectordb.query_by_key(query_data)
            records = []
            for r in results:
                agent_card_json = r.get("agent_card", "{}")
                records.append(AgentRecord(agent_card=Parse(agent_card_json, AgentCard()), owner=r.get("owner")))
            return records
        else:
            return self.storage.find_by_owner(owner)

    def _make_id(self, name: str, organization: str):
        return make_agent_id(name, organization)

    @contextmanager
    def _atomic_write(self):
        """Group an authoritative mutation with its outbox event.

        Inside the unit of work the event is only *persisted*; waking the queue
        and the synchronous listeners happens after the commit succeeded, so a
        consumer can never observe a change that was rolled back. Backends
        without transactions (file, memory, vectordb) yield False and keep
        best-effort publishing - exactly why derived projections must not be
        built on them.

        If the caller already opened a unit of work (`storage.transaction()`),
        the notification is registered as a commit hook of that *outer* unit: it
        must not fire when this block exits, because the outer one can still roll
        back. In both cases the hook is registered *after* the mutation body, so
        internal side effects deferred by the body (e.g. health-state cleanup on
        deregister) run before consumers are woken.
        """
        storage = self.storage
        if storage is None or not getattr(storage, "supports_transactions", False):
            yield False
            return
        bus = get_event_bus()
        if not self._events_share_storage(bus, storage):
            # An apparently successful SQL write must not silently lose its
            # durable change event because the bus was initialized too early.
            raise RuntimeError(
                f"Event outbox is not bound to the authoritative {type(storage).__name__}; "
                "initialize broadcast with the same storage before writing."
            )
        pending: List[Any] = []
        previous = self._deferred_events
        previous_required = self._event_must_succeed
        self._deferred_events = pending
        self._event_must_succeed = True
        try:
            with storage.transaction():
                yield True
                # The notification is registered as the LAST commit hook,
                # after any side effect the mutation body deferred (e.g.
                # health-state cleanup on deregister): internal state must
                # be consistent before consumers are woken. The hook is
                # dropped when the unit rolls back, and for a nested unit
                # it rides the outermost commit.
                storage.add_commit_hook(
                    lambda: self._notify_committed(bus, pending))
        finally:
            self._event_must_succeed = previous_required
            self._deferred_events = previous

    @staticmethod
    def _events_share_storage(bus, storage) -> bool:
        """Whether the bus outbox writes through this very storage instance."""
        return bus.shares_backend(storage)

    def _notify_committed(self, bus, events: List[Any]) -> None:
        for event in events:
            try:
                bus.notify(event)
            except Exception as e:
                logger.error(f"Failed to notify consumers of event {getattr(event, 'event_id', '?')}: {e}")

    def _publish_event(self, event_type: EventType, name: str, organization: str,
                       card_data: Optional[Dict[str, Any]] = None) -> None:
        """Publish a registry change event.

        Outside a unit of work, event failures are logged and swallowed so that
        a broadcast outage never breaks a mutation. Inside one, the failure
        propagates and rolls the transaction back: a committed record without
        its event is precisely the inconsistency projections must never see.
        """
        try:
            data = {
                "name": name,
                "organization": organization,
                "tags": self._tags_for_event(name, organization),
                "discovery_public": True,
            }
            if card_data is not None:
                data["agent_card"] = card_data
            bus = get_event_bus()
            if self._deferred_events is not None:
                # Inside a unit of work: persist now, notify only after commit.
                self._deferred_events.append(bus.persist(event_type, data))
            else:
                bus.publish(event_type, data)
        except Exception as e:
            logger.error(f"Failed to publish registry event {event_type}: {e}")
            if self._event_must_succeed:
                raise

    def _tags_for_event(self, name: str, organization: str) -> List[str]:
        """Tags for a change event payload.

        In vector-only mode there is no record store to read them from; the event
        still announces the card (the collection did change) with an empty tag set
        instead of turning registration into a silent no-op. `get_agent_tags()`
        itself keeps refusing loudly - this is only the payload.
        """
        if self.use_vectordb:
            return []
        return self.get_agent_tags(name, organization) or []

    def _stored_status(self, name: str, organization: str) -> Optional[str]:
        """Status of the authoritative record, or None when it is unknown/absent."""
        self._require_authoritative_store("reading or announcing a card's status")
        if not self.storage:
            return PENDING_STATUS
        # A failed read must propagate and roll back the mutation, not become
        # a legacy published status. Missing/deleted records are not visible.
        record = self.storage.find_by_key(name, organization)
        return (record.status or DISCOVERABLE_STATUS) if record else PENDING_STATUS

    def _publish_discoverability_event(self, event_type: EventType, name: str, organization: str,
                                       visible: bool, card_data: Optional[Dict[str, Any]] = None) -> None:
        """Publish a public change event only while the card is discoverable.

        Pending ('registered') cards are invisible to discovery consumers, so
        announcing them would leak unpublished cards to every subscriber and to
        the change feed.
        """
        if not visible:
            logger.info(
                f"Card {name}({organization}) is not discoverable; "
                f"no public {event_type.value if hasattr(event_type, 'value') else event_type} event emitted")
            return
        self._publish_event(event_type, name, organization, card_data=card_data)

    def _remove_health_state(self, name: str, organization: str) -> None:
        try:
            from agent_registry.health import get_health_service
            get_health_service().remove(name, organization)
        except Exception as e:
            logger.warning(f"Failed to clean health state for agent {name}({organization}): {e}")

    def find_by_key(self, name: str, organization: str) -> Optional[AgentCard]:
        """Search a single agent by exact name and organization. Delegates to get_by_key."""
        return self.get_by_key(name, organization)

    def find_all(self) -> List[AgentCard]:
        """Get all agents.

        A listing is an authoritative question (which cards exist *and* which are
        visible), so the vector-only deployment reports it as unavailable instead
        of returning an empty registry.
        """
        self._require_authoritative_store("listing all cards")
        return self.storage.find_all() if self.storage else []

    def get_status(self, name: str, organization: str) -> Optional[str]:
        """Status of an existing card, or None when no such record exists.

        A record whose stored status is NULL/empty is the legacy published card:
        the storage layer already reads it through `COALESCE(status, 'published')`
        (and `_stored_status` normalizes the same way), so reporting the raw NULL
        here would make a card that listings return invisible to the visibility
        checks in the server and integration ports.
        """
        self._require_authoritative_store("querying an agent's approval status")
        if not self.storage:
            return None
        record = self.storage.find_by_key(name, organization)
        return (record.status or DISCOVERABLE_STATUS) if record else None

    def get_metadata(self, name: str, organization: str) -> Dict[str, Any]:
        """Get agent metadata (agent_name, organization, status, tag)."""
        self._require_authoritative_store("reading agent metadata")
        # No default: an absent record is not a published card.
        status = self.get_status(name, organization) or PENDING_STATUS
        tags = self.get_agent_tags(name, organization) or []
        created_at = self.get_created_at(name, organization) or ''
        updated_at = self.get_updated_at(name, organization) or ''
        metadata = {
            "agent_name": name,
            "organization": organization,
            "status": status,
            "tag": tags,
            "created_at": created_at,
            "updated_at": updated_at
        }
        try:
            from agent_registry.health import get_health_service
            health_service = get_health_service()
            if health_service.enabled:
                metadata["health"] = health_service.metadata_health(name, organization)
        except Exception as e:
            logger.warning(f"Failed to enrich metadata with health status: {e}")
        return metadata

    def get_created_at(self, name: str, organization: str) -> str:
        """Get agent created_at timestamp."""
        self._require_authoritative_store("reading agent timestamps")
        return self.storage.get_created_at(name, organization) if self.storage else ''

    def get_updated_at(self, name: str, organization: str) -> str:
        """Get agent updated_at timestamp."""
        self._require_authoritative_store("reading agent timestamps")
        return self.storage.get_updated_at(name, organization) if self.storage else ''

    def update_status(self, name: str, organization: str, new_status: str) -> bool:
        """Update agent status.

        The event announces the *visibility* transition, so the change feed and
        webhook subscribers never learn about pending cards and do learn when a
        card becomes (or stops being) discoverable:

        * pending -> published  : AGENT_REGISTERED with the full card
        * published -> pending  : AGENT_DEREGISTERED (the card left discovery)
        * no visibility change  : AGENT_UPDATED with the current card
        """
        if new_status not in ('registered', 'published'):
            raise ValueError(f"Invalid status '{new_status}'. Must be 'registered' or 'published'.")
        self._require_authoritative_store("approving or unapproving a card")
        with self._lock:
            if not self.storage:
                return False
            with self._atomic_write():
                previous_status = self._stored_status(name, organization)
                result = self.storage.update_status(name, organization, new_status)
                if result:
                    record = self.storage.find_by_key(name, organization)
                    card_data = MessageToDict(record.agent_card, preserving_proto_field_name=True) if record else None
                    was_visible = is_discoverable_status(previous_status)
                    is_visible = is_discoverable_status(new_status)
                    if is_visible and not was_visible:
                        self._publish_event(EventType.AGENT_REGISTERED, name, organization, card_data=card_data)
                    elif was_visible and not is_visible:
                        self._publish_event(EventType.AGENT_DEREGISTERED, name, organization)
                    elif is_visible:
                        self._publish_event(EventType.AGENT_UPDATED, name, organization, card_data=card_data)
            return result

    def get_agents_by_status(self, status: str) -> List[AgentCard]:
        """Get agents by status."""
        self._require_authoritative_store("listing cards by approval status")
        return self.storage.find_by_status(status) if self.storage else []

    def count(self) -> int:
        """Get total number of agents.

        Deliberately index-aware in vector-only mode: this counter drives the
        registration cap (`_check_agent_limit`), and registration is the one
        capability that mode still offers, so the collection is the store to count
        there. Every *listing* answers from the record store instead (and reports
        503 without one).
        """
        if self.use_vectordb:
            entities = self.vectordb.get_all_entities({"collection_name": COLLECTION_NAME})
            return len(entities)
        return self.storage.count() if self.storage else 0

    # Agent tags methods (for other systems)
    def get_agent_tags(self, name: str, organization: str) -> List[str]:
        """Get tags associated with an agent."""
        self._require_authoritative_store("reading agent tags")
        return self.storage.get_agent_tags(name, organization) if self.storage else []

    def update_agent_tags(self, name: str, organization: str, tags: List[str]) -> bool:
        """Update tags for an agent (full replacement).

        Tags live on the authoritative record, so the change is announced like
        any other mutation - consumers must not keep serving a stale tag set
        from a projection.
        """
        self._require_authoritative_store("updating agent tags")
        with self._lock:
            if not self.storage:
                return False
            with self._atomic_write():
                visible = is_discoverable_status(self._stored_status(name, organization))
                result = self.storage.update_agent_tags(name, organization, tags)
                if result:
                    record = self.storage.find_by_key(name, organization)
                    card_data = MessageToDict(record.agent_card, preserving_proto_field_name=True) if record else None
                    self._publish_discoverability_event(
                        EventType.AGENT_UPDATED, name, organization, visible, card_data=card_data)
            return result

    def find_agents_by_tag(self, tag: str) -> List[AgentCard]:
        """Find agents that have a specific tag."""
        self._require_authoritative_store("listing cards by tag")
        return self.storage.find_by_tag(tag) if self.storage else []

    # Tag entity management methods
    def create_tag(self, name: str) -> Tag:
        """Create a new tag entity."""
        self._require_authoritative_store("creating a tag")
        with self._lock:
            if not self.storage:
                return None
            tag = Tag(name=name)
            if self.storage.create_tag(tag):
                logger.info(f"Tag created: {name}")
                return tag
            else:
                logger.warning(f"Failed to create tag: {name}")
                return None

    def get_tag(self, tag_id: str) -> Optional[Tag]:
        """Get tag by tag_id."""
        self._require_authoritative_store("reading a tag")
        return self.storage.get_tag(tag_id) if self.storage else None

    def get_tag_by_name(self, name: str) -> Optional[Tag]:
        """Get tag by name."""
        self._require_authoritative_store("reading a tag")
        return self.storage.get_tag_by_name(name) if self.storage else None

    def update_tag(self, tag_id: str, new_name: str) -> bool:
        """Rename a tag and its Agent references in one SQL unit of work."""
        self._require_authoritative_store("renaming a tag")
        with self._lock:
            if not self.storage:
                return False
            tag = self.storage.get_tag(tag_id)
            if not tag:
                logger.warning(f"Tag not found: {tag_id}")
                return False
            if tag.name == new_name:
                return True
            conflict = self.storage.get_tag_by_name(new_name)
            if conflict and conflict.tag_id != tag_id:
                return False
            old_name = tag.name
            updated = copy.deepcopy(tag)
            updated.name = new_name
            updated.update_timestamp()
            with self._atomic_write():
                if not self.storage.update_tag(tag_id, updated):
                    return False
                self._replace_tag_references(old_name, new_name)
            return True

    def delete_tag(self, tag_id: str) -> bool:
        """Unbind a tag from Agents and delete it in one SQL unit of work."""
        self._require_authoritative_store("deleting a tag")
        with self._lock:
            if not self.storage:
                return False
            tag = self.storage.get_tag(tag_id)
            if tag is None:
                return False
            with self._atomic_write():
                self._replace_tag_references(tag.name, None)
                if not self.storage.delete_tag(tag_id):
                    raise RuntimeError('Tag disappeared during deletion')
            return True

    def _replace_tag_references(self, old_name: str, new_name: Optional[str]) -> None:
        """Called with the core lock and the surrounding UoW already held."""
        for agent in self.storage.find_by_tag(old_name):
            name, organization = agent.name, agent.provider.organization
            tags = self.storage.get_agent_tags(name, organization)
            updated = [new_name if tag == old_name else tag for tag in tags
                       if tag != old_name or new_name is not None]
            updated = list(dict.fromkeys(updated))
            if not self.storage.update_agent_tags(name, organization, updated):
                raise RuntimeError('Agent tag references could not be updated')
            record = self.storage.find_by_key(name, organization)
            if record and is_discoverable_status(record.status):
                self._publish_event(EventType.AGENT_UPDATED, name, organization,
                                    card_data=MessageToDict(record.agent_card, preserving_proto_field_name=True))

    def list_tags(self) -> List[Tag]:
        """List all tags."""
        self._require_authoritative_store("listing tags")
        return self.storage.list_tags() if self.storage else []
