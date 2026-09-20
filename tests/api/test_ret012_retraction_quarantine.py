"""RET-012 terminal lifecycle semantics for escrow-backed retractions."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from musubi.api.idempotency import _GLOBAL_LEASE_CACHE
from musubi.api.idempotency_receipts import DurableReceiptStore
from musubi.lifecycle import LifecycleEventSink
from musubi.lifecycle.maturation import (
    MaturationConfig,
    MaturationCursor,
    episodic_maturation_sweep,
)
from musubi.planes.episodic import EpisodicPlane
from musubi.store.immutable_vectors import ImmutableVectorPublisher
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory

_NS = "eric/claude-code/episodic"


@pytest.fixture
def receipt_store(app_factory: Any, tmp_path: Path) -> Iterator[DurableReceiptStore]:
    store = DurableReceiptStore(tmp_path / "ret012-receipts.sqlite")
    app_factory.state.idempotency_receipt_store = store
    try:
        yield store
    finally:
        store.close()


def _body(version: int) -> dict[str, Any]:
    return {
        "namespace": _NS,
        "expected_version": version,
        "on": "2026-08-17",
        "because": "the original claim is false",
        "truth": "The corrected claim is true.",
        "tags": ["retracted", "do-not-act-on"],
    }


def _seed(
    *,
    layout: str,
    state: Literal["provisional", "matured"],
    plane: EpisodicPlane,
    publisher: ImmutableVectorPublisher,
    coordinator: Any,
) -> EpisodicMemory:
    memory = EpisodicMemory(
        namespace=_NS,
        object_id=generate_ksuid(),
        content="The original false claim.",
        state=state,
        importance=8,
    )
    if layout == "legacy":
        return asyncio.run(plane.create(memory))
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
        [
            {"id": str(row.id), "payload": dict(row.payload or {}), "vector": row.vector}
            for row in rows
        ],
        key=lambda row: row["payload"].get("point_kind", "legacy"),
    )


@pytest.mark.parametrize("layout", ["legacy", "v2"])
@pytest.mark.parametrize("state", ["provisional", "matured"])
def test_retraction_archives_legacy_and_v2_rows_from_any_active_state(
    layout: str,
    state: Literal["provisional", "matured"],
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout=layout,
        state=state,
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    before = _layout(qdrant, memory.object_id)

    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers={
            "Authorization": f"Bearer {valid_token}",
            "Idempotency-Key": f"quarantine-{layout}-{state}",
        },
        json=_body(memory.version),
    )
    assert response.status_code == 200, response.text
    stored = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert stored is not None
    assert stored.state == "archived"
    assert stored.importance == 1
    assert stored.retraction_evidence is not None
    after = _layout(qdrant, memory.object_id)
    if layout == "v2":
        assert after[1] == before[1], (
            "write-once content generation and vectors must remain whole-row invariant"
        )
        assert after[0]["vector"] == before[0]["vector"]
    else:
        assert after[0]["vector"] == before[0]["vector"]


@pytest.mark.parametrize("layout", ["legacy", "v2"])
def test_evidence_adoption_repairs_pre_quarantine_state_without_rewriting_content(
    layout: str,
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout=layout,
        state="matured",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    headers = {
        "Authorization": f"Bearer {valid_token}",
        "Idempotency-Key": f"quarantine-adopt-{layout}",
    }
    body = _body(memory.version)
    first = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert first.status_code == 200, first.text
    committed = _layout(qdrant, memory.object_id)
    committed_logical = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert committed_logical is not None

    # Model a retraction committed before RET-012: evidence and escrow are valid,
    # but the identity row still carries lifecycle-visible state and importance.
    identity = committed[0]
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"state": "matured", "importance": 6},
        points=[identity["id"]],
        wait=True,
    )
    with sqlite3.connect(receipt_store.path) as connection:
        connection.execute("DELETE FROM idempotency_receipts")
    _GLOBAL_LEASE_CACHE._entries.clear()

    adopted = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert adopted.status_code == 200, adopted.text
    repaired = _layout(qdrant, memory.object_id)
    repaired_logical = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert repaired_logical is not None
    assert adopted.json()["version"] == committed_logical.version + 1
    assert repaired_logical.state == "archived"
    assert repaired_logical.importance == 1
    assert repaired_logical.updated_at == committed_logical.updated_at
    assert repaired_logical.updated_epoch == committed_logical.updated_epoch
    if layout == "v2":
        assert repaired[1] == committed[1], (
            "adoption repair must not rewrite the immutable content generation or vector"
        )
        assert repaired[0]["vector"] == committed[0]["vector"]
    else:
        assert repaired[0]["vector"] == committed[0]["vector"]


@pytest.mark.parametrize("layout", ["legacy", "v2"])
def test_evidence_adoption_releases_committed_done_token_without_reapplying_retraction(
    layout: str,
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout=layout,
        state="matured",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    headers = {
        "Authorization": f"Bearer {valid_token}",
        "Idempotency-Key": f"quarantine-done-token-{layout}",
    }
    body = _body(memory.version)
    first = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert first.status_code == 200, first.text
    committed_logical = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert committed_logical is not None
    committed = _layout(qdrant, memory.object_id)

    # Model a crash after the attributable commit but before exact-token release.
    done = "done:1:crashed-committer"
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"update_lease_token": done},
        points=[committed[0]["id"]],
        wait=True,
    )
    with sqlite3.connect(receipt_store.path) as connection:
        connection.execute("DELETE FROM idempotency_receipts")
    _GLOBAL_LEASE_CACHE._entries.clear()

    adopted = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["version"] == committed_logical.version
    after = _layout(qdrant, memory.object_id)
    changed = [
        sorted(
            key
            for key in set(before["payload"]) | set(current["payload"])
            if before["payload"].get(key) != current["payload"].get(key)
        )
        for before, current in zip(committed, after, strict=True)
    ]
    assert after == committed, (
        "adoption must release only the committed done token; it must not reapply "
        "the retraction or rewrite payload, vectors, content, timestamps, or version; "
        f"changed top-level fields: {changed}"
    )


@pytest.mark.parametrize(
    "malformed",
    ["done:", "done:not-a-timestamp", "done:1:", "active:1:foreign-writer"],
)
def test_evidence_adoption_refuses_malformed_or_active_committed_tokens(
    malformed: str,
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout="legacy",
        state="matured",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    headers = {
        "Authorization": f"Bearer {valid_token}",
        "Idempotency-Key": f"quarantine-malformed-token-{malformed}",
    }
    body = _body(memory.version)
    first = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert first.status_code == 200, first.text
    committed = _layout(qdrant, memory.object_id)
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"update_lease_token": malformed},
        points=[committed[0]["id"]],
        wait=True,
    )
    with sqlite3.connect(receipt_store.path) as connection:
        connection.execute("DELETE FROM idempotency_receipts")
    _GLOBAL_LEASE_CACHE._entries.clear()

    refused = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert refused.status_code == 409, refused.text
    assert "active or malformed mutation lease" in refused.text
    after = _layout(qdrant, memory.object_id)
    assert after[0]["payload"]["update_lease_token"] == malformed


@pytest.mark.parametrize("layout", ["legacy", "v2"])
def test_evidence_adoption_repairs_quarantine_before_releasing_committed_token(
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
    memory = _seed(
        layout=layout,
        state="matured",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    headers = {
        "Authorization": f"Bearer {valid_token}",
        "Idempotency-Key": f"quarantine-atomic-repair-{layout}",
    }
    body = _body(memory.version)
    first = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert first.status_code == 200, first.text
    committed_logical = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert committed_logical is not None
    committed = _layout(qdrant, memory.object_id)

    # Model the combined recovery case: the old commit retained active lifecycle
    # fields and crashed after stamping its attributable done token.
    done = "done:1:crashed-pre-quarantine-committer"
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"state": "matured", "importance": 6, "update_lease_token": done},
        points=[committed[0]["id"]],
        wait=True,
    )
    with sqlite3.connect(receipt_store.path) as connection:
        connection.execute("DELETE FROM idempotency_receipts")
    _GLOBAL_LEASE_CACHE._entries.clear()

    releases: list[tuple[str | None, int | None]] = []
    real_delete_payload = qdrant.delete_payload

    def assert_quarantined_before_release(*args: Any, **kwargs: Any) -> Any:
        if "update_lease_token" in kwargs.get("keys", []):
            identity = _layout(qdrant, memory.object_id)[0]["payload"]
            releases.append((identity.get("state"), identity.get("importance")))
            assert releases[-1] == ("archived", 1), (
                "the exact token must continue fencing lifecycle writers until "
                "the quarantine repair is committed"
            )
        return real_delete_payload(*args, **kwargs)

    monkeypatch.setattr(qdrant, "delete_payload", assert_quarantined_before_release)
    adopted = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert adopted.status_code == 200, adopted.text
    assert releases == [("archived", 1)]
    assert adopted.json()["version"] == committed_logical.version + 1
    repaired = _layout(qdrant, memory.object_id)
    assert "update_lease_token" not in repaired[0]["payload"]
    assert repaired[0]["payload"]["updated_at"] == committed[0]["payload"]["updated_at"]
    assert repaired[0]["payload"]["updated_epoch"] == committed[0]["payload"]["updated_epoch"]
    if layout == "v2":
        assert repaired[1] == committed[1]
        assert repaired[0]["vector"] == committed[0]["vector"]
    else:
        assert repaired[0]["vector"] == committed[0]["vector"]


class _NoEnrichment:
    async def score_importance(self, _items: list[Any]) -> None:
        raise AssertionError("an archived retraction must never reach importance enrichment")

    async def infer_topics(self, _items: list[Any]) -> None:
        raise AssertionError("an archived retraction must never reach topic enrichment")


def test_retracted_provisional_row_cannot_reenter_maturation_after_one_hour(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
    tmp_path: Path,
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout="legacy",
        state="provisional",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers={
            "Authorization": f"Bearer {valid_token}",
            "Idempotency-Key": "quarantine-time-advanced",
        },
        json=_body(memory.version),
    )
    assert response.status_code == 200, response.text
    retracted = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert retracted is not None

    sink = LifecycleEventSink(db_path=tmp_path / "events.sqlite")
    cursor = MaturationCursor(db_path=tmp_path / "cursor.sqlite")
    try:
        report = asyncio.run(
            episodic_maturation_sweep(
                client=qdrant,
                sink=sink,
                coordinator=coordinator,
                ollama=_NoEnrichment(),
                cursor=cursor,
                config=MaturationConfig(min_age_sec=3600),
                now=retracted.updated_at + timedelta(hours=2),
            )
        )
    finally:
        sink.close()

    assert report.selected == 0
    assert report.transitioned == 0
    stored = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert stored is not None
    assert stored.state == "archived"
    assert stored.importance == 1
