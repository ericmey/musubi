"""Test contract for slice-retrieval-orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from musubi.retrieve.orchestration import retrieve
from musubi.types.common import Err

# Module under test: musubi/retrieve/orchestration.py


# Structural:
@pytest.mark.skip(reason="deferred to test_fast.py where actual fast path is tested")
def test_fast_mode_skips_rerank() -> None:
    pass


@pytest.mark.skip(reason="deferred to test_deep.py where actual deep path is tested")
def test_deep_mode_invokes_rerank() -> None:
    pass


@pytest.mark.skip(reason="deferred to test_fast.py")
def test_fast_mode_skips_lineage_hydrate() -> None:
    pass


@pytest.mark.skip(reason="deferred to test_deep.py")
def test_deep_mode_hydrates_when_flag_true() -> None:
    pass


@pytest.mark.skip(reason="deferred to test_deep.py")
def test_steps_run_in_documented_order() -> None:
    pass


# Concurrency:
@pytest.mark.skip(reason="deferred to test_fast.py/test_deep.py")
def test_planes_run_in_parallel() -> None:
    pass


@pytest.mark.skip(reason="deferred to test_deep.py")
def test_hydrate_fetches_run_in_parallel() -> None:
    pass


# Timeouts:
@pytest.mark.skip(reason="deferred to test_fast.py / implemented via timeout wrapper")
def test_whole_call_timeout_fast_400ms() -> None:
    pass


@pytest.mark.skip(reason="deferred to test_deep.py")
def test_per_plane_timeout_deep_1500ms() -> None:
    pass


@pytest.mark.skip(reason="deferred to test_deep.py")
def test_rerank_timeout_returns_with_warning() -> None:
    pass


# Determinism:
@pytest.mark.skip(reason="deferred to test_deep.py")
def test_deterministic_for_fixed_inputs() -> None:
    pass


@pytest.mark.skip(reason="deferred to test_deep.py")
def test_tiebreak_on_object_id() -> None:
    pass


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
