"""DQ-001: retrieval truncation is explicit on every affected wire surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from musubi.api.responses import (
    RankedExtra,
    RankedResultRow,
    RankedScoreComponents,
    RecentExtra,
    RecentResultRow,
    RecentScoreComponents,
)
from musubi.retrieve.context_pack import ContextCandidate, ContextPackQuery, build_context_pack
from musubi.retrieve.fast import _snippet as fast_snippet
from musubi.retrieve.orchestration import (
    RetrievalEnvelope,
    RetrievalResult,
)
from musubi.retrieve.orchestration import (
    _snippet as orchestration_snippet,
)
from musubi.retrieve.recent import _snippet as recent_snippet
from musubi.types.common import Ok

SnippetResult = tuple[str, bool, int]
SnippetFn = Callable[[dict[str, Any]], SnippetResult]
OrchestrationMock = Callable[..., Awaitable[Ok[RetrievalEnvelope]]]


def _fast(payload: dict[str, Any]) -> SnippetResult:
    return fast_snippet(payload)


def _recent(payload: dict[str, Any]) -> SnippetResult:
    return recent_snippet(payload)


def _ranked(payload: dict[str, Any]) -> SnippetResult:
    return orchestration_snippet(payload, max_chars=300)


@pytest.mark.parametrize(
    ("snippet", "cap"),
    [(_fast, 200), (_recent, 300), (_ranked, 300)],
    ids=["fast", "recent", "ranked"],
)
def test_production_snippet_boundary_is_truthful(snippet: SnippetFn, cap: int) -> None:
    exact, exact_truncated, exact_length = snippet({"content": "x" * cap})
    over, over_truncated, over_length = snippet({"content": "x" * (cap + 1)})

    assert exact == "x" * cap
    assert exact_truncated is False
    assert exact_length == cap
    assert over == "x" * cap
    assert over_truncated is True
    assert over_length == cap + 1


@pytest.mark.parametrize("snippet", [_fast, _recent, _ranked], ids=["fast", "recent", "ranked"])
def test_production_snippet_length_counts_unicode_characters(snippet: SnippetFn) -> None:
    content = "🎉🚀🔥💯"
    rendered, truncated, content_length = snippet({"content": content})

    assert len(content) == 4
    assert len(content.encode("utf-8")) == 16
    assert rendered == content
    assert truncated is False
    assert content_length == 4


def _mock_orchestration(*, recent: bool) -> OrchestrationMock:
    score_components = (
        {}
        if recent
        else {
            "relevance": 0.8,
            "recency": 0.1,
            "importance": 0.2,
            "provenance": 0.3,
            "reinforcement": 0.4,
        }
    )
    result = RetrievalResult(
        object_id="dq001-row",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        title="DQ-001",
        snippet="z" * 200,
        content_truncated=True,
        content_length=500,
        score=1.0,
        score_components=score_components,
        lineage={},
        state="matured",
        importance=5,
        provenance_score=0.8 if recent else None,
    )

    async def run(*args: object, **kwargs: object) -> Ok[RetrievalEnvelope]:
        return Ok(value=RetrievalEnvelope(results=[result], warnings=()))

    return run


@pytest.mark.parametrize("mode", ["fast", "deep", "blended", "recent"])
def test_retrieve_wire_emits_truncation_metadata(
    client: TestClient,
    valid_token: str,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(
        "musubi.api.routers.retrieve.run_orchestration_retrieve",
        _mock_orchestration(recent=mode == "recent"),
    )
    body: dict[str, object] = {
        "namespace": "eric/claude-code/episodic",
        "mode": mode,
        "limit": 5,
    }
    if mode != "recent":
        body["query_text"] = "truncation"

    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": f"Bearer {valid_token}"},
        json=body,
    )

    assert response.status_code == 200, response.text
    row = response.json()["results"][0]
    assert row["object_id"] == "dq001-row"
    assert row["content"] == "z" * 200
    assert row["content_truncated"] is True
    assert row["content_length"] == 500


def test_ranked_projection_declares_facts_after_301_1501_and_at_end_unavailable() -> None:
    content = list("x" * 1608)
    content[300] = "A"  # ordinal character 301
    content[1500] = "B"  # ordinal character 1501
    content[-1] = "Z"  # final character

    rendered, truncated, content_length = _ranked({"content": "".join(content)})

    assert len(rendered) == 300
    assert "A" not in rendered
    assert "B" not in rendered
    assert not rendered.endswith("Z")
    assert truncated is True
    assert content_length == 1608


@pytest.mark.parametrize(
    "cluster",
    ["e\u0301", "\U0001f468\u200d\U0001f469\u200d\U0001f467"],
    ids=["decomposed-combining-mark", "multi-codepoint-zwj-emoji"],
)
def test_unicode_cluster_at_ranked_boundary_is_never_silent(cluster: str) -> None:
    content = ("x" * 299) + cluster + "TAIL"

    rendered, truncated, content_length = _ranked({"content": content})

    # The current core projection is code-point bounded, not a full grapheme segmenter.
    # DQ-001 therefore remains open for grapheme-safe projection, but the wire must
    # truthfully disclose that content was cut and preserve the original character count.
    assert rendered == content[:300]
    assert truncated is True
    assert content_length == len(content)


def test_wire_models_keep_backward_compatible_defaults() -> None:
    ranked = RankedResultRow(
        object_id="ranked",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=0.5,
        content="complete",
        state="matured",
        importance=5,
        score_kind="ranked_combined",
        extra=RankedExtra(
            score_components=RankedScoreComponents(
                relevance=0.5,
                recency=0.1,
                importance=0.2,
                provenance=0.3,
                reinforcement=0.4,
            ),
            lineage={},
        ),
    )
    recent = RecentResultRow(
        object_id="recent",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=1.0,
        content="complete",
        state="matured",
        importance=5,
        score_kind="created_epoch",
        provenance_score=0.8,
        extra=RecentExtra(score_components=RecentScoreComponents(), lineage={}),
    )

    assert ranked.content_truncated is False
    assert ranked.content_length is None
    assert recent.content_truncated is False
    assert recent.content_length is None


@pytest.mark.parametrize(("length", "expected"), [(120, False), (121, True)])
def test_context_pack_reports_its_actual_display_cap(length: int, expected: bool) -> None:
    candidate = ContextCandidate(
        object_id="context",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        content="c" * length,
        state="matured",
        importance=5,
        extra={"kind": "decision", "staleness": "durable"},
    )

    pack = build_context_pack(
        [candidate],
        ContextPackQuery(mode="startup", max_items=1, max_chars=120),
    )

    item = pack.groups[0].items[0]
    assert item.content_truncated is expected
    assert item.content_length == length
    assert len(item.content) <= 120


def test_context_pack_length_uses_normalized_display_text() -> None:
    raw_content = "word    " * 20
    normalized = " ".join(raw_content.split())
    assert len(raw_content) > 120
    assert len(normalized) < 120
    candidate = ContextCandidate(
        object_id="whitespace",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        content=raw_content,
        state="matured",
        importance=5,
        extra={"kind": "decision", "staleness": "durable"},
    )

    pack = build_context_pack(
        [candidate],
        ContextPackQuery(mode="startup", max_items=1, max_chars=120),
    )

    item = pack.groups[0].items[0]
    assert item.content == normalized
    assert item.content_truncated is False
    assert item.content_length == len(normalized)
