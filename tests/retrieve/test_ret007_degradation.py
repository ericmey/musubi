"""RET-007 / C5+H11+M15 — degradation propagation red contract (CORE).

Transcribed from Shiori's accepted contract (harem-ops e9ef562,
projects/active/hermes-musubi-provider/briefs/c5-h11-m15-degradation-contract.md) and her fixture
`fixtures/test_c5_h11_m15.py`. Owner slice: slice-ret007-degradation (Musubi). Tests/docs only, no
src — the fix is authorized separately after Yua accepts this red contract.

Every fault-injection is a strict-xfail that FAILS for its named contract reason today: the core
conflates infrastructure failure with an empty result set, and (blended) already emits FREE-TEXT
warnings where the contract requires strictly allowlisted machine-readable codes and NO warning on a
healthy zero-match. Controls stay ordinary green PASS.

Allowlisted warning codes (contract §4): sparse_embedding_failed, reranker_failed,
plane_timeout_<plane>, plane_error_<plane>. Healthy no-match => warnings == [] (contract §2).

    uv run pytest tests/retrieve/test_ret007_degradation.py -v
"""

from typing import Any, cast

import pytest

from musubi.embedding.fake import FakeEmbedder
from musubi.retrieve.blended import BlendedRetrievalQuery, run_blended_retrieve
from musubi.retrieve.hybrid import hybrid_search
from musubi.retrieve.rerank import rerank
from musubi.retrieve.scoring import Hit
from musubi.types.common import Err, Ok


class DefectStillPresent(Exception):
    """Raised by a red when the current code still exhibits the defect the contract forbids."""


class MockQdrantClient:
    def __init__(self, should_timeout: bool = False, return_hits: bool = False) -> None:
        self.should_timeout = should_timeout
        self.return_hits = return_hits

    def query_points(self, *args: Any, **kwargs: Any) -> Any:
        if self.should_timeout:
            raise TimeoutError("Simulated Qdrant Timeout")
        if self.return_hits:
            hit = type(
                "MockPoint",
                (),
                {"id": "1", "payload": {"state": "matured", "updated_epoch": 1.0}, "score": 1.0},
            )()
            return type("MockResponse", (), {"points": [hit]})()
        return type("MockResponse", (), {"points": []})()


class MockFailReranker:
    def __init__(self) -> None:
        self.call_count = 0

    async def rerank(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        raise RuntimeError("Reranker failed")


class MockSuccessReranker:
    def __init__(self) -> None:
        self.call_count = 0

    async def rerank(
        self, query_text: str, candidates: list[Any], top_k: int | None = None
    ) -> list[Any]:
        self.call_count += 1
        return [1.0 for _ in candidates]


# Strict, whole-code allowlist (contract §4): a warning is allowlisted ONLY if it is EXACTLY one of
# the simple codes, OR `plane_timeout_<plane>` / `plane_error_<plane>` where <plane> is EXACTLY a
# fixed plane name — no free-text suffix, no bare prefix. So `plane_error_episodic: Timeout(...)`
# and a bare `plane_timeout_` are BOTH rejected.
_FIXED_PLANES = frozenset({"episodic", "curated", "concept", "artifact", "thought"})
_SIMPLE_CODES = frozenset({"sparse_embedding_failed", "reranker_failed"})
_PLANE_PREFIXES = ("plane_timeout_", "plane_error_")


def _is_allowlisted(code: str) -> bool:
    if code in _SIMPLE_CODES:
        return True
    for prefix in _PLANE_PREFIXES:
        if code.startswith(prefix):
            return code[len(prefix) :] in _FIXED_PLANES
    return False


# --------------------------------------------------------------------------- #
# CONTROLS — ordinary green PASS (must hold before AND after the fix)
# --------------------------------------------------------------------------- #


async def test_control_healthy_zero_match() -> None:
    """A legitimate zero-match query returns an empty Ok (not an error)."""
    result = await hybrid_search(
        client=cast(Any, MockQdrantClient()),
        embedder=FakeEmbedder(),
        namespace="test/ns",
        query="test",
        collection="musubi_episodic",
    )
    assert isinstance(result, Ok)
    assert result.value == []


async def test_control_successful_rerank() -> None:
    """A healthy reranker scores candidates."""
    hits = [
        Hit(
            object_id=str(i),
            plane="episodic",
            state="matured",
            rrf_score=1.0,
            batch_max_rrf=1.0,
            updated_epoch=1.0,
        )
        for i in range(10)
    ]
    reranker = MockSuccessReranker()
    result = await rerank(client=cast(Any, reranker), query_text="test", candidates=hits, top_k=5)
    assert reranker.call_count == 1
    assert len(result) == 5 and result[0].rerank_score == 1.0


async def test_control_successful_sparse() -> None:
    """Healthy sparse embedding returns results."""
    result = await hybrid_search(
        client=cast(Any, MockQdrantClient(return_hits=True)),
        embedder=FakeEmbedder(),
        namespace="test/ns",
        query="test",
        collection="musubi_episodic",
        sparse_weight=1.0,
    )
    assert isinstance(result, Ok) and len(result.value) == 1


async def test_control_successful_blended(monkeypatch: pytest.MonkeyPatch) -> None:
    """Healthy blended retrieval returns hits and NO warnings."""
    query = BlendedRetrievalQuery(
        namespace="test/blended", query_text="test", mode="blended", planes=["episodic"]
    )

    async def mock_run_deep(*args: Any, **kwargs: Any) -> Any:
        from musubi.retrieve.orchestration import RetrievalResult

        hit = RetrievalResult(
            object_id="1",
            namespace="test",
            plane="episodic",
            snippet="test",
            score=1.0,
            score_components={"relevance": 1.0, "recency": 1.0, "reinforcement": 1.0},
            lineage={},
            payload={},
        )
        return Ok(value=[hit])

    monkeypatch.setattr("musubi.retrieve.blended.run_deep_retrieve", mock_run_deep)
    result = await run_blended_retrieve(
        client=cast(Any, MockQdrantClient()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, MockSuccessReranker()),
        query=query,
    )
    assert isinstance(result, Ok)
    assert len(result.value.results) == 1
    assert not result.value.warnings


# --------------------------------------------------------------------------- #
# FAULT INJECTIONS — strict-xfail; each fails for its named contract reason
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="C5: hybrid_search swallows TimeoutError into Ok([]) instead of Err (BACKEND_UNAVAILABLE)",
)
async def test_c5_hybrid_timeout() -> None:
    result = await hybrid_search(
        client=cast(Any, MockQdrantClient(should_timeout=True)),
        embedder=FakeEmbedder(),
        namespace="test/ns",
        query="test",
        collection="musubi_episodic",
        timeout_s=0.1,
    )
    if isinstance(result, Ok) and result.value == []:
        raise DefectStillPresent("C5: hybrid_search swallowed TimeoutError and returned Ok([])")
    assert isinstance(result, Err)
    assert (
        "timeout" in str(result.error.detail).lower() or result.error.code == "qdrant_query_failed"
    )


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="H11: blended all-plane failure maps to Ok(empty) instead of an Err envelope (500 INTERNAL)",
)
async def test_h11_blended_all_plane_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    query = BlendedRetrievalQuery(
        namespace="test/blended", query_text="test", mode="blended", planes=["episodic", "curated"]
    )

    async def mock_run_deep(*args: Any, **kwargs: Any) -> Any:
        class FakeError:
            code = "deep_failure"
            detail = "simulated plane failure"

        return Err(error=FakeError())

    monkeypatch.setattr("musubi.retrieve.blended.run_deep_retrieve", mock_run_deep)
    result = await run_blended_retrieve(
        client=cast(Any, MockQdrantClient()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, MockFailReranker()),
        query=query,
    )
    if isinstance(result, Ok) and result.value.results == []:
        raise DefectStillPresent("H11: blended retrieve mapped all-plane failure to Ok(empty)")
    assert isinstance(result, Err)


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="M15: sparse timeout drops the sparse channel with no `sparse_embedding_failed` warning to the caller",
)
async def test_m15_sparse_timeout_silent_fallback() -> None:
    class TimeoutSparseEmbedder(FakeEmbedder):
        async def embed_sparse(self, texts: Any) -> Any:
            raise TimeoutError("Simulated Sparse Timeout")

    result = await hybrid_search(
        client=cast(Any, MockQdrantClient(return_hits=True)),
        embedder=TimeoutSparseEmbedder(),
        namespace="test/ns",
        query="test",
        collection="musubi_episodic",
        sparse_timeout_s=0.01,
    )
    warnings = getattr(result, "warnings", []) if isinstance(result, Ok) else []
    if isinstance(result, Ok) and "sparse_embedding_failed" not in warnings:
        raise DefectStillPresent(
            "M15: sparse timeout dropped the channel silently — no `sparse_embedding_failed` warning"
        )
    assert isinstance(result, Err) or "sparse_embedding_failed" in getattr(result, "warnings", [])


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="M15: reranker failure falls back to RRF silently with no `reranker_failed` warning",
)
async def test_m15_rerank_failure_silent_fallback() -> None:
    hits = [
        Hit(
            object_id=str(i),
            plane="episodic",
            state="matured",
            rrf_score=1.0,
            batch_max_rrf=1.0,
            updated_epoch=1.0,
        )
        for i in range(6)
    ]
    reranker = MockFailReranker()
    result = await rerank(client=cast(Any, reranker), query_text="test", candidates=hits, top_k=2)
    assert reranker.call_count == 1, "reranker must be called"
    # Contract: on reranker failure the caller must receive a `reranker_failed` warning. rerank()
    # returns a bare list today, carrying NO warning channel — the degradation is invisible.
    warnings = getattr(result, "warnings", None)
    if warnings is None or "reranker_failed" not in warnings:
        raise DefectStillPresent(
            "M15: reranker failure fell back to RRF silently — no `reranker_failed` warning surfaced"
        )


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="Partial-plane: M<N plane failure must surface a bounded plane_timeout_/plane_error_ code, not free-text",
)
async def test_partial_plane_failure_surfaces_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    query = BlendedRetrievalQuery(
        namespace="test/blended", query_text="test", mode="blended", planes=["episodic", "curated"]
    )
    calls = {"n": 0}

    async def mock_run_deep(*args: Any, **kwargs: Any) -> Any:
        from musubi.retrieve.orchestration import RetrievalResult

        calls["n"] += 1
        if calls["n"] == 1:  # first plane fails

            class FakeError:
                code = "plane_failure"
                detail = "simulated"

            return Err(error=FakeError())
        hit = RetrievalResult(  # second plane succeeds → partial (M<N)
            object_id="1",
            namespace="test",
            plane="curated",
            snippet="ok",
            score=1.0,
            score_components={"relevance": 1.0, "recency": 1.0, "reinforcement": 1.0},
            lineage={},
            payload={},
        )
        return Ok(value=[hit])

    monkeypatch.setattr("musubi.retrieve.blended.run_deep_retrieve", mock_run_deep)
    result = await run_blended_retrieve(
        client=cast(Any, MockQdrantClient()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, MockSuccessReranker()),
        query=query,
    )
    assert isinstance(result, Ok) and result.value.results, (
        "partial failure must still return the surviving plane's hits"
    )
    # Contract §4: the warnings must be NON-EMPTY and EVERY entry an allowlisted machine-readable
    # code — a mix of free-text + one allowlisted code must NOT satisfy the contract.
    warnings = result.value.warnings
    if not warnings or not all(_is_allowlisted(w) for w in warnings):
        raise DefectStillPresent(
            f"Partial-plane: warnings must be non-empty and ALL bounded allowlisted codes "
            f"(plane_timeout_<plane>/plane_error_<plane>); got: {warnings}"
        )


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="Healthy zero-match must carry NO warning; blended today emits free-text 'no hits in any plane'",
)
async def test_healthy_zero_match_has_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract §2: a healthy no-match is `200 OK` with `warnings == []`. Today blended appends the
    free-text 'no hits in any plane' warning, marking a healthy empty result as degraded."""
    query = BlendedRetrievalQuery(
        namespace="test/blended", query_text="test", mode="blended", planes=["episodic"]
    )

    async def mock_run_deep(*args: Any, **kwargs: Any) -> Any:
        return Ok(value=[])  # healthy plane, zero hits

    monkeypatch.setattr("musubi.retrieve.blended.run_deep_retrieve", mock_run_deep)
    result = await run_blended_retrieve(
        client=cast(Any, MockQdrantClient()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, MockSuccessReranker()),
        query=query,
    )
    assert isinstance(result, Ok) and result.value.results == []
    if result.value.warnings:
        raise DefectStillPresent(
            f"Healthy zero-match must have warnings==[], but got: {result.value.warnings}"
        )
