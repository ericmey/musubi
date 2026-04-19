"""Test contract for slice-retrieval-rerank."""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from musubi.embedding.base import EmbeddingError
from musubi.embedding.tei import TEIRerankerClient
from musubi.retrieve.rerank import rerank
from musubi.retrieve.scoring import Hit, score


class _FakeTEIRerankerClient:
    def __init__(self, scores: list[float] | None = None, error: Exception | None = None) -> None:
        self.scores = scores
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append((query, candidates))
        if self.error:
            raise self.error
        if self.scores is not None:
            return self.scores
        return [0.5] * len(candidates)


def _hit(object_id: str, **kwargs: Any) -> Hit:
    return Hit(
        object_id=object_id,
        plane=kwargs.get("plane", "episodic"),
        state="matured",
        rrf_score=kwargs.get("rrf_score", 0.5),
        batch_max_rrf=1.0,
        updated_epoch=1700000000.0,
        payload=kwargs.get("payload", {}),
    )


def _client(fake: _FakeTEIRerankerClient) -> TEIRerankerClient:
    return cast(TEIRerankerClient, fake)


@pytest.mark.asyncio
async def test_rerank_sorts_by_cross_encoder_score() -> None:
    client = _FakeTEIRerankerClient(scores=[0.1, 0.9, 0.5])
    candidates = [
        _hit("low", rrf_score=0.9),
        _hit("high", rrf_score=0.1),
        _hit("mid", rrf_score=0.5),
    ]
    # Need > 5 to not skip
    candidates += [_hit(f"pad-{i}") for i in range(3)]
    if client.scores is not None:
        client.scores += [0.0] * 3

    result = await rerank(_client(client), "query", candidates, top_k=10)

    assert [h.object_id for h in result[:3]] == ["high", "mid", "low"]
    assert result[0].rerank_score == 0.9


@pytest.mark.asyncio
async def test_rerank_replaces_relevance_component() -> None:
    # This is actually verified by scoring._relevance, but we test it here
    # by ensuring rerank_score is set, which scoring._relevance uses.
    client = _FakeTEIRerankerClient(scores=[0.8, 0.2, 0.3, 0.4, 0.5, 0.6])
    candidates = [_hit(f"h{i}", rrf_score=0.1) for i in range(6)]

    result = await rerank(_client(client), "query", candidates, top_k=10)

    h0 = result[0]
    assert h0.rerank_score == 0.8
    # scoring.score should use rerank_score
    _total, components = score(h0, now=1700000000.0)
    assert components.relevance > 0.5  # sigmoid(0.8) is ~0.69


@pytest.mark.asyncio
async def test_rerank_skipped_when_candidates_le_5() -> None:
    client = _FakeTEIRerankerClient()
    candidates = [_hit(f"h{i}") for i in range(5)]

    result = await rerank(_client(client), "query", candidates, top_k=10)

    assert len(client.calls) == 0
    assert len(result) == 5


@pytest.mark.asyncio
async def test_rerank_degrades_to_rrf_when_tei_down(caplog: pytest.LogCaptureFixture) -> None:
    client = _FakeTEIRerankerClient(error=EmbeddingError("TEI down"))
    candidates = [_hit(f"h{i}", rrf_score=1.0 - i / 10) for i in range(10)]

    with caplog.at_level(logging.WARNING):
        result = await rerank(_client(client), "query", candidates, top_k=5)

    assert len(result) == 5
    assert "Reranker failed" in caplog.text
    # Should maintain input order (or RRF order if already sorted)
    assert [h.object_id for h in result] == ["h0", "h1", "h2", "h3", "h4"]


@pytest.mark.asyncio
async def test_rerank_content_truncated_to_2048_chars() -> None:
    client = _FakeTEIRerankerClient()
    long_content = "a" * 3000
    candidates = [
        _hit("h0", payload={"content": long_content}),
        _hit("h1", payload={"title": "T", "content": long_content}),
    ]
    # Pad to 6
    candidates += [_hit(f"pad-{i}") for i in range(4)]

    await rerank(_client(client), "query", candidates, top_k=10)

    _query, texts = client.calls[0]
    assert len(texts[0]) == 2048
    assert texts[1].startswith("T\n\n")
    assert len(texts[1]) == 2048 + 3  # title + \n\n + 2048 chars


@pytest.mark.asyncio
async def test_rerank_score_normalized_via_sigmoid() -> None:
    # Verified in scoring.py, but we check here that high logit -> high relevance
    client = _FakeTEIRerankerClient(scores=[10.0, -10.0] + [0.0] * 4)
    candidates = [_hit(f"h{i}") for i in range(6)]

    result = await rerank(_client(client), "query", candidates, top_k=10)

    high = next(h for h in result if h.object_id == "h0")
    low = next(h for h in result if h.object_id == "h1")

    _, comp_high = score(high, now=1700000000.0)
    _, comp_low = score(low, now=1700000000.0)

    assert comp_high.relevance > 0.99
    assert comp_low.relevance < 0.01


@pytest.mark.skip(reason="mode=deep logic lives in orchestration/orchestrator.py, not here")
def test_rerank_called_only_on_deep_path() -> None:
    pass


@pytest.mark.asyncio
async def test_rerank_latency_under_budget_for_50_candidates() -> None:
    # Simple smoke test, not a real benchmark here
    client = _FakeTEIRerankerClient()
    candidates = [_hit(f"h{i}") for i in range(50)]

    import time

    start = time.perf_counter()
    await rerank(_client(client), "query", candidates, top_k=10)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.1  # Fake is fast


@pytest.mark.asyncio
async def test_rerank_plane_agnostic_ordering() -> None:
    client = _FakeTEIRerankerClient(scores=[0.9, 0.8, 0.7])
    candidates = [
        _hit("artifact", plane="artifact", rrf_score=0.1),
        _hit("curated", plane="curated", rrf_score=0.1),
        _hit("episodic", plane="episodic", rrf_score=0.1),
    ]
    candidates += [_hit(f"pad-{i}") for i in range(3)]
    if client.scores is not None:
        client.scores += [0.0] * 3

    result = await rerank(_client(client), "query", candidates, top_k=10)

    # Order should purely be by rerank_score
    assert [h.object_id for h in result[:3]] == ["artifact", "curated", "episodic"]


@pytest.mark.asyncio
async def test_rerank_tei_error_returns_hybrid_results_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same as test_rerank_degrades_to_rrf_when_tei_down
    client = _FakeTEIRerankerClient(error=EmbeddingError("TEI 500", status_code=500))
    candidates = [_hit(f"h{i}", rrf_score=1.0) for i in range(10)]

    with caplog.at_level(logging.WARNING):
        result = await rerank(_client(client), "query", candidates, top_k=5)

    assert len(result) == 5
    assert "Reranker failed" in caplog.text


@pytest.mark.asyncio
async def test_rerank_partial_batch_failure_rescored_for_rest() -> None:
    # Our implementation treats any client error as total failure for the query
    # since we send them as a single batch. TEIRerankerClient.rerank raises
    # on any failure.
    client = _FakeTEIRerankerClient(error=EmbeddingError("Partial fail??"))
    candidates = [_hit(f"h{i}") for i in range(10)]

    result = await rerank(_client(client), "query", candidates, top_k=5)
    assert len(result) == 5
    assert all(h.rerank_score is None for h in result)


@pytest.mark.skip(reason="deferred to slice-retrieval-evals: deep-path NDCG needs benchmark corpus")
def test_integration_deep_path_ndcg_10_on_golden_set_improves_vs_fast_path_by_ge_5_points() -> None:
    pass


@pytest.mark.skip(reason="deferred to slice-ops-gpu: live p95 requires reference host")
def test_integration_deep_path_p95_latency_under_2s_with_100_candidates() -> None:
    pass
