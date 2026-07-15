from math import log2


def ndcg_at_k(scores: list[int], ideal_scores: list[int], k: int) -> float:
    def dcg(s: list[int], limit: int) -> float:
        return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(s[:limit])))

    idcg = dcg(sorted(ideal_scores, reverse=True), k)
    return dcg(scores, k) / idcg if idcg > 0 else 0.0


def rr(relevances: list[int]) -> float:
    """Reciprocal rank: 1/(1-based rank of the first relevant item), or 0.0 if none is relevant.

    The mean of this over a query set is MRR; the gates already consume an ``mrr`` metric, this is
    the formula that produces it."""
    for i, r in enumerate(relevances):
        if r > 0:
            return 1.0 / (i + 1)
    return 0.0


def recall_at_k(relevances: list[int], total_relevant: int, k: int) -> float:
    """Recall@k: fraction of ALL relevant items retrieved within the top-k, or 0.0 when nothing is
    relevant (guards division by zero). ``relevances`` is the ranked list of per-hit relevances."""
    if total_relevant <= 0:
        return 0.0
    hits = sum(1 for r in relevances[:k] if r > 0)
    return hits / total_relevant
