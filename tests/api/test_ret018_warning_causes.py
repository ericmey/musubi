"""RET-018 contract: every canonical retrieval surface emits the same additive cause codes."""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from musubi.retrieve.orchestration import RetrievalEnvelope
from musubi.retrieve.warnings import reranker_failed
from musubi.types.common import Ok

pytestmark = pytest.mark.anyio


def test_retrieve_context_and_stream_share_reranker_cause_codes(
    client: TestClient,
    valid_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def degraded(*args: Any, **kwargs: Any) -> Any:
        return Ok(
            value=RetrievalEnvelope(
                results=[],
                warnings=(reranker_failed("episodic", cause="request_rejected"),),
            )
        )

    for module in (
        "musubi.api.routers.retrieve",
        "musubi.api.routers.context",
        "musubi.api.routers.writes_retrieve_stream",
    ):
        monkeypatch.setattr(f"{module}.run_orchestration_retrieve", degraded)

    headers = {"Authorization": f"Bearer {valid_token}"}
    expected = ["reranker_failed", "reranker_failed_request_rejected"]

    retrieve_response = client.post(
        "/v1/retrieve",
        headers=headers,
        json={
            "namespace": "eric/claude-code/episodic",
            "query_text": "cause contract",
            "mode": "deep",
            "planes": ["episodic"],
        },
    )
    assert retrieve_response.status_code == 200, retrieve_response.text
    assert retrieve_response.json()["warnings"] == expected

    context_response = client.post(
        "/v1/context",
        headers=headers,
        json={
            "namespace": "eric/claude-code",
            "query_text": "cause contract",
            "planes": ["episodic"],
            "max_items": 3,
            "max_chars": 600,
        },
    )
    assert context_response.status_code == 200, context_response.text
    assert context_response.json()["warnings"] == expected

    stream_response = client.post(
        "/v1/retrieve/stream",
        headers=headers,
        json={
            "namespace": "eric/claude-code/episodic",
            "query_text": "cause contract",
            "mode": "deep",
            "limit": 3,
        },
    )
    assert stream_response.status_code == 200, stream_response.text
    assert json.loads(stream_response.headers["X-Musubi-Warnings"]) == expected
