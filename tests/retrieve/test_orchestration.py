"""RET-010 — close legacy orchestration Test Contract gaps (Issue #509).

Named bullets from ``docs/Musubi/05-retrieval/orchestration.md`` ``## Test Contract``.
Function names match the bullets verbatim (Closure Rule).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from musubi.embedding.base import EmbeddingError
from musubi.embedding.fake import FakeEmbedder
from musubi.retrieve.deep import DeepResult
from musubi.retrieve.fast import FastHit, FastRetrieveResult
from musubi.retrieve.hybrid import HybridHit, HybridSearchResult
from musubi.retrieve.orchestration import (
    NamespaceTarget,
    RetrievalQuery,
    retrieve,
)
from musubi.retrieve.rerank import RerankResult
from musubi.retrieve.scoring import ScoreComponents, ScoredHit
from musubi.retrieve.warnings import reranker_failed
from musubi.types.common import Err, Ok, generate_ksuid

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockQdrant:
    def query_points(self, *args: Any, **kwargs: Any) -> Any:
        return type("R", (), {"points": []})()

    def batch_update_points(self, *args: Any, **kwargs: Any) -> Any:
        return None


class _TrackingReranker:
    def __init__(self) -> None:
        self.calls = 0
        self.should_hang = False
        self.hang_s = 2.0

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        self.calls += 1
        if self.should_hang:
            await asyncio.sleep(self.hang_s)
        return [float(1.0 - i * 0.01) for i in range(len(texts))]


def _score_components(**overrides: float) -> ScoreComponents:
    base = {
        "relevance": 0.5,
        "recency": 0.5,
        "importance": 0.5,
        "provenance": 0.5,
        "reinforce": 0.0,
    }
    base.update(overrides)
    return ScoreComponents(**base)


def _fast_hit(object_id: str, *, score: float = 0.5, plane: str = "episodic") -> FastHit:
    return FastHit(
        object_id=object_id,
        score=score,
        score_components=_score_components(relevance=score),
        payload={
            "namespace": "eric/test/episodic",
            "plane": plane,
            "state": "matured",
            "importance": 5,
            "content": f"content for {object_id}",
        },
        snippet=f"snippet {object_id}",
        lineage_summary={"promoted_from": None, "promoted_to": None},
        raw_rrf_score=score,
        raw_rerank_score=None,
    )


def _scored_hit(object_id: str, *, score: float = 0.5, plane: str = "episodic") -> ScoredHit:
    return ScoredHit(
        object_id=object_id,
        plane=plane,
        state="matured",
        score=score,
        score_components=_score_components(relevance=score),
        payload={
            "namespace": f"eric/test/{plane}",
            "plane": plane,
            "state": "matured",
            "importance": 5,
            "content": f"content for {object_id}",
        },
        raw_rrf_score=score,
        raw_rerank_score=score,
    )


async def _retrieve(
    *,
    mode: str,
    reranker: Any | None = None,
    account_access: bool = False,
    **q: Any,
) -> Any:
    query = RetrievalQuery(
        namespace=q.pop("namespace", "eric/test/episodic"),
        query_text=q.pop("query_text", "gpu"),
        mode=cast(Any, mode),
        planes=q.pop("planes", ["episodic"]),
        **q,
    )
    return await retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        reranker=reranker,
        query=query,
        account_access=account_access,
        now=1_700_000_000.0,
    )


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_mode_skips_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast mode must dispatch ``run_fast_retrieve`` and never call rerank."""
    calls: dict[str, int] = {"fast": 0, "deep": 0, "rerank": 0}
    reranker = _TrackingReranker()

    async def fake_fast(*args: Any, **kwargs: Any) -> Any:
        calls["fast"] += 1
        return Ok(
            value=FastRetrieveResult(results=[_fast_hit("oid-1")], warnings=()),
        )

    async def fake_deep(*args: Any, **kwargs: Any) -> Any:
        calls["deep"] += 1
        return Ok(value=DeepResult(hits=[], warnings=()))

    async def fake_rerank(*args: Any, **kwargs: Any) -> Any:
        calls["rerank"] += 1
        raise AssertionError("rerank must not be invoked on fast mode")

    monkeypatch.setattr("musubi.retrieve.orchestration.run_fast_retrieve", fake_fast)
    monkeypatch.setattr("musubi.retrieve.orchestration.run_deep_retrieve", fake_deep)
    monkeypatch.setattr("musubi.retrieve.deep.rerank", fake_rerank)
    monkeypatch.setattr("musubi.retrieve.rerank.rerank", fake_rerank)

    result = await _retrieve(mode="fast", reranker=reranker)
    assert isinstance(result, Ok)
    assert calls["fast"] == 1
    assert calls["deep"] == 0
    assert calls["rerank"] == 0
    assert reranker.calls == 0


@pytest.mark.asyncio
async def test_deep_mode_invokes_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real deep path: mocked hybrid candidates must call ``deep.rerank``."""
    rerank_calls = {"n": 0}

    async def fake_hybrid(*args: Any, **kwargs: Any) -> Any:
        # >5 candidates so the production rerank gate does not short-circuit.
        return Ok(value=HybridSearchResult(hits=_hybrid_hits(6), warnings=()))

    async def tracking_rerank(*args: Any, **kwargs: Any) -> Any:
        rerank_calls["n"] += 1
        candidates = kwargs.get("candidates")
        if candidates is None and len(args) >= 3:
            candidates = args[2]
        assert candidates is not None
        return RerankResult(
            hits=list(candidates)[: kwargs.get("top_k", len(candidates))], warnings=()
        )

    monkeypatch.setattr("musubi.retrieve.deep.hybrid_search", fake_hybrid)
    monkeypatch.setattr("musubi.retrieve.deep.rerank", tracking_rerank)
    monkeypatch.setattr(
        "musubi.retrieve.deep._hydrate_one",
        AsyncMock(side_effect=lambda hit, *a, **k: hit),
    )

    result = await _retrieve(mode="deep", reranker=_TrackingReranker(), limit=10)
    assert isinstance(result, Ok)
    assert rerank_calls["n"] == 1


@pytest.mark.asyncio
async def test_fast_mode_skips_lineage_hydrate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast mode must not enter the deep hydrate path."""
    hydrate_calls = {"n": 0}

    async def fake_fast(*args: Any, **kwargs: Any) -> Any:
        return Ok(
            value=FastRetrieveResult(
                results=[_fast_hit("oid-1")],
                warnings=(),
            )
        )

    async def fake_hydrate(*args: Any, **kwargs: Any) -> Any:
        hydrate_calls["n"] += 1
        raise AssertionError("hydrate must not run on fast mode")

    monkeypatch.setattr("musubi.retrieve.orchestration.run_fast_retrieve", fake_fast)
    monkeypatch.setattr("musubi.retrieve.deep._hydrate_one", fake_hydrate)

    result = await _retrieve(mode="fast", reranker=_TrackingReranker())
    assert isinstance(result, Ok)
    assert hydrate_calls["n"] == 0
    # Fast packs the lineage_summary from the hit, not a hydrated object.
    assert result.value.results[0].lineage == {
        "promoted_from": None,
        "promoted_to": None,
    }


@pytest.mark.asyncio
async def test_deep_mode_hydrates_when_flag_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real deep path: ``include_lineage=True`` must invoke ``_hydrate_one``."""
    hydrate_calls = {"n": 0}

    async def fake_hybrid(*args: Any, **kwargs: Any) -> Any:
        return Ok(value=HybridSearchResult(hits=_hybrid_hits(3), warnings=()))

    async def tracking_hydrate(hit: Any, *args: Any, **kwargs: Any) -> Any:
        hydrate_calls["n"] += 1
        return hit

    monkeypatch.setattr("musubi.retrieve.deep.hybrid_search", fake_hybrid)
    monkeypatch.setattr("musubi.retrieve.deep._hydrate_one", tracking_hydrate)

    result = await _retrieve(
        mode="deep",
        reranker=_TrackingReranker(),
        include_lineage=True,
        limit=10,
    )
    assert isinstance(result, Ok)
    assert hydrate_calls["n"] == 3

    # Control: flag false must not hydrate.
    hydrate_calls["n"] = 0
    result_off = await _retrieve(
        mode="deep",
        reranker=_TrackingReranker(),
        include_lineage=False,
        limit=10,
    )
    assert isinstance(result_off, Ok)
    assert hydrate_calls["n"] == 0


def _hybrid_hits(n: int = 6) -> list[HybridHit]:
    return [
        HybridHit(
            object_id=f"oid-{i}",
            score=1.0 - i * 0.01,
            payload={
                "namespace": "eric/test/episodic",
                "plane": "episodic",
                "state": "matured",
                "content": f"Hit {i}",
                "updated_epoch": 1_700_000_000.0,
                "importance": 5,
            },
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_steps_run_in_documented_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instrument deep internals: hybrid → rerank → hydrate order for deep mode."""
    order: list[str] = []

    async def fake_hybrid(*args: Any, **kwargs: Any) -> Any:
        order.append("hybrid")
        return Ok(value=HybridSearchResult(hits=_hybrid_hits(6), warnings=()))

    async def tracking_rerank(*args: Any, **kwargs: Any) -> Any:
        order.append("rerank")
        candidates = kwargs.get("candidates")
        if candidates is None and len(args) >= 3:
            candidates = args[2]
        assert candidates is not None
        return RerankResult(hits=list(candidates), warnings=())

    async def fake_hydrate(hit: Any, *args: Any, **kwargs: Any) -> Any:
        order.append("hydrate")
        return hit

    monkeypatch.setattr("musubi.retrieve.deep.hybrid_search", fake_hybrid)
    monkeypatch.setattr("musubi.retrieve.deep.rerank", tracking_rerank)
    monkeypatch.setattr("musubi.retrieve.deep._hydrate_one", fake_hydrate)

    result = await _retrieve(
        mode="deep",
        reranker=_TrackingReranker(),
        include_lineage=True,
        limit=10,
    )
    assert isinstance(result, Ok)
    # Documented deep order: hybrid fan-out, then rerank, then lineage hydrate.
    assert "hybrid" in order
    assert "rerank" in order
    assert "hydrate" in order
    assert order.index("hybrid") < order.index("rerank") < order.index("hydrate")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planes_run_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-target fanout must overlap plane work (asyncio.gather)."""
    started: list[float] = []
    finished: list[float] = []

    async def slow_fast(*args: Any, **kwargs: Any) -> Any:
        started.append(time.monotonic())
        await asyncio.sleep(0.05)
        finished.append(time.monotonic())
        oid = generate_ksuid()
        return Ok(value=FastRetrieveResult(results=[_fast_hit(str(oid))], warnings=()))

    monkeypatch.setattr("musubi.retrieve.orchestration.run_fast_retrieve", slow_fast)

    query = RetrievalQuery(
        namespace="eric/test",
        query_text="gpu",
        mode="fast",
        namespace_targets=[
            NamespaceTarget(namespace="eric/test/episodic", plane="episodic"),
            NamespaceTarget(namespace="eric/test/curated", plane="curated"),
            NamespaceTarget(namespace="eric/test/concept", plane="concept"),
        ],
    )
    t0 = time.monotonic()
    result = await retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        query=query,
        account_access=False,
        now=1_700_000_000.0,
    )
    elapsed = time.monotonic() - t0
    assert isinstance(result, Ok)
    assert len(started) == 3
    # If sequential: ~0.15s+. Overlap should finish well under 0.14s.
    assert elapsed < 0.14, f"expected parallel fanout, elapsed={elapsed:.3f}s"
    # First start and last start should be close (all scheduled together).
    assert max(started) - min(started) < 0.04


@pytest.mark.asyncio
async def test_hydrate_fetches_run_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deep lineage hydrate must gather concurrent ``_hydrate_one`` calls."""
    started: list[float] = []

    async def fake_hybrid(*args: Any, **kwargs: Any) -> Any:
        return Ok(value=HybridSearchResult(hits=_hybrid_hits(3), warnings=()))

    async def slow_hydrate(hit: Any, *args: Any, **kwargs: Any) -> Any:
        started.append(time.monotonic())
        await asyncio.sleep(0.05)
        return hit

    # Skip rerank for tiny sets (<=5) — deep still hydrates after rank.
    monkeypatch.setattr("musubi.retrieve.deep.hybrid_search", fake_hybrid)
    monkeypatch.setattr("musubi.retrieve.deep._hydrate_one", slow_hydrate)

    t0 = time.monotonic()
    result = await _retrieve(
        mode="deep",
        reranker=_TrackingReranker(),
        include_lineage=True,
        limit=10,
    )
    elapsed = time.monotonic() - t0
    assert isinstance(result, Ok)
    assert len(started) == 3
    assert elapsed < 0.14, f"expected parallel hydrate, elapsed={elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whole_call_timeout_fast_400ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orchestration wraps fast in ``asyncio.wait_for(..., 0.400)``."""

    async def slow_fast(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.55)
        return Ok(value=FastRetrieveResult(results=[], warnings=()))

    monkeypatch.setattr("musubi.retrieve.orchestration.run_fast_retrieve", slow_fast)

    result = await _retrieve(mode="fast", reranker=_TrackingReranker())
    assert isinstance(result, Err)
    assert result.error.kind == "timeout"


@pytest.mark.asyncio
async def test_per_plane_timeout_deep_1500ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deep must pass ``timeout_s=1.5`` into hybrid (spec per-plane budget)."""
    seen: dict[str, Any] = {}

    async def capturing_hybrid(*args: Any, **kwargs: Any) -> Any:
        seen["timeout_s"] = kwargs.get("timeout_s")
        from musubi.retrieve.hybrid import RetrievalError as HybridError

        return Err(error=HybridError(code="qdrant_timeout", detail="hybrid timed out"))

    monkeypatch.setattr("musubi.retrieve.deep.hybrid_search", capturing_hybrid)

    result = await _retrieve(mode="deep", reranker=_TrackingReranker())
    assert seen["timeout_s"] == 1.5
    assert isinstance(result, Err)
    assert result.error.kind == "timeout"


@pytest.mark.asyncio
async def test_rerank_timeout_returns_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rerank TEI failure/timeout must degrade with ``reranker_failed``, not hard-fail."""

    async def fake_hybrid(*args: Any, **kwargs: Any) -> Any:
        return Ok(value=HybridSearchResult(hits=_hybrid_hits(6), warnings=()))

    class BoomReranker:
        async def rerank(self, query: str, texts: list[str]) -> list[float]:
            raise EmbeddingError("rerank timed out", status_code=None)

    monkeypatch.setattr("musubi.retrieve.deep.hybrid_search", fake_hybrid)
    monkeypatch.setattr(
        "musubi.retrieve.deep._hydrate_one",
        AsyncMock(side_effect=lambda hit, *a, **k: hit),
    )

    result = await _retrieve(
        mode="deep",
        reranker=BoomReranker(),
        include_lineage=False,
        limit=10,
    )
    assert isinstance(result, Ok)
    codes = {w.code for w in result.value.warnings}
    assert "reranker_failed" in codes or any(
        w.code == reranker_failed("episodic").code for w in result.value.warnings
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_for_fixed_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same mocked hits + same ``now`` → identical ranked object_id sequence."""

    async def fake_fast(*args: Any, **kwargs: Any) -> Any:
        return Ok(
            value=FastRetrieveResult(
                results=[
                    _fast_hit("b-oid", score=0.9),
                    _fast_hit("a-oid", score=0.8),
                    _fast_hit("c-oid", score=0.7),
                ],
                warnings=(),
            )
        )

    monkeypatch.setattr("musubi.retrieve.orchestration.run_fast_retrieve", fake_fast)

    r1 = await _retrieve(mode="fast")
    r2 = await _retrieve(mode="fast")
    assert isinstance(r1, Ok) and isinstance(r2, Ok)
    ids1 = [h.object_id for h in r1.value.results]
    ids2 = [h.object_id for h in r2.value.results]
    assert ids1 == ids2 == ["b-oid", "a-oid", "c-oid"]


@pytest.mark.asyncio
async def test_tiebreak_on_object_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal scores at the cross-plane merge sort by ``object_id`` then ``plane``."""

    async def fake_fast(*args: Any, **kwargs: Any) -> Any:
        # Each leg returns one hit; equal scores so merge tiebreak decides.
        # ``collections`` / plane identity comes from the target.
        plane = kwargs.get("collections", ["musubi_episodic"])[0].removeprefix("musubi_")
        oid = {"episodic": "oid-m", "curated": "oid-a", "concept": "oid-z"}[plane]
        return Ok(
            value=FastRetrieveResult(
                results=[_fast_hit(oid, score=0.5, plane=plane)],
                warnings=(),
            )
        )

    monkeypatch.setattr("musubi.retrieve.orchestration.run_fast_retrieve", fake_fast)

    query = RetrievalQuery(
        namespace="eric/test",
        query_text="gpu",
        mode="fast",
        namespace_targets=[
            NamespaceTarget(namespace="eric/test/concept", plane="concept"),
            NamespaceTarget(namespace="eric/test/episodic", plane="episodic"),
            NamespaceTarget(namespace="eric/test/curated", plane="curated"),
        ],
    )
    result = await retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        query=query,
        account_access=False,
        now=1_700_000_000.0,
    )
    assert isinstance(result, Ok)
    ids = [h.object_id for h in result.value.results]
    assert ids == sorted(ids), f"expected object_id tiebreak ascending, got {ids}"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_query_returns_typed_error() -> None:
    res = await retrieve(
        client=AsyncMock(),
        embedder=AsyncMock(),
        query={"namespace": "ns", "query_text": "", "limit": 0},
        account_access=False,
    )
    assert isinstance(res, Err)
    assert res.error.kind == "bad_query"


@pytest.mark.skip(
    reason=(
        "deferred to slice-api-v0 / router authz: forbidden-namespace is enforced "
        "at the HTTP auth boundary, not inside orchestration.retrieve"
    )
)
def test_forbidden_namespace_returns_typed_error() -> None:
    pass


@pytest.mark.asyncio
async def test_partial_plane_failure_returns_partial_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One timed-out target must not blank survivors; warning carries the plane."""
    call_n = {"i": 0}

    async def flaky_fast(*args: Any, **kwargs: Any) -> Any:
        call_n["i"] += 1
        if call_n["i"] == 1:
            await asyncio.sleep(0.55)  # trip the 400ms wait_for → timeout Err
            return Ok(value=FastRetrieveResult(results=[], warnings=()))
        return Ok(
            value=FastRetrieveResult(
                results=[_fast_hit(f"survivor-{call_n['i']}", score=0.9)],
                warnings=(),
            )
        )

    monkeypatch.setattr("musubi.retrieve.orchestration.run_fast_retrieve", flaky_fast)

    query = RetrievalQuery(
        namespace="eric/test",
        query_text="gpu",
        mode="fast",
        namespace_targets=[
            NamespaceTarget(namespace="eric/test/episodic", plane="episodic"),
            NamespaceTarget(namespace="eric/test/curated", plane="curated"),
        ],
    )
    result = await retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        query=query,
        account_access=False,
        now=1_700_000_000.0,
    )
    assert isinstance(result, Ok)
    assert len(result.value.results) >= 1
    assert any(w.code.startswith("plane_timeout") for w in result.value.warnings)


# ---------------------------------------------------------------------------
# Integration — environment-dependent; Closure Rule state 2 with named homes
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "deferred to slice-ops-gpu / retrieval perf harness: "
        "end-to-end fast-path on 10K corpus with real TEI + Qdrant, p95 ≤ 400ms"
    )
)
def test_integration_end_to_end_fast_path_on_10K_corpus_with_real_TEI_Qdrant_p95_le_400ms() -> None:
    pass


@pytest.mark.skip(
    reason=(
        "deferred to slice-retrieval-evals / RET-004 harness: "
        "end-to-end deep-path with rerank, NDCG@10 on golden set ≥ threshold"
    )
)
def test_integration_end_to_end_deep_path_with_rerank_NDCG_10_on_golden_set_ge_threshold() -> None:
    pass


@pytest.mark.skip(
    reason=(
        "deferred to slice-ops-gpu: "
        "kill TEI mid-request, pipeline returns with documented degradation"
    )
)
def test_integration_kill_TEI_mid_request_pipeline_returns_with_documented_degradation() -> None:
    pass
