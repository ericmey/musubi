"""RET-016 bounded Qdrant offload and synchronous lineage contract."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from typing import Any, cast

import pytest
from qdrant_client import models

from musubi.observability import request_id_var
from musubi.retrieve import deep, hybrid
from musubi.retrieve.offload import (
    QDRANT_OPTIONAL_OFFLOAD_WORKERS,
    QDRANT_REQUIRED_OFFLOAD_WORKERS,
    run_optional_qdrant_offload,
    run_qdrant_offload,
)
from musubi.retrieve.scoring import ScoreComponents, ScoredHit
from musubi.types.common import Ok


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
async def test_both_qdrant_offload_pools_preserve_request_context() -> None:
    token = request_id_var.set("req-798-proof")
    try:
        required, optional = await asyncio.gather(
            run_qdrant_offload(request_id_var.get),
            run_optional_qdrant_offload(request_id_var.get),
        )
    finally:
        request_id_var.reset(token)

    assert required == "req-798-proof"
    assert optional == "req-798-proof"


@pytest.mark.asyncio
async def test_qdrant_offload_caps_simultaneous_blocking_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    all_workers_started = threading.Barrier(QDRANT_REQUIRED_OFFLOAD_WORKERS)
    active = 0
    peak = 0

    def blocking_resolve(*_args: Any, **_kwargs: Any) -> list[Any]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        all_workers_started.wait(timeout=1.0)
        with lock:
            active -= 1
        return []

    monkeypatch.setattr(hybrid, "_hits_from_response", blocking_resolve)
    await asyncio.gather(
        *(
            hybrid._resolve_hits_async(object(), client=None, collection=None)
            for _ in range(QDRANT_REQUIRED_OFFLOAD_WORKERS * 3)
        )
    )

    assert peak == QDRANT_REQUIRED_OFFLOAD_WORKERS


@pytest.mark.asyncio
async def test_query_points_calls_share_the_configured_qdrant_ceiling() -> None:
    class BlockingClient:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.all_workers_started = threading.Barrier(QDRANT_REQUIRED_OFFLOAD_WORKERS)
            self.active = 0
            self.peak = 0

        def query_points(self, **_kwargs: Any) -> object:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            self.all_workers_started.wait(timeout=1.0)
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
            for _ in range(QDRANT_REQUIRED_OFFLOAD_WORKERS * 3)
        )
    )

    assert client.peak == QDRANT_REQUIRED_OFFLOAD_WORKERS


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

    result = await deep._hydrate_lineage_async(
        [hit], cast(Any, object()), cast(Any, object()), timeout_s=0.2
    )

    assert result[0].payload["hydrated"] is True


def test_lineage_sync_seam_rejects_loop_bound_awaits() -> None:
    async def suspending_read() -> int:
        await asyncio.sleep(0)
        return 1

    with pytest.raises(
        RuntimeError, match="lineage plane get suspended; add a genuine synchronous read seam"
    ):
        deep._complete_without_suspension(suspending_read())


@pytest.mark.asyncio
async def test_saturated_lineage_offload_degrades_in_place_without_request_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [_hit(str(index)) for index in range(QDRANT_OPTIONAL_OFFLOAD_WORKERS + 5)]
    release_workers = threading.Event()

    def saturated_hydrate(item: ScoredHit, *_args: Any) -> ScoredHit:
        release_workers.wait(timeout=1.0)
        return replace(item, payload={**item.payload, "late": True})

    monkeypatch.setattr(deep, "_hydrate_one", saturated_hydrate)

    try:
        result = await asyncio.wait_for(
            deep._hydrate_lineage_async(
                hits, cast(Any, object()), cast(Any, object()), timeout_s=0.01
            ),
            timeout=0.15,
        )
    finally:
        release_workers.set()
        await run_optional_qdrant_offload(lambda: None)

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
                deep._hydrate_lineage_async(
                    hits, cast(Any, object()), cast(Any, object()), timeout_s=0.5
                )
                for _ in range(20)
            )
        ),
        timeout=1.0,
    )

    assert all(
        hit.payload.get("offloaded") is True for caller_results in results for hit in caller_results
    )


@pytest.mark.asyncio
async def test_saturated_optional_lineage_cannot_starve_required_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Earlier timeout tests deliberately leave their non-cancellable worker call running. Occupy
    # every slot with a barrier first so this test starts only after all earlier work has drained.
    drain_barrier = threading.Barrier(QDRANT_OPTIONAL_OFFLOAD_WORKERS)
    await asyncio.gather(
        *(
            run_optional_qdrant_offload(drain_barrier.wait)
            for _ in range(QDRANT_OPTIONAL_OFFLOAD_WORKERS)
        )
    )
    all_optional_workers_started = threading.Event()
    release_optional_workers = threading.Event()
    lock = threading.Lock()
    active = 0

    def stalled_hydrate(item: ScoredHit, *_args: Any) -> ScoredHit:
        nonlocal active
        with lock:
            active += 1
            if active == QDRANT_OPTIONAL_OFFLOAD_WORKERS:
                all_optional_workers_started.set()
        release_optional_workers.wait(timeout=1.0)
        return item

    class ImmediateClient:
        def query_points(self, **_kwargs: Any) -> object:
            return object()

    hits = [_hit(str(index)) for index in range(QDRANT_OPTIONAL_OFFLOAD_WORKERS)]
    monkeypatch.setattr(deep, "_hydrate_one", stalled_hydrate)
    hydration = asyncio.create_task(
        deep._hydrate_lineage_async(hits, cast(Any, object()), cast(Any, object()), timeout_s=0.1)
    )

    async def wait_until_optional_pool_is_full() -> None:
        while not all_optional_workers_started.is_set():
            await asyncio.sleep(0.001)

    try:
        await asyncio.wait_for(wait_until_optional_pool_is_full(), timeout=0.2)
        assert await hydration == hits
        await asyncio.wait_for(
            hybrid._query_points(
                cast(Any, ImmediateClient()),
                collection="musubi_episodic",
                prefetch=[],
                query_filter=models.Filter(),
                limit=1,
                timeout_s=0.05,
            ),
            timeout=0.1,
        )
    finally:
        release_optional_workers.set()
        await run_optional_qdrant_offload(lambda: None)


@pytest.mark.asyncio
async def test_twenty_callers_complete_through_the_production_deep_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProductionShapedClient:
        def query_points(self, *, collection_name: str, **_kwargs: Any) -> str:
            time.sleep(0.003)
            return collection_name

    def resolve(response: str, **_kwargs: Any) -> list[hybrid.HybridHit]:
        time.sleep(0.003)
        plane = response.removeprefix("musubi_")
        return [
            hybrid.HybridHit(
                object_id=f"{plane}-hit-{index}",
                score=0.75 - index / 100,
                payload={
                    "namespace": f"fleet/agent/{plane}",
                    "state": "matured",
                    "content": f"{plane}-{index}",
                },
            )
            for index in range(2)
        ]

    async def production_hybrid_leg(
        *, client: Any, collection: str, limit: int, timeout_s: float, **_kwargs: Any
    ) -> Any:
        response = await hybrid._query_points(
            client,
            collection=collection,
            prefetch=[],
            query_filter=models.Filter(),
            limit=limit,
            timeout_s=timeout_s,
        )
        hits = await hybrid._resolve_hits_async(response, client=client, collection=collection)
        return Ok(value=hybrid.HybridSearchResult(hits=hits))

    def hydrate(item: ScoredHit, *_args: Any) -> ScoredHit:
        time.sleep(0.003)
        return replace(item, payload={**item.payload, "hydrated": True})

    class InstrumentedReranker:
        def __init__(self) -> None:
            self.calls = 0

        async def rerank(self, query_text: str, texts: list[str]) -> list[float]:
            assert query_text == "bounded"
            assert len(texts) == 6
            self.calls += 1
            return [float(index) for index in range(len(texts))]

    monkeypatch.setattr(deep, "hybrid_search", production_hybrid_leg)
    monkeypatch.setattr(hybrid, "_hits_from_response", resolve)
    monkeypatch.setattr(deep, "_hydrate_one", hydrate)
    query = deep.RetrievalQuery(namespace="fleet/agent", query_text="bounded", limit=5)
    reranker = InstrumentedReranker()

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                deep.run_deep_retrieve(
                    cast(Any, ProductionShapedClient()),
                    cast(Any, object()),
                    cast(Any, reranker),
                    query,
                )
                for _ in range(20)
            )
        ),
        timeout=1.0,
    )

    assert all(
        isinstance(result, Ok)
        and len(result.value.hits) == 5
        and all(hit.payload.get("hydrated") is True for hit in result.value.hits)
        for result in results
    )
    assert reranker.calls == 20
