"""IDEM-007 escrow-first episodic retraction saga contract."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from musubi.api.idempotency_receipts import DurableReceiptStore
from musubi.planes.artifact import ArtifactPlane
from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.store.immutable_vectors import ImmutableVectorPublisher
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory, RetractionEvidence

_NS = "eric/claude-code/episodic"
_ARTIFACT_NS = "eric/claude-code/artifact"
_ORIGINAL = "The bridge is closed tonight. This is false."


def _headers(token: str, *, key: str = "retract-1") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def _mint(
    settings: Settings,
    *,
    subject: str,
    presence: str,
    scopes: list[str],
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": str(settings.oauth_authority).rstrip("/"),
            "sub": subject,
            "aud": "musubi",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "jti": f"idem007-{subject}",
            "scope": " ".join(scopes),
            "presence": presence,
        },
        settings.jwt_signing_key.get_secret_value(),
        algorithm="HS256",
    )


def _body(*, expected_version: int = 1, because: str = "the bridge is open") -> dict[str, Any]:
    return {
        "namespace": _NS,
        "expected_version": expected_version,
        "on": "2026-08-03",
        "because": because,
        "truth": "The bridge remains open.",
        "summary": "Retraction of a false closure claim.",
        "tags": ["retracted", "transport"],
    }


def _seed(plane: EpisodicPlane, *, content: str = _ORIGINAL) -> EpisodicMemory:
    return asyncio.run(
        plane.create(
            EpisodicMemory(
                namespace=_NS,
                object_id=generate_ksuid(),
                content=content,
                summary="false bridge status",
                state="matured",
                importance=8,
            )
        )
    )


def _seed_v2(publisher: ImmutableVectorPublisher, coordinator: Any) -> EpisodicMemory:
    memory = EpisodicMemory(
        namespace=_NS,
        object_id=generate_ksuid(),
        content=_ORIGINAL,
        summary="false bridge status",
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
        [
            {"id": str(row.id), "payload": dict(row.payload or {}), "vector": row.vector}
            for row in rows
        ],
        key=lambda row: row["payload"].get("point_kind", "legacy"),
    )


@pytest.fixture
def receipt_store(app_factory: Any, tmp_path: Path) -> Iterator[DurableReceiptStore]:
    store = DurableReceiptStore(tmp_path / "idem007-receipts.sqlite")
    app_factory.state.idempotency_receipt_store = store
    try:
        yield store
    finally:
        store.close()


def test_legacy_retraction_escrows_exact_bytes_then_commits_bounded_tombstone(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    artifact: ArtifactPlane,
    api_settings: Settings,
    receipt_store: DurableReceiptStore,
) -> None:
    memory = _seed(episodic)
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token),
        json=_body(expected_version=memory.version),
    )
    assert response.status_code == 200, response.text
    wire = response.json()
    assert wire["object_id"] == memory.object_id
    assert wire["version"] == memory.version + 1
    evidence = wire["retraction_evidence"]
    assert evidence["preserved_pointer"] == {"kind": "legacy_self"}
    assert evidence["original_sha256"] == hashlib.sha256(_ORIGINAL.encode()).hexdigest()
    assert evidence["original_utf8_bytes"] == len(_ORIGINAL.encode())
    assert evidence["omitted_bytes"] == 0, "sub-floor originals remain quoted in full by policy"

    stored = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert stored is not None
    assert stored.retraction_evidence == RetractionEvidence.model_validate(evidence)
    assert len(stored.content.encode()) <= 32_768
    assert _ORIGINAL in stored.content

    artifact_id = evidence["artifact_ref"]["artifact_id"]
    head = asyncio.run(artifact.get(namespace=_ARTIFACT_NS, object_id=artifact_id))
    assert head is not None and head.artifact_state == "stored_unindexed"
    blob = api_settings.artifact_blob_path / _ARTIFACT_NS / artifact_id
    assert blob.read_bytes() == _ORIGINAL.encode()


def test_v2_retraction_changes_anchor_only_and_preserves_content_generation_and_vectors(
    client: TestClient,
    valid_token: str,
    qdrant: QdrantClient,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    receipt_store: DurableReceiptStore,
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed_v2(publisher, coordinator)
    before = _layout(qdrant, memory.object_id)
    assert [row["payload"]["point_kind"] for row in before] == ["anchor", "content"]

    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key="v2-retraction"),
        json=_body(expected_version=memory.version),
    )
    assert response.status_code == 200, response.text
    after = _layout(qdrant, memory.object_id)
    assert after[1] == before[1], (
        "write-once content payload and vectors must be whole-row invariant"
    )
    assert after[0]["vector"] == before[0]["vector"]
    assert (
        after[0]["payload"]["committed_operation_id"]
        == before[0]["payload"]["committed_operation_id"]
    )
    assert after[0]["payload"]["live_point"] == before[0]["payload"]["live_point"]
    assert after[0]["payload"]["content"].startswith("RETRACTED")
    assert "update_lease_token" not in after[0]["payload"]


def test_missing_key_refuses_before_any_plane_or_blob_mutation(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    api_settings: Settings,
    receipt_store: DurableReceiptStore,
) -> None:
    memory = _seed(episodic)
    before = _layout(qdrant, memory.object_id)
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers={"Authorization": f"Bearer {valid_token}"},
        json=_body(expected_version=memory.version),
    )
    assert response.status_code == 400
    assert "Idempotency-Key" in response.text
    assert _layout(qdrant, memory.object_id) == before
    assert not api_settings.artifact_blob_path.exists()


def test_same_object_id_in_other_namespace_is_whole_row_invariant(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
) -> None:
    memory = _seed(episodic)
    source = _layout(qdrant, memory.object_id)[0]
    foreign_payload = dict(source["payload"])
    foreign_payload["namespace"] = "other/presence/episodic"
    foreign_point = str(uuid.uuid4())
    qdrant.upsert(
        collection_name="musubi_episodic",
        points=[
            models.PointStruct(
                id=foreign_point,
                payload=foreign_payload,
                vector=source["vector"],
            )
        ],
        wait=True,
    )
    foreign_before = qdrant.retrieve(
        collection_name="musubi_episodic",
        ids=[foreign_point],
        with_payload=True,
        with_vectors=True,
    )[0]
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key="namespace-fence"),
        json=_body(expected_version=memory.version),
    )
    assert response.status_code == 200, response.text
    foreign_after = qdrant.retrieve(
        collection_name="musubi_episodic",
        ids=[foreign_point],
        with_payload=True,
        with_vectors=True,
    )[0]
    assert foreign_after.payload == foreign_before.payload
    assert foreign_after.vector == foreign_before.vector


def test_both_namespace_authorizations_finish_before_first_stored_state_read(
    client: TestClient,
    api_settings: Settings,
    episodic: EpisodicPlane,
    receipt_store: DurableReceiptStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _seed(episodic)
    token = _mint(
        api_settings,
        subject="episodic-only",
        presence="eric/claude-code",
        scopes=[f"{_NS}:rw"],
    )
    events: list[str] = []
    from musubi.api.auth import authorize_namespace as real_authorize

    def observed_authorize(request: Any, namespace: str, **kwargs: Any) -> None:
        events.append(namespace)
        real_authorize(request, namespace, **kwargs)

    async def forbidden_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("episodic storage read before sibling-artifact authorization")

    monkeypatch.setattr("musubi.api.retraction_saga.authorize_namespace", observed_authorize)
    monkeypatch.setattr(episodic, "raw_payload", forbidden_read)
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(token, key="auth-order"),
        json=_body(expected_version=memory.version),
    )
    assert response.status_code == 403
    assert events == [_NS, _ARTIFACT_NS]


def test_stale_version_leaves_safe_verified_escrow_and_preserves_episodic_winner(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    artifact: ArtifactPlane,
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
) -> None:
    memory = _seed(episodic)
    before = _layout(qdrant, memory.object_id)
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token),
        json=_body(expected_version=memory.version - 1),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert _layout(qdrant, memory.object_id) == before
    digest = hashlib.sha256(_ORIGINAL.encode()).hexdigest()
    from musubi.planes.artifact.escrow import derive_escrow_address

    address = derive_escrow_address(
        source_namespace=_NS,
        source_object_id=memory.object_id,
        original_sha256=digest,
    )
    head = asyncio.run(
        artifact.get(namespace=address.artifact_namespace, object_id=address.artifact_id)
    )
    assert head is not None and head.sha256 == digest


def test_crash_after_verified_escrow_reuses_same_blob_and_lands_once_on_retry(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    api_settings: Settings,
    receipt_store: DurableReceiptStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from musubi.planes.artifact.escrow import derive_escrow_address

    saga = importlib.import_module("musubi.api.retraction_saga")
    memory = _seed(episodic)
    before = _layout(qdrant, memory.object_id)
    address = derive_escrow_address(
        source_namespace=_NS,
        source_object_id=memory.object_id,
        original_sha256=hashlib.sha256(_ORIGINAL.encode()).hexdigest(),
    )
    final = api_settings.artifact_blob_path / address.artifact_namespace / address.artifact_id
    real_commit = saga.retract_non_embedding_payload
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash after escrow readback")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(saga, "retract_non_embedding_payload", fail_once)
    first = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key="midpoint-retry"),
        json=_body(expected_version=memory.version),
    )
    assert first.status_code == 503
    assert calls == 1
    assert _layout(qdrant, memory.object_id) == before
    assert final.read_bytes() == _ORIGINAL.encode()
    first_inode = final.stat().st_ino

    retry = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key="midpoint-retry"),
        json=_body(expected_version=memory.version),
    )
    assert retry.status_code == 200, retry.text
    assert calls == 2
    assert final.stat().st_ino == first_inode
    landed = _layout(qdrant, memory.object_id)
    assert len(landed) == len(before)
    assert landed[0]["payload"]["version"] == memory.version + 1


@pytest.mark.parametrize(
    "code",
    [
        "blob_publish_failed",
        "blob_readback_mismatch",
        "blob_mismatch",
        "head_publish_failed",
        "head_readback_mismatch",
        "head_mismatch",
    ],
)
def test_each_escrow_failure_code_means_zero_episodic_mutation(
    code: str,
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from musubi.planes.artifact.escrow import ArtifactEscrowError
    from musubi.types.common import Err

    memory = _seed(episodic)
    before = _layout(qdrant, memory.object_id)

    async def fail_store(*_args: object, **_kwargs: object) -> object:
        return Err(
            error=ArtifactEscrowError(
                code=code,
                detail="injected escrow failure",
                artifact_namespace=_ARTIFACT_NS,
                artifact_id=generate_ksuid(),
            )
        )

    monkeypatch.setattr("musubi.api.retraction_saga.ArtifactEscrowWriter.store", fail_store)
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key=f"failure-{code}"),
        json=_body(expected_version=memory.version),
    )
    assert response.status_code == 503
    assert code in response.json()["error"]["detail"]
    assert _layout(qdrant, memory.object_id) == before


def test_caller_prose_cannot_starve_256_byte_storage_prefix_policy(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
) -> None:
    memory = _seed(episodic, content="false " * 1_000)
    before = _layout(qdrant, memory.object_id)
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key="prose-overflow"),
        json=_body(expected_version=memory.version, because="caller prose " * 4_000),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"
    assert "caller" in response.json()["error"]["detail"].lower()
    assert _layout(qdrant, memory.object_id) == before


def test_exact_private_receipt_replay_is_byte_identical_and_does_not_run_saga_twice(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
) -> None:
    memory = _seed(episodic)
    body = _body(expected_version=memory.version)
    first = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key="exact-replay"),
        json=body,
    )
    assert first.status_code == 200, first.text
    after_first = _layout(qdrant, memory.object_id)
    replay = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key="exact-replay"),
        json=body,
    )
    assert replay.status_code == first.status_code
    assert replay.content == first.content
    assert replay.headers["x-idempotent-replay"] == "true"
    assert _layout(qdrant, memory.object_id) == after_first


def test_same_key_and_digest_under_different_principal_conflicts_never_adopts(
    client: TestClient,
    valid_token: str,
    api_settings: Settings,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
) -> None:
    memory = _seed(episodic)
    body = _body(expected_version=memory.version)
    first = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key="principal-bound"),
        json=body,
    )
    assert first.status_code == 200, first.text
    landed = _layout(qdrant, memory.object_id)
    other = _mint(
        api_settings,
        subject="other-principal",
        presence="other/presence",
        scopes=[f"{_NS}:rw", f"{_ARTIFACT_NS}:rw"],
    )
    second = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(other, key="principal-bound"),
        json=body,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"
    assert "identity" in second.json()["error"]["detail"].lower()
    assert _layout(qdrant, memory.object_id) == landed


def test_existing_unparseable_evidence_fails_typed_without_falling_through_to_version(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    receipt_store: DurableReceiptStore,
) -> None:
    memory = _seed(episodic)
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"retraction_evidence": {"kind": "artifact_escrow_v1", "broken": True}},
        points=models.Filter(
            must=[
                models.FieldCondition(key="namespace", match=models.MatchValue(value=_NS)),
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=memory.object_id)
                ),
            ]
        ),
        wait=True,
    )
    before = _layout(qdrant, memory.object_id)
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key="unparseable-existing"),
        json=_body(expected_version=999),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert "evidence" in response.json()["error"]["detail"].lower()
    assert "version" not in response.json()["error"]["detail"].lower()
    assert _layout(qdrant, memory.object_id) == before
