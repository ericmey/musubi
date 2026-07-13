"""RET-007 IMPLEMENTATION — explicit-envelope reds (metadata survival + aggregation + direct paths).

Owner slice: slice-ret007-degradation-impl (#422). Tests-first, no src — these reds encode the
explicit typed success-envelope design Yua locked: retrieve internals return `results` + a tuple of
structured `RetrievalWarning(code, plane)`; the metadata MUST survive slicing / sorting / fanout /
dedup, aggregate across targets with no loss and no duplicate, and dedupe to distinct (code, plane)
only at the final request boundary. Warnings are bounded codes + a fixed plane.

Each red is strict-xfail today: the success path carries no warnings channel (Ok[list]), the
cross-plane fanout collapses per-plane timeouts to a boolean, and deep/fast drop degradation silently.
The impl flips these in the same spec-update commit.

    uv run pytest tests/retrieve/test_ret007_envelope.py -v
"""

from typing import Any, cast

import pytest

from musubi.embedding.fake import FakeEmbedder
from musubi.retrieve.blended import BlendedRetrievalQuery, run_blended_retrieve
from musubi.retrieve.deep import run_deep_retrieve
from musubi.retrieve.orchestration import RetrievalQuery, RetrievalResult
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
from musubi.types.common import Err, Ok

_FIXED_PLANES = frozenset({"episodic", "curated", "concept", "artifact", "thought"})


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


class _MockQdrant:
    def query_points(self, *args: Any, **kwargs: Any) -> Any:
        return type("R", (), {"points": []})()


class _OkReranker:
    async def rerank(
        self, query_text: str, candidates: list[Any], top_k: int | None = None
    ) -> list[float]:
        return [1.0 for _ in candidates]


def _warning_codes(result_value: Any) -> list[str]:
    """Pull the allowlisted string codes out of an envelope's structured warnings (each carrying a
    bounded `.code` + a fixed `.plane`). Returns [] when there is no warnings channel today."""
    warnings = getattr(result_value, "warnings", None)
    if not warnings:
        return []
    codes: list[str] = []
    for w in warnings:
        code = getattr(w, "code", w)  # structured RetrievalWarning.code, or a bare string
        codes.append(code)
    return codes


def _result_rows(result_value: Any) -> list[Any]:
    """The envelope's result rows (`.results`/`.items`), or the value itself if it is already a list."""
    for attr in ("results", "items"):
        rows = getattr(result_value, attr, None)
        if rows is not None:
            return list(rows)
    return list(result_value) if isinstance(result_value, list) else []


def _mk_result(object_id: str, plane: str, score: float) -> RetrievalResult:
    return RetrievalResult(
        object_id=object_id,
        namespace="test",
        plane=plane,
        snippet="x",
        score=score,
        score_components={"relevance": 1.0, "recency": 1.0, "reinforcement": 1.0},
        lineage={},
        payload={},
    )


# --------------------------------------------------------------------------- #
# multi-target aggregation: warnings survive fanout + dedup + sort + slice, no loss, no dup
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="Multi-target: a per-plane timeout is collapsed to a boolean and lost; the surviving-plane Ok carries no plane_timeout_<plane> warning",
)
async def test_multi_target_aggregates_warnings_no_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two planes: episodic times out, curated succeeds with hits. Contract: Ok envelope with the
    surviving hits AND a `plane_timeout_episodic` warning that survived the cross-plane merge."""
    from musubi.retrieve import orchestration as orch

    async def fake_run_single(*args: Any, plane: str, **kwargs: Any) -> Any:
        if plane == "episodic":
            return Err(error=orch.RetrievalError(kind="timeout", detail="episodic timed out"))
        return Ok(value=[_mk_result("c1", "curated", 1.0)])

    monkeypatch.setattr("musubi.retrieve.orchestration._run_single", fake_run_single)
    query = RetrievalQuery(
        namespace="test", query_text="q", mode="deep", planes=["episodic", "curated"]
    )
    result = await run_orchestration_retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, _OkReranker()),
        query=query,
    )
    if isinstance(result, Err):
        raise DefectStillPresent(
            f"partial timeout with a surviving plane must be Ok, not Err: {result.error}"
        )
    rows = _result_rows(result.value)
    codes = _warning_codes(result.value)
    if not rows:
        raise DefectStillPresent("surviving plane's hits were dropped")
    if "plane_timeout_episodic" not in codes:
        raise DefectStillPresent(
            f"the timed-out plane's warning was lost across the merge; codes={codes}"
        )


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="Per-request dedup: the same (code, plane) surfacing from multiple legs must be deduped to ONE warning at the request boundary",
)
async def test_multi_target_dedupes_warnings_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two namespaces on the SAME plane both time out. Contract: exactly ONE `plane_timeout_episodic`
    at the request boundary (deduped by distinct (code, plane)), not two."""
    from musubi.retrieve import orchestration as orch

    async def fake_run_single(*args: Any, plane: str, namespace: str, **kwargs: Any) -> Any:
        return Err(error=orch.RetrievalError(kind="timeout", detail=f"{namespace} timed out"))

    monkeypatch.setattr("musubi.retrieve.orchestration._run_single", fake_run_single)
    # a multi-namespace fanout on one plane (wildcard-style) — both legs time out
    query = RetrievalQuery(namespace="test", query_text="q", mode="deep", planes=["episodic"])
    result = await run_orchestration_retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, _OkReranker()),
        query=query,
    )
    # all-timeout with no surviving hits => Err(timeout) is correct; but if the impl returns an Ok
    # envelope of warnings it must not duplicate the (code, plane).
    if isinstance(result, Ok):
        codes = _warning_codes(result.value)
        if codes.count("plane_timeout_episodic") > 1:
            raise DefectStillPresent(
                f"duplicate (code, plane) not deduped at the request boundary: {codes}"
            )
        raise DefectStillPresent("no warnings channel on the Ok envelope yet")
    # today: Ok([]) or Err with no structured warning — the channel does not exist
    raise DefectStillPresent("no structured per-plane warning channel exists yet")


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="Envelope metadata must survive slice[:limit]: warnings are not attached to the sliced success value today",
)
async def test_envelope_warnings_survive_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A degraded query returning MORE hits than the limit must still carry its warning after the
    `[:limit]` slice — the warning is envelope-level metadata, not a row that can be sliced away."""
    from musubi.retrieve import orchestration as orch

    async def fake_run_single(*args: Any, plane: str, **kwargs: Any) -> Any:
        if plane == "episodic":
            return Err(error=orch.RetrievalError(kind="timeout", detail="episodic timed out"))
        return Ok(value=[_mk_result(f"c{i}", "curated", float(10 - i)) for i in range(10)])

    monkeypatch.setattr("musubi.retrieve.orchestration._run_single", fake_run_single)
    query = RetrievalQuery(
        namespace="test", query_text="q", mode="deep", planes=["episodic", "curated"], limit=3
    )
    result = await run_orchestration_retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, _OkReranker()),
        query=query,
    )
    if isinstance(result, Err):
        raise DefectStillPresent("partial timeout must be Ok, not Err")
    rows = _result_rows(result.value)
    codes = _warning_codes(result.value)
    if len(rows) != 3:
        raise DefectStillPresent(f"slice[:limit] not applied to rows: {len(rows)}")
    if "plane_timeout_episodic" not in codes:
        raise DefectStillPresent(f"warning did not survive the [:limit] slice; codes={codes}")


# --------------------------------------------------------------------------- #
# structured warning is bounded: code allowlisted + plane is a fixed plane
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="Structured RetrievalWarning must carry a bounded code + an explicit FIXED plane; today there is no structured warning at all",
)
async def test_partial_failure_warning_is_structured_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = BlendedRetrievalQuery(
        namespace="test/blended", query_text="q", mode="blended", planes=["episodic", "curated"]
    )
    calls = {"n": 0}

    async def mock_run_deep(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:

            class FakeError:
                code = "plane_failure"
                detail = "sim"

            return Err(error=FakeError())
        return Ok(value=[_mk_result("1", "curated", 1.0)])

    monkeypatch.setattr("musubi.retrieve.blended.run_deep_retrieve", mock_run_deep)
    result = await run_blended_retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, _OkReranker()),
        query=query,
    )
    if not isinstance(result, Ok):
        raise DefectStillPresent("partial failure must be Ok")
    warnings = getattr(result.value, "warnings", [])
    structured = [
        w
        for w in warnings
        if getattr(w, "code", None) is not None and getattr(w, "plane", None) in _FIXED_PLANES
    ]
    if not structured:
        raise DefectStillPresent(
            f"warnings are not structured (bounded code + fixed plane); got {warnings!r}"
        )


# --------------------------------------------------------------------------- #
# direct deep / fast degradation surfaces a warning (not only via blended)
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="Direct deep path: a sparse-embedding timeout inside run_deep_retrieve drops the sparse channel with no `sparse_embedding_failed` warning on the deep result",
)
async def test_direct_deep_degradation_surfaces_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    class TimeoutSparseEmbedder(FakeEmbedder):
        async def embed_sparse(self, texts: Any) -> Any:
            raise TimeoutError("sparse timeout")

    query = RetrievalQuery(namespace="test", query_text="q", mode="deep", planes=["episodic"])
    result = await run_deep_retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=TimeoutSparseEmbedder(),
        reranker=cast(Any, _OkReranker()),
        query=cast(Any, query),
    )
    if isinstance(result, Err):
        return  # a total deep failure -> Err is acceptable
    if "sparse_embedding_failed" not in _warning_codes(result.value):
        raise DefectStillPresent(
            "direct deep path dropped the sparse channel silently — no `sparse_embedding_failed` warning surfaced"
        )
