"""RET-016 bounded Qdrant offload and synchronous lineage contract."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from typing import Any

import pytest
from qdrant_client import models

from musubi.retrieve import deep, hybrid
from musubi.retrieve.scoring import ScoreComponents, ScoredHit


def _hit(object_id: str) -> ScoredHit:
    return ScoredHit(
        object_id=object_id,
        plane="episodic",
        state="matured",
        score=0.75,
        score_components=ScoreComponents(
            relevance=0.75,
            recency=0.5,
            importance=0.5,
            provenance=0.5,
            reinforce=0.0,
        ),
        payload={"content": object_id},
        raw_rrf_score=0.5,
    )


@pytest.mark.asyncio
async def test_qdrant_offload_never_uses_the_shared_default_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_to_thread(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("retrieval Qdrant work used asyncio.to_thread")

    monkeypatch.setattr(asyncio, "to_thread", forbidden_to_thread)
    monkeypatch.setattr(hybrid, "_hits_from_response", lambda *_args, **_kwargs: [])

    assert await hybrid._resolve_hits_async(object(), client=None, collection=None) == []


@pytest.mark.asyncio
async def test_qdrant_offload_caps_simultaneous_blocking_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from musubi.retrieve.offload import QDRANT_OFFLOAD_WORKERS

    lock = threading.Lock()
    active = 0
    peak = 0

    def blocking_resolve(*_args: Any, **_kwargs: Any) -> list[Any]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return []

    monkeypatch.setattr(hybrid, "_hits_from_response", blocking_resolve)
    await asyncio.gather(
        *(
            hybrid._resolve_hits_async(object(), client=None, collection=None)
            for _ in range(QDRANT_OFFLOAD_WORKERS * 3)
        )
    )

    assert peak == QDRANT_OFFLOAD_WORKERS


@pytest.mark.asyncio
async def test_query_points_calls_share_the_configured_qdrant_ceiling() -> None:
    from musubi.retrieve.offload import QDRANT_OFFLOAD_WORKERS

    class BlockingClient:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0

        def query_points(self, **_kwargs: Any) -> object:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            return object()

    client = BlockingClient()
    await asyncio.gather(
        *(
            hybrid._query_points(
                client,  # type: ignore[arg-type]
                collection="musubi_episodic",
                prefetch=[],
                query_filter=models.Filter(),
                limit=1,
                timeout_s=1.0,
            )
            for _ in range(QDRANT_OFFLOAD_WORKERS * 3)
        )
    )

    assert client.peak == QDRANT_OFFLOAD_WORKERS


@pytest.mark.asyncio
async def test_lineage_hydration_creates_no_per_hit_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = _hit("no-loop")

    def sync_hydrate(item: ScoredHit, *_args: Any) -> ScoredHit:
        return replace(item, payload={**item.payload, "hydrated": True})

    def forbidden_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("lineage hydration created a per-hit event loop")

    monkeypatch.setattr(deep, "_hydrate_one", sync_hydrate)
    monkeypatch.setattr(asyncio, "run", forbidden_run)

    result = await deep._hydrate_lineage_async([hit], object(), object(), timeout_s=0.2)

    assert result[0].payload["hydrated"] is True


@pytest.mark.asyncio
async def test_saturated_lineage_offload_degrades_in_place_without_request_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from musubi.retrieve.offload import QDRANT_OFFLOAD_WORKERS

    hits = [_hit(str(index)) for index in range(QDRANT_OFFLOAD_WORKERS + 5)]

    def saturated_hydrate(item: ScoredHit, *_args: Any) -> ScoredHit:
        time.sleep(0.2)
        return replace(item, payload={**item.payload, "late": True})

    monkeypatch.setattr(deep, "_hydrate_one", saturated_hydrate)

    result = await asyncio.wait_for(
        deep._hydrate_lineage_async(hits, object(), object(), timeout_s=0.01),
        timeout=0.15,
    )

    assert result == hits


@pytest.mark.asyncio
async def test_twenty_concurrent_callers_complete_with_bounded_offload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [_hit(str(index)) for index in range(5)]

    def production_shaped_hydrate(item: ScoredHit, *_args: Any) -> ScoredHit:
        time.sleep(0.02)
        return replace(item, payload={**item.payload, "offloaded": True})

    monkeypatch.setattr(deep, "_hydrate_one", production_shaped_hydrate)

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                deep._hydrate_lineage_async(hits, object(), object(), timeout_s=0.3)
                for _ in range(20)
            )
        ),
        timeout=0.5,
    )

    assert all(
        hit.payload.get("offloaded") is True for caller_results in results for hit in caller_results
    )
