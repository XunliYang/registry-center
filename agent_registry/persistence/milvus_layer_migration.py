# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Migration helpers for adding registration-layer metadata to Milvus.

The migration changes entity metadata only. Existing embeddings are read and
written back unchanged. A collection without an explicit ``layer`` field must
be copied to a new collection created with the current registry schema.
"""

from dataclasses import asdict, dataclass
from typing import Callable, Iterable, List, Optional, Set

from agent_registry.model.agent_layer import AgentLayer, normalize_layer


MIGRATION_OUTPUT_FIELDS = [
    "id", "embedding", "name", "description", "organization", "agent_card",
    "status", "owner", "layer",
]
_DYNAMIC_METADATA_FIELDS = {"status", "owner", "layer"}


class LayerMigrationRequiredError(RuntimeError):
    """Raised when a layer-filtered query targets an unmigrated collection."""


@dataclass
class LayerMigrationResult:
    source_collection: str
    mode: str
    target_collection: Optional[str] = None
    layer_field_present: bool = False
    scanned: int = 0
    updated: int = 0
    copied: int = 0
    unknown_assigned: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def schema_field_names(schema: dict) -> Set[str]:
    """Return declared field names from a Milvus collection description."""

    return {
        field.get("name") if isinstance(field, dict) else getattr(field, "name", None)
        for field in schema.get("fields", [])
        if (isinstance(field, dict) and field.get("name"))
        or getattr(field, "name", None)
    }


def schema_output_fields(schema: dict, requested: Iterable[str]) -> List[str]:
    """Keep only fields that can be returned by the collection schema."""

    fields = schema_field_names(schema)
    dynamic = bool(schema.get("enable_dynamic_field", schema.get("enable_dynamic_fields", False)))
    return [
        field for field in requested
        if field in fields or (dynamic and field in _DYNAMIC_METADATA_FIELDS)
    ]


def collection_schema(client, collection_name: str) -> dict:
    """Load and validate a collection schema from a Milvus-compatible client."""

    if not client.has_collection(collection_name):
        raise LayerMigrationRequiredError(
            f"Milvus collection '{collection_name}' does not exist"
        )
    schema = client.describe_collection(collection_name)
    if not isinstance(schema, dict):
        raise LayerMigrationRequiredError(
            f"Milvus returned an invalid schema for collection '{collection_name}'"
        )
    return schema


def collection_has_layer_field(client, collection_name: str) -> bool:
    """Return whether a collection declares the layer field explicitly."""

    return "layer" in schema_field_names(collection_schema(client, collection_name))


def require_layer_field(client, collection_name: str) -> None:
    """Fail clearly instead of silently returning incomplete layer results."""

    if not collection_has_layer_field(client, collection_name):
        raise LayerMigrationRequiredError(
            f"Milvus collection '{collection_name}' has no layer field; "
            "run migrate-milvus-layer before using layer filtering"
        )


def _query_batches(client, collection_name: str, output_fields: List[str],
                   batch_size: int) -> Iterable[List[dict]]:
    """Yield query batches using Milvus' iterator API when available."""

    query_filter = 'id != ""'
    if hasattr(client, "query_iterator"):
        iterator = client.query_iterator(
            collection_name=collection_name,
            batch_size=batch_size,
            filter=query_filter,
            output_fields=output_fields,
        )
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                yield batch
        finally:
            close = getattr(iterator, "close", None)
            if close:
                close()
        return

    # This fallback is intended for small compatible clients and test doubles.
    # Production Milvus uses query_iterator to avoid a single unbounded query.
    batch = client.query(
        collection_name=collection_name,
        filter=query_filter,
        output_fields=output_fields,
        limit=batch_size,
    )
    if batch:
        yield batch


def _normalized_entity(entity: dict) -> tuple[dict, bool, bool]:
    """Return a copy with a valid layer and change/unknown flags."""

    result = dict(entity)
    raw_layer = result.get("layer", AgentLayer.UNKNOWN.value)
    try:
        layer = normalize_layer(raw_layer)
        changed = "layer" not in result or result.get("layer") != layer
        assigned_unknown = False
    except ValueError:
        layer = AgentLayer.UNKNOWN.value
        changed = True
        assigned_unknown = True
    if "layer" not in result:
        assigned_unknown = layer == AgentLayer.UNKNOWN.value
    result["layer"] = layer
    return result, changed, assigned_unknown


def _write_batches(client, operation: str, collection_name: str,
                   batches: Iterable[List[dict]]) -> int:
    """Write batches and return the number of entities submitted."""

    written = 0
    for batch in batches:
        if not batch:
            continue
        if operation == "upsert":
            client.upsert(collection_name=collection_name, data=batch)
        else:
            client.insert(collection_name=collection_name, data=batch)
        written += len(batch)
    return written


def migrate_in_place(client, collection_name: str, batch_size: int = 500,
                     dry_run: bool = False) -> LayerMigrationResult:
    """Backfill and normalize layer values in a collection that has the field."""

    require_layer_field(client, collection_name)
    result = LayerMigrationResult(
        source_collection=collection_name,
        mode="in-place",
        layer_field_present=True,
    )
    pending: List[dict] = []
    output_fields = schema_output_fields(
        collection_schema(client, collection_name), MIGRATION_OUTPUT_FIELDS
    )
    for batch in _query_batches(client, collection_name, output_fields, batch_size):
        for entity in batch:
            result.scanned += 1
            normalized, changed, assigned_unknown = _normalized_entity(entity)
            if assigned_unknown:
                result.unknown_assigned += 1
            if changed:
                if "embedding" not in normalized:
                    raise LayerMigrationRequiredError(
                        f"Entity '{normalized.get('id')}' has no embedding; "
                        "migration would not preserve the vector"
                    )
                if not dry_run:
                    pending.append(normalized)
                    if len(pending) >= batch_size:
                        result.updated += _write_batches(
                            client, "upsert", collection_name, [pending]
                        )
                        pending = []
    if pending and not dry_run:
        result.updated += _write_batches(client, "upsert", collection_name, [pending])
    return result


def migrate_by_rebuild(client, source_collection: str, target_collection: str,
                       create_target: Callable[[str], None],
                       batch_size: int = 500,
                       dry_run: bool = False) -> LayerMigrationResult:
    """Copy a collection into a new layer-aware collection.

    ``create_target`` must create the target with the current Registry Center
    schema, including the explicit ``layer`` field. The source collection is
    never deleted or renamed by this helper.
    """

    if not target_collection:
        raise ValueError("target_collection is required for a collection rebuild")
    schema = collection_schema(client, source_collection)
    layer_present = "layer" in schema_field_names(schema)
    result = LayerMigrationResult(
        source_collection=source_collection,
        target_collection=target_collection,
        mode="rebuild",
        layer_field_present=layer_present,
    )
    if client.has_collection(target_collection):
        raise ValueError(f"target collection already exists: {target_collection}")
    if not dry_run:
        create_target(target_collection)
        require_layer_field(client, target_collection)
    source_fields = schema_output_fields(schema, MIGRATION_OUTPUT_FIELDS)
    pending: List[dict] = []
    for batch in _query_batches(client, source_collection, source_fields, batch_size):
        for entity in batch:
            result.scanned += 1
            normalized, _, assigned_unknown = _normalized_entity(entity)
            if assigned_unknown:
                result.unknown_assigned += 1
            if "embedding" not in normalized:
                raise LayerMigrationRequiredError(
                    f"Entity '{normalized.get('id')}' has no embedding; "
                    "migration would not preserve the vector"
                )
            if not dry_run:
                pending.append(normalized)
            if not dry_run and len(pending) >= batch_size:
                result.copied += _write_batches(
                    client, "insert", target_collection, [pending]
                )
                pending = []
    if pending:
        result.copied += _write_batches(client, "insert", target_collection, [pending])
    return result


def migrate_collection(client, collection_name: str,
                        target_collection: Optional[str] = None,
                        mode: str = "auto", batch_size: int = 500,
                        dry_run: bool = False,
                        create_target: Optional[Callable[[str], None]] = None
                        ) -> LayerMigrationResult:
    """Migrate a collection in-place or by creating a replacement collection."""

    if mode not in {"auto", "in-place", "rebuild"}:
        raise ValueError("mode must be one of: auto, in-place, rebuild")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    has_layer = collection_has_layer_field(client, collection_name)
    if mode == "in-place" or (mode == "auto" and has_layer):
        return migrate_in_place(client, collection_name, batch_size, dry_run)
    if create_target is None:
        raise ValueError("create_target is required when rebuilding a collection")
    return migrate_by_rebuild(
        client, collection_name, target_collection, create_target,
        batch_size=batch_size, dry_run=dry_run,
    )
