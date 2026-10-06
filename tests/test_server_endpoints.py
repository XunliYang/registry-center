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

"""Integration tests for server REST endpoints using FastAPI dependency overrides."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from a2a.types import AgentCard

from agent_registry.server import app
from agent_registry.core import RegistryCore
from agent_registry.persistence.base import AgentRecord
from agent_registry.signature.agent_card_signature_validator import (
    AgentCardSignatureValidator, ValidationResult
)


@pytest.fixture(autouse=True)
def endpoint_policy(monkeypatch):
    """CRUD response tests are independent of local ownership configuration.

    TLS ownership enforcement is covered by test_owner_identity.py.
    """
    monkeypatch.setattr('agent_registry.server.OWNER_ISOLATION_ENABLED', False)


VALID_AGENT_CARD = {
    "name": "TestAgent",
    "provider": {"organization": "TestOrg", "url": "https://test.org"},
    "description": "A test agent for testing",
    "version": "1.0.0",
    "capabilities": {"streaming": False},
    "default_input_modes": ["text/plain"],
    "default_output_modes": ["text/plain"],
    "skills": [{
        "id": "s1", "name": "TestSkill", "description": "Test",
        "tags": [], "input_modes": ["text/plain"], "output_modes": ["text/plain"]
    }]
}


def _mock_agent(name="TestAgent", org="TestOrg", desc="Test", status="published"):
    return AgentCard(
        name=name, provider={"organization": org, "url": "https://test.org"},
        description=desc, version="1.0.0",
        capabilities={"streaming": False},
        default_input_modes=[], default_output_modes=[],
        skills=[]
    )


@pytest.fixture
def mock_registry():
    mock = MagicMock(spec=RegistryCore)
    mock.count.return_value = 0
    mock.get_agents.return_value = {}
    mock.get_status.return_value = "published"
    mock.get_by_key_with_owner.return_value = AgentRecord(
        agent_card=_mock_agent(), owner=None, status="published"
    )
    return mock


@pytest.fixture
def mock_validator():
    mock = MagicMock(spec=AgentCardSignatureValidator)
    mock.validate_agent_card_async = AsyncMock(return_value=ValidationResult(is_valid=True))
    return mock


@pytest.fixture
def mock_signer():
    mock = MagicMock()
    mock.is_enabled.return_value = False
    return mock


@pytest.fixture(autouse=True)
def clean_app_overrides():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def _override_deps(mock_registry, mock_validator, mock_signer):
    from agent_registry.server import get_registry, get_signature_validator, get_registry_signer
    app.dependency_overrides[get_registry] = lambda: mock_registry
    app.dependency_overrides[get_signature_validator] = lambda: mock_validator
    app.dependency_overrides[get_registry_signer] = lambda: mock_signer


class TestRegisterEndpoint:

    def test_register_success(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=True)

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.post(
                "/rest/v1/registry-center/agent-cards",
                json={"agentCards": [VALID_AGENT_CARD]}
            )
            assert response.status_code == 201
            body = response.json()
            assert body["results"] == [{
                "name": "TestAgent",
                "organization": "TestOrg",
                "status": "published",
                "registrySigned": False,
            }]

    @pytest.mark.parametrize('invalid_card', [dict(VALID_AGENT_CARD, name='Bad Agent!'),
                                             {'unknownField': 1}, False])
    def test_register_batch_failure_reports_registered_agents(self, mock_registry, mock_validator, mock_signer, invalid_card):
        _override_deps(mock_registry, mock_validator, mock_signer)
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=True)

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.post(
                "/rest/v1/registry-center/agent-cards",
                json={"agentCards": [VALID_AGENT_CARD, invalid_card]}
            )
            assert response.status_code == 422
            body = response.json()
            assert body["registeredAgents"] == [{
                "name": "TestAgent",
                "organization": "TestOrg",
                "status": "published",
                "registrySigned": False,
            }]
            assert "Card 2/2" in body["errors"]["error"][0]["errorMessage"]

    def test_register_duplicate_returns_409(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        mock_registry.get_agents.return_value = {("TestAgent", "TestOrg"): True}
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock()

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.post(
                "/rest/v1/registry-center/agent-cards",
                json={"agentCards": [VALID_AGENT_CARD]}
            )
            assert response.status_code == 409

    def test_register_empty_agent_cards_returns_422(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        client = TestClient(app)
        response = client.post(
            "/rest/v1/registry-center/agent-cards",
            json={"agentCards": []}
        )
        assert response.status_code == 422
        body = response.json()
        assert "non-empty" in body["errors"]["error"][0]["errorMessage"]


class TestUpdateEndpoint:

    @pytest.mark.parametrize('second_card', [{'unknownField': 1}, False,
                                             dict(VALID_AGENT_CARD, name='Renamed')])
    def test_partial_update_reports_completed_cards(self, mock_registry, mock_validator, mock_signer, second_card):
        _override_deps(mock_registry, mock_validator, mock_signer)
        handler = MagicMock(handle=AsyncMock(return_value=True))
        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=handler):
            response = TestClient(app).put('/rest/v1/registry-center/agent-cards/TestOrg/TestAgent',
                                           json={'agentCards': [VALID_AGENT_CARD, second_card]})
        assert response.status_code == 422
        assert len(response.json()['updatedAgents']) == 1
        assert response.json()['updatedAgents'][0]['name'] == 'TestAgent'
        mock_validator.validate_agent_card_async.assert_awaited_once()

    def test_identity_mismatch_is_rejected_before_signature_validation(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        handler = MagicMock(handle=AsyncMock(return_value=True))
        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=handler):
            response = TestClient(app).put('/rest/v1/registry-center/agent-cards/TestOrg/TestAgent',
                                           json={'agentCards': [dict(VALID_AGENT_CARD, name='Renamed')]})
        assert response.status_code == 422
        mock_validator.validate_agent_card_async.assert_not_awaited()

    def test_update_success_returns_result_body(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=True)

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.put(
                "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
                json={"agentCards": [VALID_AGENT_CARD]}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["results"] == [{
                "name": "TestAgent",
                "organization": "TestOrg",
                "registrySigned": False,
            }]

    def test_update_not_found_returns_404(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=False)

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.put(
                "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
                json={"agentCards": [VALID_AGENT_CARD]}
            )
            assert response.status_code == 404

    def test_update_empty_agent_cards_returns_422(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        client = TestClient(app)
        response = client.put(
            "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
            json={"agentCards": []}
        )
        assert response.status_code == 422
        body = response.json()
        assert "non-empty" in body["errors"]["error"][0]["errorMessage"]


class TestGetEndpoint:

    def test_get_agents_empty(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=[])

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.get("/rest/v1/registry-center/agent-cards")
            assert response.status_code == 200


class TestGetSingleEndpoint:

    def test_get_single_agent_found(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        agent = _mock_agent()
        record = AgentRecord(agent_card=agent, owner=None, status="published")
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=record)

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.get("/rest/v1/registry-center/agent-cards/TestOrg/TestAgent")
            assert response.status_code == 200
            body = response.json()
            assert len(body["agentCards"]) == 1
            assert body["agentCards"][0]["name"] == "TestAgent"

    def test_get_single_agent_not_found_returns_empty_list(self, mock_registry, mock_validator, mock_signer):
        # 当前设计：不存在、未发布、正常命中三种情况都难以区分——不存在与未发布均返回 200 + 空列表
        _override_deps(mock_registry, mock_validator, mock_signer)
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=None)

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.get("/rest/v1/registry-center/agent-cards/TestOrg/MissingAgent")
            assert response.status_code == 200
            assert response.json() == {"agentCards": []}

    def test_get_single_agent_unpublished_returns_empty_list(self, mock_registry, mock_validator, mock_signer):
        # 当前设计：存在但未发布（registered）与不存在返回相同——200 + 空列表，不暴露存在性
        _override_deps(mock_registry, mock_validator, mock_signer)
        agent = _mock_agent()
        record = AgentRecord(agent_card=agent, owner=None, status="registered")
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=record)
        mock_registry.get_status.return_value = "registered"

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.get("/rest/v1/registry-center/agent-cards/TestOrg/TestAgent")
            assert response.status_code == 200
            assert response.json() == {"agentCards": []}


class TestDeleteEndpoint:

    def test_delete_success(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=True)

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.delete("/rest/v1/registry-center/agent-cards/TestOrg/TestAgent")
            assert response.status_code == 200
            assert response.json() == {"name": "TestAgent", "organization": "TestOrg", "deleted": True}

    def test_delete_not_found(self, mock_registry, mock_validator, mock_signer):
        _override_deps(mock_registry, mock_validator, mock_signer)
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=False)

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.delete("/rest/v1/registry-center/agent-cards/TestOrg/TestAgent")
            assert response.status_code == 404


class TestSemanticQueryEndpoint:

    def test_semantic_query_success(self, mock_registry, mock_validator, mock_signer):
        agent = _mock_agent()
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock(return_value=[agent])

        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=mock_handler):
            client = TestClient(app)
            response = client.post(
                "/rest/v1/registry-center/agent-cards/semantic-query",
                params={"top_n": 3},
                json={"task": "Find test agents"}
            )
            assert response.status_code == 200
