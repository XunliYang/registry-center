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

"""Core semantic selection keeps failures distinct from genuine no matches."""

import shutil
import tempfile

import pytest
from unittest.mock import MagicMock, patch

from a2a.types import AgentCard

from agent_registry.core import RegistryCore
from agent_registry.errors import SemanticSearchUnavailable


class TestRetrieveByTask:
    """Unit tests for RegistryCore.retrieve_by_task in file persistence mode."""

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp()
        yield d
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

    def test_llm_failure_reports_unavailable(self, registry):
        registry.register(self._make_agent())
        with patch.object(registry.llm, 'ask_llm', side_effect=RuntimeError("LLM service down")):
            with pytest.raises(SemanticSearchUnavailable):
                registry.retrieve_by_task("find test agents", top_n=5, use_vectordb=False)

    def test_llm_garbage_response_reports_unavailable(self, registry):
        registry.register(self._make_agent())
        with patch.object(registry.llm, 'ask_llm', return_value=("raw", "not-json-at-all")):
            with pytest.raises(SemanticSearchUnavailable):
                registry.retrieve_by_task("find test agents", top_n=5, use_vectordb=False)

    def test_empty_task_returns_empty_without_llm_call(self, registry):
        # 空 task 短路返回，不触发 LLM 调用
        registry.register(self._make_agent())
        with patch.object(registry.llm, 'ask_llm') as mock_ask:
            result = registry.retrieve_by_task("", top_n=5, use_vectordb=False)
        assert result == []
        mock_ask.assert_not_called()

    @pytest.mark.parametrize('response', ['', '{}', 'null', '[1]', '[{"name": 123}]'])
    def test_invalid_model_shape_is_not_no_match(self, registry, response):
        registry.register(self._make_agent())
        with patch.object(registry.llm, 'ask_llm', return_value=('raw', response)):
            with pytest.raises(SemanticSearchUnavailable):
                registry.retrieve_by_task('find', top_n=5, use_vectordb=False)

    def test_explicit_empty_selection_is_a_genuine_no_match(self, registry):
        registry.register(self._make_agent())
        with patch.object(registry.llm, 'ask_llm', return_value=('raw', '[]')):
            assert registry.retrieve_by_task('find', top_n=5, use_vectordb=False) == []

    def test_llm_selection_filters_registered_agents(self, registry):
        # 正常路径：LLM 返回的 (name, organization) 对决定哪些卡片命中
        registry.register(self._make_agent("AgentA", "TestOrg", "Agent for task A"))
        registry.register(self._make_agent("AgentB", "TestOrg", "Agent for task B"))
        llm_response = ("raw", '[{"name": "AgentA", "organization": "TestOrg"}]')
        with patch.object(registry.llm, 'ask_llm', return_value=llm_response):
            result = registry.retrieve_by_task("need agent a", top_n=5, use_vectordb=False)
        assert [a.name for a in result] == ["AgentA"]

    def test_no_registered_agents_returns_empty_without_llm_call(self, registry):
        # 注册表为空时短路返回，不触发 LLM 调用
        with patch.object(registry.llm, 'ask_llm') as mock_ask:
            result = registry.retrieve_by_task("find test agents", top_n=5, use_vectordb=False)
        assert result == []
        mock_ask.assert_not_called()


class TestVectorBranchAgreesWithRecordStore:
    """The `use_vectordb=True` candidate branch must behave like the SQL branch.

    `use_vectordb=True` on an instance that *has* a record store is the explicit
    override path (the vector-only deployment refuses the call outright, see
    `test_vector_only_mode.py`), which makes it reachable in tests with a fake
    index.
    """

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp()
        yield d
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
            reg.vectordb = MagicMock()
            # Only a vector-mode instance builds these; the override path is what
            # makes the branch reachable here.
            reg.embedding_tool = MagicMock()
            yield reg

    def test_empty_index_returns_empty_without_llm_call(self, registry):
        registry.vectordb.retrieve_entity.return_value = []
        with patch.object(registry.llm, 'ask_llm') as mock_ask:
            result = registry.retrieve_by_task("find test agents", top_n=5, use_vectordb=True)
        assert result == []
        mock_ask.assert_not_called()

    def test_candidates_that_are_not_discoverable_return_empty_without_llm_call(self, registry):
        """A pending card is not a candidate, so there is nothing to select from."""
        registry.vectordb.retrieve_entity.return_value = [
            {"name": "Pending", "organization": "TestOrg", "status": "registered"},
        ]
        with patch.object(registry.llm, 'ask_llm') as mock_ask:
            result = registry.retrieve_by_task("find test agents", top_n=5, use_vectordb=True)
        assert result == []
        mock_ask.assert_not_called()

    def test_missing_status_still_counts_as_discoverable(self, registry):
        """Legacy index rows have no status; the shared policy keeps them visible."""
        registry.vectordb.retrieve_entity.return_value = [
            {"name": "Legacy", "organization": "TestOrg", "description": "legacy row"},
        ]
        with patch.object(registry.llm, 'ask_llm',
                          return_value=("raw", '[{"name": "Legacy", "organization": "TestOrg"}]')):
            result = registry.retrieve_by_task("find legacy", top_n=5, use_vectordb=True)
        assert [a["name"] for a in result] == ["Legacy"]

    def test_discoverable_candidates_still_go_to_the_model(self, registry):
        registry.vectordb.retrieve_entity.return_value = [
            {"name": "AgentA", "organization": "TestOrg", "status": "published",
             "description": "Agent for task A"},
        ]
        with patch.object(registry.llm, 'ask_llm',
                          return_value=("raw", '[{"name": "AgentA", "organization": "TestOrg"}]')):
            result = registry.retrieve_by_task("need agent a", top_n=5, use_vectordb=True)
        assert [a["name"] for a in result] == ["AgentA"]

    def test_embedding_failure_is_unavailable_not_a_raw_error(self, registry):
        registry.embedding_tool.embed.side_effect = RuntimeError("embedding service down")
        with pytest.raises(SemanticSearchUnavailable):
            registry.retrieve_by_task("find test agents", top_n=5, use_vectordb=True)

    def test_embedding_failure_does_not_leak_upstream_detail(self, registry):
        registry.embedding_tool.embed.side_effect = RuntimeError(
            "http://internal-llm:8080 refused with api-key=secret")
        with pytest.raises(SemanticSearchUnavailable) as excinfo:
            registry.retrieve_by_task("find test agents", top_n=5, use_vectordb=True)
        assert "secret" not in str(excinfo.value)
        assert "internal-llm" not in str(excinfo.value)

    def test_both_branches_agree_that_no_candidates_is_no_match(self, registry):
        """Same question, same empty answer, whether the index or the store is read."""
        registry.vectordb.retrieve_entity.return_value = []
        with patch.object(registry.llm, 'ask_llm') as mock_ask:
            assert registry.retrieve_by_task("find", top_n=5, use_vectordb=True) == []
            assert registry.retrieve_by_task("find", top_n=5, use_vectordb=False) == []
        mock_ask.assert_not_called()
