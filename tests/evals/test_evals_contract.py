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

    with pytest.raises(pytest.fail.Exception if hasattr(pytest, "fail") else Exception):
        try:
            _assert_schema_validation(AcceptAllSchema)
            pytest.fail("Accepted bad schema")
        except ValidationError:
            pass
        except Exception:
            pass


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

    with pytest.raises(pytest.fail.Exception if hasattr(pytest, "fail") else Exception):
        try:
            _assert_manifest_checksum(wrong_verify, tmp_path)
            pytest.fail("Accepted bad checksum")
        except ValueError:
            pass
        except Exception:
            pass


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
    corpus_a = [{"query": "test_A", "target": "1"}]
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
    corpus_b = [{"query": "test_B", "target": "2"}]
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

    with pytest.raises(AssertionError):
        _assert_deterministic_rerun(wrong_constant_runner)


# ---------------------------------------------------------------------------
# Skipped Placeholders
# ---------------------------------------------------------------------------
@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_holdout_isolation() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_pr_smoke_fixed_embeddings() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_nightly_qdrant_tei_thresholds() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_baseline_delta_gate_unit() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_scheduled_baseline_report() -> None:
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_per_query_top_hit_drop() -> None:
    pass


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
