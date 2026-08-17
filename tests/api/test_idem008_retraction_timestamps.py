"""IDEM-008 retraction timestamp mutation contract."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from musubi.api.idempotency import _GLOBAL_LEASE_CACHE
from musubi.api.idempotency_receipts import DurableReceiptStore
from musubi.planes.episodic import EpisodicPlane
from musubi.store.immutable_vectors import ImmutableVectorPublisher
from musubi.types.common import epoch_of, generate_ksuid
from musubi.types.episodic import EpisodicMemory

_NS = "eric/claude-code/episodic"
_RETRACTED_AT = datetime(2026, 8, 17, 21, 30, tzinfo=UTC)
_LATER = _RETRACTED_AT + timedelta(hours=1)


@pytest.fixture
def receipt_store(app_factory: Any, tmp_path: Path) -> Iterator[DurableReceiptStore]:
    store = DurableReceiptStore(tmp_path / "idem008-receipts.sqlite")
    app_factory.state.idempotency_receipt_store = store
    try:
        yield store
    finally:
        store.close()


def _headers(*, key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


def _body(*, expected_version: int) -> dict[str, Any]:
    return {
        "namespace": _NS,
        "expected_version": expected_version,
        "on": "2026-08-17",
        "because": "the original claim is false",
        "truth": "The corrected claim is true.",
        "summary": "Retraction of a false claim.",
        "tags": ["retracted", "timestamp-contract"],
    }


def _seed_legacy(plane: EpisodicPlane) -> EpisodicMemory:
    return asyncio.run(
        plane.create(
            EpisodicMemory(
                namespace=_NS,
                object_id=generate_ksuid(),
                content="The original false claim.",
                summary="false claim",
                state="matured",
                importance=8,
            )
        )
    )


def _seed_v2(publisher: ImmutableVectorPublisher, coordinator: Any) -> EpisodicMemory:
    memory = EpisodicMemory(
        namespace=_NS,
        object_id=generate_ksuid(),
        content="The original false claim.",
        summary="false claim",
        state="matured",
        importance=8,
    )
    publisher.publish(
        coordinator,
        object_id=memory.object_id,
        namespace=memory.namespace,
        content_payload=memory.model_dump(mode="json"),
    )
    return memory


def _layout(client: QdrantClient, object_id: str) -> list[dict[str, Any]]:
    rows, _ = client.scroll(
        collection_name="musubi_episodic",
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=8,
        with_payload=True,
        with_vectors=True,
    )
    return sorted(
        [{"payload": dict(row.payload or {}), "vector": row.vector} for row in rows],
        key=lambda row: row["payload"].get("point_kind", "legacy"),
    )


@pytest.mark.parametrize("layout", ["legacy", "v2"])
def test_retraction_advances_updated_at_and_matching_updated_epoch_in_the_same_commit(
    layout: str,
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed_legacy(episodic) if layout == "legacy" else _seed_v2(publisher, coordinator)
    before = _layout(qdrant, memory.object_id)
    before_logical = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert before_logical is not None
    monkeypatch.setattr("musubi.api.retraction_saga.utc_now", lambda: _RETRACTED_AT, raising=False)

    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers={"Authorization": f"Bearer {valid_token}", **_headers(key=f"timestamp-{layout}")},
        json=_body(expected_version=before_logical.version),
    )
    assert response.status_code == 200, response.text
    after = _layout(qdrant, memory.object_id)
    stored = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert stored is not None
    assert stored.version == before_logical.version + 1
    assert stored.updated_at == _RETRACTED_AT
    assert stored.updated_epoch == epoch_of(_RETRACTED_AT)
    assert stored.updated_at > before_logical.updated_at
    if layout == "v2":
        assert after[1] == before[1], "immutable content generation and vectors must not change"
        assert after[0]["vector"] == before[0]["vector"]
    else:
        assert after[0]["vector"] == before[0]["vector"]


def test_exact_replay_returns_the_committed_timestamp_without_restamping(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    receipt_store: DurableReceiptStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _seed_legacy(episodic)
    body = _body(expected_version=memory.version)
    headers = {"Authorization": f"Bearer {valid_token}", **_headers(key="timestamp-replay")}
    monkeypatch.setattr("musubi.api.retraction_saga.utc_now", lambda: _RETRACTED_AT, raising=False)
    first = client.post(f"/v1/episodic/{memory.object_id}/retract", headers=headers, json=body)
    assert first.status_code == 200, first.text

    monkeypatch.setattr("musubi.api.retraction_saga.utc_now", lambda: _LATER, raising=False)
    replay = client.post(f"/v1/episodic/{memory.object_id}/retract", headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.content == first.content
    assert replay.headers["x-idempotent-replay"] == "true"
    stored = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert stored is not None and stored.updated_at == _RETRACTED_AT


def test_committed_evidence_adoption_returns_the_committed_timestamp_without_restamping(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    receipt_store: DurableReceiptStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _seed_legacy(episodic)
    body = _body(expected_version=memory.version)
    headers = {"Authorization": f"Bearer {valid_token}", **_headers(key="timestamp-adopt")}
    monkeypatch.setattr("musubi.api.retraction_saga.utc_now", lambda: _RETRACTED_AT, raising=False)
    first = client.post(f"/v1/episodic/{memory.object_id}/retract", headers=headers, json=body)
    assert first.status_code == 200, first.text

    with sqlite3.connect(receipt_store.path) as connection:
        connection.execute("DELETE FROM idempotency_receipts")
    _GLOBAL_LEASE_CACHE._entries.clear()
    monkeypatch.setattr("musubi.api.retraction_saga.utc_now", lambda: _LATER, raising=False)
    adopted = client.post(f"/v1/episodic/{memory.object_id}/retract", headers=headers, json=body)
    assert adopted.status_code == 200, adopted.text
    stored = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert stored is not None
    assert stored.version == memory.version + 1
    assert stored.updated_at == _RETRACTED_AT
    assert stored.updated_epoch == epoch_of(_RETRACTED_AT)


def test_stale_version_preserves_original_timestamps_after_verified_escrow(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _seed_legacy(episodic)
    before = _layout(qdrant, memory.object_id)
    monkeypatch.setattr("musubi.api.retraction_saga.utc_now", lambda: _RETRACTED_AT, raising=False)
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers={"Authorization": f"Bearer {valid_token}", **_headers(key="timestamp-stale")},
        json=_body(expected_version=memory.version - 1),
    )
    assert response.status_code == 409
    assert _layout(qdrant, memory.object_id) == before
