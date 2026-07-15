"""API tests for the ranked context-pack endpoint."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from musubi.planes.episodic import EpisodicPlane
from musubi.types.common import Ok
from musubi.types.episodic import EpisodicMemory


def test_context_endpoint_blends_recent_provisional_with_established_ranked(
    client: TestClient,
    valid_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RET-013: Canonical context returns a blended mix of recent memories
    and top-ranked established memories.

    The endpoint must:
    1. Query Qdrant twice: once in `mode="recent"` (fetching the absolute newest memories, explicitly
       including `provisional`), and once in `mode="fast"` (the standard ranked fetch).
    2. Dedupe the combined candidates (by composite identity) favoring the ranked score when present.
    3. Pass the blended candidates into `build_context_pack`.
    """
    import musubi.api.routers.context as ctx_router
    from musubi.retrieve.orchestration import RetrievalEnvelope, RetrievalResult
    from musubi.retrieve.warnings import RetrievalWarning
    from musubi.types.common import Ok

    captured_queries = []

    async def mock_retrieve(
        client: object,
        embedder: object,
        reranker: object,
        query: dict[str, object],
        account_access: bool,
    ) -> object:
        captured_queries.append(query)

        if query["mode"] == "recent":
            return Ok(
                value=RetrievalEnvelope(
                    results=[
                        RetrievalResult(
                            object_id="recent-prov-1",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="A brand new provisional thought without any query token match",
                            score=1.0,
                            score_components={},
                            lineage={},
                            state="provisional",
                            importance=5,
                            provenance_score=1.0,
                        ),
                        RetrievalResult(
                            object_id="overlap-1",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="Old recent snippet",
                            score=1.0,
                            score_components={},
                            lineage={},
                            state="matured",
                            importance=8,
                            provenance_score=1.0,
                        ),
                        RetrievalResult(
                            object_id="same-id-diff-namespace-1",
                            namespace="other/namespace/episodic",
                            plane="episodic",
                            snippet="I am from another namespace",
                            score=1.0,
                            score_components={},
                            lineage={},
                            state="matured",
                            importance=5,
                            provenance_score=1.0,
                        ),
                        RetrievalResult(
                            object_id="suppress-history",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="This is a superseded memory that should be suppressed if include_history=False",
                            score=1.0,
                            score_components={},
                            lineage={},
                            state="superseded",
                            importance=5,
                            provenance_score=1.0,
                        ),
                    ],
                    warnings=(RetrievalWarning(code="TEI_DENSE_UNAVAILABLE", plane="episodic"),),
                )
            )
        else:
            return Ok(
                value=RetrievalEnvelope(
                    results=[
                        RetrievalResult(
                            object_id="overlap-1",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="test blending Ranked duplicate has DIFFERENT and richer content",
                            score=0.95,
                            score_components={
                                "relevance": 0.95,
                                "recency": 0.8,
                                "importance": 0.8,
                                "provenance": 0.1,
                                "reinforcement": 0,
                            },
                            lineage={},
                            state="matured",
                            importance=8,
                        ),
                        RetrievalResult(
                            object_id="ranked-1",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="test blending Old but extremely relevant established memory",
                            score=0.85,
                            score_components={
                                "relevance": 0.9,
                                "recency": 0.1,
                                "importance": 0.9,
                                "provenance": 0.5,
                                "reinforcement": 1,
                            },
                            lineage={},
                            state="matured",
                            importance=9,
                        ),
                        RetrievalResult(
                            object_id="same-id-diff-namespace-1",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="test blending I am from THIS namespace",
                            score=0.85,
                            score_components={
                                "relevance": 0.9,
                                "recency": 0.1,
                                "importance": 0.9,
                                "provenance": 0.5,
                                "reinforcement": 1,
                            },
                            lineage={},
                            state="matured",
                            importance=5,
                        ),
                    ],
                    warnings=(
                        RetrievalWarning(code="TEI_DENSE_UNAVAILABLE", plane="episodic"),
                        RetrievalWarning(code="OLLAMA_TIMEOUT", plane="episodic"),
                    ),
                )
            )

    monkeypatch.setattr(ctx_router, "run_orchestration_retrieve", mock_retrieve)

    async def mock_account_delivered(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(ctx_router, "account_delivered", mock_account_delivered)

    resp = client.post(
        "/v1/context",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "planes": ["episodic"],
            "query_text": "test blending",
            "mode": "startup",
            "max_items": 10,
            # max_chars=1200
        },
    )

    assert resp.status_code == 200, resp.text

    recent_query = captured_queries[0]
    fast_query = captured_queries[1]

    # 3. Default lane-state split explicit and identical namespace_targets
    assert recent_query["mode"] == "recent"
    assert recent_query["state_filter"] == ["provisional", "matured", "promoted"]
    assert fast_query["mode"] == "fast"
    assert fast_query["state_filter"] == ["matured", "promoted"]
    assert recent_query["namespace_targets"] == fast_query["namespace_targets"]

    data = resp.json()
    items = []
    for group in data["groups"]:
        items.extend(group["items"])

    # Discriminators:
    # - zero-overlap provisional + strong established both surfaced
    # - ranked duplicate truly wins using DIFFERENT content and asserting content
    # - same object_id across different namespace/plane does NOT collapse
    # - include_history suppression (suppress-history missing)
    # - warnings stable+deduped

    ids = [i["object_id"] for i in items]

    assert "recent-prov-1" in ids  # zero-overlap provisional survived
    assert "overlap-1" in ids
    assert "ranked-1" in ids  # strong established survived
    assert "suppress-history" not in ids  # include_history=False

    overlap_item = next(i for i in items if i["object_id"] == "overlap-1")
    assert (
        overlap_item["content"] == "test blending Ranked duplicate has DIFFERENT and richer content"
    )

    # 1. In the composite identity scenario, assert there are EXACTLY TWO surfaced items with object_id same-id-diff-namespace-1
    # and assert their (namespace,plane) pairs are the two expected distinct identities
    composite_items = [i for i in items if i["object_id"] == "same-id-diff-namespace-1"]
    assert len(composite_items) == 2
    composite_identities = {(i["namespace"], i["plane"]) for i in composite_items}
    assert composite_identities == {
        ("other/namespace/episodic", "episodic"),
        ("eric/claude-code/episodic", "episodic"),
    }

    assert data["warnings"] == ["TEI_DENSE_UNAVAILABLE", "OLLAMA_TIMEOUT"]


def test_context_endpoint_max_items_mix_quota(
    client: TestClient,
    valid_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guarantee max_items=2 yields exactly one recent and one ranked item."""
    import musubi.api.routers.context as ctx_router
    from musubi.retrieve.orchestration import RetrievalEnvelope, RetrievalResult
    from musubi.types.common import Ok

    async def mock_retrieve(
        client: object,
        embedder: object,
        reranker: object,
        query: dict[str, object],
        account_access: bool,
    ) -> object:
        if query["mode"] == "recent":
            return Ok(
                value=RetrievalEnvelope(
                    results=[
                        RetrievalResult(
                            object_id="recent-1",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="recent first",
                            score=1.0,
                            score_components={},
                            lineage={},
                            state="provisional",
                            importance=5,
                            provenance_score=1.0,
                            payload={"staleness": "current"},
                        ),
                        RetrievalResult(
                            object_id="recent-2",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="recent second",
                            score=1.0,
                            score_components={},
                            lineage={},
                            state="provisional",
                            importance=5,
                            provenance_score=1.0,
                            payload={"staleness": "current"},
                        ),
                    ],
                    warnings=(),
                )
            )
        else:
            return Ok(
                value=RetrievalEnvelope(
                    results=[
                        RetrievalResult(
                            object_id="ranked-1",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="test blending highly ranked",
                            score=0.95,
                            score_components={
                                "relevance": 0.95,
                                "recency": 0.8,
                                "importance": 0.8,
                                "provenance": 0.1,
                                "reinforcement": 0,
                            },
                            lineage={},
                            state="matured",
                            importance=8,
                        ),
                        RetrievalResult(
                            object_id="ranked-2",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="test blending second ranked",
                            score=0.90,
                            score_components={
                                "relevance": 0.90,
                                "recency": 0.5,
                                "importance": 0.8,
                                "provenance": 0.1,
                                "reinforcement": 0,
                            },
                            lineage={},
                            state="matured",
                            importance=7,
                        ),
                    ],
                    warnings=(),
                )
            )

    monkeypatch.setattr(ctx_router, "run_orchestration_retrieve", mock_retrieve)

    async def mock_account_delivered(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(ctx_router, "account_delivered", mock_account_delivered)

    resp = client.post(
        "/v1/context",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "planes": ["episodic"],
            "query_text": "test blending",
            "mode": "startup",
            "max_items": 2,
        },
    )
    data = resp.json()
    items = []
    for group in data["groups"]:
        items.extend(group["items"])

    assert len(items) == 2
    object_ids = {i["object_id"] for i in items}
    assert object_ids == {"recent-1", "ranked-1"}


def test_context_endpoint_max_chars_mix_quota(
    client: TestClient,
    valid_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guarantee the requested MIX under max_chars: when both pools are non-empty and max_items>=2,
    cap the reserved recent phase to at most one-third of max_chars so at least one ranked item can survive."""
    import musubi.api.routers.context as ctx_router
    from musubi.retrieve.orchestration import RetrievalEnvelope, RetrievalResult
    from musubi.types.common import Ok

    async def mock_retrieve(
        client: object,
        embedder: object,
        reranker: object,
        query: dict[str, object],
        account_access: bool,
    ) -> object:
        if query["mode"] == "recent":
            return Ok(
                value=RetrievalEnvelope(
                    results=[
                        RetrievalResult(
                            object_id="long-recent",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="A" * 1000,
                            score=1.0,
                            score_components={},
                            lineage={},
                            state="provisional",
                            importance=5,
                            provenance_score=1.0,
                        ),
                    ],
                    warnings=(),
                )
            )
        else:
            return Ok(
                value=RetrievalEnvelope(
                    results=[
                        RetrievalResult(
                            object_id="short-ranked",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="test blending highly ranked",
                            score=0.95,
                            score_components={
                                "relevance": 0.95,
                                "recency": 0.8,
                                "importance": 0.8,
                                "provenance": 0.1,
                                "reinforcement": 0,
                            },
                            lineage={},
                            state="matured",
                            importance=8,
                        ),
                    ],
                    warnings=(),
                )
            )

    monkeypatch.setattr(ctx_router, "run_orchestration_retrieve", mock_retrieve)

    async def mock_account_delivered(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(ctx_router, "account_delivered", mock_account_delivered)

    resp = client.post(
        "/v1/context",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "planes": ["episodic"],
            "query_text": "test blending",
            "mode": "startup",
            "max_items": 10,
            "max_chars": 600,
        },
    )
    data = resp.json()
    items = []
    for group in data["groups"]:
        items.extend(group["items"])

    # recent phase capped to max_chars // 3 = 200.
    # ranked phase gets the rest.
    assert len(items) == 2
    recent_item = next(i for i in items if i["object_id"] == "long-recent")
    ranked_item = next(i for i in items if i["object_id"] == "short-ranked")

    assert recent_item["content_truncated"] is True
    assert len(recent_item["content"]) <= 200
    assert ranked_item["content_truncated"] is False


@pytest.mark.parametrize("empty_lane", ["recent", "ranked"])
def test_context_endpoint_single_lane_empty_cases(
    client: TestClient,
    valid_token: str,
    monkeypatch: pytest.MonkeyPatch,
    empty_lane: str,
) -> None:
    import musubi.api.routers.context as ctx_router
    from musubi.retrieve.orchestration import RetrievalEnvelope, RetrievalResult
    from musubi.types.common import Ok

    async def mock_retrieve(
        client: object,
        embedder: object,
        reranker: object,
        query: dict[str, object],
        account_access: bool,
    ) -> object:
        # If empty_lane is 'ranked', the mode parameter in context.py is 'fast'
        lane_to_mode = {"recent": "recent", "ranked": "fast"}
        if query["mode"] == lane_to_mode[empty_lane]:
            return Ok(value=RetrievalEnvelope(results=[], warnings=()))

        if query["mode"] == "recent":
            return Ok(
                value=RetrievalEnvelope(
                    results=[
                        RetrievalResult(
                            object_id="recent-only",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="A brand new provisional thought",
                            score=1.0,
                            score_components={},
                            lineage={},
                            state="provisional",
                            importance=5,
                            provenance_score=1.0,
                        )
                    ],
                    warnings=(),
                )
            )
        else:
            return Ok(
                value=RetrievalEnvelope(
                    results=[
                        RetrievalResult(
                            object_id="ranked-only",
                            namespace="eric/claude-code/episodic",
                            plane="episodic",
                            snippet="test blending highly ranked",
                            score=0.95,
                            score_components={
                                "relevance": 0.95,
                                "recency": 0.8,
                                "importance": 0.8,
                                "provenance": 0.1,
                                "reinforcement": 0,
                            },
                            lineage={},
                            state="matured",
                            importance=8,
                        ),
                    ],
                    warnings=(),
                )
            )

    monkeypatch.setattr(ctx_router, "run_orchestration_retrieve", mock_retrieve)

    async def mock_account_delivered(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(ctx_router, "account_delivered", mock_account_delivered)

    resp = client.post(
        "/v1/context",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "planes": ["episodic"],
            "query_text": "test blending",
            "mode": "startup",
            "max_items": 10,
        },
    )
    data = resp.json()
    items = []
    for group in data["groups"]:
        items.extend(group["items"])

    assert len(items) == 1
    if empty_lane == "recent":
        assert items[0]["object_id"] == "ranked-only"
    else:
        assert items[0]["object_id"] == "recent-only"


def test_context_endpoint_custom_state_filter_applies_to_both_lanes(
    client: TestClient,
    valid_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import musubi.api.routers.context as ctx_router
    from musubi.retrieve.orchestration import RetrievalEnvelope
    from musubi.types.common import Ok

    captured_queries = []

    async def mock_retrieve(
        client: object,
        embedder: object,
        reranker: object,
        query: dict[str, object],
        account_access: bool,
    ) -> object:
        captured_queries.append(query)
        return Ok(value=RetrievalEnvelope(results=[], warnings=()))

    monkeypatch.setattr(ctx_router, "run_orchestration_retrieve", mock_retrieve)

    async def mock_account_delivered(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(ctx_router, "account_delivered", mock_account_delivered)

    client.post(
        "/v1/context",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "planes": ["episodic"],
            "query_text": "test blending",
            "mode": "startup",
            "state_filter": ["archived"],
        },
    )

    recent_query = captured_queries[0]
    fast_query = captured_queries[1]

    assert recent_query["state_filter"] == ["archived"]
    assert fast_query["state_filter"] == ["archived"]


def test_context_endpoint_returns_grouped_server_ranked_pack(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    namespace = "eric/claude-code/episodic"

    async def _seed() -> None:
        await episodic.create(
            EpisodicMemory(
                namespace=namespace,
                content=(
                    "V-053 promptsmith compiler route made deterministic image prompts "
                    "safe for Vice LoRA work."
                ),
                tags=["kind:project-stance", "staleness:durable", "project:vice"],
                importance=8,
            )
        )
        await episodic.create(
            EpisodicMemory(
                namespace=namespace,
                content="Old CyberRealistic Lightning drift notes.",
                tags=["kind:episode", "staleness:superseded", "project:vice"],
                importance=10,
            )
        )

    asyncio.run(_seed())

    response = client.post(
        "/v1/context",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code",
            "query_text": "Vice LoRA promptsmith compiler route",
            "planes": ["episodic"],
            "max_items": 3,
            "max_chars": 600,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "startup"
    flattened = "\n".join(item["content"] for group in body["groups"] for item in group["items"])
    assert "V-053 promptsmith compiler" in flattened
    assert "CyberRealistic" not in flattened
    first = body["groups"][0]["items"][0]
    assert first["kind"] == "project-stance"
    assert first["evidence_handle"].startswith(namespace)
    assert first["why_surfaced"]


def test_context_endpoint_can_include_history_when_explicitly_requested(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
) -> None:
    namespace = "eric/claude-code/episodic"

    async def _seed() -> None:
        await episodic.create(
            EpisodicMemory(
                namespace=namespace,
                content="Retired agent-msg history for adoption-day audit.",
                tags=["kind:episode", "staleness:superseded", "topic:adoption-day"],
                importance=7,
            )
        )

    asyncio.run(_seed())

    response = client.post(
        "/v1/context",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "query_text": "agent-msg adoption-day audit",
            "include_history": True,
            "planes": ["episodic"],
        },
    )

    assert response.status_code == 200, response.text
    flattened = "\n".join(
        item["content"] for group in response.json()["groups"] for item in group["items"]
    )
    assert "Retired agent-msg history" in flattened


def test_capture_rejects_unknown_typed_kind_tag(client: TestClient, valid_token: str) -> None:
    response = client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "content": "bad typed write",
            "tags": ["kind:whatever"],
        },
    )

    assert response.status_code == 422
    assert "unknown essence kind" in response.text


def test_capture_allows_legacy_untyped_tags(client: TestClient, valid_token: str) -> None:
    response = client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "content": "legacy gist-style write",
            "tags": ["old-note", "vice"],
        },
    )

    assert response.status_code == 202


# --------------------------------------------------------------------------- #
# RET-007 — /v1/context degradation surfacing (slice-ret007-degradation-impl, #422).
#
# The canonical context surface iterates the orchestration envelope and drops its warnings
# (routers/context.py), so a degraded context response is today INDISTINGUISHABLE from a healthy one.
# These reds define the contract: a degraded retrieve makes the wire response carry the bounded codes,
# and a healthy one keeps warnings additive/default-empty. Tests-only — no src in this commit.
# --------------------------------------------------------------------------- #


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


class _FakeWarning:
    """Structured stand-in for the accepted internal RetrievalWarning(code, plane)."""

    def __init__(self, code: str, plane: str) -> None:
        self.code = code
        self.plane = plane


class _FakeEnvelope:
    """Duck-typed retrieval envelope: iterable over ``results`` (so the current router's
    ``for hit in result.value`` keeps working) plus a ``warnings`` channel the impl must thread."""

    def __init__(self, results: list[Any], warnings: list[_FakeWarning]) -> None:
        self.results = results
        self.warnings = warnings

    def __iter__(self) -> Any:
        return iter(self.results)


def _post_context(client: TestClient, valid_token: str) -> Any:
    return client.post(
        "/v1/context",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code",
            "query_text": "Vice LoRA promptsmith compiler route",
            "planes": ["episodic"],
            "max_items": 3,
            "max_chars": 600,
        },
    )


def test_context_degraded_response_carries_warnings(
    client: TestClient, valid_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded retrieve (episodic timed out) must make the /v1/context response carry the bounded
    `plane_timeout_episodic` code — otherwise the caller cannot tell degraded context from healthy."""

    async def mock_orch(*args: Any, **kwargs: Any) -> Any:
        return _fake_ok([], [_FakeWarning("plane_timeout_episodic", "episodic")])

    monkeypatch.setattr("musubi.api.routers.context.run_orchestration_retrieve", mock_orch)
    resp = _post_context(client, valid_token)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    warnings = body.get("warnings")
    if not warnings or "plane_timeout_episodic" not in warnings:
        raise DefectStillPresent(
            f"/v1/context dropped the envelope warnings — degraded context is indistinguishable "
            f"from healthy; response keys={sorted(body)}"
        )


def test_context_healthy_response_default_empty_warnings(
    client: TestClient, valid_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTROL (green now + post-impl): a healthy retrieve carries no spurious warnings — the field is
    additive and defaults empty."""

    async def mock_orch(*args: Any, **kwargs: Any) -> Any:
        return _fake_ok([], [])

    monkeypatch.setattr("musubi.api.routers.context.run_orchestration_retrieve", mock_orch)
    resp = _post_context(client, valid_token)
    assert resp.status_code == 200, resp.text
    assert resp.json().get("warnings", []) == []


def _fake_ok(results: list[Any], warnings: list[_FakeWarning]) -> Ok[Any]:
    return Ok(value=_FakeEnvelope(results, warnings))
