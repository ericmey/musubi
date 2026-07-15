import json

import pytest
from pytest import MonkeyPatch
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.types.episodic import EpisodicMemory

pytestmark = pytest.mark.anyio


async def _seed_memory(
    episodic: EpisodicPlane, qdrant: QdrantClient, namespace: str, content: str
) -> str:
    saved = await episodic.create(EpisodicMemory(namespace=namespace, content=content))
    return saved.object_id


def test_streaming_retrieval_ranked(
    client: TestClient, episodic: EpisodicPlane, qdrant: QdrantClient, valid_token: str
) -> None:
    namespace = "eric/claude-code/episodic"

    client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={"namespace": namespace, "content": "Test stream ranked content"},
    )

    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": namespace,
            "query_text": "stream ranked",
            "mode": "fast",
            "limit": 5,
        },
    )

    assert r.status_code == 200, r.text
    assert r.headers["X-Musubi-Mode"] == "fast"
    assert r.headers["X-Musubi-Limit"] == "5"
    assert r.headers["X-Musubi-Warnings"] == "[]"
    assert r.headers["content-type"].startswith("application/x-ndjson")

    lines = [line for line in r.text.split("\n") if line]
    assert len(lines) == 1

    row = json.loads(lines[0])
    assert row["object_id"] is not None
    assert row["namespace"] == namespace
    assert row["plane"] == "episodic"
    assert row["score_kind"] == "ranked_combined"
    assert "relevance" in row["extra"]["score_components"]


def test_streaming_retrieval_recent(
    client: TestClient, episodic: EpisodicPlane, qdrant: QdrantClient, valid_token: str
) -> None:
    namespace = "eric/claude-code/episodic"

    client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={"namespace": namespace, "content": "Test stream recent content"},
    )

    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": namespace,
            "query_text": "",
            "mode": "recent",
            "limit": 5,
        },
    )

    assert r.status_code == 200, r.text
    assert r.headers["X-Musubi-Mode"] == "recent"
    assert r.headers["X-Musubi-Limit"] == "5"
    assert r.headers["X-Musubi-Warnings"] == "[]"
    assert r.headers["content-type"].startswith("application/x-ndjson")

    lines = [line for line in r.text.split("\n") if line]
    assert len(lines) == 1

    row = json.loads(lines[0])
    assert row["object_id"] is not None
    assert row["namespace"] == namespace
    assert row["plane"] == "episodic"
    assert row["score_kind"] == "created_epoch"
    assert row["extra"]["score_components"] == {}


def test_streaming_retrieval_wildcard_auth_forbids(
    client: TestClient, api_settings: Settings, episodic: EpisodicPlane, qdrant: QdrantClient
) -> None:
    from tests.api.conftest import mint_token

    # Token with scope for only one namespace
    token = mint_token(api_settings, scopes=["nyla/streaming-wildcard/episodic:r"])

    # Create two rows in different namespaces
    # Use valid_token fixture? Wait, I need a token with write access to populate the DB, or just use `mint_token` with rw.
    write_token = mint_token(
        api_settings,
        scopes=["nyla/streaming-wildcard/episodic:rw", "nyla/streaming-other/episodic:rw"],
    )

    client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {write_token}"},
        json={"namespace": "nyla/streaming-wildcard/episodic", "content": "Allowed"},
    )

    client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {write_token}"},
        json={"namespace": "nyla/streaming-other/episodic", "content": "Forbidden"},
    )

    # Query with wildcard that resolves to both namespaces
    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "nyla/*/episodic",
            "query_text": "content",
            "mode": "fast",
        },
    )

    # The wildcard expansion matches both namespaces, but auth token only allows one.
    # Should yield 403 Forbidden because it checks scope for ALL expanded targets.
    assert r.status_code == 403
    assert "FORBIDDEN" in r.json()["error"]["code"]


def test_streaming_retrieval_zero_row_warning_header(client: TestClient, valid_token: str) -> None:
    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "query_text": "nothing here",
            "mode": "fast",
            "limit": 5,
        },
    )
    assert r.status_code == 200
    assert r.headers["X-Musubi-Mode"] == "fast"
    assert r.headers["X-Musubi-Limit"] == "5"
    assert (
        r.headers["X-Musubi-Warnings"] == '["TEI_DENSE_UNAVAILABLE"]'
        or "[]" in r.headers["X-Musubi-Warnings"]
    )
    # We might have warnings if we monkeypatch TEI, but let's test zero rows
    lines = [line for line in r.text.split("\n") if line]
    assert len(lines) == 0


def test_streaming_retrieval_degraded_warning_header(
    client: TestClient, valid_token: str, monkeypatch: MonkeyPatch
) -> None:
    import musubi.api.routers.writes_retrieve_stream as writes_retrieve_stream
    from musubi.retrieve.orchestration import RetrievalEnvelope
    from musubi.retrieve.warnings import RetrievalWarning
    from musubi.types.common import Ok

    # Monkeypatch orchestration to return a warning
    async def degraded_retrieve(*args: object, **kwargs: object) -> object:
        return Ok(
            value=RetrievalEnvelope(
                results=[],
                warnings=(RetrievalWarning(code="TEI_DENSE_UNAVAILABLE", plane="episodic"),),
            )
        )

    monkeypatch.setattr(writes_retrieve_stream, "run_orchestration_retrieve", degraded_retrieve)

    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "query_text": "trigger degradation",
            "mode": "blended",
            "limit": 5,
        },
    )
    assert r.status_code == 200
    assert r.headers["X-Musubi-Mode"] == "blended"
    assert "TEI_DENSE_UNAVAILABLE" in r.headers["X-Musubi-Warnings"]


def test_streaming_typed_error_mapping(
    client: TestClient, valid_token: str, monkeypatch: MonkeyPatch
) -> None:
    import musubi.api.routers.writes_retrieve_stream as writes_retrieve_stream
    from musubi.retrieve.orchestration import RetrievalError
    from musubi.types.common import Err

    # Monkeypatch orchestration to return a forced error
    async def failing_retrieve(*args: object, **kwargs: object) -> object:
        return Err(error=RetrievalError(kind="bad_query", detail="Forced bad query error"))

    monkeypatch.setattr(writes_retrieve_stream, "run_orchestration_retrieve", failing_retrieve)

    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "query_text": "trigger error",
            "mode": "fast",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"
    assert r.json()["error"]["detail"] == "Forced bad query error"
