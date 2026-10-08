# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for Agent registration layer metadata and layer-aware queries."""

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import AgentCard
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict

from agent_registry.core import RegistryCore
from agent_registry.model.agent_layer import (
    LAYER_UNSET,
    UNKNOWN_LAYER,
    normalize_layer,
    normalize_registration_item,
)
from agent_registry.persistence.base import AgentRecord
from agent_registry.persistence.file_storage import FileStorage
from agent_registry.persistence.milvus_layer_migration import LayerMigrationRequiredError
from agent_registry.persistence.sqlite_storage import SQLiteStorage
from agent_registry.server import app, _parse_layer_query_body
from agent_registry.signature.agent_card_signature_validator import (
    AgentCardSignatureValidator,
    ValidationResult,
)


def sample_card(name="layer-agent", organization="layer-org"):
    return AgentCard(
        name=name,
        provider={"organization": organization, "url": "https://example.test"},
        description="Agent used by layer tests",
        version="1.0.0",
        capabilities={"streaming": False},
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[],
    )


class TestLayerModel:

    def test_layer_accepts_vendor_defined_strings(self):
        assert normalize_layer("omc") == "omc"
        assert normalize_layer("domain_workbench") == "domain_workbench"
        assert normalize_layer("Vendor Custom Layer") == "Vendor Custom Layer"
        assert normalize_layer("OMC") == "OMC"
        for value in (None, "", "   ", 1):
            with pytest.raises(ValueError):
                normalize_layer(value)
        assert UNKNOWN_LAYER == "unknown"

    def test_layer_length_is_bounded_for_storage_backends(self):
        with pytest.raises(ValueError):
            normalize_layer("x" * 65)

    def test_registration_item_supports_legacy_and_wrapped_shapes(self):
        card = MessageToDict(sample_card(), preserving_proto_field_name=True)
        legacy = normalize_registration_item(card)
        wrapped = normalize_registration_item({"agentCard": card, "layer": "omc"})

        assert legacy.wrapped is False
        assert legacy.layer is LAYER_UNSET
        assert wrapped.wrapped is True
        assert wrapped.layer == "omc"

    def test_registration_item_rejects_mixed_or_top_level_layer(self):
        card = MessageToDict(sample_card(), preserving_proto_field_name=True)
        with pytest.raises(ValueError):
            normalize_registration_item({**card, "layer": "omc"})
        with pytest.raises(ValueError):
            normalize_registration_item({"agentCard": card, "name": card["name"]})
        with pytest.raises(ValueError):
            normalize_registration_item({"agentCard": card, "layer": None})


class TestLayerQueryValidation:

    def test_ordinary_query_defaults_and_rejects_semantic_fields(self):
        assert _parse_layer_query_body({"layer": "unknown"}) == {
            "layer": "unknown", "task": "", "limit": 100, "offset": 0,
            "semantic": False,
        }
        with pytest.raises(Exception):
            _parse_layer_query_body({"limit": 10, "topN": 2})

    def test_semantic_query_defaults_and_rejects_pagination_fields(self):
        query = _parse_layer_query_body({"task": "find an agent", "layer": "omc"})
        assert query["semantic"] is True
        assert query["top_n"] == 10
        with pytest.raises(Exception):
            _parse_layer_query_body({"task": "find an agent", "offset": 1})


class TestFileLayerStorage:

    def test_create_filter_and_update_preserves_or_clears_layer(self, tmp_path):
        storage = FileStorage(
            str(tmp_path / "agents.json"),
            str(tmp_path / "metadata.json"),
            str(tmp_path / "tags.json"),
        )
        card = sample_card()
        custom_layer = "vendor.custom-network-layer"
        assert storage.create(card, layer=custom_layer) is True
        assert storage.find_records(layer=custom_layer)[0].layer == custom_layer
        assert storage.find_records(layer="unknown") == []

        data = MessageToDict(card, preserving_proto_field_name=True)
        data["description"] = "updated"
        assert storage.update(card.name, card.provider.organization, data) is True
        assert storage.find_by_key(card.name, card.provider.organization).layer == custom_layer

        assert storage.update(
            card.name, card.provider.organization, data, layer="unknown"
        ) is True
        assert storage.find_records(layer="unknown")[0].layer == "unknown"

    def test_old_metadata_is_backfilled_to_unknown(self, tmp_path):
        card = sample_card()
        card_path = tmp_path / "agents.json"
        metadata_path = tmp_path / "metadata.json"
        tags_path = tmp_path / "tags.json"
        card_path.write_text(json.dumps([
            MessageToDict(card, preserving_proto_field_name=True)
        ]), encoding="utf-8")
        metadata_path.write_text(json.dumps([{
            "agent_name": card.name,
            "organization": card.provider.organization,
            "status": "published",
        }]), encoding="utf-8")

        storage = FileStorage(str(card_path), str(metadata_path), str(tags_path))
        assert storage.find_by_key(card.name, card.provider.organization).layer == "unknown"
        migrated = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert migrated[0]["layer"] == "unknown"


class TestSQLiteLayerStorage:

    def test_layer_column_index_and_round_trip(self, tmp_path):
        path = tmp_path / "agents.db"
        storage = SQLiteStorage.init({"sqlite.path": str(path)})
        columns = {
            row[1] for row in storage._conn.execute("PRAGMA table_info(agent_card)")
        }
        indexes = {
            row[1] for row in storage._conn.execute("PRAGMA index_list(agent_card)")
        }
        assert "layer" in columns
        assert "idx_agent_layer" in indexes

        card = sample_card()
        custom_layer = "vendor.custom-network-layer"
        assert storage.create(card, layer=custom_layer) is True
        assert storage.find_records(layer=custom_layer)[0].layer == custom_layer
        data = MessageToDict(card, preserving_proto_field_name=True)
        assert storage.update(card.name, card.provider.organization, data) is True
        assert storage.find_by_key(card.name, card.provider.organization).layer == custom_layer
        storage.close()

    def test_existing_sqlite_table_gets_layer_column(self, tmp_path):
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE agent_card (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, organization TEXT NOT NULL, owner TEXT,
                description TEXT, url TEXT, version TEXT,
                status TEXT DEFAULT 'published', provider_json TEXT NOT NULL,
                capabilities_json TEXT, skills_json TEXT,
                default_input_modes TEXT, default_output_modes TEXT,
                agent_card_json TEXT NOT NULL, tags TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

        storage = SQLiteStorage.init({"sqlite.path": str(path)})
        columns = {
            row[1] for row in storage._conn.execute("PRAGMA table_info(agent_card)")
        }
        assert "layer" in columns
        storage.close()


class TestLayerAwareEndpoints:

    @pytest.fixture(autouse=True)
    def clear_overrides(self):
        app.dependency_overrides.clear()
        yield
        app.dependency_overrides.clear()

    @staticmethod
    def _dependencies(record=None):
        registry = MagicMock(spec=RegistryCore)
        registry.count.return_value = 0
        registry.get_agents.return_value = {}
        registry.get_status.return_value = "published"
        registry.get_by_key_with_owner.return_value = record
        registry.find_records.return_value = [record] if record else []
        registry.retrieve_records_by_task.return_value = [record] if record else []

        validator = MagicMock(spec=AgentCardSignatureValidator)
        validator.validate_agent_card.return_value = ValidationResult(is_valid=True)
        signer = MagicMock()
        signer.is_enabled.return_value = False

        from agent_registry.server import (
            get_registry, get_registry_signer, get_signature_validator,
        )
        app.dependency_overrides[get_registry] = lambda: registry
        app.dependency_overrides[get_signature_validator] = lambda: validator
        app.dependency_overrides[get_registry_signer] = lambda: signer
        return registry, validator, signer

    def test_wrapped_registration_passes_layer_and_returns_it(self):
        card = MessageToDict(sample_card(), preserving_proto_field_name=True)
        registry, _, _ = self._dependencies()
        handler = MagicMock()
        handler.handle = AsyncMock(return_value=True)

        with patch("common.custom.custom_handle.HandlerRegistry.get_handler", return_value=handler):
            response = TestClient(app).post(
                "/rest/v1/registry-center/agent-cards",
                json={"agentCards": [{"agentCard": card, "layer": "omc"}]},
            )

        assert response.status_code == 201
        assert response.json()["results"][0]["layer"] == "omc"
        assert any(
            call.kwargs.get("layer") == "omc"
            for call in handler.handle.await_args_list
        )
        registry.get_agents.assert_called_once()

    def test_existing_list_query_accepts_layer_without_changing_response(self):
        card = sample_card()
        record = AgentRecord(agent_card=card, status="published", layer="omc")
        registry, _, _ = self._dependencies(record)
        handler = MagicMock()
        handler.handle = AsyncMock(return_value=None)

        with patch("common.custom.custom_handle.HandlerRegistry.get_handler", return_value=handler):
            response = TestClient(app).get(
                "/rest/v1/registry-center/agent-cards?layer=omc"
            )

        assert response.status_code == 200
        assert list(response.json()) == ["agentCards"]
        registry.find_exact.assert_called_once_with(None, None, layer="omc")

    def test_registration_detail_returns_agent_card_and_layer(self):
        record = AgentRecord(agent_card=sample_card(), status="published", layer="omc")
        registry, _, _ = self._dependencies(record)
        handler = MagicMock()
        handler.handle = AsyncMock(return_value=None)

        with patch("common.custom.custom_handle.HandlerRegistry.get_handler", return_value=handler):
            response = TestClient(app).get(
                "/rest/v1/registry-center/agent-cards-with-layer/layer-org/layer-agent"
            )

        assert response.status_code == 200
        body = response.json()
        assert body["layer"] == "omc"
        assert body["agentCard"]["name"] == "layer-agent"

    def test_registration_query_filters_by_layer_and_returns_metadata(self):
        record = AgentRecord(agent_card=sample_card(), status="published", layer="omc")
        registry, _, _ = self._dependencies(record)
        handler = MagicMock()
        handler.handle = AsyncMock(return_value=None)

        with patch("common.custom.custom_handle.HandlerRegistry.get_handler", return_value=handler):
            response = TestClient(app).post(
                "/rest/v1/registry-center/agent-cards-with-layer",
                json={"layer": "omc", "limit": 10},
            )

        assert response.status_code == 200
        assert response.json()["agents"][0]["layer"] == "omc"
        registry.find_records.assert_called_once_with(
            layer="omc", status="published", limit=11
        )

    def test_layer_query_reports_unmigrated_vector_collection(self):
        registry, _, _ = self._dependencies()
        registry.find_records.side_effect = LayerMigrationRequiredError(
            "Milvus collection 'agent_card_collection' has no layer field"
        )
        handler = MagicMock()
        handler.handle = AsyncMock(return_value=None)

        with patch("common.custom.custom_handle.HandlerRegistry.get_handler", return_value=handler):
            response = TestClient(app).post(
                "/rest/v1/registry-center/agent-cards-with-layer",
                json={"layer": "omc", "limit": 10},
            )

        assert response.status_code == 503
        assert "no layer field" in response.json()["errors"]["error"][0]["errorMessage"]

    def test_semantic_layer_query_uses_dedicated_endpoint(self):
        record = AgentRecord(agent_card=sample_card(), status="published", layer="vendor.layer")
        registry, _, _ = self._dependencies(record)
        handler = MagicMock()
        handler.handle = AsyncMock(return_value=None)

        with patch("common.custom.custom_handle.HandlerRegistry.get_handler", return_value=handler):
            response = TestClient(app).post(
                "/rest/v1/registry-center/agent-cards-with-layer/semantic-query",
                json={"layer": "vendor.layer", "task": "find an agent", "topN": 5},
            )

        assert response.status_code == 200
        assert response.json()["agents"][0]["layer"] == "vendor.layer"
        registry.retrieve_records_by_task.assert_called_once_with(
            "find an agent", 5, layer="vendor.layer", status="published"
        )
