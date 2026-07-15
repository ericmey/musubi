"""RET-004 successor — the live scheduled gate's metric core, locally provable without TEI.

These exercise the deterministic metric/aggregation/threshold logic (injected retriever), NOT live
quality numbers — those run on the scheduled x86 TEI CI. The fail-loud-without-TEI CLI contract lives
in ``test_cli.py::test_scheduled_command_fails_loud_without_tei``.
"""

from __future__ import annotations

import asyncio
from math import log2
from typing import Any

import pytest

from musubi.evals.live_gate import (
    LiveGateUnavailable,
    _hits_or_raise,
    aggregate,
    enforce_thresholds,
    evaluate_query,
    measure_hybrid_vs_dense,
    run_live_gate,
)

_REL = [{"object_id": "a", "relevance": 3}, {"object_id": "b", "relevance": 2}]


def test_evaluate_query_graded_metrics_exact() -> None:
    metrics = evaluate_query(["a", "x", "b", "y"], _REL)
    assert metrics["p@1"] == 1.0  # the top hit is the perfect (grade-3) doc
    assert metrics["mrr"] == 1.0  # first relevant hit is at rank 1
    assert metrics["recall@20"] == 1.0  # both relevant docs retrieved
    # dcg = 7/log2(2) + 3/log2(4) = 8.5 ; idcg = 7/log2(2) + 3/log2(3)
    assert metrics["ndcg@10"] == pytest.approx(8.5 / (7.0 + 3.0 / log2(3)))


def test_evaluate_query_discriminates_ranking_quality() -> None:
    """A correct implementation must reward a good ranking over a bad one — the discriminator a
    fake/degenerate metric would fail."""
    good = evaluate_query(["a", "b"], _REL)  # perfect order
    demoted = evaluate_query(["b", "a"], _REL)  # the perfect hit pushed below the partial
    missed = evaluate_query(["x", "y"], _REL)  # neither relevant hit retrieved

    assert good["ndcg@10"] == pytest.approx(1.0)
    assert good["p@1"] == 1.0 and demoted["p@1"] == 0.0
    assert (
        demoted["ndcg@10"] < good["ndcg@10"]
    )  # demoting the perfect hit must score strictly worse
    assert missed["mrr"] == 0.0 and missed["recall@20"] == 0.0 and missed["ndcg@10"] == 0.0


def test_aggregate_is_per_metric_mean_and_empty_is_empty() -> None:
    assert aggregate([]) == {}
    assert aggregate([{"ndcg@10": 1.0}, {"ndcg@10": 0.0}]) == {"ndcg@10": 0.5}


def _query(mode: str, oid: str) -> dict[str, Any]:
    return {
        "id": f"{mode}-{oid}",
        "text": "q",
        "mode": mode,
        "namespace": "test/default/blended",
        "relevant": [{"object_id": oid, "relevance": 3}],
    }


def test_run_live_gate_groups_by_mode_and_enforce_catches_subthreshold() -> None:
    """The injected retriever returns a perfect ranking for fast queries and nothing for deep. The
    gate groups per mode; enforce_thresholds passes the perfect fast mode and FAILS the empty deep
    mode (no fabricated pass), reusing the foundation's check_nightly_thresholds."""

    async def retriever(query: dict[str, Any]) -> list[str]:
        if query["mode"] == "fast":
            return [item["object_id"] for item in query["relevant"]]  # perfect
        return []  # deep pipeline "returned nothing" — must fail, never fake-pass

    by_mode = asyncio.run(run_live_gate([_query("fast", "a"), _query("deep", "b")], retriever))
    assert set(by_mode) == {"fast", "deep"}

    enforce_thresholds({"fast": by_mode["fast"]})  # perfect fast run clears its thresholds

    with pytest.raises(ValueError, match="below threshold"):
        enforce_thresholds({"deep": by_mode["deep"]})  # empty deep run fails loud


def test_enforce_thresholds_rejects_no_metrics() -> None:
    with pytest.raises(ValueError, match="no metrics"):
        enforce_thresholds({})


class _Hit:
    def __init__(self, object_id: str) -> None:
        self.object_id = object_id


class _OkResult:
    """Mirrors musubi.types.common.Ok: is_ok/is_err are METHODS, `.value` holds the payload."""

    def __init__(self, hits: list[_Hit]) -> None:
        self.results = hits

    @property
    def value(self) -> _OkResult:
        return self

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False


class _ErrResult:
    """Mirrors musubi.types.common.Err: is_err() is True, `.error` holds the failure (no `.value`)."""

    def __init__(self, error: str) -> None:
        self.error = error

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True


class _DeepOkResult:
    """Mirrors a deep DeepResult: ranked hits live on `.hits` (fast uses `.results`)."""

    def __init__(self, hits: list[_Hit]) -> None:
        self.hits = hits

    @property
    def value(self) -> _DeepOkResult:
        return self

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False


def test_hits_or_raise_extracts_ok_and_fails_loud_on_err() -> None:
    """Regression for two bugs the first live x86 run + local mechanism test caught: (1) is_ok/is_err
    are METHODS — an Err must fail loud (LiveGateUnavailable), never fall to `.value`/AttributeError;
    (2) fast hits are `.results`, deep hits are `.hits` — both must extract."""
    assert _hits_or_raise(_OkResult([_Hit("a"), _Hit("b")]), "q1") == ["a", "b"]  # fast shape
    assert _hits_or_raise(_DeepOkResult([_Hit("c"), _Hit("d")]), "q2") == ["c", "d"]  # deep shape
    with pytest.raises(LiveGateUnavailable, match="retrieval failed for query 'q1'"):
        _hits_or_raise(_ErrResult("empty_query"), "q1")


def test_measure_hybrid_vs_dense_computes_delta() -> None:
    """The harness mechanics: over the same graded corpus, a hybrid ranking that surfaces the
    relevant hit higher than dense-only yields a positive NDCG@10 delta. (The real 1000-doc numbers
    run on CI; this proves the comparison itself is correct — a harness that ignored the hybrid/dense
    distinction would report delta 0.)"""
    query = {
        "text": "t",
        "mode": "hybrid",
        "namespace": "test/default/blended",
        "relevant": [{"object_id": "a", "relevance": 3}],
    }

    async def search(_query: dict[str, Any], hybrid: bool) -> list[str]:
        return ["a", "b"] if hybrid else ["x", "a"]  # hybrid ranks the hit first; dense buries it

    result = asyncio.run(measure_hybrid_vs_dense([query], search))
    assert result["hybrid_ndcg@10"] == pytest.approx(1.0)
    assert result["dense_ndcg@10"] < result["hybrid_ndcg@10"]
    assert result["delta"] > 0.0
