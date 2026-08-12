"""RET-015 production-derived concurrency and deadline contract."""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

import pytest

from musubi.retrieve import deep, hybrid
from musubi.retrieve.rerank import RerankResult
from musubi.retrieve.scoring import Hit, ScoreComponents, ScoredHit
from musubi.settings import Settings


def _scored_hit(object_id: str = "hit-1", *, plane: str = "episodic") -> ScoredHit:
    return ScoredHit(
        object_id=object_id,
        plane=plane,
        state="matured",
        score=0.75,
        score_components=ScoreComponents(
            relevance=0.75,
            recency=0.5,
            importance=0.5,
            provenance=0.5,
            reinforce=0.0,
        ),
        payload={"content": f"content for {object_id}"},
        raw_rrf_score=0.5,
    )


def _hit(object_id: str = "hit-1", *, plane: str = "episodic") -> Hit:
    return Hit(
        object_id=object_id,
        plane=plane,
        state="matured",
        rrf_score=0.5,
        batch_max_rrf=0.5,
        updated_epoch=0.0,
        payload={"content": f"content for {object_id}"},
    )


async def _heartbeat_until(task: asyncio.Task[Any]) -> int:
    beats = 0
    while not task.done():
        beats += 1
        await asyncio.sleep(0.005)
    await task
    return beats


@pytest.mark.asyncio
async def test_anchor_resolution_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = [hybrid.HybridHit(object_id="resolved", score=1.0, payload={})]

    def blocking_resolve(*_args: Any, **_kwargs: Any) -> list[hybrid.HybridHit]:
        time.sleep(0.15)
        return sentinel

    monkeypatch.setattr(hybrid, "_hits_from_response", blocking_resolve)
    resolve = getattr(hybrid, "_resolve_hits_async", None)
    assert resolve is not None, "RET-015 requires an async offload seam for anchor resolution"

    task = asyncio.create_task(resolve(object(), client=None, collection=None))
    beats = await _heartbeat_until(task)

    assert task.result() == sentinel
    assert beats >= 10, "blocking Qdrant resolution ran on the event-loop thread"


@pytest.mark.asyncio
async def test_lineage_hydration_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [_scored_hit("one"), _scored_hit("two")]

    async def blocking_hydrate(hit: ScoredHit, *_args: Any) -> ScoredHit:
        time.sleep(0.15)
        return hit

    monkeypatch.setattr(deep, "_hydrate_one", blocking_hydrate)
    hydrate = getattr(deep, "_hydrate_lineage_async", None)
    assert hydrate is not None, "RET-015 requires an async offload seam for lineage hydration"

    task = asyncio.create_task(hydrate(hits, object(), object(), timeout_s=1.0))
    beats = await _heartbeat_until(task)

    assert task.result() == hits
    assert beats >= 10, "blocking lineage reads ran on the event-loop thread"


@pytest.mark.asyncio
async def test_reranker_timeout_degrades_to_hybrid_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [_hit(str(index)) for index in range(6)]

    async def stalled_rerank(*_args: Any, **_kwargs: Any) -> RerankResult:
        await asyncio.sleep(1.0)
        raise AssertionError("cancelled reranker should not complete")

    monkeypatch.setattr(deep, "rerank", stalled_rerank)
    bounded = getattr(deep, "_rerank_with_timeout", None)
    assert bounded is not None, "RET-015 requires the specified reranker stage budget"

    result = await asyncio.wait_for(
        bounded(object(), "query", candidates, top_k=5, timeout_s=0.01),
        timeout=0.1,
    )

    assert result.hits == candidates[:5]
    assert [(warning.code, warning.plane) for warning in result.warnings] == [
        ("reranker_failed", "episodic")
    ]


@pytest.mark.asyncio
async def test_lineage_timeout_returns_unhydrated_hit_instead_of_failing_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = _scored_hit()

    async def stalled_hydrate(*_args: Any) -> ScoredHit:
        await asyncio.sleep(1.0)
        raise AssertionError("cancelled hydration should not complete")

    monkeypatch.setattr(deep, "_hydrate_one", stalled_hydrate)
    hydrate = getattr(deep, "_hydrate_lineage_async", None)
    assert hydrate is not None, "RET-015 requires the specified lineage stage budget"

    result = await asyncio.wait_for(
        hydrate([hit], object(), object(), timeout_s=0.01),
        timeout=0.1,
    )

    assert result == [hit]


@pytest.mark.asyncio
async def test_default_blended_shape_finishes_concurrently_before_the_whole_call_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [_scored_hit(str(index)) for index in range(5)]

    async def production_shaped_hydrate(hit: ScoredHit, *_args: Any) -> ScoredHit:
        time.sleep(0.02)
        return hit

    monkeypatch.setattr(deep, "_hydrate_one", production_shaped_hydrate)
    hydrate = getattr(deep, "_hydrate_lineage_async", None)
    assert hydrate is not None

    # Ten concurrent callers x five hydrated results mirrors the Hermes burst.
    # The compressed deadline makes event-loop serialization deterministically red:
    # 50 x 20 ms cannot fit, while worker-offloaded hydration can.
    results = await asyncio.wait_for(
        asyncio.gather(*(hydrate(hits, object(), object(), timeout_s=0.2) for _ in range(10))),
        timeout=0.45,
    )

    assert results == [hits] * 10


@pytest.mark.asyncio
async def test_retrieval_semantics_are_preserved_after_async_offload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = [hybrid.HybridHit(object_id="canonical", score=0.9, payload={"version": 4})]
    seen: dict[str, Any] = {}

    def resolve(response: Any, **kwargs: Any) -> list[hybrid.HybridHit]:
        seen["response"] = response
        seen.update(kwargs)
        return expected

    monkeypatch.setattr(hybrid, "_hits_from_response", resolve)
    offload = getattr(hybrid, "_resolve_hits_async", None)
    assert offload is not None
    response, client = object(), object()

    actual = await offload(
        response,
        client=cast(Any, client),
        collection="musubi_episodic",
        anchor_aware=True,
        visible_states={"matured"},
        limit=5,
        at_epoch=123.0,
    )

    assert actual == expected
    assert seen == {
        "response": response,
        "client": client,
        "collection": "musubi_episodic",
        "anchor_aware": True,
        "visible_states": {"matured"},
        "limit": 5,
        "at_epoch": 123.0,
    }


def test_retrieval_stage_budgets_are_tunable_positive_settings() -> None:
    rerank = Settings.model_fields.get("retrieval_rerank_timeout_s")
    lineage = Settings.model_fields.get("retrieval_lineage_timeout_s")

    assert rerank is not None and rerank.default == 1.5
    assert lineage is not None and lineage.default == 0.5
    assert rerank.metadata and getattr(rerank.metadata[0], "gt", None) == 0
    assert lineage.metadata and getattr(lineage.metadata[0], "gt", None) == 0
