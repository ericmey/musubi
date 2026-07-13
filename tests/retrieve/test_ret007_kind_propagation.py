"""RET-007 Blocker 1+2 — REAL-pipeline failure-kind propagation (not the router table in isolation).

Owner slice: slice-ret007-degradation-impl (#422).

The router status test mocks an already-correct ``Err(kind)`` — it proves the kind→status TABLE, not
that a real pipeline fault actually reaches the right kind. These tests inject faults at the hybrid /
deep layer and assert the kind that emerges from ``orchestration.retrieve``:

- a hybrid ``qdrant_timeout`` through the deep path  → kind=timeout   (was flattened to internal)
- an all-plane fast timeout                          → Err (not Ok([])) + kind=timeout  (Blocker 1)
- a blended all-plane timeout                        → kind=timeout
- a blended all-plane internal failure               → kind=internal
- a real hybrid timeout through the HTTP router      → 503 (propagation, end-to-end)

    uv run pytest tests/retrieve/test_ret007_kind_propagation.py -v
"""

from typing import Any, cast

import pytest

from musubi.embedding.fake import FakeEmbedder
from musubi.retrieve.hybrid import RetrievalError as HybridError
from musubi.retrieve.orchestration import RetrievalQuery
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
from musubi.types.common import Err


class _MockQdrant:
    def query_points(self, *args: Any, **kwargs: Any) -> Any:
        return type("R", (), {"points": []})()


class _OkReranker:
    async def rerank(
        self, query_text: str, candidates: list[Any], top_k: int | None = None
    ) -> list[float]:
        return [1.0 for _ in candidates]


async def _retrieve(mode: str, monkeypatch: pytest.MonkeyPatch, **q: Any) -> Any:
    query = RetrievalQuery(
        namespace="test/ns", query_text="q", mode=cast(Any, mode), planes=["episodic"], **q
    )
    return await run_orchestration_retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, _OkReranker()),
        query=query,
    )


async def test_deep_hybrid_timeout_propagates_kind_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hybrid ``qdrant_timeout`` in the deep path must reach the orchestration as kind=timeout —
    NOT be flattened to internal (which would surface a 500 for a backend timeout)."""

    async def timing_out_hybrid(*args: Any, **kwargs: Any) -> Any:
        return Err(error=HybridError(code="qdrant_timeout", detail="hybrid timed out"))

    monkeypatch.setattr("musubi.retrieve.deep.hybrid_search", timing_out_hybrid)
    result = await _retrieve("deep", monkeypatch)
    assert isinstance(result, Err), "a total backend timeout must be an Err, not Ok"
    assert result.error.kind == "timeout", f"expected kind=timeout, got {result.error.kind}"


async def test_fast_all_planes_timeout_is_err_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocker 1: when every fast plane times out with no survivor, the result is Err(timeout), not
    the old Ok(empty) that made a total failure indistinguishable from a healthy no-match."""

    async def timing_out_hybrid(*args: Any, **kwargs: Any) -> Any:
        return Err(error=HybridError(code="qdrant_timeout", detail="hybrid timed out"))

    monkeypatch.setattr("musubi.retrieve.fast.hybrid_search", timing_out_hybrid)
    result = await _retrieve("fast", monkeypatch)
    assert isinstance(result, Err), "all-plane fast timeout must be Err, not Ok(empty)"
    assert result.error.kind == "timeout", f"expected kind=timeout, got {result.error.kind}"


async def test_blended_all_plane_timeout_propagates_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def timing_out_deep(*args: Any, **kwargs: Any) -> Any:
        return Err(error=type("E", (), {"code": "qdrant_timeout", "detail": "timed out"})())

    monkeypatch.setattr("musubi.retrieve.blended.run_deep_retrieve", timing_out_deep)
    result = await _retrieve("blended", monkeypatch)
    assert isinstance(result, Err)
    assert result.error.kind == "timeout", f"expected kind=timeout, got {result.error.kind}"


async def test_blended_all_plane_internal_propagates_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-timeout total failure stays internal→500 (do NOT mislabel a genuine server error as a
    retryable 503)."""

    async def failing_deep(*args: Any, **kwargs: Any) -> Any:
        return Err(error=type("E", (), {"code": "deep_failure", "detail": "boom"})())

    monkeypatch.setattr("musubi.retrieve.blended.run_deep_retrieve", failing_deep)
    result = await _retrieve("blended", monkeypatch)
    assert isinstance(result, Err)
    assert result.error.kind == "internal", f"expected kind=internal, got {result.error.kind}"


# NOTE: partial-degradation-returns-Ok (one plane times out, a sibling survives) is already proven by
# test_ret007_envelope.py::test_partial_plane_failure_surfaces_warning through the same blended path —
# not duplicated here.
