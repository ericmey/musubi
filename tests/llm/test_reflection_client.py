"""Unit tests for :class:`musubi.llm.reflection_client.HttpxReflectionClient`.

The Protocol contract (:class:`musubi.lifecycle.reflection.ReflectionLLM`):

    async def summarize_patterns(self, items: list[dict[str, object]]) -> str | None

Failure mode matches the Ollama pattern: return ``None`` on any
outage / error so the sweep can substitute the documented skip notice
rather than failing the whole reflection run.
"""

from __future__ import annotations

import json

import httpx
from pytest_httpx import HTTPXMock

from musubi.llm.reflection_client import HttpxReflectionClient

_BASE_URL = "http://ollama:11434"
_MODEL = "qwen3:4b"


def _chat_body(content: str) -> dict[str, object]:
    return {"message": {"role": "assistant", "content": content}}


def _items(n: int) -> list[dict[str, object]]:
    return [
        {"id": f"id-{i}", "topic": f"topic-{i}", "summary": f"summary line {i}"}
        for i in range(n)
    ]


def _client() -> HttpxReflectionClient:
    return HttpxReflectionClient(base_url=_BASE_URL, model=_MODEL)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_summarize_patterns_returns_markdown(httpx_mock: HTTPXMock) -> None:
    content = "## Theme One\n\nA paragraph about the first theme."
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(content),
    )
    out = await _client().summarize_patterns(_items(3))
    assert out is not None
    assert "## Theme One" in out


async def test_summarize_patterns_passes_items_to_prompt(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body("## T\n\nBody."),
    )
    await _client().summarize_patterns(
        [{"id": "zzz", "topic": "vault", "summary": "a marker-summary-string"}]
    )
    req = httpx_mock.get_request()
    assert req is not None
    body = json.loads(req.content)
    user_prompt = body["messages"][-1]["content"]
    assert "marker-summary-string" in user_prompt
    assert "vault" in user_prompt


async def test_summarize_patterns_empty_items_returns_empty_string_without_http(
    httpx_mock: HTTPXMock,
) -> None:
    """No items → no LLM call needed; return the empty string directly."""
    out = await _client().summarize_patterns([])
    assert out == ""
    # No requests were made.
    assert httpx_mock.get_requests() == []


# ---------------------------------------------------------------------------
# Failure paths — Protocol says return None, not raise
# ---------------------------------------------------------------------------


async def test_summarize_patterns_returns_none_on_network_failure(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    assert await _client().summarize_patterns(_items(2)) is None


async def test_summarize_patterns_returns_none_on_http_500(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        status_code=500,
        text="internal error",
    )
    assert await _client().summarize_patterns(_items(2)) is None


async def test_summarize_patterns_returns_none_on_empty_content(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(""),
    )
    assert await _client().summarize_patterns(_items(2)) is None


async def test_summarize_patterns_returns_none_on_malformed_envelope(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json={"totally": "wrong"},
    )
    assert await _client().summarize_patterns(_items(2)) is None
