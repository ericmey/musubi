"""RET-018 contract: reranker degradation keeps the base warning and adds bounded cause detail."""

from __future__ import annotations

import asyncio
from typing import cast

from musubi.embedding.tei import TEIRerankerClient
from musubi.observability.registry import default_registry
from musubi.retrieve.deep import _rerank_with_timeout
from musubi.retrieve.orchestration import RetrievalEnvelope, _finalize
from musubi.retrieve.scoring import Hit
from musubi.retrieve.warnings import (
    RetrievalWarning,
    dedupe,
    is_allowlisted,
    reranker_failed,
    wire_codes,
)
from musubi.types.common import Ok


def _snapshot(name: str) -> dict[tuple[str, ...], float]:
    metric = next(
        (item for item in default_registry()._instruments() if getattr(item, "name", None) == name),
        None,
    )
    if metric is None:
        return {}
    return {key: cast(float, value) for key, value in metric.collect()}


def _moved(
    before: dict[tuple[str, ...], float], after: dict[tuple[str, ...], float]
) -> dict[tuple[str, ...], float]:
    return {
        key: after.get(key, 0.0) - before.get(key, 0.0)
        for key in set(before) | set(after)
        if after.get(key, 0.0) != before.get(key, 0.0)
    }


def test_reranker_failure_cause_is_bounded_and_wire_additive() -> None:
    warning = reranker_failed("episodic", cause="request_rejected")

    assert is_allowlisted(warning)
    assert wire_codes((warning,)) == (
        "reranker_failed",
        "reranker_failed_request_rejected",
    )
    assert not is_allowlisted(
        RetrievalWarning(code="reranker_failed", plane="episodic", cause="HTTP 413: raw body")
    )


def test_reranker_failure_causes_dedupe_without_double_counting_base_warning() -> None:
    warnings = dedupe(
        (
            reranker_failed("episodic", cause="timeout"),
            reranker_failed("episodic", cause="timeout"),
            reranker_failed("episodic", cause="invalid_response"),
        )
    )

    assert [warning.cause for warning in warnings] == ["timeout", "invalid_response"]
    assert wire_codes(warnings) == (
        "reranker_failed",
        "reranker_failed_timeout",
        "reranker_failed_invalid_response",
    )

    base_before = _snapshot("musubi_retrieval_warnings_total")
    cause_before = _snapshot("musubi_reranker_degradation_causes_total")
    result = _finalize(Ok(value=RetrievalEnvelope(results=[], warnings=warnings)))
    assert isinstance(result, Ok)

    base_moved = _moved(base_before, _snapshot("musubi_retrieval_warnings_total"))
    cause_moved = _moved(cause_before, _snapshot("musubi_reranker_degradation_causes_total"))
    assert base_moved == {("reranker_failed", "episodic"): 1.0}
    assert cause_moved == {
        ("timeout", "episodic"): 1.0,
        ("invalid_response", "episodic"): 1.0,
    }


async def test_deep_stage_timeout_surfaces_timeout_cause() -> None:
    class SlowReranker:
        async def rerank(self, query: str, candidates: list[str]) -> list[float]:
            await asyncio.sleep(0.05)
            return [0.0] * len(candidates)

    candidates = [
        Hit(
            object_id=f"hit-{index}",
            plane="episodic",
            state="matured",
            rrf_score=1.0 - index / 10,
            batch_max_rrf=1.0,
            updated_epoch=1.0,
            payload={"content": f"candidate {index}"},
        )
        for index in range(6)
    ]
    result = await _rerank_with_timeout(
        cast(TEIRerankerClient, SlowReranker()),
        "query",
        candidates,
        top_k=5,
        timeout_s=0.001,
    )

    assert result.warnings == (reranker_failed("episodic", cause="timeout"),)
