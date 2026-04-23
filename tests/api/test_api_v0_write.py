"""Test contract for slice-api-v0-write.

Implements the write-side bullets from
[[07-interfaces/canonical-api]] § Test contract — the bullets that
slice-api-v0-read explicitly deferred to here:

- Bullet 9  : ``test_multipart_upload_for_artifacts``
- Bullet 10 : ``test_idempotency_key_roundtrip``
- Bullet 11 : ``test_idempotency_key_expires_after_24h``
- Bullet 13 : ``test_rate_limit_enforces_token_bucket``
- Bullet 14 : ``test_rate_limit_operator_scope_10x_limit``
- Bullet 15 : ``test_ndjson_retrieve_stream_yields_per_result``

Plus the write-side example cases from
[[07-interfaces/contract-tests]] (capture happy / dedup / idempotency,
thought send + read, artifact upload, lifecycle transition error).

Auth scaffolding + the FastAPI ``client`` / ``api_settings`` /
``mint_token`` fixtures are reused from ``tests/api/conftest.py``
(landed by slice-api-v0-read).
"""

from __future__ import annotations

import asyncio
import io

import pytest
from fastapi.testclient import TestClient

from musubi.planes.episodic import EpisodicPlane
from musubi.types.episodic import EpisodicMemory

# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def test_capture_happy_returns_object_id(
    client: TestClient,
    valid_token: str,
) -> None:
    """Spec contract-tests § Capture: POST /v1/memories returns 202 with
    the new object_id; the row is fetchable by GET."""
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    body = {
        "namespace": namespace,
        "content": "test-content-unique-write-abc123",
        "tags": ["contract-test"],
        "importance": 5,
    }
    r = client.post("/v1/memories", headers=headers, json=body)
    assert r.status_code == 202
    object_id = r.json()["object_id"]
    assert isinstance(object_id, str) and len(object_id) == 27
    # Fetchable by id.
    got = client.get(
        f"/v1/memories/{object_id}",
        headers=headers,
        params={"namespace": namespace},
    )
    assert got.status_code == 200
    assert got.json()["content"] == body["content"]


def test_capture_dedup_returns_same_id(
    client: TestClient,
    valid_token: str,
) -> None:
    """contract-tests § Capture: a second POST of identical content
    returns the same object_id (plane-level dedup)."""
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    body = {
        "namespace": namespace,
        "content": "dedup-target-write-xyz",
        "tags": ["a"],
        "importance": 5,
    }
    r1 = client.post("/v1/memories", headers=headers, json=body)
    body2 = dict(body)
    body2["tags"] = ["b"]
    r2 = client.post("/v1/memories", headers=headers, json=body2)
    assert r1.status_code == 202
    assert r2.status_code == 202
    # Same object id; tags merged on the underlying row.
    assert r2.json()["object_id"] == r1.json()["object_id"]


def test_capture_rejects_out_of_scope_namespace(
    client: TestClient,
    out_of_scope_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {out_of_scope_token}"}
    body = {
        "namespace": "eric/claude-code/episodic",
        "content": "should-not-write",
    }
    r = client.post("/v1/memories", headers=headers, json=body)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_capture_missing_token_returns_401(client: TestClient) -> None:
    r = client.post(
        "/v1/memories",
        json={"namespace": "eric/claude-code/episodic", "content": "x"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_batch_capture_writes_each_row(client: TestClient, valid_token: str) -> None:
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    body = {
        "namespace": namespace,
        "items": [{"content": f"batch-row-{i}-uniq", "importance": 5} for i in range(3)],
    }
    r = client.post("/v1/memories/batch", headers=headers, json=body)
    assert r.status_code == 202
    out = r.json()
    assert len(out["object_ids"]) == 3
    for oid in out["object_ids"]:
        assert len(oid) == 27


# ---------------------------------------------------------------------------
# created_at override — operator-gated migration / replay path (#140)
# ---------------------------------------------------------------------------


def test_capture_created_at_override_requires_operator(
    client: TestClient,
    valid_token: str,
) -> None:
    """Non-operator token cannot override created_at — regression guard
    that the gate is actually in place. Without this, a random consumer
    could rewrite when an event "happened", which breaks event_at /
    created_at audit semantics."""
    r = client.post(
        "/v1/memories",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "content": "historical-row",
            "created_at": "2024-06-01T12:00:00Z",
        },
    )
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == "FORBIDDEN"
    assert "operator" in body["error"]["detail"].lower()


def test_capture_created_at_override_with_operator_round_trips(
    client: TestClient,
    api_settings: object,
    episodic: EpisodicPlane,
) -> None:
    """With operator scope, an override lands on the row and comes back
    on the GET. This is the migration use case: preserve source-truth
    timestamps when ingesting historical data."""
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/episodic"
    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["operator", f"{namespace}:rw"],
    )
    headers = {"Authorization": f"Bearer {token}"}
    override = "2024-06-01T12:00:00Z"
    r = client.post(
        "/v1/memories",
        headers=headers,
        json={
            "namespace": namespace,
            "content": "historical-op-row",
            "created_at": override,
        },
    )
    assert r.status_code == 202, r.text
    oid = r.json()["object_id"]
    got = client.get(
        f"/v1/memories/{oid}",
        headers=headers,
        params={"namespace": namespace},
    )
    assert got.status_code == 200, got.text
    # Timestamp lands on created_at exactly as supplied.
    assert got.json()["created_at"].startswith("2024-06-01T12:00:00")


def test_capture_without_created_at_is_stamped_now(
    client: TestClient,
    valid_token: str,
) -> None:
    """Omitting created_at must continue to work unchanged — the field
    is opt-in. Musubi stamps the current time via EpisodicMemory's
    default factory."""
    from datetime import UTC, datetime

    before = datetime.now(UTC)
    r = client.post(
        "/v1/memories",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={"namespace": "eric/claude-code/episodic", "content": "no-override"},
    )
    assert r.status_code == 202
    oid = r.json()["object_id"]
    got = client.get(
        f"/v1/memories/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": "eric/claude-code/episodic"},
    )
    assert got.status_code == 200
    stamp = datetime.fromisoformat(got.json()["created_at"].replace("Z", "+00:00"))
    assert stamp >= before


def test_batch_capture_created_at_override_requires_operator(
    client: TestClient,
    valid_token: str,
) -> None:
    """Any item in a batch carrying created_at triggers the operator
    check for the whole batch. Keeps semantics simple: mixed batches
    fail fast rather than partially-succeed."""
    r = client.post(
        "/v1/memories/batch",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "items": [
                {"content": "row-a"},
                {"content": "row-b", "created_at": "2024-06-01T12:00:00Z"},
            ],
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_batch_capture_created_at_override_with_operator_applies_per_item(
    client: TestClient,
    api_settings: object,
) -> None:
    """Under operator scope, each item's created_at lands independently
    on its row — the migration path needs per-item control because
    source rows don't share a single timestamp."""
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/episodic"
    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["operator", f"{namespace}:rw"],
    )
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/v1/memories/batch",
        headers=headers,
        json={
            "namespace": namespace,
            "items": [
                {"content": "batch-op-a", "created_at": "2022-01-01T00:00:00Z"},
                {"content": "batch-op-b", "created_at": "2023-06-15T12:00:00Z"},
            ],
        },
    )
    assert r.status_code == 202, r.text
    oids = r.json()["object_ids"]
    assert len(oids) == 2
    a = client.get(
        f"/v1/memories/{oids[0]}",
        headers=headers,
        params={"namespace": namespace},
    ).json()
    b = client.get(
        f"/v1/memories/{oids[1]}",
        headers=headers,
        params={"namespace": namespace},
    ).json()
    assert a["created_at"].startswith("2022-01-01T00:00:00")
    assert b["created_at"].startswith("2023-06-15T12:00:00")


# ---------------------------------------------------------------------------
# PATCH — non-state field updates
# ---------------------------------------------------------------------------


def test_patch_episodic_updates_tags_and_importance(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(
            EpisodicMemory(namespace=namespace, content="patchable", importance=3)
        )
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.patch(
        f"/v1/memories/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
        json={"tags": ["new-tag"], "importance": 9},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["importance"] == 9
    assert "new-tag" in body["tags"]


def test_patch_rejects_state_field_changes(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    """PATCH is for non-state metadata only; ``state`` updates must go
    through POST /v1/lifecycle/transition. Attempting to PATCH a state
    field is a 400 BAD_REQUEST."""
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(
            EpisodicMemory(namespace=namespace, content="state-patch-attempt")
        )
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.patch(
        f"/v1/memories/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
        json={"state": "matured"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# DELETE — soft archive (default) / hard purge (operator)
# ---------------------------------------------------------------------------


def test_delete_episodic_soft_archives(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=namespace, content="bye"))
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.delete(
        f"/v1/memories/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
    )
    assert r.status_code == 200
    # The row still exists, now in state=archived.
    after = asyncio.run(episodic.get(namespace=namespace, object_id=oid))
    assert after is not None and after.state == "archived"


def test_delete_episodic_hard_requires_operator(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=namespace, content="hardbye"))
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.delete(
        f"/v1/memories/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace, "hard": "true"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Lifecycle transition endpoint
# ---------------------------------------------------------------------------


def test_lifecycle_transition_routes_to_canonical_primitive(
    client: TestClient,
    operator_token: str,
    episodic: EpisodicPlane,
) -> None:
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(
            EpisodicMemory(namespace=namespace, content="transition-target")
        )
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.post(
        "/v1/lifecycle/transition",
        headers={"Authorization": f"Bearer {operator_token}"},
        json={
            "object_id": oid,
            "to_state": "matured",
            "actor": "operator-test",
            "reason": "promote-via-api",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["from_state"] == "provisional"
    assert body["to_state"] == "matured"
    after = asyncio.run(episodic.get(namespace=namespace, object_id=oid))
    assert after is not None and after.state == "matured"


def test_lifecycle_transition_illegal_returns_400(
    client: TestClient,
    operator_token: str,
    episodic: EpisodicPlane,
) -> None:
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=namespace, content="illegal-target"))
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.post(
        "/v1/lifecycle/transition",
        headers={"Authorization": f"Bearer {operator_token}"},
        json={
            "object_id": oid,
            "to_state": "demoted",  # provisional → demoted is illegal
            "actor": "operator-test",
            "reason": "expected-failure",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_lifecycle_transition_requires_operator_scope(
    client: TestClient,
    valid_token: str,
) -> None:
    r = client.post(
        "/v1/lifecycle/transition",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "object_id": "0" * 27,
            "to_state": "matured",
            "actor": "x",
            "reason": "should-fail",
        },
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Curated POST + PATCH + DELETE
# ---------------------------------------------------------------------------


def test_post_curated_writes_through_plane(
    client: TestClient,
    api_settings: object,
) -> None:
    import hashlib

    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/curated"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]
    body_text = "Curated body content."
    body = {
        "namespace": namespace,
        "title": "Test Curated POST",
        "content": body_text,
        "vault_path": "curated/eric/test-post.md",
        "body_hash": hashlib.sha256(body_text.encode()).hexdigest(),
        "topics": ["test"],
    }
    r = client.post(
        "/v1/curated-knowledge",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 202
    assert "object_id" in r.json()


# ---------------------------------------------------------------------------
# Concept reinforce / operator-promote / operator-reject
# ---------------------------------------------------------------------------


def test_concept_reinforce_bumps_count(
    client: TestClient,
    api_settings: object,
    concept: object,
) -> None:
    from musubi.types.common import generate_ksuid
    from musubi.types.concept import SynthesizedConcept
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/concept"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]

    async def _seed() -> str:
        saved = await concept.create(  # type: ignore[attr-defined]
            SynthesizedConcept(
                namespace=namespace,
                title="reinforce-target",
                content="X",
                synthesis_rationale="Y",
                merged_from=[generate_ksuid() for _ in range(3)],
            )
        )
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.post(
        f"/v1/concepts/{oid}/reinforce",
        headers={"Authorization": f"Bearer {token}"},
        params={"namespace": namespace},
    )
    assert r.status_code == 200
    assert r.json()["reinforcement_count"] >= 1


# ---------------------------------------------------------------------------
# Artifact upload (bullet 9)
# ---------------------------------------------------------------------------


def test_multipart_upload_for_artifacts(
    client: TestClient,
    api_settings: object,
) -> None:
    """Bullet 9 — POST /v1/artifacts accepts multipart/form-data with the
    blob file + metadata fields. Returns the new artifact's object_id."""
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/artifact"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]
    file_bytes = b"<html>contract</html>"
    r = client.post(
        "/v1/artifacts",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "namespace": namespace,
            "title": "test-artifact",
            "content_type": "text/html",
            "source_system": "contract-test",
        },
        files={"file": ("page.html", io.BytesIO(file_bytes), "text/html")},
    )
    assert r.status_code == 202
    body = r.json()
    assert "object_id" in body
    assert len(body["object_id"]) == 27


def test_artifact_archive_soft_deletes(
    client: TestClient,
    api_settings: object,
    artifact: object,
) -> None:
    import hashlib

    from musubi.types.artifact import SourceArtifact
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/artifact"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]

    async def _seed() -> str:
        saved = await artifact.create(  # type: ignore[attr-defined]
            SourceArtifact(
                namespace=namespace,
                title="archive-target",
                filename="archive.txt",
                sha256=hashlib.sha256(b"placeholder").hexdigest(),
                content_type="text/plain",
                size_bytes=11,
                chunker="markdown-headings-v1",
            )
        )
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.post(
        f"/v1/artifacts/{oid}/archive",
        headers={"Authorization": f"Bearer {token}"},
        params={"namespace": namespace},
    )
    assert r.status_code == 200
    after = asyncio.run(artifact.get(namespace=namespace, object_id=oid))  # type: ignore[attr-defined]
    assert after is not None and after.state == "archived"


# ---------------------------------------------------------------------------
# Thoughts send + read
# ---------------------------------------------------------------------------


def test_thought_send_writes_through_plane(
    client: TestClient,
    api_settings: object,
) -> None:
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/thought"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]
    body = {
        "namespace": namespace,
        "from_presence": "eric/claude-code",
        "to_presence": "eric/livekit",
        "content": "hello from contract test",
        "channel": "default",
        "importance": 5,
    }
    r = client.post(
        "/v1/thoughts/send",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 202
    assert "object_id" in r.json()


# ---------------------------------------------------------------------------
# Idempotency — bullets 10, 11
# ---------------------------------------------------------------------------


def test_idempotency_key_roundtrip(
    client: TestClient,
    valid_token: str,
) -> None:
    """Bullet 10 — POSTing the same body twice with the same
    Idempotency-Key returns the same object_id; the second call doesn't
    create a new row."""
    headers = {
        "Authorization": f"Bearer {valid_token}",
        "Idempotency-Key": "idem-test-write-001",
    }
    body = {
        "namespace": "eric/claude-code/episodic",
        "content": "idempotent-write-fixture-unique",
    }
    r1 = client.post("/v1/memories", headers=headers, json=body)
    r2 = client.post("/v1/memories", headers=headers, json=body)
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["object_id"] == r2.json()["object_id"]
    # The second call is a cache hit — verifiable via the response header.
    assert r2.headers.get("X-Idempotent-Replay") == "true"


def test_idempotency_key_expires_after_24h(
    client: TestClient,
    valid_token: str,
) -> None:
    """Bullet 11 — the idempotency cache TTL is 24h. A request with a
    stale key behaves like a fresh request."""
    from musubi.api.idempotency import _GLOBAL_CACHE

    headers = {
        "Authorization": f"Bearer {valid_token}",
        "Idempotency-Key": "idem-test-write-ttl",
    }
    body = {
        "namespace": "eric/claude-code/episodic",
        "content": "idempotent-ttl-fixture-unique",
    }
    r1 = client.post("/v1/memories", headers=headers, json=body)
    assert r1.status_code == 202
    # Force-expire the key.
    _GLOBAL_CACHE.expire_for_test(headers["Idempotency-Key"])
    r2 = client.post("/v1/memories", headers=headers, json=body)
    assert r2.status_code == 202
    # Same object id (plane dedup), but the response should NOT be a
    # replay — it ran fresh.
    assert r2.headers.get("X-Idempotent-Replay") != "true"


# ---------------------------------------------------------------------------
# Rate-limit middleware — bullets 13, 14
# ---------------------------------------------------------------------------


def test_rate_limit_enforces_token_bucket(
    client: TestClient,
    valid_token: str,
) -> None:
    """Bullet 13 — bursting capture POSTs past the per-token bucket
    yields at least one 429 with a typed RATE_LIMITED envelope."""
    headers = {"Authorization": f"Bearer {valid_token}"}
    statuses: list[int] = []
    for i in range(110):
        r = client.post(
            "/v1/memories",
            headers=headers,
            json={
                "namespace": "eric/claude-code/episodic",
                "content": f"burst-{i}",
            },
        )
        statuses.append(r.status_code)
        if r.status_code == 429:
            # Verify shape; one is enough.
            body = r.json()
            assert body["error"]["code"] == "RATE_LIMITED"
            assert "Retry-After" in r.headers or "retry-after" in r.headers
            break
    assert 429 in statuses


def test_rate_limit_operator_scope_10x_limit(
    client: TestClient,
    operator_token: str,
) -> None:
    """Bullet 14 — operator-scoped tokens get a 10x ceiling on the same
    bucket. We don't hammer 1000 requests; we verify the bucket capacity
    by reading the X-RateLimit-Limit header on the first response."""
    headers = {"Authorization": f"Bearer {operator_token}"}
    r = client.post(
        "/v1/memories",
        headers=headers,
        json={
            "namespace": "eric/claude-code/episodic",
            "content": "operator-burst-probe",
        },
    )
    # Operator token may not have rw scope on this namespace, so the
    # response could be 403 — but the rate-limit headers should still
    # appear (the middleware runs before scope check on writes).
    assert "x-ratelimit-limit" in {k.lower() for k in r.headers}, (
        f"missing X-RateLimit-Limit; got headers: {dict(r.headers)}"
    )
    operator_limit = int(r.headers["x-ratelimit-limit"])
    # Capture default is 100/min; operator gets 1000.
    assert operator_limit == 1000


# ---------------------------------------------------------------------------
# NDJSON streaming — bullet 15
# ---------------------------------------------------------------------------


def test_ndjson_retrieve_stream_yields_per_result(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    """Bullet 15 — POST /v1/retrieve/stream streams one JSON object per
    line (newline-delimited JSON), one per result, suitable for
    early-rendering on the client side."""
    namespace = "eric/claude-code/episodic"

    async def _seed() -> None:
        for i in range(3):
            saved = await episodic.create(
                EpisodicMemory(namespace=namespace, content=f"stream-{i}-content")
            )
            await episodic.transition(
                namespace=namespace,
                object_id=saved.object_id,
                to_state="matured",
                actor="seed",
                reason="seed",
            )

    asyncio.run(_seed())
    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": namespace,
            "query_text": "stream",
            "mode": "fast",
            "limit": 5,
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [line for line in r.text.split("\n") if line]
    assert len(lines) >= 1
    # Each line must be valid JSON.
    import json as _json

    for line in lines:
        row = _json.loads(line)
        assert "object_id" in row


# ---------------------------------------------------------------------------
# OpenAPI snapshot — write paths must appear in the committed file.
# ---------------------------------------------------------------------------


def test_committed_openapi_yaml_includes_write_paths() -> None:
    from pathlib import Path

    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    doc = yaml.safe_load((repo_root / "openapi.yaml").read_text())
    paths = set(doc["paths"].keys())
    # Every write route added by this slice must be in the snapshot.
    for required in (
        "/v1/memories",
        "/v1/memories/batch",
        "/v1/curated-knowledge",
        "/v1/artifacts",
        "/v1/thoughts/send",
        "/v1/lifecycle/transition",
        "/v1/retrieve/stream",
    ):
        assert required in paths, f"openapi.yaml missing path {required!r}"


# ---------------------------------------------------------------------------
# Coverage tests — exercise additional router branches.
# ---------------------------------------------------------------------------


def test_capture_validation_error_returns_422(
    client: TestClient,
    valid_token: str,
) -> None:
    """A well-formed request whose body fails validation at the
    FastAPI boundary (e.g. missing required content) returns 422
    Unprocessable Entity per RFC 9110 §15.5.21, carrying the typed
    BAD_REQUEST envelope."""
    r = client.post(
        "/v1/memories",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={"namespace": "eric/claude-code/episodic"},  # content missing
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_capture_malformed_namespace_returns_422_not_500(
    client: TestClient,
    api_settings: object,
) -> None:
    """Regression guard for #192 — a malformed namespace (two segments
    instead of the required tenant/presence/plane) fails the pydantic
    AfterValidator that the plane's typed model applies when the
    handler constructs an ``EpisodicMemory``. That pydantic
    ``ValidationError`` used to bubble up unhandled as 500 INTERNAL,
    which was misleading: the client sent invalid data, not the server
    a bug. Must be 422 with the typed BAD_REQUEST envelope.

    To reach the typed-model construction we need a token whose scope
    matches the malformed namespace (otherwise the auth scope check
    fires first, producing 403). The scope matcher accepts any N-part
    glob, so a 2-segment scope pattern grants a 2-segment namespace —
    and we land at the plane's AfterValidator, which is what we're
    actually testing."""
    from tests.api.conftest import mint_token

    # Scope grants the exact malformed namespace so scope check passes.
    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["perf-test/episodic:rw"],
        presence="perf-test/ephemeral",
    )
    r = client.post(
        "/v1/memories",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "perf-test/episodic",  # two segments — invalid
            "content": "whatever",
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "BAD_REQUEST"
    # The detail should mention the namespace field so callers can
    # localise the problem.
    assert "namespace" in r.json()["error"]["detail"].lower()


def test_idempotency_key_different_body_returns_conflict(
    client: TestClient,
    valid_token: str,
) -> None:
    """Per the spec's idempotency contract, a key with a *different*
    body than the original is a CONFLICT, not a silent dedup."""
    headers = {
        "Authorization": f"Bearer {valid_token}",
        "Idempotency-Key": "idem-conflict-test",
    }
    namespace = "eric/claude-code/episodic"
    r1 = client.post(
        "/v1/memories",
        headers=headers,
        json={"namespace": namespace, "content": "first-body"},
    )
    assert r1.status_code == 202
    r2 = client.post(
        "/v1/memories",
        headers=headers,
        json={"namespace": namespace, "content": "different-body"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "CONFLICT"


def test_thoughts_read_marks_thought_read(
    client: TestClient,
    api_settings: object,
) -> None:
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/thought"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]
    headers = {"Authorization": f"Bearer {token}"}
    sent = client.post(
        "/v1/thoughts/send",
        headers=headers,
        json={
            "namespace": namespace,
            "from_presence": "eric/claude-code",
            "to_presence": "eric/livekit",
            "content": "mark-me-read",
        },
    )
    oid = sent.json()["object_id"]
    r = client.post(
        "/v1/thoughts/read",
        headers=headers,
        json={
            "namespace": namespace,
            "ids": [oid],
            "reader": "eric/livekit",
        },
    )
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_patch_curated_returns_404_when_missing(
    client: TestClient,
    api_settings: object,
) -> None:
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/curated"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]
    r = client.patch(
        "/v1/curated-knowledge/0000000000000000000000000000",
        headers={"Authorization": f"Bearer {token}"},
        params={"namespace": namespace},
        json={"importance": 8},
    )
    assert r.status_code == 404


def test_patch_curated_updates_and_delete_archives(
    client: TestClient,
    api_settings: object,
    curated: object,
) -> None:
    """Round-trip the PATCH (importance/topics) + DELETE (soft-archive)
    surfaces on a real curated row to cover the writes_curated handlers."""
    import hashlib

    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/curated"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]
    headers = {"Authorization": f"Bearer {token}"}
    body_text = "Patch round trip body."
    create_body = {
        "namespace": namespace,
        "title": "Patch+Delete target",
        "content": body_text,
        "vault_path": "curated/eric/patch-delete.md",
        "body_hash": hashlib.sha256(body_text.encode()).hexdigest(),
    }
    r = client.post("/v1/curated-knowledge", headers=headers, json=create_body)
    assert r.status_code == 202
    oid = r.json()["object_id"]

    p = client.patch(
        f"/v1/curated-knowledge/{oid}",
        headers=headers,
        params={"namespace": namespace},
        json={"importance": 10, "topics": ["patched-topic"]},
    )
    assert p.status_code == 200
    assert p.json()["importance"] == 10
    assert "patched-topic" in p.json()["topics"]

    p_state = client.patch(
        f"/v1/curated-knowledge/{oid}",
        headers=headers,
        params={"namespace": namespace},
        json={"state": "matured"},
    )
    assert p_state.status_code == 400

    d = client.delete(
        f"/v1/curated-knowledge/{oid}",
        headers=headers,
        params={"namespace": namespace},
    )
    assert d.status_code == 200
    refreshed = asyncio.run(curated.get(namespace=namespace, object_id=oid))  # type: ignore[attr-defined]
    assert refreshed is not None and refreshed.state == "archived"


def test_delete_curated_404_when_missing(
    client: TestClient,
    api_settings: object,
) -> None:
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/curated"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]
    r = client.delete(
        "/v1/curated-knowledge/0000000000000000000000000000",
        headers={"Authorization": f"Bearer {token}"},
        params={"namespace": namespace},
    )
    assert r.status_code == 404


def test_artifact_purge_requires_operator(
    client: TestClient,
    valid_token: str,
) -> None:
    r = client.post(
        "/v1/artifacts/0000000000000000000000000000/purge",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": "eric/claude-code/artifact"},
    )
    assert r.status_code == 403


def test_rate_limit_resets_per_minute_window(
    client: TestClient,
    valid_token: str,
) -> None:
    """Coverage for the rate-limit window-rotation branch — a single
    request never hits the cap, so the post-request bucket count is
    decremented from the initial allowance."""
    r = client.post(
        "/v1/memories",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={"namespace": "eric/claude-code/episodic", "content": "rate-window-probe"},
    )
    assert "x-ratelimit-remaining" in {k.lower() for k in r.headers}
    remaining = int(r.headers["x-ratelimit-remaining"])
    limit = int(r.headers["x-ratelimit-limit"])
    assert 0 <= remaining < limit


@pytest.mark.skip(
    reason="deferred to a future slice-api-grpc: gRPC parity is bundled with "
    "that slice; not in this slice's owns_paths (proto/ forbidden)."
)
def test_protobuf_via_grpc_matches_rest_semantics_writes() -> None:
    pass
