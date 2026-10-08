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

import json

import pytest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from google.protobuf.json_format import Parse

from a2a.types import AgentCard
from agent_registry.server import app, get_registry


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def published_registry_override():
    """The endpoint enforces the discovery policy through the injected registry.

    Cards are published unless a test overrides get_status, so the retrieval
    mock below stays the only thing deciding which cards come back.
    """
    registry = MagicMock()
    registry.get_status.return_value = "published"
    app.dependency_overrides[get_registry] = lambda: registry
    yield registry
    app.dependency_overrides.clear()


@pytest.fixture
def sample_agent_card():
    return Parse(json.dumps({
        "name": "SemanticTestAgent",
        "description": "An agent that handles energy saving tasks",
        "version": "1.0.0",
        "documentationUrl": "https://test-agent.example.com",
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "provider": {
            "organization": "TestOrg",
            "url": "https://test-org.example.com"
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False
        },
        "skills": [{
            "id": "skill-1",
            "name": "Energy Saving",
            "description": "Performs energy saving analysis and optimization",
            "tags": ["energy", "optimization"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"]
        }]
    }), AgentCard())


class TestSemanticQuery:
    def test_returns_empty_when_no_agents(self, client):
        with patch(
            'agent_registry.registry_instance._registry_instance',
            create=True
        ) as mock_registry:
            mock_registry.retrieve_by_task = MagicMock(return_value=[])

            response = client.post(
                "/rest/v1/registry-center/agent-cards/semantic-query?top_n=5",
                json={"task": "find energy saving agents"}
            )
            assert response.status_code == 200
            assert response.json() == {"agentCards": []}

    def test_returns_matching_agents(self, client, sample_agent_card):
        with patch(
            'agent_registry.registry_instance._registry_instance',
            create=True
        ) as mock_registry:
            mock_registry.retrieve_by_task = MagicMock(return_value=[sample_agent_card])

            response = client.post(
                "/rest/v1/registry-center/agent-cards/semantic-query?top_n=3",
                json={"task": "energy saving"}
            )
            assert response.status_code == 200
            data = response.json()
            assert "agentCards" in data
            assert len(data["agentCards"]) == 1
            agent = data["agentCards"][0]
            assert agent["name"] == "SemanticTestAgent"
            assert "skills" in agent

    def test_handles_top_n_param(self, client, sample_agent_card):
        with patch(
            'agent_registry.registry_instance._registry_instance',
            create=True
        ) as mock_registry:
            mock_registry.retrieve_by_task = MagicMock(return_value=[sample_agent_card])

            response = client.post(
                "/rest/v1/registry-center/agent-cards/semantic-query?top_n=1",
                json={"task": "test"}
            )
            assert response.status_code == 200

    @pytest.mark.parametrize('task', ['', '   ', True, 12, {}, [], 'x' * 10001])
    def test_rejects_invalid_task_without_query(self, client, task):
        with patch(
            'agent_registry.registry_instance._registry_instance',
            create=True
        ) as mock_registry:
            mock_registry.retrieve_by_task = MagicMock(return_value=[])

            response = client.post(
                "/rest/v1/registry-center/agent-cards/semantic-query",
                json={"task": task}
            )
            assert response.status_code == 422
            mock_registry.retrieve_by_task.assert_not_called()

    def test_internal_error_returns_500(self, client):
        with patch(
            'agent_registry.registry_instance._registry_instance',
            create=True
        ) as mock_registry:
            mock_registry.retrieve_by_task = MagicMock(side_effect=RuntimeError("LLM failure"))

            response = client.post(
                "/rest/v1/registry-center/agent-cards/semantic-query",
                json={"task": "test"}
            )
            assert response.status_code == 500

    def test_missing_task_field_returns_422_without_query(self, client):
        with patch(
            'agent_registry.registry_instance._registry_instance',
            create=True
        ) as mock_registry:
            mock_registry.retrieve_by_task = MagicMock(return_value=[])

            response = client.post(
                "/rest/v1/registry-center/agent-cards/semantic-query",
                json={}
            )
            assert response.status_code == 422
            mock_registry.retrieve_by_task.assert_not_called()

    @pytest.mark.parametrize("body", [b"", b"{not json", b"[]", b"null"])
    def test_malformed_request_body_returns_422(self, client, body):
        """An unparseable or non-object body is client input, never a 500."""
        response = client.post(
            "/rest/v1/registry-center/agent-cards/semantic-query",
            content=body,
            headers={"content-type": "application/json"}
        )
        assert response.status_code == 422
        message = response.json()["errors"]["error"][0]["errorMessage"]
        assert "JSON object" in message
