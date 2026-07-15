"""RET-012: cross-plane ranking is globally comparable (Issue #512).

Owner slice: slice-ret012-cross-plane-ranking (#512).

The discriminating contract: a weak plane's sole hit must not become
maximally relevant merely by being alone. Per-plane local batch
maxima currently make that happen (a sole hit divides by itself and
reaches relevance 1.0); the seam under construction will recompute
each hit's relevance against a single working-set global max BEFORE
the ``best_by_id`` dedup, then sort by ``(-score, object_id, plane)``
so cross-plane ordering is deterministic.

The first contract is bounded to nine bullets (ten items; bullet 5 is
parametrized over gather order):

    5 RED discriminating tests   (currently failing under live code)
    4 GREEN preservation guards  (passing under live code; the seam
                                  must not break them)

Test function names transcribe the slice doc's Test Contract bullets
verbatim per the AGENTS.md Test Contract Closure Rule.

    uv run pytest tests/retrieve/test_ret012_cross_plane_ranking.py -v
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from musubi.embedding.fake import FakeEmbedder
from musubi.retrieve.orchestration import (
    NamespaceTarget,
    RetrievalEnvelope,
    RetrievalQuery,
    RetrievalResult,
)
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
from musubi.retrieve.scoring import SCORE_WEIGHTS, _sigmoid
from musubi.types.common import Ok

# --------------------------------------------------------------------------- #
# Mock infra — fake _run_single returns per-leg results, real orchestrator
# runs the multi-target fanout + dedup + sort
# --------------------------------------------------------------------------- #


class _MockQdrant:
    def query_points(self, *args: Any, **kwargs: Any) -> Any:
        return type("R", (), {"points": []})()

    # RET-002: ``retrieve()`` now accounts delivered rows at the final
    # boundary (scroll → batch write). The ret012 tests deliver mocked
    # hits with no backing store, so accounting resolves to a no-op:
    # an empty scroll yields no writes. The mock must satisfy the
    # accounting client contract or the top-level ``retrieve()`` returns
    # ``Err(kind="internal", detail="access accounting failed: ...")``.
    def scroll(self, *args: Any, **kwargs: Any) -> Any:
        return ([], None)

    def batch_update_points(self, *args: Any, **kwargs: Any) -> Any:
        return None


class _OkReranker:
    async def rerank(
        self, query_text: str, candidates: list[Any], top_k: int | None = None
    ) -> list[float]:
        return [1.0 for _ in candidates]


# A per-leg "other components" profile that is constant across all legs in
# these tests. Keeps the weighted score driven by relevance alone, so the
# assertions on relevance ratio and final rank are unambiguous.
_RECENCY = 0.5
_IMPORTANCE = 0.5
_PROVENANCE = 0.5
_REINFORCE = 0.5


def _other_components_total() -> float:
    """The weighted contribution of recency/importance/provenance/reinforce at the SCORE_WEIGHTS used
    by these tests (the seam must not change those four components)."""
    return SCORE_WEIGHTS.combine(
        relevance=0.0,
        recency=_RECENCY,
        importance=_IMPORTANCE,
        provenance=_PROVENANCE,
        reinforce=_REINFORCE,
    )


def _mk_leg_result(
    *,
    object_id: str,
    plane: str,
    raw_rrf: float,
    marker: str,
    relevance_override: float | None = None,
) -> RetrievalResult:
    """Construct one per-leg ``RetrievalResult`` exactly as the current per-leg code would produce
    it for a sole hit (per-leg ``batch_max_rrf == raw_rrf`` ⇒ per-leg relevance is 1.0).

    The seam at the cross-plane merge re-anchors relevance against the
    working-set global max using the ``raw_rrf_score`` propagated from
    the per-leg ``Hit`` (and from there through ``FastHit`` /
    ``ScoredHit``). Tests must populate it here so the seam has the raw
    input to recompute against — without it the seam's passthrough
    branch fires and the contract is not exercised.
    """
    relevance = 1.0 if relevance_override is None else relevance_override
    components = {
        "relevance": relevance,
        "recency": _RECENCY,
        "importance": _IMPORTANCE,
        "provenance": _PROVENANCE,
        "reinforcement": _REINFORCE,
    }
    score = SCORE_WEIGHTS.combine(
        relevance=relevance,
        recency=_RECENCY,
        importance=_IMPORTANCE,
        provenance=_PROVENANCE,
        reinforce=_REINFORCE,
    )
    return RetrievalResult(
        object_id=object_id,
        namespace="test/ns",
        plane=plane,
        snippet="x",
        score=score,
        score_components=components,
        lineage={},
        payload={"_ret012_marker": marker, "_ret012_raw_rrf": raw_rrf},
        # RET-012: the raw inputs the seam reads. ``raw_rerank_score``
        # stays ``None`` for fast-mode (no rerank) hits.
        raw_rrf_score=raw_rrf,
        raw_rerank_score=None,
    )


def _mk_recent_result(
    *, object_id: str, plane: str, created_epoch: float, marker: str
) -> RetrievalResult:
    """Construct one recent-mode ``RetrievalResult``: ``score == created_epoch`` and
    ``score_components == {}`` (the typed-empty form for recent). The seam's passthrough
    branch is exercised when both ``raw_rrf_score`` and ``raw_rerank_score`` are ``None``."""
    return RetrievalResult(
        object_id=object_id,
        namespace="test/ns",
        plane=plane,
        snippet="x",
        score=created_epoch,
        score_components={},
        lineage={},
        payload={"_ret012_marker": marker, "_ret012_created_epoch": created_epoch},
    )


def _mk_rerank_result(
    *,
    object_id: str,
    plane: str,
    rerank_score: float,
    marker: str,
    raw_rrf: float = 0.5,
) -> RetrievalResult:
    """Construct one deep-mode ``RetrievalResult`` as the current per-leg code would produce it:
    per-leg relevance is ``_sigmoid(rerank_score)`` (intrinsic, not divided by batch_max). The
    seam's sigmoid branch fires on ``raw_rerank_score`` and preserves the per-leg value.

    The ``raw_rrf`` default (0.5) matches a typical deep / blended hit's
    hybrid RRF before rerank; production ``ScoredHit.raw_rrf_score`` is
    propagated from the input ``Hit.rrf_score``. The seam's rerank
    branch takes priority over the RRF branch (rerank is intrinsic and
    bounded [0, 1]) so the RRF value only matters for the test's audit
    payload and the dedup tie-break, not for the relevance computation.
    """
    relevance = _sigmoid(rerank_score)
    components = {
        "relevance": relevance,
        "recency": _RECENCY,
        "importance": _IMPORTANCE,
        "provenance": _PROVENANCE,
        "reinforcement": _REINFORCE,
    }
    score = SCORE_WEIGHTS.combine(
        relevance=relevance,
        recency=_RECENCY,
        importance=_IMPORTANCE,
        provenance=_PROVENANCE,
        reinforce=_REINFORCE,
    )
    return RetrievalResult(
        object_id=object_id,
        namespace="test/ns",
        plane=plane,
        snippet="x",
        score=score,
        score_components=components,
        lineage={},
        payload={
            "_ret012_marker": marker,
            "_ret012_rerank_score": rerank_score,
            "_ret012_raw_rrf": raw_rrf,
        },
        # RET-012: the raw inputs the seam reads. Deep / blended
        # carries a ``raw_rerank_score`` (cross-encoder logit) and
        # also a ``raw_rrf_score`` (the hybrid RRF before rerank,
        # propagated from ``Hit.rrf_score`` via ``score_result``).
        # The seam's rerank branch takes priority; the RRF is audit
        # metadata for the dedup tie-break on equal scores.
        raw_rrf_score=raw_rrf,
        raw_rerank_score=rerank_score,
    )


async def _run_orch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    by_plane: dict[str, RetrievalResult],
    targets: list[NamespaceTarget],
    mode: str = "fast",
) -> Any:
    """Mock ``_run_single`` to return the per-plane result in ``by_plane``, then call the real
    ``retrieve`` orchestrator with the supplied ``namespace_targets``."""

    async def fake_run_single(*args: Any, plane: str, **kwargs: Any) -> Any:
        if plane not in by_plane:
            return Ok(value=RetrievalEnvelope(results=[], warnings=()))
        return Ok(value=RetrievalEnvelope(results=[by_plane[plane]], warnings=()))

    monkeypatch.setattr("musubi.retrieve.orchestration._run_single", fake_run_single)
    return await run_orchestration_retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, _OkReranker()),
        query=RetrievalQuery(
            namespace="test/ns",
            query_text="q",
            mode=cast(Any, mode),
            planes=["curated", "episodic"],
            namespace_targets=targets,
        ),
    )


# --------------------------------------------------------------------------- #
# RED — discriminating tests (currently fail under live code, pass after seam)
# --------------------------------------------------------------------------- #


async def test_asymmetric_two_plane_fast_weak_sole_does_not_maximize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asymmetric two-plane: leg A returns 1 weak hit (raw_rrf=0.01), leg B returns 1 strong hit
    (raw_rrf=0.99). Under per-leg local max, both legs normalise the sole hit to relevance 1.0
    and the strong hit does not necessarily outrank the weak one. The seam re-anchors against
    the working-set global max (0.99), making relevance(weak) ≈ 0.01 and relevance(strong) = 1.0,
    so the strong hit strictly outranks the weak one and the relevance ratio is the global
    ratio (0.01 / 0.99), not 1.0 / 1.0."""
    weak = _mk_leg_result(object_id="weak-sole", plane="curated", raw_rrf=0.01, marker="A")
    strong = _mk_leg_result(object_id="strong-sole", plane="episodic", raw_rrf=0.99, marker="B")
    targets = [
        NamespaceTarget(namespace="test/ns", plane="curated"),
        NamespaceTarget(namespace="test/ns", plane="episodic"),
    ]
    result = await _run_orch(
        monkeypatch, by_plane={"curated": weak, "episodic": strong}, targets=targets
    )
    assert isinstance(result, Ok)
    rows = list(result.value.results)
    assert [r.object_id for r in rows] == ["strong-sole", "weak-sole"], (
        f"strong hit must outrank weak hit; got {[r.object_id for r in rows]}"
    )
    rel_strong = rows[0].score_components["relevance"]
    rel_weak = rows[1].score_components["relevance"]
    assert rel_strong == pytest.approx(1.0, abs=1e-9)
    assert rel_weak == pytest.approx(0.01 / 0.99, abs=1e-9), (
        f"weak hit's relevance must be the working-set global ratio, not 1.0; got {rel_weak}"
    )
    assert rel_strong > rel_weak, "strong hit's relevance must strictly exceed weak hit's"


async def test_three_plane_wildcard_uses_global_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three-plane wildcard fanout: one hit per plane, raw_rrf 0.99 / 0.50 / 0.01. Under per-leg
    local max, every leg's sole hit reaches relevance 1.0 and ordering is undefined. The seam
    uses one global max (0.99) across the full pre-dedup candidate set, so the relevance values
    are 1.0 / 0.50 / 0.01 (the global ratios) and the final rank order matches the raw_rrf
    order. The mixed planes exercise the wildcard fanout (ADR 0031) path under the seam."""
    leg_a = _mk_leg_result(object_id="x-strong", plane="curated", raw_rrf=0.99, marker="curated")
    leg_b = _mk_leg_result(object_id="x-mid", plane="episodic", raw_rrf=0.50, marker="episodic")
    leg_c = _mk_leg_result(object_id="x-weak", plane="concept", raw_rrf=0.01, marker="concept")
    targets = [
        NamespaceTarget(namespace="test/ns", plane="curated"),
        NamespaceTarget(namespace="test/ns", plane="episodic"),
        NamespaceTarget(namespace="test/ns", plane="concept"),
    ]
    result = await _run_orch(
        monkeypatch,
        by_plane={"curated": leg_a, "episodic": leg_b, "concept": leg_c},
        targets=targets,
    )
    assert isinstance(result, Ok)
    rows = list(result.value.results)
    assert [r.object_id for r in rows] == ["x-strong", "x-mid", "x-weak"], (
        f"final rank order must match raw_rrf order; got {[r.object_id for r in rows]}"
    )
    relevances = {r.object_id: r.score_components["relevance"] for r in rows}
    assert relevances["x-strong"] == pytest.approx(1.0, abs=1e-9)
    assert relevances["x-mid"] == pytest.approx(0.50 / 0.99, abs=1e-9)
    assert relevances["x-weak"] == pytest.approx(0.01 / 0.99, abs=1e-9)


async def test_pre_dedup_calibration_picks_higher_recalibrated_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical ordering correction: the seam calibrates the FULL pre-dedup candidate list, then
    the existing ``best_by_id`` dedup picks the highest-recalibrated copy per ``object_id``.
    Calibrating AFTER dedup can permanently discard the better copy using the bad per-leg
    score.

    Setup: two legs, both return the same ``object_id`` "shared". Leg A (curated) has raw_rrf=0.01
    (weak), leg B (episodic) has raw_rrf=0.50 (strong). Targets are ordered so leg A is FIRST —
    the first-seen would win under a per-leg-score tie. The seam's pre-dedup calibration makes
    leg A's relevance = 0.01/0.50 and leg B's = 1.0; leg B's recalibrated score is strictly
    higher, so the chosen copy is from leg B (episodic), not leg A (curated).

    Per-leg scores are constructed equal (sole hits, same other components) so the current
    dedup tie-breaks to the first-seen, making this test discriminate cleanly between
    pre-dedup and post-dedup calibration."""
    shared_oid = "shared"
    leg_a = _mk_leg_result(object_id=shared_oid, plane="curated", raw_rrf=0.01, marker="A-weak")
    leg_b = _mk_leg_result(object_id=shared_oid, plane="episodic", raw_rrf=0.50, marker="B-strong")
    targets = [
        NamespaceTarget(namespace="test/ns", plane="curated"),  # leg A first (weak)
        NamespaceTarget(namespace="test/ns", plane="episodic"),  # leg B second (strong)
    ]
    result = await _run_orch(
        monkeypatch, by_plane={"curated": leg_a, "episodic": leg_b}, targets=targets
    )
    assert isinstance(result, Ok)
    rows = list(result.value.results)
    assert len(rows) == 1, "dedup must collapse the two legs into one row"
    chosen_marker = rows[0].payload["_ret012_marker"]
    assert chosen_marker == "B-strong", (
        f"pre-dedup calibration must pick the higher-recalibrated copy (B-strong), "
        f"not the first-seen (A-weak); got {chosen_marker!r}"
    )
    assert rows[0].score_components["relevance"] == pytest.approx(1.0, abs=1e-9), (
        "the chosen copy's relevance must be the strong leg's recalibrated value (1.0), "
        "not the weak leg's (≈ 0.02)"
    )


async def test_cross_plane_tiebreak_object_id_then_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-plane tie-break: two legs, identical raw_rrf=0.5, identical per-leg score. The
    current multi-target sort has NO secondary key — ordering is the iteration order of
    ``best_by_id``. The seam replaces it with ``sorted(..., key=(-score, object_id, plane))``
    so the final order is deterministic and is by ascending object_id, then ascending plane.

    The plane tiebreak is a defense-in-depth for the (impossible-after-dedup) case where two
    rows share both score and object_id; this test pins the object_id primary key, which is
    the realistic discriminator after dedup."""
    leg_a = _mk_leg_result(object_id="row-B", plane="curated", raw_rrf=0.5, marker="curated")
    leg_b = _mk_leg_result(object_id="row-A", plane="episodic", raw_rrf=0.5, marker="episodic")
    # Leg A (object_id "row-B") is first in ``targets`` so under current code the
    # iteration order of best_by_id is "row-B" then "row-A". The seam must invert
    # this: "row-A" (lower object_id) first.
    targets = [
        NamespaceTarget(namespace="test/ns", plane="curated"),
        NamespaceTarget(namespace="test/ns", plane="episodic"),
    ]
    result = await _run_orch(
        monkeypatch, by_plane={"curated": leg_a, "episodic": leg_b}, targets=targets
    )
    assert isinstance(result, Ok)
    rows = list(result.value.results)
    assert [r.object_id for r in rows] == ["row-A", "row-B"], (
        f"object_id tiebreak must put lower object_id first; got {[r.object_id for r in rows]}"
    )


@pytest.mark.parametrize(
    "plane_order",
    [
        ("curated", "episodic"),
        ("episodic", "curated"),
    ],
    ids=["curated_first", "episodic_first"],
)
async def test_dedup_equal_score_prefers_lower_plane(
    monkeypatch: pytest.MonkeyPatch, plane_order: tuple[str, str]
) -> None:
    """Equal-score copies of the same ``object_id`` from different legs must dedup to the
    lexicographically smaller plane, regardless of which leg was first in the gather.

    The previous strict ``hit.score > current.score`` dedup picked the first-seen copy on
    equal scores, which made the result depend on gather order and could not be repaired
    by the final ``(-score, object_id, plane)`` sort (one copy was already discarded by
    the time the sort ran). The fix uses the same deterministic key in the dedup as in
    the final sort: higher score wins; on tie, lower object_id wins; on further tie,
    lower plane wins.

    The two parametrized ``plane_order`` cases (curated first vs episodic first) prove
    the dedup is order-independent for the same input.
    """
    shared_oid = "shared-equal"
    # Both legs carry the same ``raw_rrf`` and the same other components ⇒ identical
    # per-leg score ⇒ identical recalibrated score after the seam. The dedup must
    # choose deterministically, not by gather order.
    leg_a = _mk_leg_result(
        object_id=shared_oid,
        plane=plane_order[0],
        raw_rrf=0.5,
        marker=f"leg-{plane_order[0]}",
    )
    leg_b = _mk_leg_result(
        object_id=shared_oid,
        plane=plane_order[1],
        raw_rrf=0.5,
        marker=f"leg-{plane_order[1]}",
    )
    targets = [
        NamespaceTarget(namespace="test/ns", plane=plane_order[0]),
        NamespaceTarget(namespace="test/ns", plane=plane_order[1]),
    ]
    result = await _run_orch(
        monkeypatch,
        by_plane={plane_order[0]: leg_a, plane_order[1]: leg_b},
        targets=targets,
    )
    assert isinstance(result, Ok)
    rows = list(result.value.results)
    assert len(rows) == 1, (
        f"dedup must collapse the two equal-score copies into one row; got {len(rows)} "
        f"(plane_order={plane_order})"
    )
    expected_lower = min(plane_order[0], plane_order[1])
    chosen_plane = rows[0].plane
    assert chosen_plane == expected_lower, (
        f"dedup must prefer the lexicographically smaller plane on equal score; "
        f"expected {expected_lower!r}, got {chosen_plane!r} (plane_order={plane_order})"
    )


# --------------------------------------------------------------------------- #
# GREEN — preservation guards (pass under live code; the seam must not break them)
# --------------------------------------------------------------------------- #


async def test_single_target_fast_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-target fast path is bit-for-bit preserved: the seam only runs in the
    ``len(targets) > 1`` branch. A single-target request with a sole hit still gets the
    per-leg weighted score (relevance 1.0 for a sole hit, SCORE_WEIGHTS combine of the
    intrinsic components)."""
    sole = _mk_leg_result(object_id="sole-fast", plane="curated", raw_rrf=0.5, marker="only-leg")
    targets = [NamespaceTarget(namespace="test/ns", plane="curated")]

    async def fake_run_single(*args: Any, plane: str, **kwargs: Any) -> Any:
        return Ok(value=RetrievalEnvelope(results=[sole], warnings=()))

    monkeypatch.setattr("musubi.retrieve.orchestration._run_single", fake_run_single)
    result = await run_orchestration_retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, _OkReranker()),
        query=RetrievalQuery(
            namespace="test/ns",
            query_text="q",
            mode="fast",
            planes=["curated"],
            namespace_targets=targets,
        ),
    )
    assert isinstance(result, Ok)
    rows = list(result.value.results)
    assert len(rows) == 1
    assert rows[0].object_id == "sole-fast"
    assert rows[0].score_components["relevance"] == pytest.approx(1.0, abs=1e-9)
    expected_score = SCORE_WEIGHTS.combine(
        relevance=1.0,
        recency=_RECENCY,
        importance=_IMPORTANCE,
        provenance=_PROVENANCE,
        reinforce=_REINFORCE,
    )
    assert rows[0].score == pytest.approx(expected_score, abs=1e-9)


async def test_rerank_sigmoid_relevance_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-encoder sigmoid relevance is preserved by the seam. A deep leg with a
    ``rerank_score`` keeps its ``relevance == _sigmoid(rerank_score)`` after the seam,
    regardless of any other leg's raw_rrf in the fanout. This is a preservation guard
    per the design ACK (deep / blended coverage is not a claimed RED)."""
    rerank_score = 2.0
    expected_relevance = _sigmoid(rerank_score)
    deep_hit = _mk_rerank_result(
        object_id="deep-hit", plane="curated", rerank_score=rerank_score, marker="deep"
    )
    fast_hit = _mk_leg_result(object_id="fast-hit", plane="episodic", raw_rrf=0.99, marker="fast")
    targets = [
        NamespaceTarget(namespace="test/ns", plane="curated"),
        NamespaceTarget(namespace="test/ns", plane="episodic"),
    ]
    result = await _run_orch(
        monkeypatch,
        by_plane={"curated": deep_hit, "episodic": fast_hit},
        targets=targets,
        mode="deep",
    )
    assert isinstance(result, Ok)
    rows = list(result.value.results)
    deep_row = next(r for r in rows if r.object_id == "deep-hit")
    fast_row = next(r for r in rows if r.object_id == "fast-hit")
    assert deep_row.score_components["relevance"] == pytest.approx(expected_relevance, abs=1e-9), (
        "deep hit's relevance must be sigmoid(rerank_score), independent of fast leg's rrf"
    )
    assert fast_row.score_components["relevance"] == pytest.approx(1.0, abs=1e-9), (
        "fast hit's relevance must be 1.0 (sole hit in a 0.99/0.99 working set)"
    )


async def test_recent_mode_passthrough_at_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recent mode is a passthrough at the seam. Recent rows have ``raw_rrf_score is None``
    and ``raw_rerank_score is None`` (the new contract); the seam's else branch leaves
    ``score`` and ``score_components`` unchanged. Recent's existing ``created_epoch``
    ordering survives untouched."""
    newer = _mk_recent_result(
        object_id="r-new", plane="curated", created_epoch=2000.0, marker="curated"
    )
    older = _mk_recent_result(
        object_id="r-old", plane="episodic", created_epoch=1000.0, marker="episodic"
    )
    targets = [
        NamespaceTarget(namespace="test/ns", plane="curated"),
        NamespaceTarget(namespace="test/ns", plane="episodic"),
    ]
    result = await _run_orch(
        monkeypatch,
        by_plane={"curated": newer, "episodic": older},
        targets=targets,
        mode="recent",
    )
    assert isinstance(result, Ok)
    rows = list(result.value.results)
    assert [r.object_id for r in rows] == ["r-new", "r-old"], (
        f"recent ordering must remain newest-first by created_epoch; "
        f"got {[r.object_id for r in rows]}"
    )
    assert rows[0].score == pytest.approx(2000.0, abs=1e-9)
    assert rows[1].score == pytest.approx(1000.0, abs=1e-9)
    assert rows[0].score_components == {}


async def test_empty_working_set_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every leg returns zero hits, the seam runs over an empty candidate list and
    returns empty. No exception, no warning raised by the seam itself. The cross-plane
    merge returns an empty result envelope (the existing top-level timeout / internal
    error path is unchanged)."""
    targets = [
        NamespaceTarget(namespace="test/ns", plane="curated"),
        NamespaceTarget(namespace="test/ns", plane="episodic"),
    ]
    result = await _run_orch(monkeypatch, by_plane={}, targets=targets)
    assert isinstance(result, Ok)
    assert list(result.value.results) == []
    assert result.value.warnings == ()
