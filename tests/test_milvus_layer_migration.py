# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Milvus layer migration without a Milvus service."""

import copy

import pytest

from agent_registry.persistence.milvus_layer_migration import (
    LayerMigrationRequiredError,
    migrate_collection,
    migrate_in_place,
    require_layer_field,
)
from common.vector_db.vector_db_client.milvus_client import MilvusDBClient


def _schema(names, dynamic=False):
    return {
        "fields": [{"name": name} for name in names],
        "enable_dynamic_field": dynamic,
    }


class _Iterator:
    def __init__(self, rows):
        self._rows = list(rows)
        self.closed = False

    def next(self):
        if not self._rows:
            return []
        return self._rows.pop(0)

    def close(self):
        self.closed = True


class _MilvusDouble:
    def __init__(self, schemas, entities):
        self.schemas = schemas
        self.entities = entities
        self.upserted = []
        self.inserted = []
        self.loaded = []

    def has_collection(self, collection_name):
        return collection_name in self.schemas

    def describe_collection(self, collection_name):
        return self.schemas[collection_name]

    def load_collection(self, collection_name):
        self.loaded.append(collection_name)

    def query_iterator(self, collection_name, batch_size, filter, output_fields):
        del batch_size, filter
        rows = [
            {field: row[field] for field in output_fields if field in row}
            for row in self.entities.get(collection_name, [])
        ]
        return _Iterator([rows] if rows else [])

    def upsert(self, collection_name, data):
        self.upserted.extend(copy.deepcopy(data))
        by_id = {row["id"]: row for row in self.entities[collection_name]}
        for row in data:
            by_id[row["id"]] = copy.deepcopy(row)
        self.entities[collection_name] = list(by_id.values())

    def insert(self, collection_name, data):
        self.inserted.extend(copy.deepcopy(data))
        self.entities.setdefault(collection_name, []).extend(copy.deepcopy(data))


_BASE_FIELDS = ["id", "embedding", "name", "description", "organization", "agent_card"]
_LAYER_FIELDS = _BASE_FIELDS + ["status", "owner", "layer"]


def _entity(agent_id, layer_marker=None):
    entity = {
        "id": agent_id,
        "embedding": [0.1, 0.2],
        "name": agent_id,
        "description": "test agent",
        "organization": "test-org",
        "agent_card": "{}",
        "status": "published",
        "owner": None,
    }
    if layer_marker is not None:
        entity["layer"] = layer_marker
    return entity


def test_in_place_migration_backfills_unknown_and_preserves_embedding():
    client = _MilvusDouble(
        {"agents": _schema(_LAYER_FIELDS)},
        {"agents": [_entity("known", "omc"), _entity("legacy")]},
    )

    result = migrate_in_place(client, "agents", batch_size=1)

    assert result.scanned == 2
    assert result.updated == 1
    assert result.unknown_assigned == 1
    assert client.upserted == [{**_entity("legacy"), "layer": "unknown"}]
    assert client.entities["agents"][1]["embedding"] == [0.1, 0.2]
    assert client.loaded == ["agents"]


def test_in_place_migration_preserves_vendor_defined_layer():
    custom_layer = "vendor.custom-network-layer"
    client = _MilvusDouble(
        {"agents": _schema(_LAYER_FIELDS)},
        {"agents": [_entity("custom", custom_layer)]},
    )

    result = migrate_in_place(client, "agents")

    assert result.scanned == 1
    assert result.updated == 0
    assert result.unknown_assigned == 0
    assert client.upserted == []
    assert client.entities["agents"][0]["layer"] == custom_layer


def test_rebuild_migration_copies_legacy_entities_to_layer_aware_collection():
    source_rows = [_entity("one"), _entity("two")]
    client = _MilvusDouble(
        {"legacy_agents": _schema(_BASE_FIELDS)},
        {"legacy_agents": copy.deepcopy(source_rows)},
    )
    source_before = copy.deepcopy(source_rows)

    def create_target(collection_name):
        client.schemas[collection_name] = _schema(_LAYER_FIELDS)
        client.entities[collection_name] = []

    result = migrate_collection(
        client,
        "legacy_agents",
        target_collection="layer_agents",
        create_target=create_target,
    )

    assert result.mode == "rebuild"
    assert result.scanned == 2
    assert result.copied == 2
    assert result.unknown_assigned == 2
    assert client.entities["legacy_agents"] == source_before
    assert [row["layer"] for row in client.entities["layer_agents"]] == [
        "unknown", "unknown"
    ]
    assert all(row["embedding"] == [0.1, 0.2] for row in client.entities["layer_agents"])
    assert client.loaded == ["legacy_agents"]


def test_rebuild_dry_run_scans_without_creating_or_writing_target():
    client = _MilvusDouble(
        {"legacy_agents": _schema(_BASE_FIELDS)},
        {"legacy_agents": [_entity("one")]},
    )
    created = []

    result = migrate_collection(
        client,
        "legacy_agents",
        target_collection="layer_agents",
        dry_run=True,
        create_target=lambda name: created.append(name),
    )

    assert result.scanned == 1
    assert result.copied == 0
    assert result.unknown_assigned == 1
    assert created == []
    assert "layer_agents" not in client.schemas


def test_layer_filter_requires_explicit_layer_field():
    client = _MilvusDouble({"legacy_agents": _schema(_BASE_FIELDS)}, {})

    with pytest.raises(LayerMigrationRequiredError, match="no layer field"):
        require_layer_field(client, "legacy_agents")


def test_migration_rejects_entity_without_embedding():
    entity = _entity("broken")
    del entity["embedding"]
    client = _MilvusDouble(
        {"agents": _schema(_LAYER_FIELDS)},
        {"agents": [entity]},
    )

    with pytest.raises(LayerMigrationRequiredError, match="no embedding"):
        migrate_in_place(client, "agents")

    assert client.upserted == []


def test_milvus_client_uses_only_fields_supported_by_legacy_schema():
    client = _MilvusDouble({"legacy_agents": _schema(_BASE_FIELDS)}, {})
    wrapper = MilvusDBClient.__new__(MilvusDBClient)
    wrapper.client = client

    assert wrapper._output_fields_for_collection("legacy_agents") == [
        "id", "name", "description", "organization", "agent_card"
    ]
    with pytest.raises(LayerMigrationRequiredError, match="no layer field"):
        wrapper._require_layer_field("legacy_agents")


def test_milvus_client_rejects_layer_write_before_migration():
    client = _MilvusDouble({"legacy_agents": _schema(_BASE_FIELDS)}, {})
    wrapper = MilvusDBClient.__new__(MilvusDBClient)
    wrapper.client = client

    with pytest.raises(LayerMigrationRequiredError, match="no layer field"):
        wrapper.insert_entity({
            "collection_name": "legacy_agents",
            "entity": {"id": "agent", "layer": "omc", "embedding": []},
        })
