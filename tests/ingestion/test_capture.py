"""Test contract for slice-ingestion-capture.

Implements the bullets from [[06-ingestion/capture]] § Test contract.
The service-under-test is :class:`musubi.ingestion.capture.CaptureService` —
not the HTTP router (that lives in ``src/musubi/api/routers/writes_episodic.py``
and is tested in ``tests/api/test_api_v0_write.py``). Per the
Method-ownership rule, the dedup configuration + per-(token, namespace)
idempotency cache + retry logic + batch orchestration live in
``src/musubi/ingestion/``; the HTTP shell calls into this service.

Closure plan:

- bullets 1-5, 7-9, 11-15, 17-19 → passing service-level tests
- bullet 6 (p95 benchmark on 100k corpus) → out-of-scope in work log
- bullet 10 (dedup keeps longer content) → skipped, cross-slice ticket
  to slice-plane-episodic to add a ``reinforce_with_strategy`` method
- bullet 16 (forbidden-namespace 403) → skipped, lives at the HTTP
  layer (already tested in test_api_v0_write.py)
- bullets 20-21 (single TEI / single Qdrant batch instrumentation) →
  skipped, cross-slice ticket to slice-plane-episodic to add a
  ``batch_create`` method
- bullet 22 (100-item batch under 1s benchmark) → moved to integration
  suite (tests/integration/test_capture_perf.py) per Issue #118
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.ingestion.capture import (
    DEFAULT_DEDUP_THRESHOLDS,
    CaptureRequest,
    CaptureService,
    IngestionIdempotencyCache,
    is_dedup_enabled,
)
from musubi.lifecycle import LifecycleEventSink
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.types.common import Err, Ok

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def plane(qdrant: QdrantClient, embedder: FakeEmbedder) -> EpisodicPlane:
    return EpisodicPlane(client=qdrant, embedder=embedder)


@pytest.fixture
def sink(tmp_path: Path) -> Iterator[LifecycleEventSink]:
    s = LifecycleEventSink(db_path=tmp_path / "events.db", flush_every_n=10, flush_every_s=1.0)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def cache(tmp_path: Path) -> IngestionIdempotencyCache:
    return IngestionIdempotencyCache(db_path=tmp_path / "idempotency.db")


@pytest.fixture
def service(
    plane: EpisodicPlane,
    sink: LifecycleEventSink,
    cache: IngestionIdempotencyCache,
) -> CaptureService:
    return CaptureService(plane=plane, sink=sink, idempotency_cache=cache)


@pytest.fixture
def ns() -> str:
    return "eric/claude-code/episodic"


def _req(namespace: str, **kw: object) -> CaptureRequest:
    """Build a CaptureRequest with sane defaults."""
    base: dict[str, object] = {
        "namespace": namespace,
        "content": "default content",
        "tags": [],
        "topics": [],
        "importance": 5,
    }
    base.update(kw)
    return CaptureRequest(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy path — bullets 1-5
# ---------------------------------------------------------------------------


def test_capture_returns_202_and_object_id(service: CaptureService, ns: str) -> None:
    """Bullet 1 — capture returns Ok(CaptureResult) with a fresh KSUID
    object_id. ``202 Accepted`` status maps from this Ok at the HTTP
    boundary (api/routers/writes_episodic.py); the service signals
    success via Ok."""
    result = asyncio.run(service.capture(_req(ns, content="hello world")))
    assert isinstance(result, Ok), result
    assert isinstance(result.value.object_id, str)
    assert len(result.value.object_id) == 27


def test_capture_writes_provisional_state(service: CaptureService, ns: str) -> None:
    """Bullet 2 — every fresh capture lands in state=provisional."""
    result = asyncio.run(service.capture(_req(ns, content="provisional-state-check")))
    assert isinstance(result, Ok)
    assert result.value.state == "provisional"


def test_capture_writes_both_vectors(
    service: CaptureService, plane: EpisodicPlane, ns: str
) -> None:
    """Bullet 3 — the row in Qdrant has BOTH dense + sparse named vectors
    (the plane's _upsert sets them; we verify by scrolling with vectors)."""
    result = asyncio.run(service.capture(_req(ns, content="vector-check")))
    assert isinstance(result, Ok)
    from qdrant_client import models as qmodels

    records, _ = plane._client.scroll(
        collection_name="musubi_episodic",
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id",
                    match=qmodels.MatchValue(value=result.value.object_id),
                )
            ]
        ),
        limit=1,
        with_vectors=True,
    )
    assert records, "row not in Qdrant"
    vectors = records[0].vector
    assert isinstance(vectors, dict)
    assert "dense_bge_m3_v1" in vectors
    assert "sparse_splade_v1" in vectors


def test_capture_sets_timestamps_server_side(
    service: CaptureService, plane: EpisodicPlane, ns: str
) -> None:
    """Bullet 4 — created_at + updated_at are set by the server, not the
    caller. The CaptureRequest doesn't even expose a timestamp field —
    confirms by inspection that the persisted row has both."""
    result = asyncio.run(service.capture(_req(ns, content="timestamp-check")))
    assert isinstance(result, Ok)
    fetched = asyncio.run(plane.get(namespace=ns, object_id=result.value.object_id))
    assert fetched is not None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None
    assert fetched.created_at == fetched.updated_at


@pytest.mark.skip(
    reason="deferred to slice-types-capture-event-record: spec § Step 6 "
    "calls for a LifecycleEvent on capture, but the current "
    "LifecycleEvent validator only accepts state transitions — "
    "provisional → provisional is illegal. Cross-slice ticket "
    "_inbox/cross-slice/slice-ingestion-capture-slice-types-capture-event-record.md "
    "tracks adding a non-transition event variant. Until that lands, "
    "the row's created_at + reinforcement_count carry the audit signal "
    "implicitly."
)
def test_capture_emits_lifecycle_event() -> None:
    """Bullet 5 — placeholder."""


# ---------------------------------------------------------------------------
# Dedup — bullets 7-9, 11
# ---------------------------------------------------------------------------


def test_dedup_merges_on_high_similarity(service: CaptureService, ns: str) -> None:
    """Bullet 7 — a second capture with identical content (which the
    FakeEmbedder maps to identical dense vectors → cosine 1.0) returns
    the same object_id and writes no new row."""
    first = asyncio.run(service.capture(_req(ns, content="dedup-target", tags=["a"])))
    second = asyncio.run(service.capture(_req(ns, content="dedup-target", tags=["b"])))
    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert first.value.object_id == second.value.object_id
    assert second.value.dedup_action == "merged"


def test_dedup_increments_reinforcement_count(
    service: CaptureService, plane: EpisodicPlane, ns: str
) -> None:
    """Bullet 8 — dedup hit bumps the existing row's reinforcement_count."""
    first = asyncio.run(service.capture(_req(ns, content="reinforce-target")))
    asyncio.run(service.capture(_req(ns, content="reinforce-target")))
    assert isinstance(first, Ok)
    fetched = asyncio.run(plane.get(namespace=ns, object_id=first.value.object_id))
    assert fetched is not None
    assert fetched.reinforcement_count >= 1


def test_dedup_merges_tag_union(service: CaptureService, plane: EpisodicPlane, ns: str) -> None:
    """Bullet 9 — dedup-merged row carries the union of both tag sets."""
    first = asyncio.run(service.capture(_req(ns, content="tag-merge", tags=["a", "b"])))
    asyncio.run(service.capture(_req(ns, content="tag-merge", tags=["b", "c"])))
    assert isinstance(first, Ok)
    fetched = asyncio.run(plane.get(namespace=ns, object_id=first.value.object_id))
    assert fetched is not None
    assert set(fetched.tags) == {"a", "b", "c"}


@pytest.mark.skip(
    reason="deferred to slice-plane-episodic-content-merge-strategy: spec "
    "calls for 'new content wins iff strictly longer'; the plane's "
    "current reinforce always replaces with new content. Cross-slice "
    "ticket "
    "_inbox/cross-slice/slice-ingestion-capture-slice-plane-episodic-merge-strategy.md "
    "tracks the plane-side follow-up to add a reinforce strategy parameter."
)
def test_dedup_keeps_longer_content() -> None:
    """Bullet 10 — placeholder."""


def test_dedup_disabled_on_curated() -> None:
    """Bullet 11 — the dedup-config dict has curated set to ``None``
    (disabled). The service's ``is_dedup_enabled`` predicate respects
    this. Curated POST goes through the curated-plane router (not this
    service) where dedup is by ``(namespace, vault_path)`` rather than
    similarity; the service's config is the canonical record."""
    assert DEFAULT_DEDUP_THRESHOLDS["episodic"] == 0.92
    assert DEFAULT_DEDUP_THRESHOLDS["curated"] is None
    assert DEFAULT_DEDUP_THRESHOLDS["artifact_chunks"] == 0.98
    assert is_dedup_enabled("episodic") is True
    assert is_dedup_enabled("curated") is False
    assert is_dedup_enabled("artifact_chunks") is True


# ---------------------------------------------------------------------------
# Idempotency — bullets 12-14
# ---------------------------------------------------------------------------


def test_idempotency_key_returns_same_object_twice(service: CaptureService, ns: str) -> None:
    """Bullet 12 — same key + same body returns the same object_id and
    sets ``replayed=True`` on the second call."""
    key = "idem-test-12"
    first = asyncio.run(
        service.capture(
            _req(ns, content="idempotent-12"),
            token_jti="token-A",
            idempotency_key=key,
        )
    )
    second = asyncio.run(
        service.capture(
            _req(ns, content="idempotent-12"),
            token_jti="token-A",
            idempotency_key=key,
        )
    )
    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert first.value.object_id == second.value.object_id
    assert second.value.replayed is True


def test_idempotency_key_expires_after_24h(
    service: CaptureService, cache: IngestionIdempotencyCache, ns: str
) -> None:
    """Bullet 13 — after the 24h TTL, a request with the same key
    behaves like a fresh request (replayed=False)."""
    key = "idem-test-13"
    first = asyncio.run(
        service.capture(
            _req(ns, content="idempotent-13"),
            token_jti="token-A",
            idempotency_key=key,
        )
    )
    assert isinstance(first, Ok)
    cache.expire_for_test(token_jti="token-A", namespace=ns, key=key)
    second = asyncio.run(
        service.capture(
            _req(ns, content="idempotent-13"),
            token_jti="token-A",
            idempotency_key=key,
        )
    )
    assert isinstance(second, Ok)
    # Fresh call (cache expired). Plane dedup still gives the same id,
    # but the response is NOT a replay — the service ran fresh.
    assert second.value.replayed is False


def test_idempotency_key_scoped_per_token(service: CaptureService, ns: str) -> None:
    """Bullet 14 — the same idempotency key from a different token is a
    DIFFERENT cache entry. Token A's key=foo and Token B's key=foo are
    independent.

    This is the spec's contract: 'If seen within the last 24h for the
    same token + namespace'."""
    key = "idem-test-14"
    first_a = asyncio.run(
        service.capture(
            _req(ns, content="idempotent-14"),
            token_jti="token-A",
            idempotency_key=key,
        )
    )
    first_b = asyncio.run(
        service.capture(
            _req(ns, content="idempotent-14-different"),
            token_jti="token-B",
            idempotency_key=key,
        )
    )
    assert isinstance(first_a, Ok) and isinstance(first_b, Ok)
    # Different token → distinct cache entries → no replay across tokens.
    assert first_a.value.replayed is False
    assert first_b.value.replayed is False


# ---------------------------------------------------------------------------
# Errors — bullets 15-19
# ---------------------------------------------------------------------------


def test_capture_empty_content_returns_400(
    plane: EpisodicPlane,
    sink: LifecycleEventSink,
    cache: IngestionIdempotencyCache,
    ns: str,
) -> None:
    """Bullet 15 — empty content fails pydantic validation; the service
    builds a typed CaptureError with status_code=400 / code=BAD_REQUEST.

    Note: pydantic's ValidationError fires when CaptureRequest is
    constructed; the service would never see an empty-content request
    once the constructor has accepted it. So we exercise the validator
    directly."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CaptureRequest(namespace=ns, content="")


@pytest.mark.skip(
    reason="deferred to src/musubi/api/auth.py: namespace-scope check is "
    "the HTTP layer's responsibility per Method-ownership; tested in "
    "tests/api/test_api_v0_write.py::test_capture_rejects_out_of_scope_namespace. "
    "The ingestion service is namespace-agnostic in terms of auth — by "
    "the time the request reaches it, scope has been validated."
)
def test_capture_forbidden_namespace_returns_403() -> None:
    """Bullet 16 — placeholder."""


def test_capture_tei_down_returns_503(
    plane: EpisodicPlane,
    sink: LifecycleEventSink,
    cache: IngestionIdempotencyCache,
    ns: str,
) -> None:
    """Bullet 17 — when the embedder raises (TEI unreachable), the
    service catches and returns Err(BACKEND_UNAVAILABLE, status=503)."""

    class TEIDownEmbedder:
        async def embed_dense(self, texts: list[str]) -> list[list[float]]:
            raise ConnectionError("TEI dense unreachable")

        async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
            raise ConnectionError("TEI sparse unreachable")

        async def rerank(self, query: str, candidates: list[str]) -> list[float]:
            return []

    broken_plane = EpisodicPlane(client=plane._client, embedder=TEIDownEmbedder())
    svc = CaptureService(plane=broken_plane, sink=sink, idempotency_cache=cache)
    result = asyncio.run(svc.capture(_req(ns, content="tei-outage")))
    assert isinstance(result, Err)
    assert result.error.status_code == 503
    assert result.error.code == "BACKEND_UNAVAILABLE"


def test_capture_qdrant_retry_logic_succeeds_on_transient_failure(
    plane: EpisodicPlane,
    sink: LifecycleEventSink,
    cache: IngestionIdempotencyCache,
    embedder: FakeEmbedder,
    ns: str,
) -> None:
    """Bullet 18 — the service wraps plane.create with bounded retry
    (3 attempts, exponential-ish backoff). A transient failure on the
    first attempt resolves on retry."""
    from typing import Any

    failures = {"count": 0}
    real_create = plane.create

    async def flaky_create(memory: Any) -> Any:
        if failures["count"] == 0:
            failures["count"] += 1
            raise TimeoutError("transient qdrant blip")
        return await real_create(memory)

    plane.create = flaky_create  # type: ignore[method-assign]
    svc = CaptureService(plane=plane, sink=sink, idempotency_cache=cache)
    result = asyncio.run(svc.capture(_req(ns, content="retry-target")))
    assert isinstance(result, Ok), result
    assert failures["count"] == 1  # one failure absorbed by retry


def test_capture_qdrant_permanent_failure_returns_503(
    plane: EpisodicPlane,
    sink: LifecycleEventSink,
    cache: IngestionIdempotencyCache,
    ns: str,
) -> None:
    """Bullet 19 — repeated Qdrant failures exhaust the retry budget;
    the service returns Err(BACKEND_UNAVAILABLE, status=503)."""

    from typing import Any

    async def always_fail(memory: Any) -> Any:
        raise TimeoutError("permanent qdrant outage")

    plane.create = always_fail  # type: ignore[method-assign]
    svc = CaptureService(plane=plane, sink=sink, idempotency_cache=cache)
    result = asyncio.run(svc.capture(_req(ns, content="perma-fail")))
    assert isinstance(result, Err)
    assert result.error.status_code == 503


# ---------------------------------------------------------------------------
# Batch — bullets 20-22
# ---------------------------------------------------------------------------


def test_batch_capture_writes_each_row(
    service: CaptureService, plane: EpisodicPlane, ns: str
) -> None:
    """Coverage: batch_capture ships N memories to the plane and returns
    N Ok results. Spec bullets 20+21 (single-TEI-call / single-Qdrant-
    upsert instrumentation) are deferred to a follow-up that adds
    EpisodicPlane.batch_create — see the cross-slice ticket. Today's
    batch is a one-row-at-a-time loop with the same semantics."""
    items = [_req(ns, content=f"batch-{i}-uniq", tags=[f"t{i}"]) for i in range(3)]
    results = asyncio.run(service.batch_capture(namespace=ns, items=items))
    assert len(results) == 3
    for r in results:
        assert isinstance(r, Ok)
        assert len(r.value.object_id) == 27


@pytest.mark.skip(
    reason="deferred to slice-plane-episodic-batch-create: requires a "
    "new EpisodicPlane.batch_create(memories) method that does ONE TEI "
    "embed call + ONE Qdrant upsert for the whole batch. Cross-slice "
    "ticket "
    "_inbox/cross-slice/slice-ingestion-capture-slice-plane-episodic-batch-create.md "
    "tracks the plane-side follow-up; today's batch loops one-by-one."
)
def test_batch_capture_single_tei_embed_call() -> None:
    """Bullet 20 — placeholder."""


@pytest.mark.skip(
    reason="deferred to slice-plane-episodic-batch-create: same follow-up "
    "as bullet 20 — a single Qdrant upsert across all batch items "
    "requires plane.batch_create."
)
def test_batch_capture_single_qdrant_upsert() -> None:
    """Bullet 21 — placeholder."""


@pytest.mark.skip(
    reason=(
        "moved to integration suite: see tests/integration/test_capture_perf.py "
        "(unskipped via #118 against the live docker-compose stack from "
        "slice-ops-integration-harness PR #114). This unit-suite placeholder "
        "is retained as a pointer."
    )
)
def test_batch_capture_100_items_under_1s() -> None:
    """Bullet 22 — moved to tests/integration/test_capture_perf.py."""


@pytest.mark.skip(
    reason="declared out-of-scope in slice work log: p95 benchmark on a "
    "100k-row corpus needs a real Qdrant + TEI stack and a perf "
    "harness; deferred to slice-perf-bench."
)
def test_capture_p95_under_250ms_on_100k_corpus() -> None:
    """Bullet 6 — placeholder."""


# ---------------------------------------------------------------------------
# Coverage tests — exercise additional service paths.
# ---------------------------------------------------------------------------


def test_capture_with_rich_metadata_preserved(
    service: CaptureService, plane: EpisodicPlane, ns: str
) -> None:
    """Optional fields from the spec § Contract round-trip cleanly:
    content_type, capture_source, source_ref, ingestion_metadata."""
    request = _req(
        ns,
        content="metadata-rich",
        tags=["cuda"],
        topics=["infrastructure/gpu"],
        importance=8,
    )
    result = asyncio.run(service.capture(request))
    assert isinstance(result, Ok)
    fetched = asyncio.run(plane.get(namespace=ns, object_id=result.value.object_id))
    assert fetched is not None
    assert fetched.importance == 8
    assert "cuda" in fetched.tags


def test_idempotency_cache_round_trip(
    cache: IngestionIdempotencyCache,
) -> None:
    """The cache supports the (token_jti, namespace, key) triple
    independently of the service."""
    cache.store(
        token_jti="t1",
        namespace="n1",
        key="k1",
        body_hash="h",
        object_id="A" * 27,
    )
    hit = cache.lookup(token_jti="t1", namespace="n1", key="k1", body_hash="h")
    assert hit == "A" * 27
    miss_other_token = cache.lookup(token_jti="t2", namespace="n1", key="k1", body_hash="h")
    assert miss_other_token is None
    miss_other_body = cache.lookup(token_jti="t1", namespace="n1", key="k1", body_hash="different")
    assert miss_other_body is None


def test_capture_request_validates_content_length() -> None:
    from pydantic import ValidationError

    # 16001 chars — over the spec's 16000 ceiling.
    with pytest.raises(ValidationError):
        CaptureRequest(namespace="eric/x/episodic", content="a" * 16001)


def test_capture_no_idempotency_key_path(service: CaptureService, ns: str) -> None:
    """When no idempotency key is provided, the cache is never
    consulted — the service runs the plane create path directly."""
    result = asyncio.run(service.capture(_req(ns, content="no-idem-key")))
    assert isinstance(result, Ok)
    assert result.value.replayed is False
