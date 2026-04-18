"""Payload-index bootstrap — idempotent ``ensure_indexes``.

Adds every index declared in :data:`musubi.store.specs.INDEXES_BY_COLLECTION`
plus every universal index in :data:`UNIVERSAL_INDEXES` to the target
collection. Qdrant's ``create_payload_index`` raises when an index already
exists; we catch that and continue — which is what makes this idempotent.
"""

from __future__ import annotations

from qdrant_client import QdrantClient, models

from musubi.store.names import CollectionName
from musubi.store.specs import IndexSpec, PayloadSchema, all_indexes_for


def _payload_schema_type(schema: PayloadSchema) -> models.PayloadSchemaType:
    """Translate the string-form schema to qdrant-client's enum."""
    match schema:
        case "keyword":
            return models.PayloadSchemaType.KEYWORD
        case "integer":
            return models.PayloadSchemaType.INTEGER
        case "float":
            return models.PayloadSchemaType.FLOAT
        case "bool":
            return models.PayloadSchemaType.BOOL
        case "text":
            return models.PayloadSchemaType.TEXT
        case "datetime":
            return models.PayloadSchemaType.DATETIME


def _already_indexed_fields(client: QdrantClient, collection_name: str) -> set[str]:
    """Return the set of field names already indexed on ``collection_name``.

    Uses ``get_collection().payload_schema`` — present on real Qdrant servers,
    returns ``{}`` on qdrant-client's local in-memory mode. Combined with the
    try/except fallback in :func:`_create_index_tolerating_exists`, both
    deployment modes behave correctly.
    """
    try:
        info = client.get_collection(collection_name=collection_name)
    except Exception:
        return set()
    schema = getattr(info, "payload_schema", None) or {}
    return set(schema.keys())


def _create_index_tolerating_exists(
    client: QdrantClient,
    collection_name: str,
    spec: IndexSpec,
) -> bool:
    """Create one index, swallow "already exists" errors. Returns True if created new."""
    try:
        client.create_payload_index(
            collection_name=collection_name,
            field_name=spec.field_name,
            field_schema=_payload_schema_type(spec.schema),
        )
    except Exception as exc:  # qdrant uses ResponseHandlingException / UnexpectedResponse
        message = str(exc).lower()
        if "already" in message or "exists" in message:
            return False
        raise
    return True


def ensure_indexes(
    client: QdrantClient,
    *,
    only: CollectionName | None = None,
) -> dict[CollectionName, list[str]]:
    """Apply every declared payload index to each collection.

    Args:
        client: qdrant-client instance (local or server).
        only: if set, ensure indexes only for that one collection.

    Returns:
        ``{collection_name: [field_name, ...]}`` of indexes newly created on
        this call. Fields that already had an index do not appear.
    """
    from musubi.store.specs import REGISTRY

    targets: list[CollectionName] = [only] if only is not None else [spec.name for spec in REGISTRY]
    created: dict[CollectionName, list[str]] = {}
    for name in targets:
        existing = _already_indexed_fields(client, name)
        new_fields: list[str] = []
        for spec in all_indexes_for(name):
            if spec.field_name in existing:
                continue
            if _create_index_tolerating_exists(client, name, spec):
                new_fields.append(spec.field_name)
        created[name] = new_fields
    return created


__all__ = ["ensure_indexes"]
