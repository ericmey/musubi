"""OpenAI-wire tests for :class:`musubi.llm.HttpxOllamaClient` (ADR 0043).

Same client, second wire protocol: ``api="openai"`` speaks
``/v1/chat/completions`` with strict ``json_schema`` response_format so the
lifecycle worker can target LiteLLM-fronted house models. The contract above
the transport is identical to the Ollama wire — same Protocol methods, same
return-None-on-failure — so these tests mirror ``test_ollama.py`` shapes and
assert only what the new wire adds:

- URL normalization (base with or without a trailing ``/v1``).
- ``Authorization: Bearer`` present exactly when a key is configured.
- ``response_format`` carries ``json_schema`` + ``strict: true`` + the
  task-named schema.
- Content extraction from ``choices[0].message.content``.
- Transport/HTTP failure → ``None`` (never raise).
- Unknown ``api`` value fails loudly at construction.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from musubi.lifecycle.maturation import OllamaImportance
from musubi.lifecycle.synthesis import SynthesisInput
from musubi.llm import HttpxOllamaClient
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory

_BASE = "http://litellm:4000"
_MODEL = "house/backup"
_KEY = "sk-litellm-test"


def _completion_body(payload: dict[str, object]) -> dict[str, object]:
    """Wrap a JSON payload in the OpenAI chat-completion envelope."""
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]}


def _importance_items(n: int) -> list[OllamaImportance]:
    return [
        OllamaImportance(
            object_id=generate_ksuid(),
            content=f"item {i} content",
            captured_importance=5,
        )
        for i in range(n)
    ]


def _client(base: str = _BASE, key: str | None = _KEY) -> HttpxOllamaClient:
    return HttpxOllamaClient(base_url=base, model=_MODEL, api="openai", api_key=key)


async def test_score_importance_happy_path_openai(httpx_mock: HTTPXMock) -> None:
    items = _importance_items(2)
    httpx_mock.add_response(
        url=f"{_BASE}/v1/chat/completions",
        method="POST",
        json=_completion_body(
            {
                "items": [
                    {"id": items[0].object_id, "importance": 7},
                    {"id": items[1].object_id, "importance": 3},
                ]
            }
        ),
    )
    result = await _client().score_importance(items)
    assert result == {items[0].object_id: 7, items[1].object_id: 3}


async def test_request_shape_bearer_and_strict_json_schema(httpx_mock: HTTPXMock) -> None:
    items = _importance_items(1)
    httpx_mock.add_response(
        url=f"{_BASE}/v1/chat/completions",
        method="POST",
        json=_completion_body({"items": []}),
    )
    await _client().score_importance(items)

    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == f"Bearer {_KEY}"
    body = json.loads(request.content)
    assert body["model"] == _MODEL
    assert body["temperature"] == 0
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "musubi_importance"
    assert rf["json_schema"]["schema"]["type"] == "object"
    # Ollama-wire keys must NOT leak onto the OpenAI wire.
    assert "format" not in body
    assert "options" not in body


async def test_base_url_with_trailing_v1_is_not_doubled(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{_BASE}/v1/chat/completions",
        method="POST",
        json=_completion_body({"items": []}),
    )
    client = _client(base=f"{_BASE}/v1")
    assert await client.score_importance(_importance_items(1)) == {}


async def test_no_api_key_sends_no_authorization_header(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{_BASE}/v1/chat/completions",
        method="POST",
        json=_completion_body({"items": []}),
    )
    await _client(key=None).score_importance(_importance_items(1))
    assert "Authorization" not in httpx_mock.get_requests()[0].headers


async def test_http_error_returns_none(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{_BASE}/v1/chat/completions",
        method="POST",
        status_code=502,
        text="bad gateway",
    )
    assert await _client().score_importance(_importance_items(1)) is None


async def test_synthesize_cluster_happy_path_openai(httpx_mock: HTTPXMock) -> None:
    memories = [
        EpisodicMemory(namespace="hana/hw-7ds/episodic", content=f"memory {i}") for i in range(3)
    ]
    httpx_mock.add_response(
        url=f"{_BASE}/v1/chat/completions",
        method="POST",
        json=_completion_body(
            {
                "title": "Sunday gravy at house scale",
                "content": "Cooking for eight changed the kitchen's rhythm.",
                "rationale": "All three memories describe the same cooking arc.",
                "tags": ["cooking", "house"],
                "importance": 6,
                "contradicts_notice": "",
            }
        ),
    )
    out = await _client().synthesize_cluster(SynthesisInput(memories))
    assert out is not None
    assert out.title == "Sunday gravy at house scale"
    assert out.importance == 6


def test_unknown_api_value_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unsupported llm api"):
        HttpxOllamaClient(base_url=_BASE, model=_MODEL, api="typo")
