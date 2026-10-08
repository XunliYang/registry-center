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

"""
R2 regression tests: one visibility policy for every public surface.

A card awaiting approval ('registered') is stored but not discoverable. It must
not be returned by list / exact / semantic queries, must not enter the semantic
selection prompt, and must not produce a public change event (which is what the
change feed and webhook subscribers consume).
"""

import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import AgentCard
from fastapi.testclient import TestClient

from agent_registry.broadcast import events as events_module
from agent_registry.core import (
    DISCOVERABLE_STATUS,
    PENDING_STATUS,
    RegistryCore,
    is_discoverable_status,
)
from agent_registry.persistence.base import AgentRecord
from agent_registry.server import app, get_registry
from common.custom.interface_type import InterfaceType


def make_agent(name="TestAgent", org="TestOrg", desc="Test agent"):
    return AgentCard(
        name=name,
        provider={"organization": org, "url": "https://test.org"},
        description=desc,
        version="1.0.0",
        capabilities={"streaming": False},
        default_input_modes=[],
        default_output_modes=[],
        skills=[],
    )


class FakeBus:
    """Records what core would publish (file backends publish best-effort)."""

    def __init__(self):
        self.events = []

    def publish(self, event_type, data):
        self.events.append((event_type, data))

    def persist(self, event_type, data):
        self.events.append((event_type, data))
        return SimpleNamespace(event_id=str(len(self.events)))

    @property
    def event_types(self):
        return [event_type for event_type, _ in self.events]


@pytest.fixture
def bus():
    fake = FakeBus()
    with patch("agent_registry.core.get_event_bus", return_value=fake):
        yield fake


@pytest.fixture
def temp_dir():
    directory = tempfile.mkdtemp()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def registry(temp_dir, bus):
    with patch("agent_registry.core.get_llm_instance", return_value=MagicMock()), \
         patch("agent_registry.core.get_embed_instance", return_value=MagicMock()), \
         patch("agent_registry.core.get_root_path", return_value=temp_dir), \
         patch("agent_registry.config.get_conf", return_value={}), \
         patch("agent_registry.config.get_persistence_conf", return_value={"persistence.mode": "file"}):
        core = RegistryCore(
            persistence_file="agentcard.json",
            persistence_metadata_file="agentregistry.json",
            use_vectordb=False,
            persistence_mode="file",
            persistence_conf={},
        )
        yield core
        core.close()


class TestStatusPolicy:

    def test_missing_status_counts_as_discoverable(self):
        assert is_discoverable_status(None) is True

    def test_pending_status_is_not_discoverable(self):
        assert is_discoverable_status(PENDING_STATUS) is False

    def test_find_all_can_filter_by_status(self, registry):
        registry.register(make_agent("PublishedAgent"))
        registry.register_with_status(make_agent("PendingAgent"), initial_status=PENDING_STATUS)
        published = registry.storage.find_all(status=DISCOVERABLE_STATUS)
        assert [agent.name for agent in published] == ["PublishedAgent"]
        assert len(registry.storage.find_all()) == 2

    def test_legacy_record_without_status_entry_stays_discoverable(self, registry):
        registry.register(make_agent("LegacyAgent"))
        registry.storage._status_map.pop(("LegacyAgent", "TestOrg"), None)
        assert [a.name for a in registry.storage.find_by_status(DISCOVERABLE_STATUS)] == ["LegacyAgent"]


class TestPendingCardEvents:

    def _pending(self, registry, name="PendingAgent"):
        assert registry.register_with_status(make_agent(name), initial_status=PENDING_STATUS) is True

    def test_pending_registration_emits_no_public_event(self, registry, bus):
        self._pending(registry)
        assert bus.event_types == []
        assert registry.get_status("PendingAgent", "TestOrg") == PENDING_STATUS

    def test_published_registration_emits_registered_event(self, registry, bus):
        registry.register(make_agent("PublishedAgent"))
        assert bus.event_types == [events_module.EventType.AGENT_REGISTERED]
        assert bus.events[0][1]["agent_card"]["name"] == "PublishedAgent"

    def test_approval_emits_the_discoverability_event(self, registry, bus):
        self._pending(registry)
        bus.events.clear()
        assert registry.update_status("PendingAgent", "TestOrg", DISCOVERABLE_STATUS) is True
        assert bus.event_types == [events_module.EventType.AGENT_REGISTERED]
        assert bus.events[0][1]["agent_card"]["name"] == "PendingAgent"

    def test_unpublish_emits_removal_without_card_payload(self, registry, bus):
        registry.register(make_agent("PublishedAgent"))
        bus.events.clear()
        assert registry.update_status("PublishedAgent", "TestOrg", PENDING_STATUS) is True
        assert bus.event_types == [events_module.EventType.AGENT_DEREGISTERED]
        assert "agent_card" not in bus.events[0][1]

    def test_pending_card_update_emits_no_public_event(self, registry, bus):
        self._pending(registry)
        bus.events.clear()
        card = make_agent("PendingAgent")
        registry.update("PendingAgent", "TestOrg", {
            "name": card.name,
            "description": "changed",
            "provider": {"organization": "TestOrg", "url": "https://test.org"},
        })
        assert bus.event_types == []

    def test_pending_card_deregistration_emits_no_public_event(self, registry, bus):
        self._pending(registry)
        bus.events.clear()
        assert registry.deregister("PendingAgent", "TestOrg") is True
        assert bus.event_types == []

    def test_pending_card_tag_update_emits_no_public_event(self, registry, bus):
        self._pending(registry)
        bus.events.clear()
        registry.update_agent_tags("PendingAgent", "TestOrg", ["tag-a"])
        assert bus.event_types == []


class TestSemanticCandidates:

    def test_pending_card_is_not_a_candidate(self, registry, bus):
        registry.register(make_agent("PublishedAgent", desc="published agent"))
        registry.register_with_status(make_agent("PendingAgent", desc="pending agent"),
                                      initial_status=PENDING_STATUS)
        captured = {}

        def fake_select(task, agents_info, top_n):
            captured["agents_info"] = agents_info
            # The model picks both cards, including the one it must never see.
            return [("TestOrg", "PublishedAgent"), ("TestOrg", "PendingAgent")]

        with patch.object(registry, "_select_agents_by_llm", side_effect=fake_select):
            result = registry.retrieve_by_task("find agents", top_n=5, use_vectordb=False)

        assert [agent.name for agent in result] == ["PublishedAgent"]
        prompt_names = [info["name"] for info in captured["agents_info"]]
        assert prompt_names == ["PublishedAgent"]

    def test_unpublished_between_selection_and_result_is_dropped(self, registry, bus):
        registry.register(make_agent("PublishedAgent"))
        registry.register_with_status(make_agent("PendingAgent"), initial_status=PENDING_STATUS)

        def fake_select(task, agents_info, top_n):
            return [("TestOrg", "PendingAgent")]

        with patch.object(registry, "_select_agents_by_llm", side_effect=fake_select):
            result = registry.retrieve_by_task("find agents", top_n=5, use_vectordb=False)
        assert result == []


def _pending_card(name="PendingAgent"):
    return make_agent(name)


class _Router:
    """Routes HandlerRegistry.get_handler() to per-interface fakes."""

    def __init__(self, cards=None, record=None):
        self.cards = cards or []
        self.record = record

    def __call__(self, interface_type, *args, **kwargs):
        handler = MagicMock()
        handler.handle = AsyncMock(return_value=None)
        if interface_type == InterfaceType.QUERY:
            handler.handle = AsyncMock(return_value=list(self.cards))
        elif interface_type == InterfaceType.RETRIEVE:
            handler.handle = AsyncMock(return_value=list(self.cards))
        elif interface_type == InterfaceType.GET:
            handler.handle = AsyncMock(return_value=self.record)
        return handler


def _client_with(status_by_name, cards, record=None):
    registry = MagicMock()
    registry.get_status.side_effect = lambda name, org: status_by_name.get(name)
    app.dependency_overrides[get_registry] = lambda: registry
    client = TestClient(app)
    router = _Router(cards=cards, record=record)
    return client, patcher(router)


def patcher(router):
    return patch("common.custom.custom_handle.HandlerRegistry.get_handler", side_effect=router)


class TestEndpointVisibility:

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_list_excludes_pending_cards(self):
        pending = _pending_card()
        published = make_agent("PublishedAgent")
        client, handler_patch = _client_with(
            {"PendingAgent": PENDING_STATUS, "PublishedAgent": DISCOVERABLE_STATUS},
            [pending, published])
        with handler_patch:
            response = client.get("/rest/v1/registry-center/agent-cards")
        assert response.status_code == 200
        names = [card["name"] for card in response.json()["agentCards"]]
        assert names == ["PublishedAgent"]

    def test_exact_query_excludes_pending_card(self):
        pending = _pending_card()
        client, handler_patch = _client_with(
            {"PendingAgent": PENDING_STATUS}, [],
            record=AgentRecord(agent_card=pending, owner=None, status=PENDING_STATUS))
        with handler_patch:
            response = client.get("/rest/v1/registry-center/agent-cards/TestOrg/PendingAgent")
        assert response.status_code == 200
        assert response.json() == {"agentCards": []}

    def test_semantic_query_excludes_pending_card(self):
        pending = _pending_card()
        published = make_agent("PublishedAgent")
        client, handler_patch = _client_with(
            {"PendingAgent": PENDING_STATUS, "PublishedAgent": DISCOVERABLE_STATUS},
            [pending, published])
        with handler_patch:
            response = client.post(
                "/rest/v1/registry-center/agent-cards/semantic-query?top_n=5",
                json={"task": "find agents"})
        assert response.status_code == 200
        names = [card["name"] for card in response.json()["agentCards"]]
        assert names == ["PublishedAgent"]

    def test_semantic_query_returns_published_cards(self):
        published = make_agent("PublishedAgent")
        client, handler_patch = _client_with({"PublishedAgent": DISCOVERABLE_STATUS}, [published])
        with handler_patch:
            response = client.post(
                "/rest/v1/registry-center/agent-cards/semantic-query?top_n=5",
                json={"task": "find agents"})
        assert response.status_code == 200
        assert [card["name"] for card in response.json()["agentCards"]] == ["PublishedAgent"]
