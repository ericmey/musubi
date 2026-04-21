"""Unit tests for :class:`musubi.llm.promotion_client.HttpxPromotionClient`.

The Protocol contract (:class:`musubi.lifecycle.promotion.PromotionLLM`):

- Happy path → returns a :class:`PromotionRender` with at least one H2
  section, body between 100 and 20k chars, no AI-disclaimer strings.
- Network failure / timeout / HTTP 4xx/5xx → raise (the sweep wraps
  each call in try/except and records a rejection — see
  ``_promote_concept``). Returning ``None`` like the maturation Ollama
  client would be silently consumed as "rendering succeeded with no
  content," which is worse than a loud failure.
- Envelope parse failure → raise ``ValueError``.
- Validation failure (body too short, no H2, AI disclaimer) → raise
  ``ValueError`` with the concrete message — lets the sweep record a
  specific rejection reason.

Tests drive the client through ``pytest-httpx`` so no live Ollama is
required.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from musubi.llm.promotion_client import HttpxPromotionClient

_BASE_URL = "http://ollama:11434"
_MODEL = "qwen3:4b"

_VALID_BODY = (
    "## Overview\n\n"
    "This is a curated note about the concept. It contains enough content\n"
    "to clear the 100-character minimum set by the validator, written in a\n"
    "neutral voice that matches the user's style.\n\n"
    "## Details\n\nAnother section with more depth and a couple of links.\n"
)


def _chat_body(payload: dict[str, object]) -> dict[str, object]:
    return {"message": {"role": "assistant", "content": json.dumps(payload)}}


def _valid_payload() -> dict[str, object]:
    return {
        "body": _VALID_BODY,
        "wikilinks": ["[[concept-a]]", "[[topic-b]]"],
        "sections": ["Overview", "Details"],
    }


def _client() -> HttpxPromotionClient:
    return HttpxPromotionClient(base_url=_BASE_URL, model=_MODEL)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_render_curated_markdown_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(_valid_payload()),
    )
    client = _client()
    render = await client.render_curated_markdown(
        title="A concept",
        content="Concept body.",
        rationale="Concept rationale.",
        top_memories=["memory one", "memory two"],
    )
    assert "## Overview" in render.body
    assert render.wikilinks == ["[[concept-a]]", "[[topic-b]]"]
    assert render.sections == ["Overview", "Details"]


async def test_render_includes_top_memories_in_prompt(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(_valid_payload()),
    )
    client = _client()
    await client.render_curated_markdown(
        title="T",
        content="C",
        rationale="R",
        top_memories=["the-tell-tale-memory-string"],
    )
    request = httpx_mock.get_request()
    assert request is not None
    body = json.loads(request.content)
    user_prompt = body["messages"][-1]["content"]
    assert "the-tell-tale-memory-string" in user_prompt


# ---------------------------------------------------------------------------
# Failure paths — Protocol contract says RAISE, not return None
# ---------------------------------------------------------------------------


async def test_render_raises_on_network_failure(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    client = _client()
    with pytest.raises(httpx.ConnectError):
        await client.render_curated_markdown(
            title="T", content="C", rationale="R", top_memories=[]
        )


async def test_render_raises_on_http_500(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        status_code=500,
        text="internal error",
    )
    client = _client()
    with pytest.raises(httpx.HTTPStatusError):
        await client.render_curated_markdown(
            title="T", content="C", rationale="R", top_memories=[]
        )


async def test_render_raises_on_invalid_envelope(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json={"unexpected": "shape"},
    )
    client = _client()
    with pytest.raises(ValueError, match="envelope"):
        await client.render_curated_markdown(
            title="T", content="C", rationale="R", top_memories=[]
        )


async def test_render_raises_when_body_has_no_h2(httpx_mock: HTTPXMock) -> None:
    bad = dict(_valid_payload())
    bad["body"] = "Just a paragraph, no heading at all, but long enough to pass the 100 char minimum check easily so pydantic's length clears."
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(bad),
    )
    client = _client()
    with pytest.raises(ValueError):
        await client.render_curated_markdown(
            title="T", content="C", rationale="R", top_memories=[]
        )


async def test_render_raises_on_ai_disclaimer(httpx_mock: HTTPXMock) -> None:
    bad = dict(_valid_payload())
    bad["body"] = (
        "## Note\n\nAs an AI model I cannot have personal experiences, but this is my "
        "attempt to render the concept as requested above."
    )
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(bad),
    )
    client = _client()
    with pytest.raises(ValueError):
        await client.render_curated_markdown(
            title="T", content="C", rationale="R", top_memories=[]
        )


async def test_render_raises_on_body_too_short(httpx_mock: HTTPXMock) -> None:
    bad = dict(_valid_payload())
    bad["body"] = "## Too short"
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(bad),
    )
    client = _client()
    with pytest.raises(ValueError):
        await client.render_curated_markdown(
            title="T", content="C", rationale="R", top_memories=[]
        )
