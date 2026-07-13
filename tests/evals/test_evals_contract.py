import pytest

class ContractViolation(Exception):
    pass

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for MRR/NDCG metric formula correctness")
def test_eval_metric_formula_correctness():
    raise ContractViolation("Metric math functions are unproven")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for corpus schema validation")
def test_eval_corpus_schema_validation():
    raise ContractViolation("Corpus loader skips Pydantic schema validation")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for corpus manifest checksum")
def test_eval_corpus_manifest_checksum():
    raise ContractViolation("Corpus modifications bypass manifest hash checks")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for deterministic rerun stability")
def test_eval_deterministic_rerun():
    raise ContractViolation("Repeated queries not proven deterministic")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for holdout isolation")
def test_eval_holdout_isolation():
    raise ContractViolation("Holdout queries/labels not proven isolated from tuning/calibration")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for PR smoke fixed embeddings")
def test_eval_pr_smoke_fixed_embeddings():
    raise ContractViolation("PR pipeline lacks fast in-memory smoke gate")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for nightly qdrant tei thresholds")
def test_eval_nightly_qdrant_tei_thresholds():
    raise ContractViolation("Scheduled pipeline lacks explicit MRR/NDCG threshold enforcement")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for baseline delta gate unit")
def test_eval_baseline_delta_gate_unit():
    raise ContractViolation("Delta math unproven via fixed boundary inputs")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for scheduled baseline report")
def test_eval_scheduled_baseline_report():
    raise ContractViolation("Scheduled pipeline does not fail on explicit degradation deltas")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for per query top hit drop")
def test_eval_per_query_top_hit_drop():
    raise ContractViolation("Per-query regression fails silently instead of hard-failing")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for abstention FPR")
def test_eval_abstention_fpr():
    raise ContractViolation("FPR and explicitly train-calibrated thresholds unproven against noise")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for contradiction blending")
def test_eval_contradiction_blending():
    raise ContractViolation("Contradiction context not proven retrievable")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for cross plane blending")
def test_eval_cross_plane_blending():
    raise ContractViolation("Cross-plane hybrid retrieval not proven")

@pytest.mark.xfail(strict=True, raises=ContractViolation, reason="RET-004: Missing eval for provisional immediate recall")
def test_eval_provisional_immediate_recall():
    raise ContractViolation("Provisional memories drop off due to score penalty without explicit proof")
