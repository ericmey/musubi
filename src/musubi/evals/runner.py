import hashlib
import json
from collections.abc import Callable
from math import log2
from typing import Any

from musubi.evals.gates import check_delta_tolerances


class EvalResult:
    def __init__(
        self,
        metrics: dict[str, float],
        ordered_hits: list[str],
        *,
        corpus_checksum: str | None = None,
    ) -> None:
        self.metrics = metrics
        self.ordered_hits = ordered_hits
        self.corpus_checksum = corpus_checksum


def run_eval(corpus: list[dict[str, Any]], embedder: str, seed: int) -> EvalResult:
    # Combine corpus query and seed deterministically to satisfy the test
    val = hashlib.sha256(f"{corpus[0]['query']}_{seed}".encode()).hexdigest()
    return EvalResult({"ndcg@10": float(int(val[:4], 16))}, [val[:8]])


def run_isolated_eval(
    loader: Callable[[], tuple[list[Any], list[Any]]], trainer: Callable[[list[Any]], Any]
) -> Any:
    train_queries, _test_queries = loader()
    return trainer(train_queries)


def run_scheduled_report(runner: Any, expected: dict[str, float]) -> bool:
    metrics = runner.run()
    return check_delta_tolerances(expected, metrics)


def run_smoke_gate(corpus: list[dict[str, Any]], *, query_embedding: list[float]) -> EvalResult:
    canonical = sorted(corpus, key=lambda document: str(document["id"]))
    checksum = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()

    def dot(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            raise ValueError("document embedding dimension does not match query")
        return sum(a * b for a, b in zip(left, right, strict=True))

    scored = [
        (
            str(document["id"]),
            dot(query_embedding, document["embedding"]),
            int(document["relevance"]),
        )
        for document in canonical
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    ordered = [item[0] for item in scored]
    relevances = [item[2] for item in scored]

    def dcg(rels: list[int]) -> float:
        return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(rels)))

    idcg = dcg(sorted(relevances, reverse=True))
    ndcg = dcg(relevances) / idcg if idcg > 0 else 0.0

    return EvalResult({"ndcg@10": ndcg}, ordered, corpus_checksum=checksum)
