"""RET-008 / #502 — fenced per-record lease invariants.

Integration tests manipulate ``access_lease_token`` directly on a real Qdrant server and drive
:func:`musubi.store.access_lease.lease_increment_access`; the exhaustion + single-loop guards are
unit tests. Bring the server up with ``make test-integration-up`` (port 6339).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import TransitionIntent, _intended_patch
from musubi.planes.curated.plane import CuratedPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.store import bootstrap
from musubi.store.access_lease import AccessLeaseExhausted, lease_increment_access
from musubi.store.memory_serialization import LEASE_OWNED_FIELDS
from musubi.store.names import collection_for_plane
from musubi.types.common import generate_ksuid
from musubi.types.curated import CuratedKnowledge
from musubi.types.episodic import EpisodicMemory

_COLL = collection_for_plane("episodic")
_CURATED_COLL = collection_for_plane("curated")


@pytest.fixture
def real_qdrant() -> Iterator[QdrantClient]:
    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    client = QdrantClient(host="localhost", port=port)
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


def _seed(client: QdrantClient) -> tuple[str, str]:
    ns = f"lease-{generate_ksuid()[:8].lower()}/dev/episodic"
    row = asyncio.run(
        EpisodicPlane(client=client, embedder=FakeEmbedder()).create(
            EpisodicMemory(namespace=ns, content="lease invariant", state="matured")
        )
    )
    return ns, row.object_id


def _row(client: QdrantClient, oid: str) -> dict[str, Any]:
    recs, _ = client.scroll(
        collection_name=_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
        limit=1,
        with_payload=True,
    )
    return dict(recs[0].payload or {}) if recs else {}


def _curated_count(client: QdrantClient, oid: str) -> int:
    recs, _ = client.scroll(
        collection_name=_CURATED_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
        limit=1,
        with_payload=True,
    )
    return (recs[0].payload or {}).get("access_count", -1) if recs else -1


def _set_token(client: QdrantClient, ns: str, oid: str, token: str) -> None:
    client.set_payload(
        collection_name=_COLL,
        payload={"access_lease_token": token},
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )


@pytest.mark.integration
def test_update_and_release_atomic_readback(real_qdrant: QdrantClient) -> None:
    ns, oid = _seed(real_qdrant)
    asyncio.run(lease_increment_access(real_qdrant, _COLL, {(ns, oid)}))
    row = _row(real_qdrant, oid)
    assert row.get("access_count") == 1  # incremented
    assert row.get("access_lease_token") is None  # released in the same fenced update


@pytest.mark.integration
def test_nonexpired_lease_cannot_be_stolen(real_qdrant: QdrantClient) -> None:
    ns, oid = _seed(real_qdrant)
    fresh = f"held:{int(time.time() * 1_000_000)}:heldbyanother"  # issued NOW → live
    _set_token(real_qdrant, ns, oid, fresh)
    # A live foreign lease must never be stolen — the increment can't proceed and fails loud
    # rather than corrupting the counter.
    with pytest.raises(AccessLeaseExhausted):
        asyncio.run(lease_increment_access(real_qdrant, _COLL, {(ns, oid)}))
    row = _row(real_qdrant, oid)
    assert row.get("access_count") == 0  # untouched
    assert row.get("access_lease_token") == fresh  # the live lease is intact


@pytest.mark.integration
def test_expired_lease_exact_token_takeover_recovers(real_qdrant: QdrantClient) -> None:
    """A crashed holder leaves an EXPIRED token; the next writer takes over that exact token and
    completes the increment (crash-between-acquire-and-update recovery)."""
    ns, oid = _seed(real_qdrant)
    expired = f"held:{int(time.time() * 1_000_000) - 10_000_000}:crashedholder"  # 10s ago → expired
    _set_token(real_qdrant, ns, oid, expired)
    asyncio.run(lease_increment_access(real_qdrant, _COLL, {(ns, oid)}))
    row = _row(real_qdrant, oid)
    assert row.get("access_count") == 1  # taken over + incremented
    assert row.get("access_lease_token") is None  # released


@pytest.mark.integration
def test_old_holder_fenced_after_takeover(real_qdrant: QdrantClient) -> None:
    """After an expired token is taken over, the OLD holder's fenced write (on its old token) must
    match zero points — it can never corrupt the counter post-takeover."""
    ns, oid = _seed(real_qdrant)
    old = f"held:{int(time.time() * 1_000_000) - 10_000_000}:oldholder"
    _set_token(real_qdrant, ns, oid, old)
    asyncio.run(lease_increment_access(real_qdrant, _COLL, {(ns, oid)}))
    # The old holder resumes and tries its fenced increment+release on its stale token:
    real_qdrant.set_payload(
        collection_name=_COLL,
        payload={"access_count": 999, "access_lease_token": None},
        points=models.Filter(
            must=[
                models.FieldCondition(key="object_id", match=models.MatchValue(value=oid)),
                models.FieldCondition(key="access_lease_token", match=models.MatchValue(value=old)),
            ]
        ),
    )
    assert _row(real_qdrant, oid).get("access_count") == 1  # old holder's write matched zero


# ── unit guards ───────────────────────────────────────────────────────────────
class _AlwaysLiveLeaseClient:
    """Fake client whose rows always show a FRESH foreign lease → acquire can never win."""

    def scroll(self, *_a: Any, **_k: Any) -> Any:
        token = f"held:{int(time.time() * 1_000_000)}:foreign"
        rec = type(
            "R",
            (),
            {
                "id": "p1",
                "payload": {
                    "namespace": "n/n/episodic",
                    "object_id": "o",
                    "access_lease_token": token,
                    "access_count": 0,
                },
            },
        )()
        return ([rec], None)

    def batch_update_points(self, *_a: Any, **_k: Any) -> Any:
        return None


@pytest.mark.integration
def test_full_payload_update_cannot_reset_leased_increment(real_qdrant: QdrantClient) -> None:
    """Gap-2 arbiter: a full-payload UPDATE that carries a STALE access_count (a concurrent
    transition/patch that read the row before the lease bump) must NOT reset the leased value.
    `memory_update_payload` excludes the lease-owned fields; the set_payload merge leaves them
    exactly as the lease last wrote them."""
    from musubi.store.memory_serialization import memory_update_payload

    ns, oid = _seed(real_qdrant)  # access_count starts at 0
    stale = _row(real_qdrant, oid)  # snapshot with access_count == 0 (pre-bump)
    asyncio.run(lease_increment_access(real_qdrant, _COLL, {(ns, oid)}))
    assert _row(real_qdrant, oid).get("access_count") == 1

    # Simulate a concurrent transition/patch writing back its STALE (count==0) model.
    stale_model = EpisodicMemory.model_validate(stale)  # carries access_count = 0
    real_qdrant.set_payload(
        collection_name=_COLL,
        payload=memory_update_payload(stale_model),  # excludes access_count / last_accessed_at
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )
    assert _row(real_qdrant, oid).get("access_count") == 1  # leased increment survived


def test_lease_exhaustion_is_fail_loud() -> None:
    with pytest.raises(AccessLeaseExhausted):
        asyncio.run(
            lease_increment_access(
                cast(Any, _AlwaysLiveLeaseClient()), _COLL, {("n/n/episodic", "o")}
            )
        )


@pytest.mark.asyncio
async def test_single_loop_deliveries_stay_correct() -> None:
    """GUARD: within one event loop the sync client blocks across the read→write, so concurrent
    deliveries already serialize — this must stay true (no lease contention needed)."""
    from types import SimpleNamespace

    from musubi.retrieve.accounting import account_delivered

    client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        ns = "eric/dev/episodic"
        row = await EpisodicPlane(client=client, embedder=FakeEmbedder()).create(
            EpisodicMemory(namespace=ns, content="single loop", state="matured")
        )
        d = SimpleNamespace(plane="episodic", object_id=row.object_id, namespace=ns)
        await asyncio.gather(*[account_delivered(client, [d]) for _ in range(5)])
        assert _row(client, row.object_id).get("access_count") == 5
    finally:
        client.close()


# ── the four remaining proofs (RET-008 restart note) ───────────────────────────


class _CommitRacer:
    """Wraps a real client. On the FIRST commit (the batched op that sets ``access_count``) it
    stomps the row's token to an EXPIRED foreign value BEFORE the real fenced commit runs — so our
    exact-held-fenced commit matches ZERO. This models a stall/takeover BETWEEN confirm and commit.
    Thereafter it passes through, so the lease RETRIES, takes over the expired token, and lands
    exactly one increment (no loss, no double)."""

    def __init__(self, inner: QdrantClient, collection: str, object_id: str) -> None:
        self._inner = inner
        self._collection = collection
        self._object_id = object_id
        self._raced = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def batch_update_points(
        self, *, collection_name: str, update_operations: Any, **kw: Any
    ) -> Any:
        is_commit = any(
            isinstance(op, models.SetPayloadOperation)
            and "access_count" in (op.set_payload.payload or {})
            for op in update_operations
        )
        if is_commit and not self._raced:
            self._raced = True
            expired = f"held:{int(time.time() * 1_000_000) - 10_000_000}:racer"
            self._inner.set_payload(
                collection_name=self._collection,
                payload={"access_lease_token": expired},
                points=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="object_id", match=models.MatchValue(value=self._object_id)
                        )
                    ]
                ),
            )
        return self._inner.batch_update_points(
            collection_name=collection_name, update_operations=update_operations, **kw
        )


@pytest.mark.integration
def test_delayed_expiry_between_confirm_and_commit_retries_and_lands_exactly_once(
    real_qdrant: QdrantClient,
) -> None:
    """Proof 1: a stall/takeover BETWEEN confirm and commit makes the fenced commit match zero →
    our ``done`` token is absent → the op does NOT falsely attribute; it RETRIES and still lands
    exactly one increment. The mechanism already handles this; the test makes it an explicit
    discriminator."""
    ns, oid = _seed(real_qdrant)
    racer = cast(QdrantClient, _CommitRacer(real_qdrant, _COLL, oid))
    asyncio.run(lease_increment_access(racer, _COLL, {(ns, oid)}))
    row = _row(real_qdrant, oid)
    assert racer._raced  # type: ignore[attr-defined]  # the mid-commit stall was actually injected
    assert row.get("access_count") == 1  # exactly one — the zero-matched commit did not lose it
    assert row.get("access_lease_token") is None  # released after the retry landed


@pytest.mark.integration
def test_crash_after_done_before_clear_recovers_without_double_count(
    real_qdrant: QdrantClient,
) -> None:
    """Proof 2: a predecessor COMMITTED (its increment landed, ``done`` token written) then crashed
    before clearing. The stale EXPIRED ``done`` token is taken over by the next writer, which does
    its own increment on top — the predecessor's increment is already counted (not double-counted)
    and the stuck ``done`` token is cleared."""
    ns, oid = _seed(real_qdrant)
    # Predecessor committed: increment already landed (count=1), done token written, then crashed.
    real_qdrant.set_payload(
        collection_name=_COLL,
        payload={"access_count": 1},
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )
    expired_done = f"done:{int(time.time() * 1_000_000) - 10_000_000}:crashedcommitter"
    _set_token(real_qdrant, ns, oid, expired_done)

    asyncio.run(lease_increment_access(real_qdrant, _COLL, {(ns, oid)}))
    row = _row(real_qdrant, oid)
    assert row.get("access_count") == 2  # predecessor's 1 + this writer's 1 — no double-count
    assert row.get("access_lease_token") is None  # the stuck done token was cleared


@pytest.mark.integration
def test_dedup_merge_upsert_preserves_leased_access_count(
    real_qdrant: QdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof 3: ``EpisodicPlane.create()``'s dedup-MERGE path re-upserts an existing row via a
    full-point upsert. If the dedup probe read the row BEFORE a concurrent leased increment, the
    merge carries a STALE ``access_count`` and the upsert would reset the increment. The reinforce
    path now reads the lease-owned fields FRESH at upsert time (``preserve_lease_fields``), so the
    leased increment survives. RED before the wiring, GREEN after.

    The stale probe is injected deterministically by pinning ``_find_dedup_candidate`` to a
    pre-bump snapshot — modelling the probe-before / upsert-after race without a live thread."""
    plane = EpisodicPlane(client=real_qdrant, embedder=FakeEmbedder())
    ns, oid = _seed(real_qdrant)  # access_count starts at 0
    stale_existing = EpisodicMemory.model_validate(_row(real_qdrant, oid))  # count == 0 snapshot

    asyncio.run(lease_increment_access(real_qdrant, _COLL, {(ns, oid)}))
    assert _row(real_qdrant, oid).get("access_count") == 1

    # The dedup probe returns the STALE (count==0) candidate as if it read before the bump.
    monkeypatch.setattr(
        plane, "_find_dedup_candidate", lambda namespace, dense: (stale_existing, None, None)
    )
    asyncio.run(
        plane.create(EpisodicMemory(namespace=ns, content="near duplicate", state="matured"))
    )
    assert _row(real_qdrant, oid).get("access_count") == 1  # leased increment survived the merge


def _seed_curated(client: QdrantClient, *, body_hash: str) -> CuratedKnowledge:
    ns = f"lease-{generate_ksuid()[:8].lower()}/dev/curated"
    return asyncio.run(
        CuratedPlane(client=client, embedder=FakeEmbedder()).create(
            CuratedKnowledge(
                namespace=ns,
                content="lease invariant body",
                title="lease invariant",
                vault_path="notes/lease.md",
                body_hash=body_hash,
            )
        )
    )


@pytest.mark.integration
def test_curated_update_upsert_preserves_leased_access_count(real_qdrant: QdrantClient) -> None:
    """Proof 4a: ``CuratedPlane.create()``'s same-id UPDATE path (same ``object_id``, new body)
    re-upserts via a full-point upsert built from the INCOMING model, which carries a DEFAULT
    ``access_count = 0``. Without preservation the upsert resets a leased increment to 0. RED before
    the wiring, GREEN after (reads the stored lease-owned fields fresh). Naturally non-vacuous — the
    incoming model's stale count is real, not injected."""
    row = _seed_curated(real_qdrant, body_hash="a" * 64)
    asyncio.run(
        lease_increment_access(real_qdrant, _CURATED_COLL, {(str(row.namespace), row.object_id)})
    )
    assert _curated_count(real_qdrant, row.object_id) == 1

    # Same object_id + vault_path, DIFFERENT body → the same-id UPDATE path.
    asyncio.run(
        CuratedPlane(client=real_qdrant, embedder=FakeEmbedder()).create(
            CuratedKnowledge(
                object_id=row.object_id,
                namespace=row.namespace,
                content="a different body entirely",
                title="lease invariant",
                vault_path="notes/lease.md",
                body_hash="b" * 64,
            )
        )
    )
    assert _curated_count(real_qdrant, row.object_id) == 1  # leased increment survived the update


def test_transition_patch_never_carries_lease_owned_fields() -> None:
    """Proof 4b: ``CuratedPlane.transition()`` delegates to the lifecycle coordinator, which applies
    only ``_intended_patch`` via a fenced ``set_payload`` MERGE. The patch is state/version/lineage
    only — it structurally CANNOT carry a lease-owned field, so a transition can never reset a
    leased increment. This guards that property: if a lease-owned field is ever added to the patch,
    this fails."""
    intent = TransitionIntent(
        collection=_CURATED_COLL,
        object_id="obj-1",
        namespace="n/dev/curated",
        expected_version=1,
        target_state="matured",
        actor="tester",
        reason="proof",
        updated_at="2026-07-15T00:00:00Z",
        updated_epoch=1,
        superseded_by=None,
        supersedes=(),
        merged_from=(),
        contradicts=(),
        promoted_to=None,
        promoted_at=None,
    )
    patch = _intended_patch(intent)
    assert LEASE_OWNED_FIELDS.isdisjoint(patch.keys())
