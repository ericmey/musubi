import copy
from collections.abc import Callable
from math import log2
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from pydantic import BaseModel, ValidationError

from musubi.evals.gates import FrozenModelConfig, MockEvalReport
from musubi.evals.runner import EvalResult


class MockQuery(NamedTuple):
    id: str
    text: str
    labels: list[str]


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
    assert round(ndcg_impl(scores, ideal, 2), 4) == 0.8581, "K-truncation mismatch"

    # 3. Zero IDCG protection
    assert ndcg_impl([0, 0], [0, 0], 10) == 0.0


def _assert_rr(rr_impl: Callable[[list[int]], float]) -> None:
    # First relevant at rank 3 -> reciprocal rank 1/3
    assert round(rr_impl([0, 0, 1, 0]), 4) == 0.3333
    # First relevant at rank 1 -> 1.0
    assert rr_impl([2, 0, 0]) == 1.0
    # No relevant item -> 0.0
    assert rr_impl([0, 0, 0]) == 0.0


def _assert_recall_at_k(recall_impl: Callable[[list[int], int, int], float]) -> None:
    # 2 of 4 relevant items retrieved within the top-3
    assert recall_impl([1, 0, 1, 0], 4, 3) == 0.5
    # All relevant items retrieved
    assert recall_impl([1, 1], 2, 10) == 1.0
    # Zero total relevant -> 0.0 (no division by zero)
    assert recall_impl([0, 0], 0, 5) == 0.0


def test_metric_functions_reproduce_known_values() -> None:
    try:
        from musubi.evals.metrics import ndcg_at_k, recall_at_k, rr
    except ImportError:
        raise DefectStillPresent("musubi.evals.metrics module does not exist")
    _assert_ndcg_at_k(ndcg_at_k)
    _assert_rr(rr)
    _assert_recall_at_k(recall_at_k)


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

    with pytest.raises(AssertionError, match="K-truncation mismatch"):
        _assert_ndcg_at_k(wrong_ndcg_ignores_k)

    # Fault: Zero IDCG raises ZeroDivisionError
    def wrong_ndcg_zero_div(scores: list[int], ideal_scores: list[int], k: int) -> float:
        def dcg(s: list[int], limit: int) -> float:
            return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(s[:limit])))

        idcg = dcg(sorted(ideal_scores, reverse=True), k)
        return dcg(scores, k) / idcg

    with pytest.raises(ZeroDivisionError, match="division by zero"):
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
    try:
        model_class.model_validate({"id": "q002", "text": "bad query"})
        raise ValueError("Schema validation failed to enforce required fields")
    except ValidationError:
        pass


def test_golden_query_file_schema_validates() -> None:
    try:
        from musubi.evals.schema import GoldenQuery
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

    with pytest.raises(ValueError, match="Schema validation failed to enforce required fields"):
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
    try:
        verify_func(manifest, tmp_path)
        raise RuntimeError("Failed to raise checksum error")
    except ValueError:
        pass


def test_corpus_snapshot_checksum_verified_before_run(tmp_path: Path) -> None:
    try:
        from musubi.evals.corpus import verify_manifest
    except ImportError:
        raise DefectStillPresent("musubi.evals.corpus module does not exist")
    _assert_manifest_checksum(verify_manifest, tmp_path)


def test_manifest_verification_fails_closed_for_missing_or_malformed_files(
    tmp_path: Path,
) -> None:
    from musubi.evals.corpus import verify_manifest

    with pytest.raises(ValueError, match="non-empty mapping"):
        verify_manifest({"files": []}, tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        verify_manifest({"files": {"missing.yaml": "0" * 64}}, tmp_path)


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

    with pytest.raises(RuntimeError, match="Failed to raise checksum error"):
        _assert_manifest_checksum(wrong_verify, tmp_path)


# ---------------------------------------------------------------------------
# Test 4: Deterministic Rerun Stability
# ---------------------------------------------------------------------------


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
    assert res1.metrics != res3.metrics or res1.ordered_hits != res3.ordered_hits, (
        "Sensitivity mismatch"
    )


def test_eval_run_deterministic_across_reruns() -> None:
    try:
        from musubi.evals.runner import run_eval
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

    with pytest.raises(AssertionError, match="Sensitivity mismatch"):
        _assert_deterministic_rerun(wrong_constant_runner)


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
    try:
        check_func({"ndcg@10": 0.6499, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.55}, "deep")
        raise RuntimeError("Failed to catch ndcg@10")
    except ValueError:
        pass
    try:
        check_func({"ndcg@10": 0.65, "mrr": 0.6999, "recall@20": 0.85, "p@1": 0.55}, "deep")
        raise RuntimeError("Failed to catch mrr")
    except ValueError:
        pass
    try:
        check_func({"ndcg@10": 0.65, "mrr": 0.70, "recall@20": 0.8499, "p@1": 0.55}, "deep")
        raise RuntimeError("Failed to catch recall@20")
    except ValueError:
        pass
    try:
        check_func({"ndcg@10": 0.65, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.5499}, "deep")
        raise RuntimeError("Failed to catch p@1")
    except ValueError:
        pass

    # 2. Fast mode healthy exact boundaries
    assert (
        check_func({"ndcg@10": 0.55, "mrr": 0.55, "recall@20": 0.70, "p@1": 0.40}, "fast") is True
    )
    # Fast mode negative edge cases
    try:
        check_func({"ndcg@10": 0.5499, "mrr": 0.55, "recall@20": 0.70, "p@1": 0.40}, "fast")
        raise RuntimeError("Failed to catch ndcg@10")
    except ValueError:
        pass
    try:
        check_func({"ndcg@10": 0.55, "mrr": 0.5499, "recall@20": 0.70, "p@1": 0.40}, "fast")
        raise RuntimeError("Failed to catch mrr")
    except ValueError:
        pass
    try:
        check_func({"ndcg@10": 0.55, "mrr": 0.55, "recall@20": 0.6999, "p@1": 0.40}, "fast")
        raise RuntimeError("Failed to catch recall@20")
    except ValueError:
        pass
    try:
        check_func({"ndcg@10": 0.55, "mrr": 0.55, "recall@20": 0.70, "p@1": 0.3999}, "fast")
        raise RuntimeError("Failed to catch p@1")
    except ValueError:
        pass

    # 3. Missing, Non-numeric, Non-finite protections
    try:
        check_func({"mrr": 0.70, "recall@20": 0.85, "p@1": 0.55}, "deep")
        raise RuntimeError("Failed to catch missing ndcg@10")
    except ValueError:
        pass  # Missing ndcg
    try:
        check_func({"ndcg@10": math.nan, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.55}, "deep")
        raise RuntimeError("Failed to catch nan ndcg@10")
    except ValueError:
        pass
    try:
        check_func({"ndcg@10": math.inf, "mrr": 0.70, "recall@20": 0.85, "p@1": 0.55}, "deep")
        raise RuntimeError("Failed to catch inf ndcg@10")
    except ValueError:
        pass

    # 4. Unknown mode
    try:
        check_func({"ndcg@10": 1.0, "mrr": 1.0, "recall@20": 1.0, "p@1": 1.0}, "unknown_mode")
        raise RuntimeError("Failed to catch unknown mode")
    except ValueError:
        pass


def test_eval_nightly_qdrant_tei_thresholds() -> None:
    try:
        from musubi.evals.gates import check_nightly_thresholds
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

    with pytest.raises(RuntimeError, match="Failed to catch ndcg@10"):
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

    with pytest.raises(RuntimeError, match="Failed to catch nan ndcg@10"):
        _assert_nightly_thresholds(wrong_check_accepts_nan)


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
    try:
        delta_check_func(baseline, {"ndcg@10": 0.7799, "mrr": 0.85, "latency_p95_ms": 100.0})
        raise RuntimeError("Failed to catch ndcg@10 delta")
    except ValueError:
        pass
    try:
        delta_check_func(baseline, {"ndcg@10": 0.80, "mrr": 0.8199, "latency_p95_ms": 100.0})
        raise RuntimeError("Failed to catch mrr delta")
    except ValueError:
        pass
    try:
        delta_check_func(baseline, {"ndcg@10": 0.80, "mrr": 0.85, "latency_p95_ms": 120.1})
        raise RuntimeError("Failed to catch latency_p95_ms delta")
    except ValueError:
        pass

    # 3. Missing/non-numeric/non-finite FAIL
    try:
        delta_check_func(baseline, {"mrr": 0.85, "latency_p95_ms": 100.0})
        raise RuntimeError("Failed to catch missing ndcg@10")
    except ValueError:
        pass
    try:
        delta_check_func(baseline, {"ndcg@10": math.nan, "mrr": 0.85, "latency_p95_ms": 100.0})
        raise RuntimeError("Failed to catch NaN delta")
    except ValueError:
        pass


def test_ci_gate_fails_on_ndcg_regression() -> None:
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

    with pytest.raises(RuntimeError, match="Failed to catch ndcg@10 delta"):
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

    with pytest.raises(RuntimeError, match="Failed to catch mrr delta"):
        _assert_baseline_delta_gate(wrong_delta_ignores_mrr)

    def wrong_delta_latency_direction(base: dict[str, float], cand: dict[str, float]) -> bool:
        if cand.get("ndcg@10") is None or cand["ndcg@10"] < base["ndcg@10"] - 0.02:
            raise ValueError("regression on ndcg@10")
        if cand.get("mrr") is None or cand["mrr"] < base["mrr"] - 0.03:
            raise ValueError("regression on mrr")
        # Fault: demands latency decreases (impossible threshold)
        if (
            cand.get("latency_p95_ms") is None
            or cand["latency_p95_ms"] > base["latency_p95_ms"] * 0.80
        ):
            raise ValueError("regression on latency_p95_ms")
        return True

    with pytest.raises(ValueError, match="regression on latency_p95_ms"):
        _assert_baseline_delta_gate(wrong_delta_latency_direction)


def _assert_scheduled_baseline_report(report_func: Callable[[Any, dict[str, float]], bool]) -> None:
    class MockRunner:
        def run(self) -> dict[str, float]:
            return {
                "ndcg@10": 0.75,
                "mrr": 0.85,
                "latency_p95_ms": 100.0,
            }  # Fails ndcg delta vs 0.80

    try:
        report_func(MockRunner(), {"ndcg@10": 0.80, "mrr": 0.85, "latency_p95_ms": 100.0})
        raise RuntimeError("Failed to raise ValueError")
    except ValueError:
        pass


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

    with pytest.raises(RuntimeError, match="Failed to raise ValueError"):
        _assert_scheduled_baseline_report(wrong_report_logs_only)


def _assert_per_query_top_hit_drop(
    check_func: Callable[[dict[str, list[str]], dict[str, list[str]]], bool],
) -> None:
    baseline = {"q1": ["docA", "docB"]}
    candidate_fail = {"q1": ["docX"] * 10 + ["docA"]}
    try:
        check_func(baseline, candidate_fail)
        raise RuntimeError("Failed to catch top hit drop")
    except ValueError:
        pass

    candidate_pass = {"q1": ["docX", "docA"]}
    assert check_func(baseline, candidate_pass) is True


def test_eval_compare_reports_per_query_diffs() -> None:
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

    with pytest.raises(RuntimeError, match="Failed to catch top hit drop"):
        _assert_per_query_top_hit_drop(wrong_drop_check_warns_only)


# ---------------------------------------------------------------------------
# Tranche 2: Holdout, Smoke Gate, Abstention
# ---------------------------------------------------------------------------


def _assert_holdout_isolation(
    run_eval_func: Callable[
        [Callable[[], tuple[list[MockQuery], list[MockQuery]]], Callable[[list[MockQuery]], Any]],
        Any,
    ],
) -> None:
    loader_calls = 0

    def loader() -> tuple[list[MockQuery], list[MockQuery]]:
        nonlocal loader_calls
        loader_calls += 1
        return [MockQuery("1", "a", ["train_l1"])], [MockQuery("2", "b", ["test_l2"])]

    actual_trained_queries: list[MockQuery] = []
    trainer_calls = 0

    def spy_trainer(q_list: list[MockQuery]) -> Any:
        nonlocal trainer_calls
        trainer_calls += 1
        actual_trained_queries.extend(q_list)
        return "mock_model"

    run_eval_func(loader, spy_trainer)

    assert loader_calls == 1, "Holdout leakage: Loader not called exactly once"
    assert trainer_calls == 1, "Holdout leakage: Trainer not called exactly once"

    trained_ids = {q.id for q in actual_trained_queries}
    trained_labels = {lbl for q in actual_trained_queries for lbl in q.labels}

    assert "1" in trained_ids, "Holdout leakage: Trainer never saw train IDs"
    assert "train_l1" in trained_labels, "Holdout leakage: Trainer never saw train labels"
    assert "2" not in trained_ids, "Holdout leakage: Trainer saw test IDs"
    assert "test_l2" not in trained_labels, "Holdout leakage: Trainer saw test labels"


def test_holdout_split_excluded_from_tuning_runs() -> None:
    try:
        from musubi.evals.runner import run_isolated_eval
    except ImportError:
        raise DefectStillPresent("musubi.evals modules missing holdout splits")
    _assert_holdout_isolation(run_isolated_eval)


def test_discrimination_holdout_isolation() -> None:
    def correct_runner(ld: Any, tr: Any) -> Any:
        train_q, _test_q = ld()
        tr(train_q)

    _assert_holdout_isolation(correct_runner)

    def wrong_runner_does_nothing(ld: Any, tr: Any) -> Any:
        pass

    with pytest.raises(AssertionError, match="Holdout leakage: Loader not called exactly once"):
        _assert_holdout_isolation(wrong_runner_does_nothing)

    def wrong_runner_trains_empty(ld: Any, tr: Any) -> Any:
        ld()
        tr([])

    with pytest.raises(AssertionError, match="Holdout leakage: Trainer never saw train IDs"):
        _assert_holdout_isolation(wrong_runner_trains_empty)

    def wrong_runner_trains_test_ids(ld: Any, tr: Any) -> Any:
        train_q, _test_q = ld()
        tr([*train_q, MockQuery("2", "b", ["train_fake"])])

    with pytest.raises(AssertionError, match="Holdout leakage: Trainer saw test IDs"):
        _assert_holdout_isolation(wrong_runner_trains_test_ids)

    def wrong_runner_trains_test_labels(ld: Any, tr: Any) -> Any:
        train_q, _test_q = ld()
        tr([*train_q, MockQuery("3", "c", ["test_l2"])])

    with pytest.raises(AssertionError, match="Holdout leakage: Trainer saw test labels"):
        _assert_holdout_isolation(wrong_runner_trains_test_labels)


def _assert_pr_smoke_fixed_embeddings(
    run_func: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus_q1 = [
        {"id": "doc1", "text": "alpha", "embedding": [1.0, 0.0], "relevance": 1},
        {"id": "doc2", "text": "beta", "embedding": [0.0, 1.0], "relevance": 0},
    ]
    q1_emb = [1.0, 0.0]

    corpus_q2 = [
        {"id": "doc1", "text": "alpha", "embedding": [1.0, 0.0], "relevance": 0},
        {"id": "doc2", "text": "beta", "embedding": [0.0, 1.0], "relevance": 1},
    ]
    q2_emb = [0.0, 1.0]

    import hashlib
    import json
    from math import log2

    def compute_hash(c: list[dict[str, Any]]) -> str:
        return hashlib.sha256(
            json.dumps(sorted(c, key=lambda x: x["id"]), sort_keys=True).encode("utf-8")
        ).hexdigest()

    hash_q1 = compute_hash(corpus_q1)
    hash_q2 = compute_hash(corpus_q2)

    network_called = False

    def mock_network(*args: Any, **kwargs: Any) -> Any:
        nonlocal network_called
        network_called = True
        raise RuntimeError("Network call forbidden in smoke gate")

    import urllib.request

    import qdrant_client

    monkeypatch.setattr(urllib.request, "urlopen", mock_network)
    monkeypatch.setattr(qdrant_client, "QdrantClient", mock_network)

    def verify_result(
        res: Any,
        expected_hash: str,
        expected_ranking: list[str],
        expected_relevances: list[int],
        fix_label: str,
    ) -> None:
        if network_called:
            raise ValueError("Qdrant/TEI network hit detected")

        actual_hash = getattr(res, "corpus_checksum", "")
        if actual_hash != expected_hash:
            # specifically catch order dependence
            raw_hash = hashlib.sha256(
                json.dumps(corpus_q1, sort_keys=True).encode("utf-8")
            ).hexdigest()
            rev_hash = hashlib.sha256(
                json.dumps(list(reversed(corpus_q1)), sort_keys=True).encode("utf-8")
            ).hexdigest()
            if actual_hash in (raw_hash, rev_hash):
                raise ValueError(f"Checksum depends on input order {fix_label}")
            raise ValueError(f"Checksum mismatch {fix_label}")

        ordered_hits = getattr(res, "ordered_hits", [])
        if ordered_hits != expected_ranking:
            raise ValueError(f"Ranking mismatch {fix_label}")

        def dcg(r_list: list[int]) -> float:
            return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(r_list)))

        idcg = dcg(sorted(expected_relevances, reverse=True))
        expected_ndcg = dcg(expected_relevances) / idcg if idcg > 0 else 0.0

        reported_ndcg = getattr(res, "metrics", {}).get("ndcg@10", -1.0)
        if reported_ndcg < 0.0:
            raise ValueError(f"Metric below 0 {fix_label}")
        if reported_ndcg > 1.0:
            raise ValueError(f"Metric above 1 {fix_label}")
        if abs(reported_ndcg - expected_ndcg) > 0.001:
            raise ValueError(f"Metrics mismatch {fix_label}")

    try:
        res1_a = run_func(corpus_q1, query_embedding=q1_emb)
        res1_b = run_func(corpus_q1, query_embedding=q1_emb)
    except TypeError as e:
        if "query_embedding" in str(e):
            raise DefectStillPresent("Runner missing query_embedding parameter") from e
        raise

    verify_result(res1_a, hash_q1, ["doc1", "doc2"], [1, 0], "q1")
    if getattr(res1_a, "metrics", {}) != getattr(res1_b, "metrics", {}):
        raise ValueError("Metrics non-deterministic q1")
    if getattr(res1_a, "ordered_hits", []) != getattr(res1_b, "ordered_hits", []):
        raise ValueError("Ranking non-deterministic q1")

    res1_permuted = run_func(list(reversed(corpus_q1)), query_embedding=q1_emb)
    verify_result(res1_permuted, hash_q1, ["doc1", "doc2"], [1, 0], "q1_permuted")

    res2_a = run_func(corpus_q2, query_embedding=q2_emb)
    res2_b = run_func(corpus_q2, query_embedding=q2_emb)
    verify_result(res2_a, hash_q2, ["doc2", "doc1"], [1, 0], "q2")
    if getattr(res2_a, "metrics", {}) != getattr(res2_b, "metrics", {}):
        raise ValueError("Metrics non-deterministic q2")
    if getattr(res2_a, "ordered_hits", []) != getattr(res2_b, "ordered_hits", []):
        raise ValueError("Ranking non-deterministic q2")


def test_eval_pr_smoke_fixed_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from musubi.evals.runner import run_smoke_gate
    except ImportError:
        raise DefectStillPresent("musubi.evals.runner missing smoke gate")
    _assert_pr_smoke_fixed_embeddings(run_smoke_gate, monkeypatch)


def test_discrimination_pr_smoke_fixed_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    def correct_smoke(corpus: list[dict[str, Any]], query_embedding: list[float]) -> Any:
        import hashlib
        import json
        from math import log2

        h = hashlib.sha256(
            json.dumps(sorted(corpus, key=lambda x: x["id"]), sort_keys=True).encode("utf-8")
        ).hexdigest()

        def dot(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b))

        scored = []
        for c in corpus:
            scored.append((c["id"], dot(query_embedding, c["embedding"]), c["relevance"]))

        scored.sort(key=lambda x: x[1], reverse=True)
        ordered_hits = [x[0] for x in scored]

        relevances = [x[2] for x in scored]

        def dcg(rels: list[int]) -> float:
            return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(rels)))

        idcg = dcg(sorted(relevances, reverse=True))
        ndcg = dcg(relevances) / idcg if idcg > 0 else 0.0

        return type(
            "MockResult",
            (),
            {"metrics": {"ndcg@10": ndcg}, "corpus_checksum": h, "ordered_hits": ordered_hits},
        )()

    def wrong_smoke_requires_qdrant(
        corpus: list[dict[str, Any]], query_embedding: list[float]
    ) -> Any:
        import contextlib

        import qdrant_client

        with contextlib.suppress(Exception):
            qdrant_client.QdrantClient("http://localhost:6333")
        return correct_smoke(corpus, query_embedding=query_embedding)

    def wrong_smoke_echoes_input_order(
        corpus: list[dict[str, Any]], query_embedding: list[float]
    ) -> Any:
        import hashlib
        import json
        from math import log2

        h = hashlib.sha256(
            json.dumps(sorted(corpus, key=lambda x: x["id"]), sort_keys=True).encode("utf-8")
        ).hexdigest()
        ordered = [d["id"] for d in corpus]
        relevances = [d["relevance"] for d in corpus]

        def dcg(rels: list[int]) -> float:
            return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(rels)))

        idcg = dcg(sorted(relevances, reverse=True))
        ndcg = dcg(relevances) / idcg if idcg > 0 else 0.0
        return type(
            "MockResult",
            (),
            {"metrics": {"ndcg@10": ndcg}, "corpus_checksum": h, "ordered_hits": ordered},
        )()

    def wrong_smoke_hardcodes_first_fixture(
        corpus: list[dict[str, Any]], query_embedding: list[float]
    ) -> Any:
        # Always ranks doc1 first
        import hashlib
        import json

        h = hashlib.sha256(
            json.dumps(sorted(corpus, key=lambda x: x["id"]), sort_keys=True).encode("utf-8")
        ).hexdigest()
        return type(
            "MockResult",
            (),
            {"metrics": {"ndcg@10": 1.0}, "corpus_checksum": h, "ordered_hits": ["doc1", "doc2"]},
        )()

    def wrong_smoke_ignores_query(
        corpus: list[dict[str, Any]], query_embedding: list[float]
    ) -> Any:
        # Always uses q1_emb
        return correct_smoke(corpus, query_embedding=[1.0, 0.0])

    def wrong_smoke_metric_below_0(
        corpus: list[dict[str, Any]], query_embedding: list[float]
    ) -> Any:
        res = correct_smoke(corpus, query_embedding=query_embedding)
        res.metrics["ndcg@10"] = -0.5
        return res

    def wrong_smoke_metric_above_1(
        corpus: list[dict[str, Any]], query_embedding: list[float]
    ) -> Any:
        res = correct_smoke(corpus, query_embedding=query_embedding)
        res.metrics["ndcg@10"] = 1.5
        return res

    def wrong_smoke_checksum_depends_on_input_order(
        corpus: list[dict[str, Any]], query_embedding: list[float]
    ) -> Any:
        res = correct_smoke(corpus, query_embedding=query_embedding)
        import hashlib
        import json

        res.corpus_checksum = hashlib.sha256(
            json.dumps(corpus, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return res

    _assert_pr_smoke_fixed_embeddings(correct_smoke, monkeypatch)

    with pytest.raises(ValueError, match="Qdrant/TEI network hit detected"):
        _assert_pr_smoke_fixed_embeddings(wrong_smoke_requires_qdrant, monkeypatch)

    with pytest.raises(ValueError, match="Ranking mismatch q1_permuted"):
        _assert_pr_smoke_fixed_embeddings(wrong_smoke_echoes_input_order, monkeypatch)

    with pytest.raises(ValueError, match="Ranking mismatch q2"):
        _assert_pr_smoke_fixed_embeddings(wrong_smoke_hardcodes_first_fixture, monkeypatch)

    with pytest.raises(ValueError, match="Ranking mismatch q2"):
        _assert_pr_smoke_fixed_embeddings(wrong_smoke_ignores_query, monkeypatch)

    with pytest.raises(ValueError, match="Metric below 0 q1"):
        _assert_pr_smoke_fixed_embeddings(wrong_smoke_metric_below_0, monkeypatch)

    with pytest.raises(ValueError, match="Metric above 1 q1"):
        _assert_pr_smoke_fixed_embeddings(wrong_smoke_metric_above_1, monkeypatch)

    with pytest.raises(ValueError, match="Checksum depends on input order q1_permuted"):
        _assert_pr_smoke_fixed_embeddings(wrong_smoke_checksum_depends_on_input_order, monkeypatch)


def _assert_abstention_fpr(
    eval_func: Callable[[FrozenModelConfig, dict[str, list[dict[str, Any]]]], MockEvalReport],
) -> None:
    # 1. First scenario
    config1 = FrozenModelConfig(
        thresholds=(("fast", 0.5), ("deep", 0.8)), version="v1.0", calibrated_on="train_split_A"
    )

    test_data1 = {
        "fast_noise": [{"score": 0.4}, {"score": 0.6}],  # fpr 0.5
        "fast_answerable": [{"score": 0.6}, {"score": 0.4}],  # fnr 0.5
        "deep_noise": [{"score": 0.7}],  # fpr 0.0
        "deep_answerable": [{"score": 0.9}],  # fnr 0.0
    }

    # B4 Snapshot before
    snapshot_version = config1.version
    snapshot_cal = config1.calibrated_on
    snapshot_thresh = config1.thresholds

    r1 = eval_func(config1, test_data1)

    # B4 Verify snapshot
    if config1.version != snapshot_version:
        raise ValueError("Config version mutated")
    if config1.calibrated_on != snapshot_cal:
        raise ValueError("Config calibrated_on mutated")
    if config1.thresholds != snapshot_thresh:
        raise ValueError("Config threshold mutated")

    if r1.version != "v1.0":
        raise ValueError("Version mismatch")
    if r1.calibrated_on != "train_split_A":
        raise ValueError("Calibration mismatch")

    if getattr(r1, "fast_fpr") != 0.5:
        raise ValueError("Fast FPR exact mismatch 1")
    if getattr(r1, "fast_fnr") != 0.5:
        raise ValueError("Fast FNR exact mismatch 1")
    if getattr(r1, "deep_fpr") != 0.0:
        raise ValueError("Deep FPR exact mismatch 1")
    if getattr(r1, "deep_fnr") != 0.0:
        raise ValueError("Deep FNR exact mismatch 1")

    # 2. Second scenario
    config2 = FrozenModelConfig(
        thresholds=(("fast", 0.2), ("deep", 0.5)), version="v1.1", calibrated_on="train_split_B"
    )

    test_data2 = {
        "fast_noise": [{"score": 0.1}],  # fpr 0.0
        "fast_answerable": [{"score": 0.3}, {"score": 0.1}],  # fnr 0.5
        "deep_noise": [{"score": 0.6}, {"score": 0.4}],  # fpr 0.5
        "deep_answerable": [{"score": 0.7}],  # fnr 0.0
    }

    # B4 Snapshot before
    snapshot_version2 = config2.version
    snapshot_cal2 = config2.calibrated_on
    snapshot_thresh2 = config2.thresholds

    r2 = eval_func(config2, test_data2)

    # B4 Verify snapshot
    if config2.version != snapshot_version2:
        raise ValueError("Config version mutated")
    if config2.calibrated_on != snapshot_cal2:
        raise ValueError("Config calibrated_on mutated")
    if config2.thresholds != snapshot_thresh2:
        raise ValueError("Config threshold mutated")

    if r2.version != "v1.1":
        raise ValueError("Version mismatch")
    if r2.calibrated_on != "train_split_B":
        raise ValueError("Calibration mismatch")

    if getattr(r2, "fast_fpr") != 0.0:
        raise ValueError("Fast FPR exact mismatch 2")
    if getattr(r2, "fast_fnr") != 0.5:
        raise ValueError("Fast FNR exact mismatch 2")
    if getattr(r2, "deep_fpr") != 0.5:
        raise ValueError("Deep FPR exact mismatch 2")
    if getattr(r2, "deep_fnr") != 0.0:
        raise ValueError("Deep FNR exact mismatch 2")


def test_eval_abstention_fpr() -> None:
    try:
        from musubi.evals.gates import check_abstention_fpr
    except ImportError:
        raise DefectStillPresent("musubi.evals.gates missing abstention")
    _assert_abstention_fpr(check_abstention_fpr)


def test_discrimination_abstention_fpr() -> None:
    def correct_check(
        config: FrozenModelConfig, results: dict[str, list[dict[str, Any]]]
    ) -> MockEvalReport:
        thresh_dict = dict(config.thresholds)

        def calc_fpr(hits: list[dict[str, Any]], thresh: float) -> float:
            fp = sum(1 for h in hits if h["score"] >= thresh)
            return fp / len(hits) if hits else 0.0

        def calc_fnr(hits: list[dict[str, Any]], thresh: float) -> float:
            fn = sum(1 for h in hits if h["score"] < thresh)
            return fn / len(hits) if hits else 0.0

        return MockEvalReport(
            version=config.version,
            calibrated_on=config.calibrated_on,
            fast_fpr=calc_fpr(results["fast_noise"], thresh_dict["fast"]),
            fast_fnr=calc_fnr(results["fast_answerable"], thresh_dict["fast"]),
            deep_fpr=calc_fpr(results["deep_noise"], thresh_dict["deep"]),
            deep_fnr=calc_fnr(results["deep_answerable"], thresh_dict["deep"]),
        )

    _assert_abstention_fpr(correct_check)

    def wrong_check_hardcodes_first_report(
        config: FrozenModelConfig, results: dict[str, list[dict[str, Any]]]
    ) -> MockEvalReport:
        return MockEvalReport("v1.0", "train_split_A", 0.5, 0.5, 0.0, 0.0)

    with pytest.raises(ValueError, match="Version mismatch"):
        _assert_abstention_fpr(wrong_check_hardcodes_first_report)

    def wrong_check_ignores_threshold(
        config: FrozenModelConfig, results: dict[str, list[dict[str, Any]]]
    ) -> MockEvalReport:
        return MockEvalReport(config.version, config.calibrated_on, 0.0, 0.0, 0.0, 0.0)

    with pytest.raises(ValueError, match="Fast FPR exact mismatch 1"):
        _assert_abstention_fpr(wrong_check_ignores_threshold)

    def wrong_check_swaps_fpr_fnr(
        config: FrozenModelConfig, results: dict[str, list[dict[str, Any]]]
    ) -> MockEvalReport:
        rep = correct_check(config, results)
        rep.fast_fpr, rep.fast_fnr = rep.fast_fnr, rep.fast_fpr
        rep.deep_fpr, rep.deep_fnr = rep.deep_fnr, rep.deep_fpr
        return rep

    with pytest.raises(ValueError, match="Fast FPR exact mismatch 2"):
        _assert_abstention_fpr(wrong_check_swaps_fpr_fnr)

    def wrong_check_calibration_mismatch(
        config: FrozenModelConfig, results: dict[str, list[dict[str, Any]]]
    ) -> MockEvalReport:
        rep = correct_check(config, results)
        rep.calibrated_on = "wrong_cal"
        return rep

    with pytest.raises(ValueError, match="Calibration mismatch"):
        _assert_abstention_fpr(wrong_check_calibration_mismatch)

    def wrong_check_mutates_threshold(
        config: FrozenModelConfig, results: dict[str, list[dict[str, Any]]]
    ) -> MockEvalReport:
        object.__setattr__(config, "thresholds", (("fast", 0.0), ("deep", 0.0)))
        return correct_check(config, results)

    with pytest.raises(ValueError, match="Config threshold mutated"):
        _assert_abstention_fpr(wrong_check_mutates_threshold)

    def wrong_check_mutates_version(
        config: FrozenModelConfig, results: dict[str, list[dict[str, Any]]]
    ) -> MockEvalReport:
        object.__setattr__(config, "version", "v0.0")
        return correct_check(config, results)

    with pytest.raises(ValueError, match="Config version mutated"):
        _assert_abstention_fpr(wrong_check_mutates_version)

    def wrong_check_bad_fn_only(
        config: FrozenModelConfig, results: dict[str, list[dict[str, Any]]]
    ) -> MockEvalReport:
        rep = correct_check(config, results)
        rep.fast_fnr = 1.0  # Deliberately ruin FNR
        return rep

    with pytest.raises(ValueError, match="Fast FNR exact mismatch 1"):
        _assert_abstention_fpr(wrong_check_bad_fn_only)


def _assert_contradiction_blending(
    eval_func: Callable[[list[dict[str, Any]], str, float], list[dict[str, Any]]],
) -> None:
    corpus_con = [
        {"id": "doc_pro", "text": "X is safe.", "base_score": 0.9},
        {"id": "doc_con", "text": "X is dangerous.", "base_score": 0.85},
        {"id": "doc_other", "text": "X was discovered in 1990.", "base_score": 0.7},
    ]
    corpus_ctrl = [
        {"id": "doc_pro", "text": "X is safe.", "base_score": 0.9},
        {"id": "doc_agree", "text": "X is secure.", "base_score": 0.85},
        {"id": "doc_other", "text": "X was discovered in 1990.", "base_score": 0.7},
    ]

    # Test 1: Penalty 0.1
    res_con_1 = eval_func(corpus_con, "Is X safe?", 0.1)
    res_ctrl_1 = eval_func(corpus_ctrl, "Is X safe?", 0.1)

    # Test 2: Penalty 0.2
    res_con_2 = eval_func(corpus_con, "Is X safe?", 0.2)

    ctrl_dict = {r["id"]: r for r in res_ctrl_1}
    if ctrl_dict["doc_pro"].get("contradiction_penalty", 0.0) != 0.0:
        raise ValueError("Control penalty applied incorrectly")
    if abs(ctrl_dict["doc_pro"]["score"] - 0.9) > 0.001:
        raise ValueError("Control score mutated incorrectly")

    con_dict_1 = {r["id"]: r for r in res_con_1}
    con_dict_2 = {r["id"]: r for r in res_con_2}

    top_5 = [r["id"] for r in res_con_1[:5]]
    if "doc_pro" not in top_5 or "doc_con" not in top_5:
        raise ValueError("Contradictory facts not in top-K context")

    if abs(con_dict_1["doc_pro"].get("contradiction_penalty", 0.0) - 0.1) > 0.001:
        raise ValueError("doc_pro penalty mismatch 1")
    if abs(con_dict_1["doc_con"].get("contradiction_penalty", 0.0) - 0.1) > 0.001:
        raise ValueError("doc_con penalty mismatch 1")

    if abs(con_dict_1["doc_pro"]["score"] - 0.8) > 0.001:  # 0.9 - 0.1
        raise ValueError("doc_pro exact score math failure 1")
    if abs(con_dict_1["doc_con"]["score"] - 0.75) > 0.001:  # 0.85 - 0.1
        raise ValueError("doc_con exact score math failure 1")

    if abs(con_dict_2["doc_pro"].get("contradiction_penalty", 0.0) - 0.2) > 0.001:
        raise ValueError("doc_pro penalty mismatch 2")
    if abs(con_dict_2["doc_pro"]["score"] - 0.7) > 0.001:  # 0.9 - 0.2
        raise ValueError("doc_pro exact score math failure 2")

    if con_dict_1["doc_other"].get("contradiction_penalty", 0.0) != 0.0:
        raise ValueError("Penalize-all detected")
    if abs(con_dict_1["doc_other"]["score"] - 0.7) > 0.001:
        raise ValueError("doc_other exact score math failure")


def test_eval_contradiction_blending() -> None:
    try:
        from musubi.evals.gates import check_contradiction_blending
    except ImportError:
        raise DefectStillPresent("musubi.evals.gates missing contradiction")
    _assert_contradiction_blending(check_contradiction_blending)


def test_discrimination_contradiction_blending() -> None:
    def correct_check(
        corpus: list[dict[str, Any]], query: str, config_pen: float
    ) -> list[dict[str, Any]]:
        has_con = any(d["id"] == "doc_con" for d in corpus)
        res = []
        for d in corpus:
            pen = config_pen if has_con and d["id"] in ("doc_pro", "doc_con") else 0.0
            res.append(
                {"id": d["id"], "score": d["base_score"] - pen, "contradiction_penalty": pen}
            )
        res.sort(key=lambda x: x["score"], reverse=True)
        return res

    def wrong_ignore_penalty(
        corpus: list[dict[str, Any]], query: str, config_pen: float
    ) -> list[dict[str, Any]]:
        return [
            {"id": d["id"], "score": d["base_score"], "contradiction_penalty": 0.0} for d in corpus
        ]

    def wrong_drop_one(
        corpus: list[dict[str, Any]], query: str, config_pen: float
    ) -> list[dict[str, Any]]:
        c = [d for d in corpus if d["id"] != "doc_con"]
        return correct_check(c, query, config_pen)

    def wrong_penalize_all(
        corpus: list[dict[str, Any]], query: str, config_pen: float
    ) -> list[dict[str, Any]]:
        has_con = any(d["id"] == "doc_con" for d in corpus)
        res = []
        for d in corpus:
            pen = config_pen if has_con else 0.0
            res.append(
                {"id": d["id"], "score": d["base_score"] - pen, "contradiction_penalty": pen}
            )
        return res

    def wrong_hardcode(
        corpus: list[dict[str, Any]], query: str, config_pen: float
    ) -> list[dict[str, Any]]:
        res = []
        for d in corpus:
            pen = config_pen if d["id"] == "doc_pro" else 0.0
            res.append(
                {"id": d["id"], "score": d["base_score"] - pen, "contradiction_penalty": pen}
            )
        return res

    def wrong_arbitrary_score_math(
        corpus: list[dict[str, Any]], query: str, config_pen: float
    ) -> list[dict[str, Any]]:
        has_con = any(d["id"] == "doc_con" for d in corpus)
        res = []
        for d in corpus:
            pen = config_pen if has_con and d["id"] in ("doc_pro", "doc_con") else 0.0
            # Wrong: arbitrary lowered score instead of exact base - penalty
            score = 0.5 if pen > 0 else d["base_score"]
            res.append({"id": d["id"], "score": score, "contradiction_penalty": pen})
        res.sort(key=lambda x: x["score"], reverse=True)
        return res

    def wrong_insensitive_to_config(
        corpus: list[dict[str, Any]], query: str, config_pen: float
    ) -> list[dict[str, Any]]:
        return correct_check(corpus, query, 0.1)  # Hardcodes 0.1, ignores config_pen

    _assert_contradiction_blending(correct_check)

    with pytest.raises(ValueError, match="doc_pro penalty mismatch 1"):
        _assert_contradiction_blending(wrong_ignore_penalty)

    with pytest.raises(ValueError, match="Contradictory facts not in top-K context"):
        _assert_contradiction_blending(wrong_drop_one)

    with pytest.raises(ValueError, match="Penalize-all detected"):
        _assert_contradiction_blending(wrong_penalize_all)

    with pytest.raises(ValueError, match="Control penalty applied incorrectly"):
        _assert_contradiction_blending(wrong_hardcode)

    with pytest.raises(ValueError, match="doc_pro exact score math failure 1"):
        _assert_contradiction_blending(wrong_arbitrary_score_math)

    with pytest.raises(ValueError, match="doc_pro penalty mismatch 2"):
        _assert_contradiction_blending(wrong_insensitive_to_config)


def _assert_cross_plane_blending(
    eval_func: Callable[
        [list[dict[str, Any]], list[dict[str, Any]], dict[str, float]], list[dict[str, Any]]
    ],
) -> None:
    curated = [
        {"id": "c1", "score": 0.8},
        {"id": "dup1", "score": 0.6},
    ]
    episodic = [
        {"id": "e1", "score": 0.9},
        {"id": "dup1", "score": 0.7},
    ]

    res1 = eval_func(curated, episodic, {"curated": 1.0, "episodic": 0.5})
    res2 = eval_func(curated, episodic, {"curated": 0.5, "episodic": 1.0})

    r1_dict = {r["id"]: r for r in res1}
    r2_dict = {r["id"]: r for r in res2}

    if "c1" not in r1_dict or "e1" not in r1_dict:
        raise ValueError("Missing multi-plane hits")

    if abs(r1_dict["dup1"]["score"] - 0.6) > 0.001:
        raise ValueError("Double-count boost or wrong math")
    if abs(r2_dict["dup1"]["score"] - 0.7) > 0.001:
        raise ValueError("Double-count boost or wrong math")

    if abs(r1_dict["c1"]["score"] - 0.8) > 0.001:
        raise ValueError("Wrong score math")
    if abs(r1_dict["e1"]["score"] - 0.45) > 0.001:
        raise ValueError("Wrong score math")

    order1 = [r["id"] for r in res1]
    order2 = [r["id"] for r in res2]
    if order1 == order2:
        raise ValueError("Ordering unaffected by weights")

    provs = r1_dict["dup1"].get("provenance", [])
    if "curated" not in provs or "episodic" not in provs:
        raise ValueError("Missing blended provenance")


def test_eval_cross_plane_blending() -> None:
    try:
        from musubi.evals.gates import check_cross_plane_blending
    except ImportError:
        raise DefectStillPresent("musubi.evals.gates missing cross plane")
    _assert_cross_plane_blending(check_cross_plane_blending)


def test_discrimination_cross_plane_blending() -> None:
    def correct_check(cur: list[Any], epi: list[Any], w: dict[str, float]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for d in cur:
            merged[d["id"]] = {
                "id": d["id"],
                "score": d["score"] * w.get("curated", 1.0),
                "provenance": ["curated"],
            }
        for d in epi:
            ns = d["score"] * w.get("episodic", 1.0)
            if d["id"] in merged:
                merged[d["id"]]["score"] = max(merged[d["id"]]["score"], ns)
                merged[d["id"]]["provenance"].append("episodic")
            else:
                merged[d["id"]] = {"id": d["id"], "score": ns, "provenance": ["episodic"]}
        res = list(merged.values())
        res.sort(key=lambda x: x["score"], reverse=True)
        return res

    def wrong_single_plane(
        cur: list[Any], epi: list[Any], w: dict[str, float]
    ) -> list[dict[str, Any]]:
        return correct_check(cur, [], w)

    def wrong_unweighted(
        cur: list[Any], epi: list[Any], w: dict[str, float]
    ) -> list[dict[str, Any]]:
        return correct_check(cur, epi, {"curated": 1.0, "episodic": 1.0})

    def wrong_double_count_boost(
        cur: list[Any], epi: list[Any], w: dict[str, float]
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for d in cur:
            merged[d["id"]] = {
                "id": d["id"],
                "score": d["score"] * w.get("curated", 1.0),
                "provenance": ["curated"],
            }
        for d in epi:
            ns = d["score"] * w.get("episodic", 1.0)
            if d["id"] in merged:
                merged[d["id"]]["score"] += ns
                merged[d["id"]]["provenance"].append("episodic")
            else:
                merged[d["id"]] = {"id": d["id"], "score": ns, "provenance": ["episodic"]}
        res = list(merged.values())
        res.sort(key=lambda x: x["score"], reverse=True)
        return res

    def wrong_no_provenance(
        cur: list[Any], epi: list[Any], w: dict[str, float]
    ) -> list[dict[str, Any]]:
        res = correct_check(cur, epi, w)
        for r in res:
            r["provenance"] = ["curated"]
        return res

    _assert_cross_plane_blending(correct_check)

    with pytest.raises(ValueError, match="Missing multi-plane hits"):
        _assert_cross_plane_blending(wrong_single_plane)

    with pytest.raises(ValueError, match=r"Wrong score math|Double-count boost"):
        _assert_cross_plane_blending(wrong_unweighted)

    with pytest.raises(ValueError, match="Double-count boost"):
        _assert_cross_plane_blending(wrong_double_count_boost)

    with pytest.raises(ValueError, match="Missing blended provenance"):
        _assert_cross_plane_blending(wrong_no_provenance)


def _assert_provisional_immediate_recall(
    check_func: Callable[[dict[str, list[str]], str], bool],
) -> None:
    # Ensure provisional doc is in top hits
    assert check_func({"q_prov": ["docA", "prov_doc"]}, "prov_doc") is True

    try:
        check_func({"q_prov": ["docA", "docB"]}, "prov_doc")
        raise RuntimeError("Failed to catch missing provisional document")
    except ValueError:
        pass


def test_eval_provisional_immediate_recall() -> None:
    try:
        from musubi.evals.gates import check_provisional_recall
    except ImportError:
        raise DefectStillPresent("musubi.evals.gates missing provisional recall")
    _assert_provisional_immediate_recall(check_provisional_recall)


def test_discrimination_provisional_immediate_recall() -> None:
    def correct_check(results: dict[str, list[str]], prov_id: str) -> bool:
        for q, hits in results.items():
            if prov_id not in hits:
                raise ValueError("Provisional doc not recalled")
        return True

    _assert_provisional_immediate_recall(correct_check)

    def wrong_check_ignores_missing(results: dict[str, list[str]], prov_id: str) -> bool:
        return True

    with pytest.raises(RuntimeError, match="Failed to catch missing provisional document"):
        _assert_provisional_immediate_recall(wrong_check_ignores_missing)
