import pytest
from pydantic import ValidationError


class DefectStillPresent(Exception):
    pass


# ---------------------------------------------------------------------------
# Test 1: Metric Formula Correctness
# ---------------------------------------------------------------------------
@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason="RET-004: NDCG Metric implementation missing from module",
)
def test_eval_metric_formula_correctness():
    try:
        from musubi.evals.metrics import ndcg_at_k
    except ImportError:
        raise DefectStillPresent("musubi.evals.metrics module does not exist")

    # Baseline comparison
    scores = [3, 1, 2, 0]
    ideal = [3, 2, 1, 0]

    # Mathematical bound
    assert round(ndcg_at_k(scores, ideal, 10), 4) == 0.9721

    # Boundary: K-truncation explicitly filters list before IDCG normalization
    assert round(ndcg_at_k(scores, ideal, k=2), 4) == 0.8581

    # Boundary: Zero IDCG (no relevant documents) evaluates safely to 0.0 without ZeroDivisionError
    assert ndcg_at_k([0, 0], [0, 0], k=10) == 0.0


# ---------------------------------------------------------------------------
# Test 2: Corpus Schema Validation
# ---------------------------------------------------------------------------
@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: Corpus Pydantic Schema loader missing"
)
def test_eval_corpus_schema_validation():
    try:
        from musubi.evals.schema import GoldenQuery
    except ImportError:
        raise DefectStillPresent("musubi.evals.schema module does not exist")

    # Control: Healthy schema passes
    valid_data = {
        "id": "q001",
        "text": "healthy query",
        "relevant": [{"object_id": "1", "relevance": 3}],
        "mode": "fast",
        "namespace": "test/ns",
    }
    obj = GoldenQuery.model_validate(valid_data)
    assert obj.id == "q001"
    assert len(obj.relevant) == 1

    # Fault: Missing required 'relevant' field fails
    with pytest.raises(ValidationError):
        GoldenQuery.model_validate({"id": "q002", "text": "bad query"})


# ---------------------------------------------------------------------------
# Test 3: Corpus Manifest Checksum
# ---------------------------------------------------------------------------
@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: Corpus manifest checksum logic missing"
)
def test_eval_corpus_manifest_checksum(tmp_path):
    try:
        from musubi.evals.corpus import verify_manifest
    except ImportError:
        raise DefectStillPresent("musubi.evals.corpus module does not exist")

    # Setup files
    corpus_file = tmp_path / "corpus.yaml"
    corpus_file.write_bytes(b"content")
    import hashlib

    true_hash = hashlib.sha256(b"content").hexdigest()

    manifest = {"name": "test_corpus", "files": {"corpus.yaml": true_hash}}

    # Control: Correct hash passes cleanly
    assert verify_manifest(manifest, base_dir=tmp_path) is True

    # Fault: One-byte mutation breaks checksum
    corpus_file.write_bytes(b"content2")
    with pytest.raises(ValueError, match="checksum"):
        verify_manifest(manifest, base_dir=tmp_path)


# ---------------------------------------------------------------------------
# Test 4: Deterministic Rerun Stability
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, raises=DefectStillPresent, reason="RET-004: Eval runner missing")
def test_eval_deterministic_rerun():
    try:
        from musubi.evals.runner import run_eval
    except ImportError:
        raise DefectStillPresent("musubi.evals.runner module does not exist")

    # Desired Behavior: Running the exact same evaluation configuration twice
    # must produce the exact same metric output and ranking order.

    corpus = [{"query": "test", "target": "1"}]
    # Run 1
    res1 = run_eval(corpus=corpus, embedder="fake", seed=42)
    # Run 2
    res2 = run_eval(corpus=corpus, embedder="fake", seed=42)

    assert res1.metrics["ndcg@10"] == res2.metrics["ndcg@10"]
    assert res1.ordered_hits == res2.ordered_hits

    # Sensitivity Control: Changing seed/query produces DIFFERENT output
    res3 = run_eval(corpus=[{"query": "different", "target": "2"}], embedder="fake", seed=99)
    assert res1.metrics != res3.metrics or res1.ordered_hits != res3.ordered_hits


# The remaining 10 tests are preserved as documentation of the pending inventory,
# explicitly raising a skipped exception so they do not artificially pad the test count.


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_holdout_isolation():
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_pr_smoke_fixed_embeddings():
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_nightly_qdrant_tei_thresholds():
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_baseline_delta_gate_unit():
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_scheduled_baseline_report():
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_per_query_top_hit_drop():
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_abstention_fpr():
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_contradiction_blending():
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_cross_plane_blending():
    pass


@pytest.mark.skip(reason="Pending RET-004 implementation")
def test_eval_provisional_immediate_recall():
    pass
