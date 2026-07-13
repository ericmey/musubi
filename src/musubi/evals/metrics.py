from math import log2


def ndcg_at_k(scores: list[int], ideal_scores: list[int], k: int) -> float:
    def dcg(s: list[int], limit: int) -> float:
        return float(sum((2**r - 1) / log2(i + 2) for i, r in enumerate(s[:limit])))

    idcg = dcg(sorted(ideal_scores, reverse=True), k)
    return dcg(scores, k) / idcg if idcg > 0 else 0.0
