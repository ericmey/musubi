import pytest
from typing import Any
from collections.abc import Callable
from math import log2

def _assert_data_driven_retrieval(runner_func: Callable[[list[dict[str, Any]]], Any]) -> None:
    # A true data-driven runner must process queries against documents using embeddings.
    # It must NOT just echo the input order.
    # We supply a corpus that explicitly decouples queries from the candidate order.
    corpus = [
        {
            "query": "find doc1",
            "mode": "fast",
            "candidates": [
                {"id": "doc2", "embedding": [0.0, 0.0], "relevance": 0},
                {"id": "doc1", "embedding": [1.0, 1.0], "relevance": 1}
            ],
            "query_embedding": [1.0, 1.0]
        }
    ]
    
    try:
        res = runner_func(corpus)
    except Exception as e:
        raise ValueError(f"Runner failed to process data-driven schema: {e}")

    # 1. Metric Range [0,1]
    ndcg = getattr(res, "metrics", {}).get("ndcg@10", -1.0)
    if ndcg < 0.0 or ndcg > 1.0:
        raise ValueError(f"Metrics out of range [0, 1]: {ndcg}")
        
    # 2. Real deterministic retrieval (order by similarity, not input order)
    # query [1,1] dot [1,1] = 2.0 (doc1)
    # query [1,1] dot [0,0] = 0.0 (doc2)
    # So doc1 must be ranked before doc2
    ordered = getattr(res, "ordered_hits", [])
    if ordered != ["doc1", "doc2"]:
        raise ValueError(f"Failed to rank by similarity. Got: {ordered}")

def test_discrimination_data_driven_retrieval() -> None:
    def correct_runner(corpus: list[dict[str, Any]]) -> Any:
        q_emb = corpus[0]["query_embedding"]
        cands = corpus[0]["candidates"]
        
        def dot(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b))
            
        scored = []
        for c in cands:
            scored.append((c["id"], dot(q_emb, c["embedding"]), c["relevance"]))
            
        scored.sort(key=lambda x: x[1], reverse=True)
        ordered_hits = [x[0] for x in scored]
        
        rels = [x[2] for x in scored]
        def dcg(r: list[int]) -> float:
            return float(sum((2**v - 1) / log2(i + 2) for i, v in enumerate(r)))
        idcg = dcg(sorted(rels, reverse=True))
        ndcg = dcg(rels) / idcg if idcg > 0 else 0.0
        
        return type("MockResult", (), {"metrics": {"ndcg@10": ndcg}, "ordered_hits": ordered_hits})()

    def wrong_runner_echoes_input(corpus: list[dict[str, Any]]) -> Any:
        # Echoes input order of candidates
        ordered_hits = [c["id"] for c in corpus[0]["candidates"]]
        return type("MockResult", (), {"metrics": {"ndcg@10": 1.0}, "ordered_hits": ordered_hits})()
        
    def wrong_runner_bad_metrics(corpus: list[dict[str, Any]]) -> Any:
        res = correct_runner(corpus)
        res.metrics["ndcg@10"] = 42.0
        return res

    _assert_data_driven_retrieval(correct_runner)
    
    with pytest.raises(ValueError, match="Failed to rank by similarity"):
        _assert_data_driven_retrieval(wrong_runner_echoes_input)
        
    with pytest.raises(ValueError, match="Metrics out of range"):
        _assert_data_driven_retrieval(wrong_runner_bad_metrics)

class DefectStillPresent(Exception):
    pass

@pytest.mark.xfail(strict=True, raises=ValueError, reason="Toy implementation echoes input and fails data schema")
def test_toy_implementation_is_data_driven() -> None:
    from musubi.evals.runner import run_smoke_gate
    _assert_data_driven_retrieval(run_smoke_gate)

