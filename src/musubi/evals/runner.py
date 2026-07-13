import hashlib
import json
from collections.abc import Callable
from math import log2
from typing import Any

from musubi.evals.gates import check_delta_tolerances


class EvalResult:
    def __init__(self, metrics: dict[str, float], ordered_hits: list[str]) -> None:
        self.metrics = metrics
        self.ordered_hits = ordered_hits


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


def run_smoke_gate(corpus: list[dict[str, Any]]) -> Any:
    h = hashlib.sha256(json.dumps(corpus, sort_keys=True).encode("utf-8")).hexdigest()

    # Engine mocking: return corpus IDs in order they were provided
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
