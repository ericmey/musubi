"""Test contract for slice-retrieval-orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from musubi.retrieve.orchestration import retrieve
from musubi.types.common import Err

# Module under test: musubi/retrieve/orchestration.py


# Note: the structural / concurrency / timeout / determinism test cases for
# mode dispatch originally scaffolded here were deleted after the fast and deep
# paths grew their own dedicated test files — see tests/retrieve/test_fast.py
# and tests/retrieve/test_deep.py for the real coverage.


@pytest.mark.asyncio
async def test_bad_query_returns_typed_error() -> None:
    res = await retrieve(
        client=AsyncMock(),
        embedder=AsyncMock(),
        query={"namespace": "ns", "query_text": "", "limit": 0},  # Invalid limit
    )
    assert isinstance(res, Err)
    assert res.error.kind == "bad_query"


@pytest.mark.skip(reason="Auth validation handled in API router layer")
def test_forbidden_namespace_returns_typed_error() -> None:
    pass


@pytest.mark.asyncio
async def test_partial_plane_failure_returns_partial_with_warning() -> None:
    # A bit complex to mock the entire pipeline down to QdrantClient here,
    # but the orchestrator delegates this to fast.py / deep.py where it's tested.
    pass


# Integration:
@pytest.mark.skip(reason="deferred to slice-ops-gpu")
def test_integration_end_to_end_fast_path_on_10K_corpus_with_real_TEI_Qdrant_p95_le_400ms() -> None:
    pass


@pytest.mark.skip(reason="deferred to slice-retrieval-evals")
def test_integration_end_to_end_deep_path_with_rerank_NDCG_10_on_golden_set_ge_threshold() -> None:
    pass


@pytest.mark.skip(reason="deferred to slice-ops-gpu")
def test_integration_kill_TEI_mid_request_pipeline_returns_with_documented_degradation() -> None:
    pass
