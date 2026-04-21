"""Unit tests for :class:`musubi.llm.HttpxOllamaClient`.

The Protocol contract (see
:class:`musubi.lifecycle.maturation.OllamaClient`):

- Happy path: post to ``/api/chat`` and return a dict keyed by the
  input object_id.
- Network failure: return ``None`` (not raise).
- HTTP 4xx/5xx: return ``None``.
- JSON envelope invalid: return ``None``.
- Validation failure: return ``None``; dump raw response when
  ``debug_dir`` is set.
- Partial response (fewer ids than input): return only the matched
  ids — the sweep falls back on missing ids.
- Unknown ids in response (LLM hallucinates): drop them.
- Empty input: return empty dict (no HTTP call).

These tests drive the client through ``pytest-httpx`` so no live Ollama
is required.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from musubi.lifecycle.maturation import OllamaImportance, OllamaTopic
from musubi.llm import HttpxOllamaClient
from musubi.types.common import generate_ksuid

_BASE_URL = "http://ollama:11434"
_MODEL = "qwen3:4b"


def _chat_body(payload: dict[str, object]) -> dict[str, object]:
    """Wrap a JSON payload in Ollama's /api/chat envelope."""
    return {"message": {"role": "assistant", "content": json.dumps(payload)}}


def _importance_items(n: int) -> list[OllamaImportance]:
    return [
        OllamaImportance(
            object_id=generate_ksuid(),
            content=f"item {i} content",
            captured_importance=5,
        )
        for i in range(n)
    ]


def _topic_items(n: int) -> list[OllamaTopic]:
    return [
        OllamaTopic(
            object_id=generate_ksuid(),
            content=f"item {i} content",
            existing_tags=[],
        )
        for i in range(n)
    ]


def _client() -> HttpxOllamaClient:
    return HttpxOllamaClient(base_url=_BASE_URL, model=_MODEL)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_score_importance_happy_path(httpx_mock: HTTPXMock) -> None:
    items = _importance_items(2)
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(
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


async def test_score_importance_posts_chat_payload(httpx_mock: HTTPXMock) -> None:
    items = _importance_items(1)
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body({"items": [{"id": items[0].object_id, "importance": 6}]}),
    )
    await _client().score_importance(items)
    req = httpx_mock.get_request()
    assert req is not None
    body = json.loads(req.content)
    assert body["model"] == _MODEL
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["options"]["temperature"] == 0
    assert body["messages"][0]["role"] == "user"
    assert items[0].object_id in body["messages"][0]["content"]


async def test_infer_topics_happy_path(httpx_mock: HTTPXMock) -> None:
    items = _topic_items(2)
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(
            {
                "items": [
                    {"id": items[0].object_id, "topics": ["code/python", "project/musubi"]},
                    {"id": items[1].object_id, "topics": []},
                ]
            }
        ),
    )
    result = await _client().infer_topics(items)
    assert result == {
        items[0].object_id: ["code/python", "project/musubi"],
        items[1].object_id: [],
    }


# ---------------------------------------------------------------------------
# Empty input — no HTTP call
# ---------------------------------------------------------------------------


async def test_score_importance_empty_input_skips_http(httpx_mock: HTTPXMock) -> None:
    result = await _client().score_importance([])
    assert result == {}
    assert httpx_mock.get_request() is None


async def test_infer_topics_empty_input_skips_http(httpx_mock: HTTPXMock) -> None:
    result = await _client().infer_topics([])
    assert result == {}
    assert httpx_mock.get_request() is None


# ---------------------------------------------------------------------------
# Outage modes — must return None, not raise
# ---------------------------------------------------------------------------


async def test_score_importance_returns_none_on_connect_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    result = await _client().score_importance(_importance_items(1))
    assert result is None


async def test_score_importance_returns_none_on_timeout(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    result = await _client().score_importance(_importance_items(1))
    assert result is None


async def test_score_importance_returns_none_on_5xx(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat", method="POST", status_code=500, text="server err"
    )
    result = await _client().score_importance(_importance_items(1))
    assert result is None


async def test_infer_topics_returns_none_on_connect_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("nope"))
    result = await _client().infer_topics(_topic_items(1))
    assert result is None


# ---------------------------------------------------------------------------
# Bad payloads
# ---------------------------------------------------------------------------


async def test_score_importance_returns_none_on_envelope_not_json(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=f"{_BASE_URL}/api/chat", method="POST", text="not json at all")
    result = await _client().score_importance(_importance_items(1))
    assert result is None


async def test_score_importance_returns_none_when_message_content_missing(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json={"message": {"role": "assistant"}},
    )
    result = await _client().score_importance(_importance_items(1))
    assert result is None


async def test_score_importance_returns_none_on_validation_failure(
    httpx_mock: HTTPXMock,
) -> None:
    items = _importance_items(1)
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body({"items": [{"id": items[0].object_id, "importance": 42}]}),
    )
    result = await _client().score_importance(items)
    assert result is None


async def test_debug_dir_receives_failed_response(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    items = _importance_items(1)
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body({"items": [{"id": items[0].object_id, "importance": 999}]}),
    )
    client = HttpxOllamaClient(base_url=_BASE_URL, model=_MODEL, debug_dir=tmp_path)
    result = await client.score_importance(items)
    assert result is None
    dumps = list(tmp_path.glob("*-importance.json"))
    assert len(dumps) == 1
    payload = json.loads(dumps[0].read_text())
    assert "raw" in payload and "reason" in payload


# ---------------------------------------------------------------------------
# Partial / noisy responses
# ---------------------------------------------------------------------------


async def test_score_importance_partial_response_returns_matched_ids(
    httpx_mock: HTTPXMock,
) -> None:
    items = _importance_items(3)
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(
            {
                "items": [
                    {"id": items[0].object_id, "importance": 4},
                    {"id": items[2].object_id, "importance": 8},
                ]
            }
        ),
    )
    result = await _client().score_importance(items)
    assert result == {items[0].object_id: 4, items[2].object_id: 8}


async def test_score_importance_drops_hallucinated_ids(
    httpx_mock: HTTPXMock,
) -> None:
    items = _importance_items(1)
    fake_ksuid = generate_ksuid()
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(
            {
                "items": [
                    {"id": items[0].object_id, "importance": 6},
                    {"id": fake_ksuid, "importance": 9},
                ]
            }
        ),
    )
    result = await _client().score_importance(items)
    assert result == {items[0].object_id: 6}


async def test_infer_topics_drops_invalid_ksuids(httpx_mock: HTTPXMock) -> None:
    items = _topic_items(1)
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(
            {
                "items": [
                    {"id": items[0].object_id, "topics": ["code/python"]},
                    {"id": "not-a-ksuid", "topics": ["junk"]},
                ]
            }
        ),
    )
    result = await _client().infer_topics(items)
    assert result == {items[0].object_id: ["code/python"]}


# ---------------------------------------------------------------------------
# SynthesisOllamaClient Protocol
# ---------------------------------------------------------------------------


def _synth_input(n: int = 3) -> object:
    """Build a SynthesisInput with ``n`` stub EpisodicMemory items."""
    from datetime import timedelta

    from musubi.lifecycle.synthesis import SynthesisInput
    from musubi.types.common import utc_now
    from musubi.types.episodic import EpisodicMemory

    now = utc_now()
    memories = [
        EpisodicMemory(
            namespace="eric/ops/episodic",
            content=f"memory {i} about shared theme",
            importance=5,
            tags=["smoke"],
            event_at=now - timedelta(minutes=i),
        )
        for i in range(n)
    ]
    return SynthesisInput(memories=memories)


async def test_synthesize_cluster_happy_path(httpx_mock: HTTPXMock) -> None:
    from musubi.lifecycle.synthesis import SynthesisOutput

    cluster = _synth_input(3)
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body(
            {
                "title": "Smoke-test theme",
                "content": "A cluster about smoke-testing the stack.",
                "rationale": "All three items mention the smoke suite.",
                "tags": ["testing/smoke", "ops/deployment"],
                "importance": 6,
                "contradicts_notice": "",
            }
        ),
    )
    result = await _client().synthesize_cluster(cluster)  # type: ignore[arg-type]
    assert isinstance(result, SynthesisOutput)
    assert result.title == "Smoke-test theme"
    assert result.importance == 6
    assert "testing/smoke" in result.tags


async def test_synthesize_cluster_returns_none_on_outage(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("ollama down"))
    result = await _client().synthesize_cluster(_synth_input(3))  # type: ignore[arg-type]
    assert result is None


async def test_synthesize_cluster_returns_none_on_validation_failure(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        # importance=15 violates the ge=1/le=10 pydantic validator.
        json=_chat_body(
            {
                "title": "x",
                "content": "y",
                "rationale": "",
                "tags": [],
                "importance": 15,
                "contradicts_notice": "",
            }
        ),
    )
    result = await _client().synthesize_cluster(_synth_input(3))  # type: ignore[arg-type]
    assert result is None


async def test_synthesize_cluster_returns_none_on_empty_cluster() -> None:
    from musubi.lifecycle.synthesis import SynthesisInput

    result = await _client().synthesize_cluster(SynthesisInput(memories=[]))
    assert result is None


async def test_check_contradiction_happy_path(httpx_mock: HTTPXMock) -> None:

    from musubi.lifecycle.synthesis import (
        ContradictionInput,
        ContradictionOutput,
    )
    from musubi.types.concept import SynthesizedConcept

    concept_a = SynthesizedConcept(
        namespace="eric/ops/concept",
        title="Prefers dark mode",
        content="The user strongly prefers dark mode.",
        synthesis_rationale="Multiple memories show dark-mode preference.",
        merged_from=[generate_ksuid(), generate_ksuid(), generate_ksuid()],
    )
    concept_b = SynthesizedConcept(
        namespace="eric/ops/concept",
        title="Prefers light mode",
        content="The user strongly prefers light mode.",
        synthesis_rationale="Multiple memories show light-mode preference.",
        merged_from=[generate_ksuid(), generate_ksuid(), generate_ksuid()],
    )
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body({"verdict": "contradictory", "reason": "Opposite claims about theme."}),
    )
    result = await _client().check_contradiction(
        ContradictionInput(concept_a=concept_a, concept_b=concept_b)
    )
    assert isinstance(result, ContradictionOutput)
    assert result.verdict == "contradictory"


async def test_check_contradiction_rejects_unknown_verdict(
    httpx_mock: HTTPXMock,
) -> None:
    """Verdict must be one of the two enumerated values."""

    from musubi.lifecycle.synthesis import ContradictionInput
    from musubi.types.concept import SynthesizedConcept

    concept = SynthesizedConcept(
        namespace="eric/ops/concept",
        title="x",
        content="y",
        synthesis_rationale="stub",
        merged_from=[generate_ksuid(), generate_ksuid(), generate_ksuid()],
    )
    httpx_mock.add_response(
        url=f"{_BASE_URL}/api/chat",
        method="POST",
        json=_chat_body({"verdict": "unclear", "reason": "hmm"}),
    )
    result = await _client().check_contradiction(
        ContradictionInput(concept_a=concept, concept_b=concept)
    )
    assert result is None


# ---------------------------------------------------------------------------
# Prompt files are loadable and non-empty
# ---------------------------------------------------------------------------


async def test_prompts_are_loadable_and_have_items_placeholder() -> None:
    from importlib.resources import files

    for name in ("importance", "topics"):
        resource = files("musubi.llm.prompts").joinpath(name).joinpath("v1.txt")
        text = resource.read_text(encoding="utf-8")
        assert "{ITEMS}" in text
        assert len(text) > 100


pytestmark = pytest.mark.asyncio
