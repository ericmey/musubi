"""API tests for the ranked context-pack endpoint."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from musubi.planes.episodic import EpisodicPlane
from musubi.types.common import Ok
from musubi.types.episodic import EpisodicMemory


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
