# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""
Shared SQL storage backend for PostgreSQL, GaussDB, and SQLite.

Subclasses provide dialect-specific connection management, query sets, and
schema initialization while inheriting all CRUD logic from this base class.
"""

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from a2a.types import AgentCard
from google.protobuf.json_format import MessageToDict, Parse
from loguru import logger

from agent_registry.model.tag import Tag
from agent_registry.model.agent_layer import UNKNOWN_LAYER, LAYER_UNSET, default_layer, normalize_layer
from .base import StorageBackend, AgentRecord


class SqlStorageBackend(StorageBackend):
    """Shared SQL CRUD logic. Subclasses set `queries` and implement connections."""

    queries = None
    _integrity_error = Exception
    # SQL backends commit a mutation and its outbox event in one transaction.
    supports_transactions = True
    # Backends whose `_acquire_conn()` hands out one shared connection (SQLite)
    # must serialize writes: another thread committing on that same connection
    # would commit a unit of work that is still in flight.
    shares_single_connection = False
    # Parameter placeholder for dialect-agnostic helper queries (%s for psycopg2).
    param_ph = "%s"
    # Dialect name for auxiliary SQL stores that need dialect-specific SQL
    # (e.g. the outbox version counter: MySQL has no UPDATE ... RETURNING).
    dialect = "sql"
    # Whether the dialect supports `CREATE INDEX IF NOT EXISTS`. Backends that
    # don't (e.g. MySQL) set this to False; auxiliary SQL stores use it to pick
    # a duplicate-tolerant plain CREATE INDEX instead.
    supports_create_index_if_not_exists = True

    def ensure_index(self, ddl_if_not_exists: str, ddl_plain: str) -> None:
        """Create an index via dialect-appropriate DDL.

        Dialects without CREATE INDEX IF NOT EXISTS run the plain form and
        tolerate ONLY the duplicate-index error (MySQL errno 1061); any other
        failure propagates instead of being swallowed.
        """
        if self.supports_create_index_if_not_exists:
            self._execute_write(ddl_if_not_exists)
            return
        try:
            self._execute_write(ddl_plain)
        except Exception as e:
            if getattr(e, "args", None) and e.args and e.args[0] == 1061:
                logger.debug(f"Index already exists, skipping: {e}")
            else:
                raise

    # ---- unit of work ----

    # Created at import time so the lazily-built helpers below cannot race: two
    # threads must never end up with different locks or transaction slots.
    _lazy_init_lock = threading.Lock()

    @property
    def _tx_state(self):
        """Per-thread transaction slot (SQLite connections are thread-bound)."""
        state = getattr(self, "_tx_local", None)
        if state is None:
            with SqlStorageBackend._lazy_init_lock:
                state = getattr(self, "_tx_local", None)
                if state is None:
                    state = threading.local()
                    self._tx_local = state
        return state

    def _tx_conn(self):
        """Connection of the unit of work in progress on this thread, if any."""
        return getattr(self._tx_state, "conn", None)

    @property
    def _conn_lock(self):
        lock = getattr(self, "_conn_lock_obj", None)
        if lock is None:
            with SqlStorageBackend._lazy_init_lock:
                lock = getattr(self, "_conn_lock_obj", None)
                if lock is None:
                    lock = threading.RLock()
                    self._conn_lock_obj = lock
        return lock

    @contextmanager
    def _serialize(self):
        """Serialize connection access when the backend shares one connection."""
        if self.shares_single_connection:
            with self._conn_lock:
                yield
        else:
            yield

    def add_commit_hook(self, callback) -> bool:
        """Run `callback` after the *enclosing* unit of work commits.

        Returns False when no unit of work is active on this thread, in which
        case the caller owns the commit and must run the callback itself. Hooks
        registered inside a nested block fire when the outermost unit commits
        and are dropped when it rolls back, so a deferred side effect (event
        notification, health cleanup) can never run for a change that did not
        commit.
        """
        hooks = getattr(self._tx_state, "on_commit", None)
        if hooks is None:
            return False
        hooks.append(callback)
        return True

    def _begin_transaction(self, conn) -> None:
        """Dialect hook: open an explicit transaction when the driver doesn't.

        psycopg2/gaussdb/sqlite3 start one implicitly on the first statement;
        pymysql-backed pools run with autocommit=True and must override this,
        otherwise every statement commits on its own and a rollback could not
        undo the record write.
        """

    @contextmanager
    def transaction(self):
        """Run several writes as one unit of work.

        Every `_execute_*` call inside the block reuses a single connection and
        a single commit, so an authoritative record and its outbox event can no
        longer diverge (previously each write committed on its own). A nested
        `transaction()` joins the outer unit instead of opening a second one, and
        a failure marks the whole unit rollback-only even if an outer caller
        catches the nested exception. A partial record/outbox commit is never
        allowed.
        """
        if self._tx_conn() is not None:
            try:
                yield
            except Exception as exc:
                self._tx_state.rollback_only = True
                if self._tx_state.rollback_cause is None:
                    self._tx_state.rollback_cause = exc
                raise
            return
        hooks = []
        with self._serialize():
            conn = self._acquire_conn()
            self._tx_state.conn = conn
            self._tx_state.on_commit = hooks
            self._tx_state.rollback_only = False
            self._tx_state.rollback_cause = None
            try:
                self._begin_transaction(conn)
                yield
                if self._tx_state.rollback_only:
                    raise RuntimeError(
                        "Transaction is rollback-only after a nested failure"
                    ) from self._tx_state.rollback_cause
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._tx_state.on_commit = None
                self._tx_state.conn = None
                self._tx_state.rollback_only = False
                self._tx_state.rollback_cause = None
                self._release_conn(conn)
        # Committed and the connection lock is released: deferred side effects
        # (event notifications, health cleanup) are safe to run only now.
        for callback in hooks:
            try:
                callback()
            except Exception as e:
                logger.error(f"Post-commit callback failed: {e}")

    # ---- connection management (subclass implements) ----

    def _acquire_conn(self):
        raise NotImplementedError

    def _release_conn(self, conn):
        pass

    # ---- execution helpers ----

    def _execute_write(self, query: str, params: tuple = None) -> int:
        tx_conn = self._tx_conn()
        if tx_conn is not None:
            # Inside a unit of work: reuse its connection, commit stays with it.
            cur = tx_conn.cursor()
            try:
                cur.execute(query, params or ())
                return cur.rowcount
            finally:
                cur.close()
        with self._serialize():
            conn = self._acquire_conn()
            cur = None
            try:
                cur = conn.cursor()
                cur.execute(query, params or ())
                conn.commit()
                return cur.rowcount
            except Exception:
                conn.rollback()
                raise
            finally:
                if cur:
                    cur.close()
                self._release_conn(conn)

    def _execute_read_one(self, query: str, params: tuple = None):
        tx_conn = self._tx_conn()
        if tx_conn is not None:
            # Read inside a unit of work so uncommitted writes are visible.
            cur = tx_conn.cursor()
            try:
                cur.execute(query, params or ())
                return cur.fetchone()
            finally:
                cur.close()
        with self._serialize():
            conn = self._acquire_conn()
            cur = None
            try:
                cur = conn.cursor()
                cur.execute(query, params or ())
                return cur.fetchone()
            finally:
                if cur:
                    cur.close()
                self._release_conn(conn)

    def _execute_read_all(self, query: str, params: tuple = None):
        tx_conn = self._tx_conn()
        if tx_conn is not None:
            cur = tx_conn.cursor()
            try:
                cur.execute(query, params or ())
                return cur.fetchall()
            finally:
                cur.close()
        with self._serialize():
            conn = self._acquire_conn()
            cur = None
            try:
                cur = conn.cursor()
                cur.execute(query, params or ())
                return cur.fetchall()
            finally:
                if cur:
                    cur.close()
                self._release_conn(conn)

    # ---- startup pre-check ----

    def check_connection(self) -> None:
        """Round-trip sanity check used by the startup pre-check.

        Startup-only: callers must run this before concurrent registry traffic,
        because this direct probe does not enter the shared-connection lock.
        Raises the driver's connection/operational error when the backend is
        unreachable. Uses explicit cursor close (not `with`) because
        sqlite3 cursors don't support the context manager protocol.
        """
        conn = self._acquire_conn()
        cur = None
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        finally:
            if cur:
                cur.close()
            self._release_conn(conn)

    # ---- value parsing ----

    @staticmethod
    def _parse_json(val):
        if val is None:
            return None
        if isinstance(val, (dict, list)):
            return val
        if isinstance(val, str):
            return json.loads(val)
        return val

    @staticmethod
    def _parse_tags(val):
        if not val:
            return []
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            return json.loads(val)
        return []

    @staticmethod
    def _parse_timestamp(val):
        if val and hasattr(val, 'isoformat'):
            return val.isoformat()
        return str(val) if val else ''

    def _row_to_agent(self, row) -> AgentCard:
        data = self._parse_json(row[0])
        return AgentCard(**data)

    def _row_to_agent_record(self, row) -> AgentRecord:
        agent = self._row_to_agent(row)
        stored_owner = row[1] if len(row) > 1 else None
        stored_status = row[2] if len(row) > 2 else 'published'
        tags = self._parse_tags(row[3]) if len(row) > 3 else []
        created_at = self._parse_timestamp(row[4]) if len(row) > 4 else ''
        updated_at = self._parse_timestamp(row[5]) if len(row) > 5 else ''
        raw_layer = row[6] if len(row) > 6 else UNKNOWN_LAYER
        try:
            stored_layer = normalize_layer(raw_layer)
        except ValueError:
            stored_layer = UNKNOWN_LAYER
        return AgentRecord(
            agent_card=agent, owner=stored_owner,
            status=stored_status, tags=tags,
            created_at=created_at, updated_at=updated_at,
            layer=stored_layer,
        )

    def _row_to_tag(self, row) -> Tag:
        return Tag(
            tag_id=row[0],
            name=row[1],
            created_at=self._parse_timestamp(row[2]),
            updated_at=self._parse_timestamp(row[3])
        )

    def _to_tag_query_param(self, tag: str):
        """Convert tag to FIND_BY_TAG parameter. Override for JSONB backends."""
        return tag

    def _get_agent_fields(self, agent: AgentCard, owner: Optional[str] = None,
                          status: str = 'published',
                          layer: str = UNKNOWN_LAYER) -> tuple:
        agent_dict = MessageToDict(agent, preserving_proto_field_name=True)
        now = datetime.now(timezone.utc)
        layer = default_layer(layer)
        return (
            agent.name,
            agent.provider.organization,
            owner,
            agent_dict.get('description'),
            agent_dict.get('documentation_url'),
            agent_dict.get('version'),
            status,
            layer,
            json.dumps(agent_dict.get('provider', {})),
            json.dumps(agent_dict.get('capabilities', {})) if agent_dict.get('capabilities') else None,
            json.dumps(agent_dict.get('skills', [])) if agent_dict.get('skills') else None,
            json.dumps(agent_dict.get('default_input_modes', [])) if agent_dict.get('default_input_modes') else None,
            json.dumps(agent_dict.get('default_output_modes', [])) if agent_dict.get('default_output_modes') else None,
            json.dumps(agent_dict),
            now,
            now
        )

    # ---- StorageBackend implementation ----

    def create(self, agent: AgentCard, owner: Optional[str] = None,
               status: str = 'published',
               layer: str = UNKNOWN_LAYER) -> bool:
        existing = self.find_by_key(agent.name, agent.provider.organization)
        if existing:
            logger.warning(f"Agent already exists: {agent.name} (org={agent.provider.organization})")
            return False
        affected = self._execute_write(
            self.queries.CREATE_AGENT_WITH_OWNER.value,
            self._get_agent_fields(agent, owner, status, layer)
        )
        if affected > 0:
            logger.info(f"Created agent: {agent.name} (org={agent.provider.organization}, owner={owner}, status={status})")
        return affected > 0

    def find_by_key(self, name: str, organization: str,
                    owner: Optional[str] = None) -> Optional[AgentRecord]:
        if owner is not None:
            row = self._execute_read_one(
                self.queries.FIND_BY_KEY_WITH_OWNER.value,
                (name, organization, owner)
            )
        else:
            row = self._execute_read_one(
                self.queries.FIND_BY_KEY_ANY_OWNER.value,
                (name, organization)
            )
        if row:
            return self._row_to_agent_record(row)
        return None

    def find_by_name(self, name: str) -> List[AgentCard]:
        rows = self._execute_read_all(self.queries.FIND_BY_NAME.value, (f"%{name}%",))
        result = [self._row_to_agent(r) for r in rows]
        logger.debug(f"Found {len(result)} agents by name '{name}'")
        return result

    def find_by_organization(self, organization: str) -> List[AgentCard]:
        rows = self._execute_read_all(self.queries.FIND_BY_ORG.value, (organization,))
        result = [self._row_to_agent(r) for r in rows]
        logger.debug(f"Found {len(result)} agents by organization '{organization}'")
        return result

    def find_all(self, status: Optional[str] = None) -> List[AgentCard]:
        if status is not None:
            return self.find_by_status(status)
        rows = self._execute_read_all(self.queries.FIND_ALL.value)
        result = [self._row_to_agent(r) for r in rows]
        logger.debug(f"Found {len(result)} agents (find_all)")
        return result

    def find_records(self, name: Optional[str] = None,
                     organization: Optional[str] = None,
                     layer: Optional[str] = None,
                     status: Optional[str] = None) -> List[AgentRecord]:
        """Find complete registration records with storage-side filtering."""

        conditions = ["1 = 1"]
        params = []
        ph = self.param_ph
        if name is not None:
            conditions.append(f"LOWER(name) LIKE LOWER({ph})")
            params.append(f"%{name}%")
        if organization is not None:
            conditions.append(f"organization = {ph}")
            params.append(organization)
        if layer is not None:
            conditions.append(f"layer = {ph}")
            params.append(normalize_layer(layer))
        if status is not None:
            conditions.append(f"status = {ph}")
            params.append(status)

        query = (
            "SELECT agent_card_json, owner, status, tags, created_at, updated_at, layer "
            "FROM agent_card WHERE " + " AND ".join(conditions) +
            " ORDER BY organization ASC, name ASC, owner IS NULL ASC, owner ASC, id ASC"
        )
        rows = self._execute_read_all(query, tuple(params))
        return [self._row_to_agent_record(row) for row in rows]

    def find_by_owner(self, owner: str) -> List[AgentRecord]:
        rows = self._execute_read_all(self.queries.FIND_BY_OWNER.value, (owner,))
        result = []
        for row in rows:
            result.append(self._row_to_agent_record(row))
        logger.debug(f"Found {len(result)} agents by owner '{owner}'")
        return result

    def find_by_status(self, status: str) -> List[AgentCard]:
        rows = self._execute_read_all(self.queries.FIND_BY_STATUS.value, (status,))
        return [self._row_to_agent(r) for r in rows]

    def find_by_tag(self, tag: str) -> List[AgentCard]:
        param = self._to_tag_query_param(tag)
        rows = self._execute_read_all(self.queries.FIND_BY_TAG.value, (param,))
        result = [self._row_to_agent(r) for r in rows]
        logger.debug(f"Found {len(result)} agents by tag '{tag}'")
        return result

    def update(self, name: str, organization: str, agent_data: Dict[str, Any],
               owner: Optional[str] = None, layer=LAYER_UNSET) -> bool:
        existing = self.find_by_key(name, organization)
        if existing is None:
            return False
        agent = Parse(json.dumps(agent_data), AgentCard())
        if agent.name != name or agent.provider.organization != organization:
            raise ValueError('Cannot change primary key(name or organization) during update.')
        agent_dict = MessageToDict(agent, preserving_proto_field_name=True)
        # Card edits are not approval operations; preserve governance state.
        status_value = existing.status or 'published'
        now = datetime.now(timezone.utc)
        layer_is_set = layer is not LAYER_UNSET
        layer_value = normalize_layer(layer) if layer_is_set else None

        if owner is not None:
            query = (self.queries.UPDATE_AGENT_WITH_OWNER_LAYER.value
                     if layer_is_set else self.queries.UPDATE_AGENT_WITH_OWNER.value)
            params = ((json.dumps(agent_dict), status_value, layer_value, now,
                       name, organization, owner)
                      if layer_is_set else
                      (json.dumps(agent_dict), status_value, now,
                       name, organization, owner))
            affected = self._execute_write(
                query, params
            )
        else:
            if existing and existing.owner:
                query = (self.queries.UPDATE_AGENT_WITH_OWNER_LAYER.value
                         if layer_is_set else self.queries.UPDATE_AGENT_WITH_OWNER.value)
                params = ((json.dumps(agent_dict), status_value, layer_value, now,
                           name, organization, existing.owner)
                          if layer_is_set else
                          (json.dumps(agent_dict), status_value, now,
                           name, organization, existing.owner))
                affected = self._execute_write(
                    query, params
                )
            else:
                query = self.queries.UPDATE_AGENT_LAYER.value if layer_is_set else self.queries.UPDATE_AGENT.value
                params = ((json.dumps(agent_dict), status_value, layer_value, now,
                           name, organization)
                          if layer_is_set else
                          (json.dumps(agent_dict), status_value, now,
                           name, organization))
                affected = self._execute_write(
                    query, params
                )
        logger.info(f"Updated agent: {name} (org={organization}, owner={owner}), affected={affected}")
        return affected > 0

    def update_status(self, name: str, organization: str, new_status: str) -> bool:
        now = datetime.now(timezone.utc)
        affected = self._execute_write(
            self.queries.UPDATE_STATUS.value,
            (new_status, now, name, organization)
        )
        return affected > 0

    def delete(self, name: str, organization: str,
               owner: Optional[str] = None) -> bool:
        if owner is not None:
            affected = self._execute_write(
                self.queries.DELETE_AGENT_WITH_OWNER.value,
                (name, organization, owner)
            )
        else:
            existing = self.find_by_key(name, organization)
            if existing and existing.owner:
                affected = self._execute_write(
                    self.queries.DELETE_AGENT_WITH_OWNER.value,
                    (name, organization, existing.owner)
                )
            else:
                affected = self._execute_write(
                    self.queries.DELETE_AGENT.value,
                    (name, organization)
                )
        logger.info(f"Deleted agent: {name} (org={organization}, owner={owner}), affected={affected}")
        return affected > 0

    def count(self) -> int:
        row = self._execute_read_one(self.queries.COUNT.value)
        return row[0] if row else 0

    def count_by_status(self, status: str) -> int:
        row = self._execute_read_one(self.queries.COUNT_BY_STATUS.value, (status,))
        return row[0] if row else 0

    def get_created_at(self, name: str, organization: str) -> str:
        row = self._execute_read_one(self.queries.GET_CREATED_AT.value, (name, organization))
        if row and row[0]:
            return self._parse_timestamp(row[0])
        return ''

    def get_updated_at(self, name: str, organization: str) -> str:
        row = self._execute_read_one(self.queries.GET_UPDATED_AT.value, (name, organization))
        if row and row[0]:
            return self._parse_timestamp(row[0])
        return ''

    def get_agent_tags(self, name: str, organization: str) -> List[str]:
        row = self._execute_read_one(self.queries.GET_AGENT_TAGS.value, (name, organization))
        if row and row[0]:
            return self._parse_tags(row[0])
        return []

    def update_agent_tags(self, name: str, organization: str,
                          new_tags: List[str]) -> bool:
        now = datetime.now(timezone.utc)
        affected = self._execute_write(
            self.queries.UPDATE_AGENT_TAGS.value,
            (json.dumps(new_tags), now, name, organization)
        )
        return affected > 0

    # ---- tag entity management ----

    def create_tag(self, tag: Tag) -> bool:
        try:
            self._execute_write(
                self.queries.CREATE_TAG.value,
                (tag.tag_id, tag.name, tag.created_at, tag.updated_at)
            )
            logger.info(f"Tag created: {tag.name} (ID: {tag.tag_id})")
            return True
        except self._integrity_error as e:
            logger.warning(f"Tag already exists: {tag.name} - {e}")
            return False

    def get_tag(self, tag_id: str) -> Optional[Tag]:
        row = self._execute_read_one(self.queries.GET_TAG_BY_ID.value, (tag_id,))
        if row:
            return self._row_to_tag(row)
        return None

    def get_tag_by_name(self, name: str) -> Optional[Tag]:
        row = self._execute_read_one(self.queries.GET_TAG_BY_NAME.value, (name,))
        if row:
            return self._row_to_tag(row)
        return None

    def update_tag(self, tag_id: str, tag: Tag) -> bool:
        try:
            now = datetime.now(timezone.utc).isoformat()
            affected = self._execute_write(
                self.queries.UPDATE_TAG.value,
                (tag.name, now, tag_id)
            )
            logger.info(f"Tag updated: {tag.name} (ID: {tag_id})")
            return affected > 0
        except self._integrity_error as e:
            logger.warning(f"Tag name already exists: {tag.name} - {e}")
            return False

    def delete_tag(self, tag_id: str) -> bool:
        affected = self._execute_write(self.queries.DELETE_TAG.value, (tag_id,))
        logger.info(f"Tag deleted: ID {tag_id}")
        return affected > 0

    def list_tags(self) -> List[Tag]:
        rows = self._execute_read_all(self.queries.LIST_TAGS.value)
        result = [self._row_to_tag(r) for r in rows]
        logger.debug(f"Listed {len(result)} tags")
        return result
