"""API tests for the ranked context-pack endpoint."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from musubi.planes.episodic import EpisodicPlane
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
