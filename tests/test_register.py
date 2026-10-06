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

"""Tests for RegistryCore registration, update, delete, and query operations."""

import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from a2a.types import AgentCard
from agent_registry.core import RegistryCore, make_agent_key


class TestRegistryCoreFileMode:
    """Unit tests for RegistryCore in file persistence mode."""

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp()
        yield d
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def registry(self, temp_dir):
        with patch('agent_registry.core.get_llm_instance', return_value=MagicMock()), \
             patch('agent_registry.core.get_embed_instance', return_value=MagicMock()), \
             patch('agent_registry.core.get_root_path', return_value=temp_dir), \
             patch('agent_registry.config.get_conf', return_value={}), \
             patch('agent_registry.config.get_persistence_conf', return_value={'persistence.mode': 'file'}):
            reg = RegistryCore(
                persistence_file='agentcard.json',
                persistence_metadata_file='agentregistry.json',
                use_vectordb=False,
                persistence_mode='file',
                persistence_conf={}
            )
            yield reg

    def _make_agent(self, name="TestAgent", org="TestOrg", desc="Test agent"):
        data = {
            "name": name,
            "provider": {"organization": org, "url": "https://test.org"},
            "description": desc,
            "version": "1.0.0",
            "skills": [],
            "capabilities": {"streaming": False},
            "default_input_modes": [],
            "default_output_modes": [],
        }
        return AgentCard(**data)

    # ---- registration ----

    def test_register_success(self, registry):
        agent = self._make_agent()
        result = registry.register(agent)
        assert result is True
        found = registry.get_by_key("TestAgent", "TestOrg")
        assert found is not None
        assert found.name == "TestAgent"

    def test_register_with_status(self, registry):
        agent = self._make_agent()
        result = registry.register_with_status(agent, initial_status='registered')
        assert result is True
        status = registry.get_status("TestAgent", "TestOrg")
        assert status == 'registered'

    def test_register_sets_timestamps(self, registry):
        agent = self._make_agent()
        registry.register(agent)
        created = registry.get_created_at("TestAgent", "TestOrg")
        updated = registry.get_updated_at("TestAgent", "TestOrg")
        assert created != ''
        assert updated != ''
        assert created == updated

    def test_register_default_published(self, registry):
        agent = self._make_agent()
        registry.register(agent)
        status = registry.get_status("TestAgent", "TestOrg")
        assert status == 'published'

    def test_count_zero_initially(self, registry):
        assert registry.count() == 0

    def test_count_increases_after_register(self, registry):
        registry.register(self._make_agent())
        assert registry.count() == 1

    def test_get_by_key_found(self, registry):
        registry.register(self._make_agent())
        agent = registry.get_by_key("TestAgent", "TestOrg")
        assert agent is not None
        assert agent.name == "TestAgent"

    def test_get_by_key_not_found(self, registry):
        assert registry.get_by_key("Nope", "Nope") is None

    def test_get_agents_dict_structure(self, registry):
        agent = self._make_agent()
        registry.register(agent)
        agents = registry.get_agents()
        key = make_agent_key("TestAgent", "TestOrg")
        assert key in agents
        assert agents[key] is True

    def test_find_exact_by_name_and_org(self, registry):
        registry.register(self._make_agent("A1", "O1"))
        registry.register(self._make_agent("A2", "O2"))
        results = registry.find_exact(name="A1", organization="O1")
        assert len(results) == 1
        assert results[0].name == "A1"

    def test_find_exact_by_name_only(self, registry):
        registry.register(self._make_agent("UniqueName", "OrgX"))
        registry.register(self._make_agent("Other", "OrgY"))
        results = registry.find_exact(name="UniqueName")
        assert len(results) == 1

    def test_find_exact_by_org_only(self, registry):
        registry.register(self._make_agent("X", "SpecialOrg"))
        registry.register(self._make_agent("Y", "SpecialOrg"))
        results = registry.find_exact(organization="SpecialOrg")
        assert len(results) == 2

    # ---- update ----

    def test_update_success(self, registry):
        registry.register(self._make_agent("Upd", "Org"))
        updated_data = {
            "name": "Upd", "provider": {"organization": "Org", "url": "https://new.org"},
            "description": "Updated desc", "version": "2.0.0",
            "skills": [], "capabilities": {"streaming": True},
            "default_input_modes": [], "default_output_modes": [],
        }
        result = registry.update("Upd", "Org", updated_data)
        assert result is True
        agent = registry.get_by_key("Upd", "Org")
        assert agent.description == "Updated desc"

    def test_update_not_found(self, registry):
        result = registry.update("Ghost", "Org", {"name": "Ghost", "provider": {"organization": "Org"}})
        assert result is False

    def test_update_changes_name_raises(self, registry):
        registry.register(self._make_agent("Orig", "Org"))
        data = {"name": "Changed", "provider": {"organization": "Org", "url": "https://x.com"},
                "description": "d", "version": "1", "skills": [], "capabilities": {},
                "default_input_modes": [], "default_output_modes": []}
        with pytest.raises(ValueError, match="Cannot change primary key"):
            registry.update("Orig", "Org", data)

    # ---- deregister ----

    def test_deregister_success(self, registry):
        agent = self._make_agent("Del", "Org")
        registry.register(agent)
        result = registry.deregister("Del", "Org")
        assert result is True
        assert registry.get_by_key("Del", "Org") is None

    def test_deregister_not_found(self, registry):
        result = registry.deregister("Ghost", "Org")
        assert result is False

    def test_deregister_cleans_metadata(self, registry):
        agent = self._make_agent("Clean", "Org")
        registry.register(agent)
        registry.deregister("Clean", "Org")
        assert registry.get_status("Clean", "Org") is None

    # ---- status ----

    def test_get_status_after_register(self, registry):
        registry.register_with_status(self._make_agent("S", "O"), initial_status='registered')
        assert registry.get_status("S", "O") == 'registered'

    def test_get_status_not_found(self, registry):
        assert registry.get_status("X", "Y") is None

    def test_update_status(self, registry):
        registry.register_with_status(self._make_agent("S2", "O"), initial_status='registered')
        result = registry.update_status("S2", "O", "published")
        assert result is True
        assert registry.get_status("S2", "O") == "published"

    def test_update_status_not_found(self, registry):
        assert registry.update_status("Ghost", "Org", "published") is False

    def test_get_agents_by_status(self, registry):
        registry.register_with_status(self._make_agent("Pub", "O"), initial_status='published')
        registry.register_with_status(self._make_agent("Reg", "O"), initial_status='registered')
        published = registry.get_agents_by_status('published')
        registered = registry.get_agents_by_status('registered')
        assert len(published) == 1
        assert published[0].name == "Pub"
        assert len(registered) == 1
        assert registered[0].name == "Reg"

    # ---- persistence round-trip ----

    def test_persistence_round_trip(self, temp_dir):
        with patch('agent_registry.core.get_llm_instance', return_value=MagicMock()), \
             patch('agent_registry.core.get_embed_instance', return_value=MagicMock()), \
             patch('agent_registry.core.get_root_path', return_value=temp_dir), \
             patch('agent_registry.config.get_conf', return_value={}), \
             patch('agent_registry.config.get_persistence_conf', return_value={'persistence.mode': 'file'}):
            reg1 = RegistryCore(persistence_file='agentcard.json', persistence_metadata_file='agentregistry.json',
                                use_vectordb=False, persistence_mode='file', persistence_conf={})
            reg1.register(self._make_agent("Persist", "Org"))
            reg1.update_status("Persist", "Org", "registered")

            reg2 = RegistryCore(persistence_file='agentcard.json', persistence_metadata_file='agentregistry.json',
                                use_vectordb=False, persistence_mode='file', persistence_conf={})
            assert reg2.count() == 1
            assert reg2.get_status("Persist", "Org") == "registered"
            assert reg2.get_by_key("Persist", "Org") is not None

    # ---- _make_id ----

    def test_make_id_with_separator_no_collision(self, registry):
        id1 = registry._make_id("ab", "c")
        id2 = registry._make_id("a", "bc")
        assert id1 != id2


class TestFileStorageCRUD:
    """Unit tests for FileStorage create/update/delete/query operations."""

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp()
        yield d
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def fs(self, temp_dir):
        from agent_registry.persistence.file_storage import FileStorage
        fp = os.path.join(temp_dir, "agents.json")
        mf = os.path.join(temp_dir, "registry.json")
        tf = os.path.join(temp_dir, "tags.json")
        return FileStorage(fp, mf, tf)

    def _make_agent(self, name="A", org="O", desc="d"):
        return AgentCard(
            name=name, provider={"organization": org, "url": "https://x.com"},
            description=desc, version="1", skills=[], capabilities={},
            default_input_modes=[], default_output_modes=[]
        )

    def test_create_uses_status_param(self, fs):
        agent = self._make_agent()
        fs.create(agent, status='registered')
        key = ("A", "O")
        assert fs._status_map[key] == 'registered'

    def test_create_uses_owner_param(self, fs):
        agent = self._make_agent()
        fs.create(agent, owner="owner1")
        key = ("A", "O")
        assert fs._owner_map[key] == "owner1"

    def test_create_duplicate_returns_false(self, fs):
        fs.create(self._make_agent())
        assert fs.create(self._make_agent()) is False

    def test_find_by_key_with_status_and_tags(self, fs):
        fs.create(self._make_agent("N", "O"), owner="o1")
        fs.update_agent_tags("N", "O", ["tag1"])
        record = fs.find_by_key("N", "O")
        assert record is not None
        assert record.status == "published"
        assert record.tags == ["tag1"]
        assert record.owner == "o1"
        assert record.created_at

    def test_find_by_key_not_found(self, fs):
        assert fs.find_by_key("X", "Y") is None

    def test_find_by_name(self, fs):
        fs.create(self._make_agent("AAA", "O1"))
        fs.create(self._make_agent("BBB", "O2"))
        results = fs.find_by_name("AAA")
        assert len(results) == 1
        assert results[0].name == "AAA"

    def test_find_all(self, fs):
        fs.create(self._make_agent("A", "O"))
        fs.create(self._make_agent("B", "O"))
        assert len(fs.find_all()) == 2

    def test_find_by_organization(self, fs):
        fs.create(self._make_agent("A", "OrgX"))
        fs.create(self._make_agent("B", "OrgY"))
        assert len(fs.find_by_organization("OrgX")) == 1

    def test_find_by_status(self, fs):
        fs.create(self._make_agent("Pub", "O"), status='published')
        fs.create(self._make_agent("Reg", "O"), status='registered')
        assert len(fs.find_by_status("published")) == 1
        assert len(fs.find_by_status("registered")) == 1

    def test_update_success(self, fs):
        fs.create(self._make_agent("U", "O"))
        data = {"name": "U", "provider": {"organization": "O", "url": "https://y.com"},
                "description": "new", "version": "2", "skills": [], "capabilities": {},
                "default_input_modes": [], "default_output_modes": []}
        assert fs.update("U", "O", data) is True

    def test_update_not_found(self, fs):
        assert fs.update("X", "Y", {"name": "X"}) is False

    def test_update_updates_status(self, fs):
        fs.create(self._make_agent("Us", "O"), status='published')
        data = {"name": "Us", "provider": {"organization": "O", "url": "https://y.com"},
                "description": "new", "version": "2", "skills": [], "capabilities": {},
                "default_input_modes": [], "default_output_modes": []}
        fs.update("Us", "O", data)
        fs.update_status("Us", "O", "registered")
        assert fs.find_by_key("Us", "O").status == "registered"

    def test_delete_success(self, fs):
        fs.create(self._make_agent("D", "O"))
        assert fs.delete("D", "O") is True
        key = ("D", "O")
        assert key not in fs._agents

    def test_delete_not_found(self, fs):
        assert fs.delete("X", "Y") is False

    def test_persistence_roundtrip(self, temp_dir):
        from agent_registry.persistence.file_storage import FileStorage
        fp = os.path.join(temp_dir, "agents.json")
        mf = os.path.join(temp_dir, "registry.json")
        tf = os.path.join(temp_dir, "tags.json")
        fs1 = FileStorage(fp, mf, tf)
        fs1.create(self._make_agent("P", "O"), owner="o1")
        fs1.update_agent_tags("P", "O", ["t1"])
        fs2 = FileStorage(fp, mf, tf)
        record = fs2.find_by_key("P", "O")
        assert record is not None
        assert record.owner == "o1"
        assert record.tags == ["t1"]

    def test_count(self, fs):
        assert fs.count() == 0
        fs.create(self._make_agent("A", "O"))
        assert fs.count() == 1

    def test_find_by_owner(self, fs):
        fs.create(self._make_agent("A1", "O1"), owner="x")
        fs.create(self._make_agent("A2", "O2"), owner="y")
        results = fs.find_by_owner("x")
        assert len(results) == 1
        assert results[0].agent_card.name == "A1"

    def test_find_by_tag(self, fs):
        fs.create(self._make_agent("A1", "O1"))
        fs.create(self._make_agent("A2", "O2"))
        fs.update_agent_tags("A1", "O1", ["prod"])
        fs.update_agent_tags("A2", "O2", ["dev"])
        agents = fs.find_by_tag("prod")
        assert len(agents) == 1
        assert agents[0].name == "A1"

    def test_update_status_direct(self, fs):
        fs.create(self._make_agent("S", "O"), status='registered')
        assert fs.update_status("S", "O", "published") is True
        assert fs.find_by_key("S", "O").status == "published"

    def test_get_created_at(self, fs):
        fs.create(self._make_agent("C", "O"))
        created = fs.get_created_at("C", "O")
        assert created != ''

    def test_get_updated_at(self, fs):
        fs.create(self._make_agent("U", "O"))
        updated = fs.get_updated_at("U", "O")
        assert updated != ''
