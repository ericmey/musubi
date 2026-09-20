"""RET-012 terminal lifecycle semantics for escrow-backed retractions."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from musubi.api.idempotency import _GLOBAL_LEASE_CACHE
from musubi.api.idempotency_receipts import DurableReceiptStore
from musubi.lifecycle import LifecycleEventSink, TransitionResult
from musubi.lifecycle.maturation import (
    MaturationConfig,
    MaturationCursor,
    episodic_maturation_sweep,
)
from musubi.planes.episodic import EpisodicPlane
from musubi.store.immutable_vectors import (
    ImmutableVectorPublisher,
    NonEmbeddingPatchConflict,
)
from musubi.types.common import Ok, generate_ksuid
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
    content: str = "The original false claim.",
) -> EpisodicMemory:
    # `content` is a parameter because the plane DEDUPS on it: two seeds with identical
    # content collapse onto one row, and a cell that seeds a "control" and a "subject"
    # then silently operates on the same object. That cost a confusing 409 once.
    memory = EpisodicMemory(
        namespace=_NS,
        object_id=generate_ksuid(),
        content=content,
        state=state,
        importance=8,
    )
    if layout == "legacy":
        saved = asyncio.run(plane.create(memory))
        if state == "provisional":
            return saved
        result = asyncio.run(
            plane.transition(
                namespace=saved.namespace,
                object_id=saved.object_id,
                to_state="matured",
                actor="test-fixture",
                reason="seed requested RET-012 state",
                coordinator=coordinator,
            )
        )
        assert isinstance(result, Ok), result
        assert isinstance(result.value, TransitionResult)
        matured = asyncio.run(plane.get(namespace=saved.namespace, object_id=saved.object_id))
        assert matured is not None
        return matured
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
    authoritative = [
        row["payload"] for row in before if row["payload"].get("point_kind") != "content"
    ]
    assert len(authoritative) == 1
    assert authoritative[0]["state"] == state
    assert authoritative[0]["version"] == memory.version

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
    [
        "done:",
        "done:not-a-timestamp:writer",
        "done:1:",
        "done:+1:writer",
        "active:1:foreign-writer",
    ],
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


@pytest.mark.parametrize(
    ("issued_us", "expected_status"),
    [
        (15_000_000, 409),  # exactly the five-second boundary is still owned
        (19_999_999, 409),  # a fresh committed token belongs to its live writer
        (14_999_999, 200),  # only a strictly expired token is adoptable
    ],
)
def test_evidence_adoption_obeys_shared_done_token_ttl(
    issued_us: int,
    expected_status: int,
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
        layout="legacy",
        state="matured",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    headers = {
        "Authorization": f"Bearer {valid_token}",
        "Idempotency-Key": f"quarantine-done-token-ttl-{issued_us}",
    }
    body = _body(memory.version)
    first = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert first.status_code == 200, first.text
    committed = _layout(qdrant, memory.object_id)
    token = f"done:{issued_us}:writer"
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"update_lease_token": token},
        points=[committed[0]["id"]],
        wait=True,
    )
    with sqlite3.connect(receipt_store.path) as connection:
        connection.execute("DELETE FROM idempotency_receipts")
    _GLOBAL_LEASE_CACHE._entries.clear()
    monkeypatch.setattr(time, "time", lambda: 20.0)

    adopted = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=headers,
        json=body,
    )
    assert adopted.status_code == expected_status, adopted.text
    after = _layout(qdrant, memory.object_id)
    if expected_status == 409:
        assert after[0]["payload"]["update_lease_token"] == token
    else:
        assert "update_lease_token" not in after[0]["payload"]


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


def _raw_authoritative(client: QdrantClient, object_id: str) -> dict[str, Any]:
    """The single non-content payload, read raw. Refuses on anything but exactly one."""
    rows = [r for r in _layout(client, object_id) if r["payload"].get("point_kind") != "content"]
    assert len(rows) == 1, f"expected one authoritative row, got {len(rows)}"
    payload: dict[str, Any] = rows[0]["payload"]
    return payload


def test_completed_retraction_refuses_archived_to_matured_restore(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
) -> None:
    """RET-012's headline: a retracted row cannot later mature.

    `archived -> matured` is a LEGAL episodic edge (operator restore,
    `types/lifecycle_event.py:39`), so nothing in the state machine stops a retracted
    row from taking it. The only refusal is the coordinator's completed-retraction
    guard, and it used to be nested under `if token is not None` -- unreachable once
    the saga released its lease (Copilot round 21 on musubi#732, hole musubi#781).
    """
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
            "Idempotency-Key": "quarantine-restore-refused",
        },
        json=_body(memory.version),
    )
    assert response.status_code == 200, response.text

    # PRECONDITION, asserted rather than assumed -- this cell is worthless if the row
    # still carries a lease, because then the OLD guard would refuse and the cell would
    # pass without exercising the fix at all.
    payload = _raw_authoritative(qdrant, memory.object_id)
    assert payload["state"] == "archived"
    assert payload.get("retraction_evidence") is not None
    assert "update_lease_token" not in payload, (
        "the saga must have released its lease for this cell to mean anything; "
        f"payload still holds {payload.get('update_lease_token')!r}"
    )

    retracted = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert retracted is not None
    result = asyncio.run(
        episodic.transition(
            namespace=_NS,
            object_id=memory.object_id,
            to_state="matured",
            actor="operator-restore",
            reason="attempt to restore a retracted row",
            coordinator=coordinator,
        )
    )
    assert not isinstance(result, Ok), f"retracted row was restored: {result}"
    assert result.error.code == "terminal_apply_failure", result.error

    after = _raw_authoritative(qdrant, memory.object_id)
    assert after["state"] == "archived"
    assert after["importance"] == 1


def test_ordinary_archived_row_without_retraction_evidence_still_restores(
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
) -> None:
    """The other half, and it is not established by reading the guard.

    The terminal check keys on `retraction_evidence`, so an ordinary archived row --
    archived by lifecycle, never retracted -- must still take the operator-restore
    edge. Without this cell the fix above is indistinguishable from freezing every
    archived row permanently.
    """
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout="legacy",
        state="provisional",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    archived = asyncio.run(
        episodic.transition(
            namespace=_NS,
            object_id=memory.object_id,
            to_state="archived",
            actor="lifecycle",
            reason="ordinary archival, no retraction",
            coordinator=coordinator,
        )
    )
    assert isinstance(archived, Ok), archived

    payload = _raw_authoritative(qdrant, memory.object_id)
    assert payload["state"] == "archived"
    assert payload.get("retraction_evidence") is None, (
        "this row must NOT be retracted, or the cell proves nothing about ordinary restore"
    )

    restored = asyncio.run(
        episodic.transition(
            namespace=_NS,
            object_id=memory.object_id,
            to_state="matured",
            actor="operator-restore",
            reason="ordinary archived restore must remain legal",
            coordinator=coordinator,
        )
    )
    assert isinstance(restored, Ok), f"ordinary archived restore was refused: {restored}"
    assert _raw_authoritative(qdrant, memory.object_id)["state"] == "matured"


def _publish_descriptor_json(content: str) -> str:
    """Exactly the framing `ImmutableVectorPublisher._descriptor_json` produces.

    A bare descriptor is ABANDONED by the handler, which would make any cell built on
    it pass whether or not the guard exists. Derived from the production shape, not
    guessed.
    """
    return json.dumps(
        {"descriptor": {"op": "set", "set_fields": {"content": content}, "embed_kind": "episodic"}},
        sort_keys=True,
        separators=(",", ":"),
    )


def _admit_and_drive(coordinator: Any, memory: EpisodicMemory, *, content: str, tag: str) -> Any:
    opk = f"immutable_vector_publish:{memory.object_id}:{tag}"
    status = coordinator.enqueue_custom_intent(
        kind="immutable_vector_publish",
        object_id=memory.object_id,
        namespace=memory.namespace,
        collection="musubi_episodic",
        patch_json=_publish_descriptor_json(content),
        operation_key=opk,
    )
    assert status == "admitted", status
    return coordinator.drive_intent(opk)


def test_custom_publish_intent_admitted_before_retraction_cannot_mutate_after(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
) -> None:
    """The interleaving the coordinator guard alone did not cover.

    A custom immutable-vector publish never reaches `_apply_conditional`, so the
    completed-retraction guard there does not see it. An intent admitted BEFORE the
    retraction is already durable in the outbox; it must not replay afterwards and
    rewrite a quarantined row (Copilot round 22 on musubi#732).

    The CONTROL half is load-bearing: it proves this exact descriptor DOES rewrite an
    un-retracted row. Without it the refusal half passes for any reason the intent
    fails, including a malformed payload, and the cell measures nothing.
    """
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)

    # CONTROL -- same descriptor, no retraction. Must land.
    control = _seed(
        layout="legacy",
        state="provisional",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
        content="A distinct control claim for the custom-intent control arm.",
    )
    control_report = _admit_and_drive(coordinator, control, content="REWRITTEN", tag="control")
    assert control_report.finalized == 1, control_report
    assert _raw_authoritative(qdrant, control.object_id)["content"] == "REWRITTEN", (
        "control did not land; the refusal half below would prove nothing"
    )

    # REFUSAL -- intent admitted first, retraction lands, then the intent drives.
    memory = _seed(
        layout="legacy",
        state="provisional",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    opk = f"immutable_vector_publish:{memory.object_id}:preretraction"
    status = coordinator.enqueue_custom_intent(
        kind="immutable_vector_publish",
        object_id=memory.object_id,
        namespace=_NS,
        collection="musubi_episodic",
        patch_json=_publish_descriptor_json("REWRITTEN AFTER RETRACTION"),
        operation_key=opk,
    )
    assert status == "admitted", status

    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers={
            "Authorization": f"Bearer {valid_token}",
            "Idempotency-Key": "quarantine-custom-intent-interleave",
        },
        json=_body(memory.version),
    )
    assert response.status_code == 200, response.text

    before = _raw_authoritative(qdrant, memory.object_id)
    assert before.get("retraction_evidence") is not None
    content_before = before["content"]

    report = coordinator.drive_intent(opk)
    assert report.abandoned == 1, f"pre-retraction intent was not refused terminally: {report}"
    assert report.finalized == 0, report

    after = _raw_authoritative(qdrant, memory.object_id)
    assert after["content"] == content_before, (
        "a pre-retraction custom intent rewrote a quarantined row"
    )
    assert "REWRITTEN AFTER RETRACTION" not in str(after["content"])
    assert after.get("retraction_evidence") is not None


def test_retracted_row_cannot_regain_importance_through_patch(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
) -> None:
    """The fourth write path, found by enumerating them rather than by a review round.

    `EpisodicPlane.patch` reaches `patch_non_embedding_payload` directly -- no
    coordinator, so neither the `_apply_conditional` guard nor the
    `_drive_custom_intent` guard sees it. It blocks `content`, but `importance` is a
    legal patch field, and "regain importance" is one of the four things RET-012's
    contract says a retracted row cannot do.

    Measured open before the fix: importance 1 -> 9 on a row with evidence intact.
    """
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout="legacy",
        state="provisional",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )

    # CONTROL -- the same patch on a NON-retracted row must still work, or this cell
    # would pass against a plane that simply refuses every patch.
    control = _seed(
        layout="legacy",
        state="provisional",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
        content="A distinct control claim for the importance-patch control arm.",
    )
    asyncio.run(
        episodic.patch(
            namespace=_NS,
            object_id=control.object_id,
            importance=9,
            actor="operator",
            reason="ordinary importance patch",
        )
    )
    assert _raw_authoritative(qdrant, control.object_id)["importance"] == 9, (
        "ordinary patch did not land; the refusal below would prove nothing"
    )

    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers={
            "Authorization": f"Bearer {valid_token}",
            "Idempotency-Key": "quarantine-no-importance-regain",
        },
        json=_body(memory.version),
    )
    assert response.status_code == 200, response.text
    before = _raw_authoritative(qdrant, memory.object_id)
    assert before.get("retraction_evidence") is not None
    assert before["importance"] == 1

    with pytest.raises(NonEmbeddingPatchConflict):
        asyncio.run(
            episodic.patch(
                namespace=_NS,
                object_id=memory.object_id,
                importance=9,
                actor="operator",
                reason="attempt to restore importance on a retracted row",
            )
        )

    after = _raw_authoritative(qdrant, memory.object_id)
    assert after["importance"] == 1, "a retracted row regained importance"
    assert after["state"] == "archived"


def test_retraction_committing_after_the_preflight_still_cannot_be_overwritten(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
) -> None:
    """The one window a read-only preflight can never close.

    The custom-intent guard reads the row, sees no evidence, and hands off to a handler
    that performs its OWN read and its OWN fenced write. Two orderings follow, and only
    one of them is the defect::

        preflight ok -> handler reads (N) -> retraction commits (N+1) -> write fenced on N
            already blocked, by the VERSION fence

        preflight ok -> retraction commits (N+1) -> handler reads (N+1) -> write fenced on N+1
            LANDS on a quarantined row, and only an evidence predicate on the write stops it

    `inject_pre_publish_once` fires after the handler has read, so it can only produce the
    first ordering -- a cell built on it passes with the evidence predicate removed and
    proves the version fence instead. This wraps the registered handler so the retraction
    commits BEFORE the handler reads, which is the second ordering.

    A pre-read is not a fence. The server-side condition on the write is what gates
    (Copilot round 25 on musubi#732).
    """
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout="legacy",
        state="provisional",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
        content="A claim retracted between preflight and handler read.",
    )

    inner = coordinator._intent_handlers["immutable_vector_publish"]
    fired: list[bool] = []

    def retract_then_handle(ctx: Any) -> str:
        # The coordinator preflight has ALREADY passed at this point (no evidence yet).
        if not fired:
            fired.append(True)
            resp = client.post(
                f"/v1/episodic/{memory.object_id}/retract",
                headers={
                    "Authorization": f"Bearer {valid_token}",
                    "Idempotency-Key": "quarantine-after-preflight",
                },
                json=_body(memory.version),
            )
            assert resp.status_code == 200, resp.text
        # The real handler now reads FRESH -- already retracted, version bumped -- and
        # rebases onto it, so its version fence will pass.
        return str(inner(ctx))

    coordinator.register_intent_handler("immutable_vector_publish", retract_then_handle)
    try:
        try:
            publisher.publish(
                coordinator,
                object_id=memory.object_id,
                namespace=memory.namespace,
                content_payload={"content": "OVERWRITTEN AFTER PREFLIGHT"},
            )
        except Exception:
            pass  # losing the fence is the CORRECT outcome; the assertions are the proof
    finally:
        coordinator.register_intent_handler("immutable_vector_publish", inner)

    assert fired, "the handler wrapper never ran; this cell proves nothing"

    after = _raw_authoritative(qdrant, memory.object_id)
    assert after.get("retraction_evidence") is not None, "the retraction did not commit"
    assert after["state"] == "archived"
    assert "OVERWRITTEN AFTER PREFLIGHT" not in str(after["content"]), (
        "a publish that rebased past the preflight overwrote a quarantined row"
    )
