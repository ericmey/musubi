import contextlib
import copy
from collections.abc import Callable
from math import log2
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError


class DefectStillPresent(Exception):
    pass


# ---------------------------------------------------------------------------
# Test 1: Metric Formula Correctness
# ---------------------------------------------------------------------------
def _assert_ndcg_at_k(ndcg_impl: Callable[[list[int], list[int], int], float]) -> None:
    # 1. Base case
    scores = [3, 1, 2, 0]
    ideal = [3, 2, 1, 0]
    assert round(ndcg_impl(scores, ideal, 10), 4) == 0.9721

    # 2. K-truncation assertion
    assert round(ndcg_impl(scores, ideal, 2), 4) == 0.8581

    # 3. Zero IDCG protection
    assert ndcg_impl([0, 0], [0, 0], 10) == 0.0


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason="RET-004: NDCG Metric implementation missing from module",
)
def test_eval_metric_formula_correctness() -> None:
    try:
        from musubi.evals.metrics import ndcg_at_k  # type: ignore[import-untyped]
    except ImportError:
        raise DefectStillPresent("musubi.evals.metrics module does not exist")
    _assert_ndcg_at_k(ndcg_at_k)


def test_discrimination_ndcg_at_k() -> None:
    # Control: Correct
    def correct_ndcg(scores: list[int], ideal_scores: list[int], k: int) -> float:
        def dcg(s: list[int], limit: int) -> float:
            return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(s[:limit])))

        idcg = dcg(sorted(ideal_scores, reverse=True), k)
        return dcg(scores, k) / idcg if idcg > 0 else 0.0

    _assert_ndcg_at_k(correct_ndcg)

    # Fault: Ignores K
    def wrong_ndcg_ignores_k(scores: list[int], ideal_scores: list[int], k: int) -> float:
        def dcg(s: list[int]) -> float:
            return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(s)))

        idcg = dcg(sorted(ideal_scores, reverse=True))
        return dcg(scores) / idcg if idcg > 0 else 0.0

    with pytest.raises(AssertionError):
        _assert_ndcg_at_k(wrong_ndcg_ignores_k)

    # Fault: Zero IDCG raises ZeroDivisionError
    def wrong_ndcg_zero_div(scores: list[int], ideal_scores: list[int], k: int) -> float:
        def dcg(s: list[int], limit: int) -> float:
            return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(s[:limit])))

        idcg = dcg(sorted(ideal_scores, reverse=True), k)
        return dcg(scores, k) / idcg

    with pytest.raises(ZeroDivisionError):
        _assert_ndcg_at_k(wrong_ndcg_zero_div)


# ---------------------------------------------------------------------------
# Test 2: Corpus Schema Validation
# ---------------------------------------------------------------------------
def _assert_schema_validation(model_class: Any) -> None:
    # Control: Healthy schema passes
    valid_data = {
        "id": "q001",
        "text": "healthy query",
        "relevant": [{"object_id": "1", "relevance": 3}],
        "mode": "fast",
        "namespace": "test/ns",
    }
    obj = model_class.model_validate(valid_data)
    assert getattr(obj, "id") == "q001"

    # Fault: Missing required 'relevant' field fails
    with pytest.raises(ValidationError):
        model_class.model_validate({"id": "q002", "text": "bad query"})


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: Corpus Pydantic Schema loader missing"
)
def test_eval_corpus_schema_validation() -> None:
    try:
        from musubi.evals.schema import GoldenQuery  # type: ignore[import-untyped]
    except ImportError:
        raise DefectStillPresent("musubi.evals.schema module does not exist")
    _assert_schema_validation(GoldenQuery)


def test_discrimination_corpus_schema() -> None:
    class CorrectSchema(BaseModel):
        id: str
        text: str
        relevant: list[Any]
        mode: str
        namespace: str

    _assert_schema_validation(CorrectSchema)

    # Fault: Schema accepts anything (no strict typing)
    class AcceptAllSchema:
        @classmethod
        def model_validate(cls, data: dict[str, Any]) -> Any:
            return type("Obj", (), data)()

    import _pytest.outcomes

    with contextlib.suppress(_pytest.outcomes.Failed):
        _assert_schema_validation(AcceptAllSchema)


# ---------------------------------------------------------------------------
# Test 3: Corpus Manifest Checksum
# ---------------------------------------------------------------------------
def _assert_manifest_checksum(
    verify_func: Callable[[dict[str, Any], Path], bool], tmp_path: Path
) -> None:
    import hashlib

    corpus_file = tmp_path / "corpus.yaml"
    corpus_file.write_bytes(b"content")
    true_hash = hashlib.sha256(b"content").hexdigest()

    manifest = {"name": "test_corpus", "files": {"corpus.yaml": true_hash}}
    # Control: Correct hash passes cleanly
    assert verify_func(manifest, tmp_path) is True

    # Fault: One-byte mutation breaks checksum
    corpus_file.write_bytes(b"content2")
    with pytest.raises(ValueError, match="checksum"):
        verify_func(manifest, tmp_path)


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: Corpus manifest checksum logic missing"
)
def test_eval_corpus_manifest_checksum(tmp_path: Path) -> None:
    try:
        from musubi.evals.corpus import verify_manifest  # type: ignore[import-untyped]
    except ImportError:
        raise DefectStillPresent("musubi.evals.corpus module does not exist")
    _assert_manifest_checksum(verify_manifest, tmp_path)


def test_discrimination_manifest_checksum(tmp_path: Path) -> None:
    import hashlib

    def correct_verify(manifest: dict[str, Any], base_dir: Path) -> bool:
        for fname, expected_hash in manifest.get("files", {}).items():
            fpath = base_dir / fname
            actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
            if actual != expected_hash:
                raise ValueError(f"checksum mismatch for {fname}")
        return True

    _assert_manifest_checksum(correct_verify, tmp_path)

    # Fault: Checksum always returns True
    def wrong_verify(manifest: dict[str, Any], base_dir: Path) -> bool:
        return True

    import _pytest.outcomes

    with contextlib.suppress(_pytest.outcomes.Failed):
        _assert_manifest_checksum(wrong_verify, tmp_path)


# ---------------------------------------------------------------------------
# Test 4: Deterministic Rerun Stability
# ---------------------------------------------------------------------------
class EvalResult:
    def __init__(self, metrics: dict[str, float], ordered_hits: list[str]) -> None:
        self.metrics = metrics
        self.ordered_hits = ordered_hits


def _assert_deterministic_rerun(
    run_eval_func: Callable[[list[dict[str, Any]], str, int], EvalResult],
) -> None:
    corpus_a: list[dict[str, Any]] = [{"query": "test_A", "target": "1"}]
    frozen_corpus_a = copy.deepcopy(corpus_a)

    # Run 1
    res1 = run_eval_func(corpus_a, "fake", 42)
    # Run 2
    res2 = run_eval_func(corpus_a, "fake", 42)

    # Corpus mutability guard
    assert corpus_a == frozen_corpus_a, "Runner mutated the input corpus"

    # Determinism
    assert res1.metrics["ndcg@10"] == res2.metrics["ndcg@10"]
    assert res1.ordered_hits == res2.ordered_hits

    # Sensitivity: Different corpus MUST produce different result
    corpus_b: list[dict[str, Any]] = [{"query": "test_B", "target": "2"}]
    res3 = run_eval_func(corpus_b, "fake", 42)
    assert res1.metrics != res3.metrics or res1.ordered_hits != res3.ordered_hits


@pytest.mark.xfail(strict=True, raises=DefectStillPresent, reason="RET-004: Eval runner missing")
def test_eval_deterministic_rerun() -> None:
    try:
        from musubi.evals.runner import run_eval  # type: ignore[import-untyped]
    except ImportError:
        raise DefectStillPresent("musubi.evals.runner module does not exist")
    _assert_deterministic_rerun(run_eval)


def test_discrimination_deterministic_rerun() -> None:
    def correct_runner(corpus: list[dict[str, Any]], embedder: str, seed: int) -> EvalResult:
        import hashlib

        # Combine corpus query and seed deterministically
        val = hashlib.sha256(f"{corpus[0]['query']}_{seed}".encode()).hexdigest()
        return EvalResult({"ndcg@10": float(int(val[:4], 16))}, [val[:8]])

    _assert_deterministic_rerun(correct_runner)

    # Fault: Constant runner (ignores corpus)
    def wrong_constant_runner(corpus: list[dict[str, Any]], embedder: str, seed: int) -> EvalResult:
        return EvalResult({"ndcg@10": float(seed)}, [str(seed)])

    try:
        _assert_deterministic_rerun(wrong_constant_runner)
        pytest.fail("Accepted bad constant runner")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tranche 1: Baseline & Delta Enforcement
# ---------------------------------------------------------------------------


def _assert_nightly_thresholds(check_func: Callable[[dict[str, float], str], bool]) -> None:
    import math

    # 1. Deep mode healthy exact boundaries
    assert (
        check_func({"ndcg@10": 0.65, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.55}, "deep") is True
    )
    # Deep mode negative edge cases
    with pytest.raises(ValueError, match="ndcg@10"):
        check_func({"ndcg@10": 0.6499, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.55}, "deep")
    with pytest.raises(ValueError, match="mrr"):
        check_func({"ndcg@10": 0.65, "mrr": 0.6999, "recall@20": 0.85, "p@1": 0.55}, "deep")
    with pytest.raises(ValueError, match="recall@20"):
        check_func({"ndcg@10": 0.65, "mrr": 0.70, "recall@20": 0.8499, "p@1": 0.55}, "deep")
    with pytest.raises(ValueError, match="p@1"):
        check_func({"ndcg@10": 0.65, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.5499}, "deep")

    # 2. Fast mode healthy exact boundaries
    assert (
        check_func({"ndcg@10": 0.55, "mrr": 0.55, "recall@20": 0.70, "p@1": 0.40}, "fast") is True
    )
    # Fast mode negative edge cases
    with pytest.raises(ValueError, match="ndcg@10"):
        check_func({"ndcg@10": 0.5499, "mrr": 0.55, "recall@20": 0.70, "p@1": 0.40}, "fast")
    with pytest.raises(ValueError, match="mrr"):
        check_func({"ndcg@10": 0.55, "mrr": 0.5499, "recall@20": 0.70, "p@1": 0.40}, "fast")
    with pytest.raises(ValueError, match="recall@20"):
        check_func({"ndcg@10": 0.55, "mrr": 0.55, "recall@20": 0.6999, "p@1": 0.40}, "fast")
    with pytest.raises(ValueError, match="p@1"):
        check_func({"ndcg@10": 0.55, "mrr": 0.55, "recall@20": 0.70, "p@1": 0.3999}, "fast")

    # 3. Missing, Non-numeric, Non-finite protections
    with pytest.raises(ValueError):
        check_func({"mrr": 0.70, "recall@20": 0.85, "p@1": 0.55}, "deep")  # Missing ndcg
    with pytest.raises(ValueError):
        check_func({"ndcg@10": math.nan, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.55}, "deep")
    with pytest.raises(ValueError):
        check_func({"ndcg@10": math.inf, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.55}, "deep")

    # 4. Unknown mode
    with pytest.raises(ValueError):
        check_func({"ndcg@10": 1.0, "mrr": 1.0, "recall@20": 1.0, "p@1": 1.0}, "unknown_mode")


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason="RET-004: Missing eval for nightly qdrant tei thresholds",
)
def test_eval_nightly_qdrant_tei_thresholds() -> None:
    try:
        from musubi.evals.gates import check_nightly_thresholds  # type: ignore[import-untyped]
    except ImportError:
        raise DefectStillPresent("musubi.evals.gates module does not exist")
    _assert_nightly_thresholds(check_nightly_thresholds)


def test_discrimination_nightly_thresholds() -> None:
    import math

    def correct_check(metrics: dict[str, float], mode: str) -> bool:
        targets = {
            "deep": {"ndcg@10": 0.65, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.55},
            "fast": {"ndcg@10": 0.55, "mrr": 0.55, "recall@20": 0.70, "p@1": 0.40},
        }
        if mode not in targets:
            raise ValueError("Unknown mode")

        for k, v in targets[mode].items():
            val = metrics.get(k)
            if val is None or not math.isfinite(val) or val < v:
                raise ValueError(f"Metric {k} below threshold {v}")
        return True

    _assert_nightly_thresholds(correct_check)

    def wrong_check_ignores_mode(metrics: dict[str, float], mode: str) -> bool:
        targets = {"ndcg@10": 0.55, "mrr": 0.55, "recall@20": 0.70, "p@1": 0.40}
        for k, v in targets.items():
            if metrics.get(k, 0.0) < v:
                raise ValueError(f"Metric {k} below threshold {v}")
        return True

    import _pytest.outcomes

    with contextlib.suppress(_pytest.outcomes.Failed):
        _assert_nightly_thresholds(wrong_check_ignores_mode)

    def wrong_check_accepts_nan(metrics: dict[str, float], mode: str) -> bool:
        targets = {
            "deep": {"ndcg@10": 0.65, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.55},
            "fast": {"ndcg@10": 0.55, "mrr": 0.55, "recall@20": 0.70, "p@1": 0.40},
        }
        if mode not in targets:
            raise ValueError()
        for k, v in targets[mode].items():
            val = metrics.get(k)
            if val is None:
                raise ValueError()
            # Fails to check math.isfinite
            if val < v:
                raise ValueError()
        return True

    try:
        _assert_nightly_thresholds(wrong_check_accepts_nan)
        pytest.fail("Accepted bad check")
    except BaseException:
        pass


def _assert_baseline_delta_gate(
    delta_check_func: Callable[[dict[str, float], dict[str, float]], bool],
) -> None:
    import math

    baseline = {"ndcg@10": 0.80, "mrr": 0.85, "latency_p95_ms": 100.0}

    # 1. Exact boundaries PASS
    assert (
        delta_check_func(
            baseline,
            {
                "ndcg@10": 0.78,  # drop exactly 0.02
                "mrr": 0.82,  # drop exactly 0.03
                "latency_p95_ms": 120.0,  # increase exactly 20%
            },
        )
        is True
    )

    # 2. Epsilon-beyond FAIL for EACH
    with pytest.raises(ValueError, match="ndcg@10"):
        delta_check_func(baseline, {"ndcg@10": 0.7799, "mrr": 0.85, "latency_p95_ms": 100.0})
    with pytest.raises(ValueError, match="mrr"):
        delta_check_func(baseline, {"ndcg@10": 0.80, "mrr": 0.8199, "latency_p95_ms": 100.0})
    with pytest.raises(ValueError, match="latency_p95_ms"):
        delta_check_func(baseline, {"ndcg@10": 0.80, "mrr": 0.85, "latency_p95_ms": 120.1})

    # 3. Missing/non-numeric/non-finite FAIL
    with pytest.raises(ValueError):
        delta_check_func(baseline, {"mrr": 0.85, "latency_p95_ms": 100.0})  # Missing ndcg
    with pytest.raises(ValueError):
        delta_check_func(baseline, {"ndcg@10": math.nan, "mrr": 0.85, "latency_p95_ms": 100.0})


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason="RET-004: Missing eval for baseline delta gate unit",
)
def test_eval_baseline_delta_gate_unit() -> None:
    try:
        from musubi.evals.gates import check_delta_tolerances
    except ImportError:
        raise DefectStillPresent("musubi.evals.gates module does not exist")
    _assert_baseline_delta_gate(check_delta_tolerances)


def test_discrimination_baseline_delta_gate() -> None:
    import math

    def correct_delta(base: dict[str, float], cand: dict[str, float]) -> bool:
        if (
            cand.get("ndcg@10") is None
            or not math.isfinite(cand["ndcg@10"])
            or cand["ndcg@10"] < base["ndcg@10"] - 0.02
        ):
            raise ValueError("regression on ndcg@10")
        if (
            cand.get("mrr") is None
            or not math.isfinite(cand["mrr"])
            or cand["mrr"] < base["mrr"] - 0.03
        ):
            raise ValueError("regression on mrr")
        if (
            cand.get("latency_p95_ms") is None
            or not math.isfinite(cand["latency_p95_ms"])
            or cand["latency_p95_ms"] > base["latency_p95_ms"] * 1.20
        ):
            raise ValueError("regression on latency_p95_ms")
        return True

    _assert_baseline_delta_gate(correct_delta)

    def wrong_delta_allows_any(base: dict[str, float], cand: dict[str, float]) -> bool:
        return True

    import _pytest.outcomes

    with contextlib.suppress(_pytest.outcomes.Failed):
        _assert_baseline_delta_gate(wrong_delta_allows_any)

    def wrong_delta_ignores_mrr(base: dict[str, float], cand: dict[str, float]) -> bool:
        if cand.get("ndcg@10") is None or cand["ndcg@10"] < base["ndcg@10"] - 0.02:
            raise ValueError("regression on ndcg@10")
        if (
            cand.get("latency_p95_ms") is None
            or cand["latency_p95_ms"] > base["latency_p95_ms"] * 1.20
        ):
            raise ValueError("regression on latency_p95_ms")
        return True

    import _pytest.outcomes

    with contextlib.suppress(_pytest.outcomes.Failed):
        _assert_baseline_delta_gate(wrong_delta_ignores_mrr)

    def wrong_delta_latency_direction(base: dict[str, float], cand: dict[str, float]) -> bool:
        if cand.get("ndcg@10") is None or cand["ndcg@10"] < base["ndcg@10"] - 0.02:
            raise ValueError()
        if cand.get("mrr") is None or cand["mrr"] < base["mrr"] - 0.03:
            raise ValueError()
        # Fault: demands latency decreases (impossible threshold)
        if (
            cand.get("latency_p95_ms") is None
            or cand["latency_p95_ms"] > base["latency_p95_ms"] * 0.80
        ):
            raise ValueError()
        return True

    import _pytest.outcomes

    try:
        _assert_baseline_delta_gate(wrong_delta_latency_direction)
        pytest.fail("Accepted bad check")
    except Exception:
        pass


def _assert_scheduled_baseline_report(report_func: Callable[[Any, dict[str, float]], bool]) -> None:
    class MockRunner:
        def run(self) -> dict[str, float]:
            return {
                "ndcg@10": 0.75,
                "mrr": 0.85,
                "latency_p95_ms": 100.0,
            }  # Fails ndcg delta vs 0.80

    with pytest.raises(ValueError, match="ndcg@10"):
        report_func(MockRunner(), {"ndcg@10": 0.80, "mrr": 0.85, "latency_p95_ms": 100.0})


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason="RET-004: Missing eval for scheduled baseline report",
)
def test_eval_scheduled_baseline_report() -> None:
    try:
        from musubi.evals.runner import run_scheduled_report
    except ImportError:
        raise DefectStillPresent("musubi.evals.runner module does not exist")
    _assert_scheduled_baseline_report(run_scheduled_report)


def test_discrimination_scheduled_baseline() -> None:
    def correct_report(runner: Any, expected: dict[str, float]) -> bool:
        metrics = runner.run()
        # Delegates to the same delta gate math
        if metrics.get("ndcg@10", 0.0) < expected["ndcg@10"] - 0.02:
            raise ValueError("ndcg@10")
        return True

    _assert_scheduled_baseline_report(correct_report)

    def wrong_report_logs_only(runner: Any, expected: dict[str, float]) -> bool:
        runner.run()
        return True

    import _pytest.outcomes

    with contextlib.suppress(_pytest.outcomes.Failed):
        _assert_scheduled_baseline_report(wrong_report_logs_only)


def _assert_per_query_top_hit_drop(
    check_func: Callable[[dict[str, list[str]], dict[str, list[str]]], bool],
) -> None:
    baseline = {"q1": ["docA", "docB"]}
    candidate_fail = {"q1": ["docX"] * 10 + ["docA"]}
    with pytest.raises(ValueError, match="top-relevant dropped"):
        check_func(baseline, candidate_fail)

    candidate_pass = {"q1": ["docX", "docA"]}
    assert check_func(baseline, candidate_pass) is True


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason="RET-004: Missing eval for per query top hit drop",
)
def test_eval_per_query_top_hit_drop() -> None:
    try:
        from musubi.evals.gates import check_top_hit_drops
    except ImportError:
        raise DefectStillPresent("musubi.evals.gates module does not exist")
    _assert_per_query_top_hit_drop(check_top_hit_drops)


def test_discrimination_per_query_drop() -> None:
    def correct_drop_check(base: dict[str, list[str]], cand: dict[str, list[str]]) -> bool:
        for q, hits in base.items():
            top_hit = hits[0] if hits else None
            if top_hit:
                cand_hits = cand.get(q, [])
                if top_hit not in cand_hits[:10]:
                    raise ValueError("top-relevant dropped")
        return True

    _assert_per_query_top_hit_drop(correct_drop_check)

    def wrong_drop_check_warns_only(base: dict[str, list[str]], cand: dict[str, list[str]]) -> bool:
        return True

    import _pytest.outcomes

    with contextlib.suppress(_pytest.outcomes.Failed):
        _assert_per_query_top_hit_drop(wrong_drop_check_warns_only)


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_abstention_fpr() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_contradiction_blending() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_cross_plane_blending() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_provisional_immediate_recall() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_holdout_isolation() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_pr_smoke_fixed_embeddings() -> None:
    pass
