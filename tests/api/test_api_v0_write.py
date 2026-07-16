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
from pydantic import ValidationError
from qdrant_client import QdrantClient

from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.types.episodic import EpisodicMemory
from tests.api.conftest import mint_token

# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

_COORDINATOR: LifecycleTransitionCoordinator | None = None


@pytest.fixture(autouse=True)
def _install_coordinator(qdrant: QdrantClient, api_settings: Settings) -> None:
    global _COORDINATOR
    _COORDINATOR = LifecycleTransitionCoordinator(
        client=qdrant, db_path=api_settings.lifecycle_sqlite_path
    )


def _coord() -> LifecycleTransitionCoordinator:
    assert _COORDINATOR is not None
    return _COORDINATOR


def _get_tags(
    client: TestClient, headers: dict[str, str], namespace: str, object_id: str
) -> list[str]:
    got = client.get(
        f"/v1/episodic/{object_id}",
        headers=headers,
        params={"namespace": namespace},
    )
    assert got.status_code == 200, got.text
    tags = got.json()["tags"]
    assert isinstance(tags, list)
    return tags


def test_capture_happy_returns_object_id(
    client: TestClient,
    valid_token: str,
) -> None:
    """Spec contract-tests § Capture: POST /v1/episodic returns 202 with
    the new object_id; the row is fetchable by GET."""
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    body = {
        "namespace": namespace,
        "content": "test-content-unique-write-abc123",
        "tags": ["contract-test"],
        "importance": 5,
    }
    r = client.post("/v1/episodic", headers=headers, json=body)
    assert r.status_code == 202
    object_id = r.json()["object_id"]
    assert isinstance(object_id, str) and len(object_id) == 27
    # Fetchable by id.
    got = client.get(
        f"/v1/episodic/{object_id}",
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
    r1 = client.post("/v1/episodic", headers=headers, json=body)
    body2 = dict(body)
    body2["tags"] = ["b"]
    r2 = client.post("/v1/episodic", headers=headers, json=body2)
    assert r1.status_code == 202
    assert r2.status_code == 202
    # Same object id; tags merged on the underlying row.
    assert r2.json()["object_id"] == r1.json()["object_id"]


def test_capture_adds_default_typed_episode_tags(
    client: TestClient,
    valid_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    r = client.post(
        "/v1/episodic",
        headers=headers,
        json={
            "namespace": namespace,
            "content": "typed-default-capture-write",
            "tags": ["src:direct-test"],
        },
    )
    assert r.status_code == 202, r.text
    object_id = r.json()["object_id"]
    assert _get_tags(client, headers, namespace, object_id) == [
        "src:direct-test",
        "kind:episode",
        "staleness:episodic",
    ]


def test_capture_preserves_explicit_typed_tags(
    client: TestClient,
    valid_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    tags = ["src:direct-test", "kind:project-stance", "staleness:durable"]
    r = client.post(
        "/v1/episodic",
        headers=headers,
        json={
            "namespace": namespace,
            "content": "typed-explicit-capture-write",
            "tags": tags,
        },
    )
    assert r.status_code == 202, r.text
    object_id = r.json()["object_id"]
    saved_tags = _get_tags(client, headers, namespace, object_id)
    assert saved_tags == tags
    assert "kind:episode" not in saved_tags
    assert "staleness:episodic" not in saved_tags


def test_capture_adds_missing_staleness_when_kind_supplied(
    client: TestClient,
    valid_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    r = client.post(
        "/v1/episodic",
        headers=headers,
        json={
            "namespace": namespace,
            "content": "typed-kind-only-capture-write",
            "tags": ["src:direct-test", "kind:project-stance"],
        },
    )
    assert r.status_code == 202, r.text
    assert _get_tags(client, headers, namespace, r.json()["object_id"]) == [
        "src:direct-test",
        "kind:project-stance",
        "staleness:episodic",
    ]


def test_capture_adds_missing_kind_when_staleness_supplied(
    client: TestClient,
    valid_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    r = client.post(
        "/v1/episodic",
        headers=headers,
        json={
            "namespace": namespace,
            "content": "typed-staleness-only-capture-write",
            "tags": ["src:direct-test", "staleness:current"],
        },
    )
    assert r.status_code == 202, r.text
    assert _get_tags(client, headers, namespace, r.json()["object_id"]) == [
        "src:direct-test",
        "staleness:current",
        "kind:episode",
    ]


def test_capture_does_not_double_add_default_typed_tags(
    client: TestClient,
    valid_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    r = client.post(
        "/v1/episodic",
        headers=headers,
        json={
            "namespace": namespace,
            "content": "typed-defaults-already-present-write",
            "tags": ["src:direct-test", "kind:episode", "staleness:episodic"],
        },
    )
    assert r.status_code == 202, r.text
    tags = _get_tags(client, headers, namespace, r.json()["object_id"])
    assert tags == ["src:direct-test", "kind:episode", "staleness:episodic"]
    assert tags.count("kind:episode") == 1
    assert tags.count("staleness:episodic") == 1


def test_capture_rejects_unknown_typed_tags(
    client: TestClient,
    valid_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    r = client.post(
        "/v1/episodic",
        headers=headers,
        json={
            "namespace": namespace,
            "content": "typed-invalid-capture-write",
            "tags": ["src:direct-test", "kind:whatever"],
        },
    )
    assert r.status_code == 422
    assert "unknown essence kind tag 'kind:whatever'" in r.text


def test_capture_rejects_out_of_scope_namespace(
    client: TestClient,
    out_of_scope_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {out_of_scope_token}"}
    body = {
        "namespace": "eric/claude-code/episodic",
        "content": "should-not-write",
    }
    r = client.post("/v1/episodic", headers=headers, json=body)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_capture_missing_token_returns_401(client: TestClient) -> None:
    r = client.post(
        "/v1/episodic",
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
    r = client.post("/v1/episodic/batch", headers=headers, json=body)
    assert r.status_code == 202
    out = r.json()
    assert len(out["object_ids"]) == 3
    for oid in out["object_ids"]:
        assert len(oid) == 27


def test_batch_capture_adds_default_typed_episode_tags_per_item(
    client: TestClient,
    valid_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {valid_token}"}
    namespace = "eric/claude-code/episodic"
    body = {
        "namespace": namespace,
        "items": [
            {
                "content": "batch-typed-default-write",
                "tags": ["src:direct-test"],
            },
            {
                "content": "batch-typed-explicit-write",
                "tags": ["src:direct-test", "kind:project-stance", "staleness:durable"],
            },
        ],
    }
    r = client.post("/v1/episodic/batch", headers=headers, json=body)
    assert r.status_code == 202, r.text
    object_ids = r.json()["object_ids"]

    assert _get_tags(client, headers, namespace, object_ids[0]) == [
        "src:direct-test",
        "kind:episode",
        "staleness:episodic",
    ]

    assert _get_tags(client, headers, namespace, object_ids[1]) == [
        "src:direct-test",
        "kind:project-stance",
        "staleness:durable",
    ]


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
        "/v1/episodic",
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
        "/v1/episodic",
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
        f"/v1/episodic/{oid}",
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
        "/v1/episodic",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={"namespace": "eric/claude-code/episodic", "content": "no-override"},
    )
    assert r.status_code == 202
    oid = r.json()["object_id"]
    got = client.get(
        f"/v1/episodic/{oid}",
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
        "/v1/episodic/batch",
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
        "/v1/episodic/batch",
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
        f"/v1/episodic/{oids[0]}",
        headers=headers,
        params={"namespace": namespace},
    ).json()
    b = client.get(
        f"/v1/episodic/{oids[1]}",
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
        f"/v1/episodic/{oid}",
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
        f"/v1/episodic/{oid}",
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
        f"/v1/episodic/{oid}",
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
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace, "hard": "true"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Corrupted-row regressions (2026-07-11)
#
# A row carrying a payload key the strict read model forbids used to be
# unreadable AND unremovable: every path guarded existence with `plane.get()`,
# which deserializes, so the guard 500'd before the delete ran. A memory that
# cannot be deleted *because it is too broken to read* can teach a falsehood
# forever. These three tests are the proof it cannot happen again.
#
# Lived instance: aoi/command-chair/episodic/3GJhJLAvYXzIp8Qe8tuPHR9S9th, bricked
# 2026-07-10 by a `retracted_original` key sent through PATCH.
# ---------------------------------------------------------------------------


def _brick(episodic: EpisodicPlane, qdrant: QdrantClient, namespace: str, content: str) -> str:
    """Seed a row, then write an unmodeled payload key straight into Qdrant.

    This bypasses the API on purpose — it reproduces the state of a row that was
    corrupted *before* the PATCH allowlist existed, which is the state real rows
    in production are actually in.
    """
    from musubi.planes.episodic.plane import episodic_point_id

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=namespace, content=content))
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"retracted_original": "an unmodeled key the read model forbids"},
        points=[episodic_point_id(oid)],
        wait=True,  # the test reads this back immediately; without wait it can race
    )
    # Precondition: the row is genuinely unreadable, and unreadable for the REASON we
    # think. A bare `pytest.raises(Exception)` would pass on a typo, a connection
    # error, or a missing collection — it would prove the row is broken without
    # proving it is broken by `extra_forbidden`, which is the whole premise.
    with pytest.raises(ValidationError) as exc:
        asyncio.run(episodic.get(namespace=namespace, object_id=oid))
    assert any(e["type"] == "extra_forbidden" for e in exc.value.errors()), (
        "the row must be unreadable because of the forbidden extra key, not for some "
        "other reason — otherwise this fixture is not reproducing the real defect"
    )
    return oid


def _raw_point(qdrant: QdrantClient, object_id: str) -> dict[str, object] | None:
    """The persisted payload, unvalidated — for asserting what actually landed on disk
    rather than trusting an HTTP status code."""
    from musubi.planes.episodic.plane import episodic_point_id

    points = qdrant.retrieve(
        collection_name="musubi_episodic",
        ids=[episodic_point_id(object_id)],
        with_payload=True,
    )
    return dict(points[0].payload) if points and points[0].payload else None


def test_corrupted_row_can_still_be_hard_deleted(
    client: TestClient,
    api_settings: Settings,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
) -> None:
    """The row a hard-delete most needs to remove is the one it could not remove.

    Note the token: hard-delete needs BOTH namespace write (the route's outer
    ``require_auth(access="w")``) AND operator scope (the in-handler check). The
    bare ``operator_token`` fixture carries only the latter and is refused at the
    door — which is why the real failure in production was a 500, not a 403.
    """
    namespace = "eric/claude-code/episodic"
    oid = _brick(episodic, qdrant, namespace, "bricked-hard")
    token = mint_token(api_settings, scopes=["operator", f"{namespace}:rw"])

    r = client.delete(
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {token}"},
        params={"namespace": namespace, "hard": "true"},
    )
    assert r.status_code == 204, f"corrupted row must be hard-deletable, got {r.text}"
    # And it is actually gone from the index, not merely reported gone.
    assert not asyncio.run(episodic.exists(namespace=namespace, object_id=oid))


def test_corrupted_row_can_still_be_soft_archived(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
) -> None:
    """Soft-delete carried the same deserializing guard — so a bad row could not
    even be archived out of the way."""
    namespace = "eric/claude-code/episodic"
    oid = _brick(episodic, qdrant, namespace, "bricked-soft")

    r = client.delete(
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
    )
    assert r.status_code == 200, f"corrupted row must be archivable, got {r.text}"

    # A 200 proves the handler did not raise. It does NOT prove the archive landed.
    # Read the raw point and assert the state actually moved on disk — the endpoint
    # could return 200 having done nothing at all and this test would still have
    # passed. (Yua, PR #398 review: "regression proof is weaker than the PR claims.")
    payload = _raw_point(qdrant, oid)
    assert payload is not None, "row vanished; soft-delete must archive, not remove"
    assert payload["state"] == "archived", f"state did not move on disk: {payload['state']!r}"
    # And the corruption is still there — soft-delete archives, it does not repair.
    # If this ever stops being true, something is silently rewriting payloads.
    assert "retracted_original" in payload


def test_patch_rejects_unknown_fields_that_would_brick_the_row(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    """The write model must never accept what the read model forbids.

    `_FORBIDDEN_PATCH_FIELDS` was a denylist of four names; every key nobody had
    thought of went straight into the payload and made the row permanently
    unreadable. Note the old failure shape: `set_payload` SUCCEEDED and only the
    refreshing `get()` raised — so the caller saw a 500 and believed the write had
    failed, while the row was already destroyed.
    """
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=namespace, content="patchme"))
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.patch(
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
        json={"retracted_original": "this would have bricked the row forever"},
    )
    assert r.status_code == 400, f"unknown PATCH field must be refused, got {r.status_code}"
    assert "retracted_original" in r.json()["error"]["detail"]

    # The decisive assertion: the row is UNHARMED. A rejected write must not be a
    # partial write.
    after = asyncio.run(episodic.get(namespace=namespace, object_id=oid))
    assert after is not None and after.content == "patchme"


def test_patch_accepts_a_retract_shaped_body(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    """Retraction must keep working. This is the caller contract, not an internal one.

    Musubi is append-only: a false memory cannot be deleted, only rewritten to say
    that it lied. ``memory-data musubi retract`` is the fleet's ONLY mechanism for
    neutralising a falsehood, and it PATCHes exactly this shape:

        {"content": ..., "summary": ..., "tags": [...], "importance": 1}

    The first cut of the PATCH allowlist omitted ``content`` — so every retraction
    across the fleet would have started returning 400. A memory-integrity fix that
    disables the tool for fixing memory is worse than the bug it closes.

    The tests I wrote for my own fix could not have caught this: they exercised the
    code I had just written, not the callers that depend on it. Caught by Yua in
    adversarial review (PR #398, 2026-07-11). Hence this test — it asserts the
    CONTRACT, from the caller's side.
    """
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(
            EpisodicMemory(namespace=namespace, content="a claim that turned out to be false")
        )
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.patch(
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
        json={
            "content": "RETRACTED 2026-07-11. This memory was FALSE. Do not act on it.",
            "summary": "RETRACTED: this memory was false.",
            "tags": ["retracted", "kind:episode", "staleness:episodic"],
            "importance": 1,
        },
    )
    assert r.status_code == 200, f"retraction must not be refused, got {r.status_code}: {r.text}"

    after = asyncio.run(episodic.get(namespace=namespace, object_id=oid))
    assert after is not None
    assert after.content.startswith("RETRACTED"), "the retraction text must actually land"
    assert after.importance == 1


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
        "/v1/curated",
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
    r1 = client.post("/v1/episodic", headers=headers, json=body)
    r2 = client.post("/v1/episodic", headers=headers, json=body)
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["object_id"] == r2.json()["object_id"]
    # The second call is a cache hit — verifiable via the response header.
    assert r2.headers.get("X-Idempotent-Replay") == "true"


def test_idempotency_key_expires_after_ttl(app_factory: object, valid_token: str) -> None:
    """Bullet 11 — the idempotency lease cache TTL. A completed replay entry replays UP TO and
    INCLUDING the TTL boundary, and past it the entry is CLEANED by ``acquire`` so the next
    identical request executes fresh (no replay).

    Phase B: this drives the REAL cleanup semantics — an ``IdempotencyLeaseCache`` with an INJECTED
    clock (overriding the route provider, which the store-only observer also reads back via
    ``request.state.idem_cache``), advancing the clock rather than reaching into private state. The
    expiry comparison is strict (``now - completed_at > ttl``), so AT the TTL the entry still
    replays and only strictly PAST it is cleaned — both boundaries are asserted."""
    from fastapi.testclient import TestClient as _TC

    from musubi.api.idempotency import IdempotencyLeaseCache, get_idempotency_lease_cache

    class _Clock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

    clock = _Clock()
    ttl = 100.0
    cache = IdempotencyLeaseCache(clock=clock, ttl_s=ttl)
    app_factory.dependency_overrides[get_idempotency_lease_cache] = lambda: cache  # type: ignore[attr-defined]

    headers = {
        "Authorization": f"Bearer {valid_token}",
        "Idempotency-Key": "idem-test-write-ttl",
    }
    body = {"namespace": "eric/claude-code/episodic", "content": "idempotent-ttl-fixture-unique"}

    with _TC(app_factory) as client:  # type: ignore[arg-type]
        first = client.post("/v1/episodic", headers=headers, json=body)
        assert first.status_code == 202 and first.headers.get("X-Idempotent-Replay") != "true"

        # AT the TTL boundary: strict `>` comparison → the completed entry is NOT yet expired, so
        # an identical request still REPLAYS.
        clock.t += ttl
        at_boundary = client.post("/v1/episodic", headers=headers, json=body)
        assert at_boundary.status_code == 202
        assert at_boundary.headers.get("X-Idempotent-Replay") == "true", (
            "at exactly the TTL the entry must still replay (expiry is strict `> ttl`)"
        )

        # PAST the TTL: acquire's cleanup removes the expired completed entry → fresh execution,
        # NOT a replay.
        clock.t += 0.5
        past_ttl = client.post("/v1/episodic", headers=headers, json=body)
        assert past_ttl.status_code == 202
        assert past_ttl.headers.get("X-Idempotent-Replay") != "true", (
            "past the TTL the completed entry must be cleaned so the request executes fresh"
        )


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
            "/v1/episodic",
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
        "/v1/episodic",
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
                coordinator=_coord(),
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
        "/v1/episodic",
        "/v1/episodic/batch",
        "/v1/curated",
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
        "/v1/episodic",
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
        "/v1/episodic",
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
        "/v1/episodic",
        headers=headers,
        json={"namespace": namespace, "content": "first-body"},
    )
    assert r1.status_code == 202
    r2 = client.post(
        "/v1/episodic",
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
        "/v1/curated/0000000000000000000000000000",
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
    r = client.post("/v1/curated", headers=headers, json=create_body)
    assert r.status_code == 202
    oid = r.json()["object_id"]

    p = client.patch(
        f"/v1/curated/{oid}",
        headers=headers,
        params={"namespace": namespace},
        json={"importance": 10, "topics": ["patched-topic"]},
    )
    assert p.status_code == 200
    assert p.json()["importance"] == 10
    assert "patched-topic" in p.json()["topics"]

    p_state = client.patch(
        f"/v1/curated/{oid}",
        headers=headers,
        params={"namespace": namespace},
        json={"state": "matured"},
    )
    assert p_state.status_code == 400

    d = client.delete(
        f"/v1/curated/{oid}",
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
        "/v1/curated/0000000000000000000000000000",
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


def test_artifact_purge_truthful_and_idempotent_and_fenced(
    client: TestClient,
    api_settings: object,
    qdrant: object,
) -> None:
    import io
    from pathlib import Path

    from qdrant_client import models

    from musubi.embedding.fake import FakeEmbedder
    from musubi.planes.artifact.indexer import ArtifactIndexer
    from tests.api.conftest import mint_token
    from tests.api.test_api_v0_write import _coord

    namespace = "eric/ops/artifact"
    rw_token = mint_token(api_settings, scopes=[f"{namespace}:rw"])  # type: ignore[arg-type]

    # Register the indexer so reconcile_once works
    coord = _coord()
    indexer = ArtifactIndexer(
        client=qdrant,
        embedder=FakeEmbedder(),
        blob_root=getattr(api_settings, "artifact_blob_path"),
    )
    indexer.register(coord)

    # 1. Create artifact with rw token
    file_bytes = b"blob data chunks words here"
    r = client.post(
        "/v1/artifacts",
        headers={"Authorization": f"Bearer {rw_token}"},
        data={
            "namespace": namespace,
            "title": "purge-test",
            "content_type": "text/plain",
        },
        files={"file": ("test.txt", io.BytesIO(file_bytes), "text/plain")},
    )
    assert r.status_code == 202
    obj_id = r.json()["object_id"]

    # 2. Process the indexing intent so it gets physical chunks and a committed generation
    coord.reconcile_once()

    chunks_col = "musubi_artifact_chunks"

    # Verify things exist and are committed
    blob_path = Path(getattr(api_settings, "artifact_blob_path")) / namespace / obj_id
    assert blob_path.exists()
    head_res = qdrant.scroll(
        collection_name="musubi_artifact",
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=obj_id))]
        ),
    )[0]  # type: ignore[attr-defined]
    assert len(head_res) == 1
    assert (
        head_res[0].payload is not None
        and head_res[0].payload.get("committed_generation") is not None
    )
    chunks_res = qdrant.scroll(
        collection_name=chunks_col,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="artifact_id", match=models.MatchValue(value=obj_id))]
        ),
    )[0]  # type: ignore[attr-defined]
    assert len(chunks_res) > 0

    # 3. Purge with operator token
    op_token = mint_token(api_settings, scopes=["operator", f"{namespace}:rw"])  # type: ignore[arg-type]
    r_purge = client.post(
        f"/v1/artifacts/{obj_id}/purge",
        headers={"Authorization": f"Bearer {op_token}"},
        params={"namespace": namespace},
    )
    assert r_purge.status_code == 200
    assert r_purge.json() == {"status": "purged"}

    # 4. Verify deletions
    assert not blob_path.exists()
    head_res_after = qdrant.scroll(
        collection_name="musubi_artifact",
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=obj_id))]
        ),
    )[0]  # type: ignore[attr-defined]
    assert len(head_res_after) == 0
    chunks_res_after = qdrant.scroll(
        collection_name=chunks_col,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="artifact_id", match=models.MatchValue(value=obj_id))]
        ),
    )[0]  # type: ignore[attr-defined]
    assert len(chunks_res_after) == 0

    # 5. Retry idempotent
    r_purge_retry = client.post(
        f"/v1/artifacts/{obj_id}/purge",
        headers={"Authorization": f"Bearer {op_token}"},
        params={"namespace": namespace},
    )
    assert r_purge_retry.status_code == 200
    assert r_purge_retry.json() == {"status": "purged"}

    # 6. Load-bearing no-resurrection discriminator
    coord.enqueue_index_intent(object_id=obj_id, namespace=namespace)
    coord.reconcile_once()

    head_res_fenced = qdrant.scroll(
        collection_name="musubi_artifact",
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=obj_id))]
        ),
    )[0]  # type: ignore[attr-defined]
    assert len(head_res_fenced) == 0
    chunks_res_fenced = qdrant.scroll(
        collection_name=chunks_col,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="artifact_id", match=models.MatchValue(value=obj_id))]
        ),
    )[0]  # type: ignore[attr-defined]
    assert len(chunks_res_fenced) == 0


def test_rate_limit_resets_per_minute_window(
    client: TestClient,
    valid_token: str,
) -> None:
    """Coverage for the rate-limit window-rotation branch — a single
    request never hits the cap, so the post-request bucket count is
    decremented from the initial allowance."""
    r = client.post(
        "/v1/episodic",
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


# ---------------------------------------------------------------------------
# Curated corrupted-row regressions (2026-07-11, PR #398 review by Yua)
#
# The episodic fix was proposed as complete. It was not: `writes_curated` carried
# BOTH bugs in full. Curated is the plane that matters most — it is the shared
# settled-truth layer every agent reads as fact. A false row here that cannot be
# removed is permanent false ground for the whole fleet.
# ---------------------------------------------------------------------------


def _seed_curated(client: TestClient, headers: dict[str, str], namespace: str, slug: str) -> str:
    import hashlib

    body_text = f"curated body for {slug}"
    r = client.post(
        "/v1/curated",
        headers=headers,
        json={
            "namespace": namespace,
            "title": f"curated {slug}",
            "content": body_text,
            "vault_path": f"curated/eric/{slug}.md",
            "body_hash": hashlib.sha256(body_text.encode()).hexdigest(),
        },
    )
    assert r.status_code == 202, r.text
    return str(r.json()["object_id"])


def _brick_curated(qdrant: QdrantClient, object_id: str) -> None:
    """Write an unmodeled payload key straight into Qdrant, bypassing the API —
    reproducing the state a real row would be in, not the state the new allowlist
    would permit."""
    from musubi.planes.curated.plane import _point_id

    qdrant.set_payload(
        collection_name="musubi_curated",
        payload={"retracted_original": "an unmodeled key the read model forbids"},
        points=[_point_id(object_id)],
        wait=True,  # the test reads this back immediately; without wait it can race
    )


def test_curated_patch_rejects_unknown_fields(
    client: TestClient,
    api_settings: Settings,
) -> None:
    """A five-name denylist guarded the shared truth plane. Everything it had not
    imagined became permanent, unreadable, unremovable false ground."""
    namespace = "eric/claude-code/curated"
    headers = {"Authorization": f"Bearer {mint_token(api_settings, scopes=[f'{namespace}:rw'])}"}
    oid = _seed_curated(client, headers, namespace, "reject-unknown")

    r = client.patch(
        f"/v1/curated/{oid}",
        headers=headers,
        params={"namespace": namespace},
        json={"retracted_original": "this would have bricked shared truth forever"},
    )
    assert r.status_code == 400, f"unknown PATCH field must be refused, got {r.status_code}"
    assert "retracted_original" in r.json()["error"]["detail"]

    # The row is UNHARMED — a rejected write must not be a partial write.
    got = client.get(f"/v1/curated/{oid}", headers=headers, params={"namespace": namespace})
    assert got.status_code == 200, "the row must still be readable after a refused PATCH"


def test_curated_corrupted_row_can_still_be_archived(
    client: TestClient,
    api_settings: Settings,
    qdrant: QdrantClient,
) -> None:
    """`delete_curated` guarded with a deserializing `get()` it never used — so a
    corrupted curated row could not even be archived out of the way."""
    namespace = "eric/claude-code/curated"
    headers = {"Authorization": f"Bearer {mint_token(api_settings, scopes=[f'{namespace}:rw'])}"}
    oid = _seed_curated(client, headers, namespace, "archive-bricked")
    _brick_curated(qdrant, oid)

    # Precondition: genuinely unreadable, and for the reason we think.
    got = client.get(f"/v1/curated/{oid}", headers=headers, params={"namespace": namespace})
    assert got.status_code == 500, "fixture must reproduce a real corrupted row"

    r = client.delete(
        f"/v1/curated/{oid}",
        headers=headers,
        params={"namespace": namespace},
    )
    assert r.status_code == 200, f"corrupted curated row must be archivable, got {r.text}"

    # Assert it landed on disk, not merely that the handler returned 200.
    from musubi.planes.curated.plane import _point_id

    points = qdrant.retrieve(
        collection_name="musubi_curated", ids=[_point_id(oid)], with_payload=True
    )
    assert points and points[0].payload
    assert points[0].payload["state"] == "archived"


def test_patch_empty_content_is_refused_and_row_unharmed(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    """PATCH/read validation parity — the allowlist alone was not enough.

    `PatchEpisodicRequest.content` was declared `str | None` (unconstrained), while the
    persisted `MemoryObject.content` is `Field(min_length=1)`. So `{"content": ""}` passed
    the REQUEST model, persisted via `set_payload`, and then failed the REFRESH read with
    `string_too_short` — recreating the exact 500-after-corruption failure this PR exists
    to kill. My fix for the bricking bug introduced a new way to brick a row.

    The allowlist stops unknown KEYS. It does nothing about invalid VALUES of known keys.
    Caught by Yua, rev2 review.
    """
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=namespace, content="intact"))
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.patch(
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
        json={"content": ""},
    )
    assert r.status_code in (400, 422), f"empty content must be refused, got {r.status_code}"

    # The decisive assertion: the row is STILL READABLE. A refused write must not persist.
    after = asyncio.run(episodic.get(namespace=namespace, object_id=oid))
    assert after is not None and after.content == "intact"


def test_hard_delete_removes_an_identity_damaged_row_via_http(
    client: TestClient,
    api_settings: Settings,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
) -> None:
    """The OPERATOR route must remove a row whose payload identifiers are gone.

    Rev3 hardened `EpisodicPlane.delete()` with deterministic point-ID addressing — and
    left THIS route calling payload-filtered `plane.exists()` and deleting via an
    `object_id` payload filter. So a row that had lost its `namespace`/`object_id` keys
    returned 404 and stayed stored, through the very path the fleet and operators actually
    use. The path nobody calls was fixed; the path that failed in production was not.
    (Yua, rev3 review of PR #398.)
    """
    from musubi.planes.episodic.plane import episodic_point_id
    from musubi.store.raw_lookup import retrieve_by_point_id

    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(
            EpisodicMemory(namespace=namespace, content="identity-will-be-stripped")
        )
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    pid = episodic_point_id(oid)

    # Destroy the identifiers every payload-filtered lookup searches by.
    qdrant.clear_payload(collection_name="musubi_episodic", points_selector=[pid], wait=True)
    assert retrieve_by_point_id(qdrant, "musubi_episodic", point_id=pid) == {}, (
        "fixture must leave the point present but identity-damaged"
    )

    token = mint_token(api_settings, scopes=["operator", f"{namespace}:rw"])
    r = client.delete(
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {token}"},
        params={"namespace": namespace, "hard": "true"},
    )
    assert r.status_code == 204, f"identity-damaged row must be removable via HTTP, got {r.text}"

    # Gone from storage, not merely reported gone.
    assert retrieve_by_point_id(qdrant, "musubi_episodic", point_id=pid) is None


def test_patch_explicit_null_unknown_field_is_rejected_not_silently_dropped(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    """An explicit null must not become a FALSE SUCCESS.

    Both PATCH handlers built `incoming` with `model_dump(exclude_none=True)`, which drops
    explicitly-supplied nulls BEFORE the allowlist and the canonical merged-row guard ever
    see them. So:

        PATCH {"retracted_original": null}  ->  incoming == {}  ->  200 OK, nothing written

    The endpoint returned success without applying the mutation and without rejecting it.
    The caller believes an unknown field was accepted. That is the defect this entire PR is
    about — a component reporting success without doing the work — sitting in the guard
    written to prevent it. (Yua, review of d5c7e0f.)

    `exclude_unset=True` preserves the caller's actual key set, so unknown keys are rejected
    whatever their value.
    """
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=namespace, content="intact"))
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.patch(
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
        json={"retracted_original": None},
    )
    assert r.status_code == 400, (
        f"an unknown field must be rejected whatever its value; a null must not be silently "
        f"dropped into a no-op 200. Got {r.status_code}."
    )
    assert "retracted_original" in r.json()["error"]["detail"]

    after = asyncio.run(episodic.get(namespace=namespace, object_id=oid))
    assert after is not None and after.content == "intact"


def test_patch_explicit_null_on_a_known_field_is_judged_by_the_canonical_model(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    """`{"content": null}` was treated as omission. It is not an omission — it is a request
    to set content to null, which the persisted model forbids. It must be judged, not
    silently discarded, and the row must survive either way."""
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=namespace, content="intact"))
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.patch(
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
        json={"content": None},
    )
    assert r.status_code in (400, 422), f"a null content must be judged, not dropped: {r.text}"

    # Whatever the verdict, the row is UNHARMED.
    after = asyncio.run(episodic.get(namespace=namespace, object_id=oid))
    assert after is not None and after.content == "intact"


def test_curated_patch_explicit_null_unknown_field_is_rejected(
    client: TestClient,
    api_settings: Settings,
) -> None:
    """Same false-success on the shared-truth plane. Both handlers used exclude_none, so an
    explicit null on an unknown field became a no-op 200 on CURATED too.

    Testing the class, not the example — that is the mistake that produced the previous
    three commits.
    """
    namespace = "eric/claude-code/curated"
    headers = {"Authorization": f"Bearer {mint_token(api_settings, scopes=[f'{namespace}:rw'])}"}
    oid = _seed_curated(client, headers, namespace, "explicit-null")

    r = client.patch(
        f"/v1/curated/{oid}",
        headers=headers,
        params={"namespace": namespace},
        json={"retracted_original": None},
    )
    assert r.status_code == 400, f"unknown field must be rejected even when null: {r.text}"
    assert "retracted_original" in r.json()["error"]["detail"]

    got = client.get(f"/v1/curated/{oid}", headers=headers, params={"namespace": namespace})
    assert got.status_code == 200, "the row must be unharmed by a refused PATCH"


def test_patch_omitted_field_is_still_omitted(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    """`exclude_unset` must not turn omission into a null write.

    Fixing the null bug by switching to exclude_unset would be worthless if it then wrote
    `None` over every field the caller simply did not mention. A PATCH of one field must
    leave the others exactly as they were.
    """
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(
            EpisodicMemory(namespace=namespace, content="keep me", summary="keep this too")
        )
        return str(saved.object_id)

    oid = asyncio.run(_seed())
    r = client.patch(
        f"/v1/episodic/{oid}",
        headers={"Authorization": f"Bearer {valid_token}"},
        params={"namespace": namespace},
        json={"importance": 9},  # content and summary NOT mentioned
    )
    assert r.status_code == 200, r.text

    after = asyncio.run(episodic.get(namespace=namespace, object_id=oid))
    assert after is not None
    assert after.importance == 9
    assert after.content == "keep me", "an omitted field must not be nulled"
    assert after.summary == "keep this too", "an omitted field must not be nulled"
