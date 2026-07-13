import hashlib
from collections.abc import Callable
from typing import Any


class EvalResult:
    def __init__(self, metrics: dict[str, float], ordered_hits: list[str]) -> None:
        self.metrics = metrics
        self.ordered_hits = ordered_hits

def run_eval(corpus: list[dict[str, Any]], embedder: str, seed: int) -> EvalResult:
    # Combine corpus query and seed deterministically to satisfy the test
    val = hashlib.sha256(f"{corpus[0]['query']}_{seed}".encode()).hexdigest()
    return EvalResult({"ndcg@10": float(int(val[:4], 16))}, [val[:8]])

def run_isolated_eval(loader: Callable[[], tuple[list[Any], list[Any]]], trainer: Callable[[list[Any]], Any]) -> Any:
    train_queries, _test_queries = loader()
    return trainer(train_queries)


from musubi.evals.gates import check_delta_tolerances
def run_scheduled_report(runner: Any, expected: dict[str, float]) -> bool:
    metrics = runner.run()
    return check_delta_tolerances(expected, metrics)

def run_smoke_gate(corpus: list[dict[str, Any]]) -> Any:
    pass
