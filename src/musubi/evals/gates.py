import math
from dataclasses import dataclass
from typing import Any


def check_nightly_thresholds(metrics: dict[str, float], mode: str) -> bool:
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

def check_delta_tolerances(base: dict[str, float], cand: dict[str, float]) -> bool:
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

def check_top_hit_drops(base: dict[str, list[str]], cand: dict[str, list[str]]) -> bool:
    for q, hits in base.items():
        top_hit = hits[0] if hits else None
        if top_hit:
            cand_hits = cand.get(q, [])
            if top_hit not in cand_hits[:10]:
                raise ValueError("top-relevant dropped")
    return True



@dataclass(frozen=True)
class FrozenModelConfig:
    thresholds: tuple[tuple[str, float], ...]
    version: str
    calibrated_on: str

class MockEvalReport:
    def __init__(
        self,
        version: str,
        calibrated_on: str,
        fast_fpr: float,
        fast_fnr: float,
        deep_fpr: float,
        deep_fnr: float,
    ):
        self.version = version
        self.calibrated_on = calibrated_on
        self.fast_fpr = fast_fpr
        self.fast_fnr = fast_fnr
        self.deep_fpr = deep_fpr
        self.deep_fnr = deep_fnr

def check_abstention_fpr(config: FrozenModelConfig, results: dict[str, list[dict[str, Any]]]) -> MockEvalReport:
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

def check_contradiction_blending(corpus: list[dict[str, Any]], query: str, config_pen: float) -> list[dict[str, Any]]:
    has_con = any(d["id"] == "doc_con" for d in corpus)
    res = []
    for d in corpus:
        pen = config_pen if has_con and d["id"] in ("doc_pro", "doc_con") else 0.0
        res.append(
            {"id": d["id"], "score": d["base_score"] - pen, "contradiction_penalty": pen}
        )
    res.sort(key=lambda x: x["score"], reverse=True)
    return res

def check_cross_plane_blending(cur: list[Any], epi: list[Any], w: dict[str, float]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for d in cur:
        merged[d["id"]] = {"id": d["id"], "score": d["score"] * w.get("curated", 1.0), "provenance": ["curated"]}
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

def check_provisional_recall(results: dict[str, list[str]], prov_id: str) -> bool:
    for _q, hits in results.items():
        if prov_id not in hits:
            raise ValueError("Provisional doc not recalled")
    return True
