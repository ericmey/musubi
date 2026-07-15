import json

import pytest
from pytest import MonkeyPatch
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings

pytestmark = pytest.mark.anyio


def test_streaming_retrieval_ranked(
    client: TestClient, episodic: EpisodicPlane, qdrant: QdrantClient, valid_token: str
) -> None:
    namespace = "eric/claude-code/episodic"

    resp = client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={"namespace": namespace, "content": "Test stream ranked content"},
    )
    assert resp.status_code // 100 == 2

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

    # Strong schema assertion for ranked mode
    assert "title" in row
    assert "state" in row
    assert "importance" in row
    assert row["score_kind"] == "ranked_combined"
    assert "extra" in row
    assert "lineage" in row["extra"]

    components = row["extra"]["score_components"]
    assert all(
        k in components
        for k in ["relevance", "recency", "importance", "provenance", "reinforcement"]
    )


def test_streaming_retrieval_recent(
    client: TestClient, episodic: EpisodicPlane, qdrant: QdrantClient, valid_token: str
) -> None:
    namespace = "eric/claude-code/episodic"

    resp = client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={"namespace": namespace, "content": "Test stream recent content"},
    )
    assert resp.status_code // 100 == 2

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

    token = mint_token(api_settings, scopes=["nyla/streaming-wildcard/episodic:r"])

    write_token = mint_token(
        api_settings,
        scopes=["nyla/streaming-wildcard/episodic:rw", "nyla/streaming-other/episodic:rw"],
    )

    r1 = client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {write_token}"},
        json={"namespace": "nyla/streaming-wildcard/episodic", "content": "Allowed"},
    )
    assert r1.status_code // 100 == 2

    r2 = client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {write_token}"},
        json={"namespace": "nyla/streaming-other/episodic", "content": "Forbidden"},
    )
    assert r2.status_code // 100 == 2

    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "nyla/*/episodic",
            "query_text": "content",
            "mode": "fast",
        },
    )

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
    assert r.headers["X-Musubi-Warnings"] == "[]"
    lines = [line for line in r.text.split("\n") if line]
    assert len(lines) == 0


def test_streaming_retrieval_degraded_warning_header(
    client: TestClient, valid_token: str, monkeypatch: MonkeyPatch
) -> None:
    import musubi.api.routers.writes_retrieve_stream as writes_retrieve_stream
    from musubi.retrieve.orchestration import RetrievalEnvelope
    from musubi.retrieve.warnings import RetrievalWarning
    from musubi.types.common import Ok

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
    assert r.headers["X-Musubi-Warnings"] == '["TEI_DENSE_UNAVAILABLE"]'
    lines = [line for line in r.text.split("\n") if line]
    assert len(lines) == 0


def test_streaming_typed_error_mapping(
    client: TestClient, valid_token: str, monkeypatch: MonkeyPatch
) -> None:
    import musubi.api.routers.writes_retrieve_stream as writes_retrieve_stream
    from musubi.retrieve.orchestration import RetrievalError
    from musubi.types.common import Err

    current_error_kind = "bad_query"

    async def failing_retrieve(*args: object, **kwargs: object) -> object:
        # Use model_construct to bypass literal validation for the unknown test case
        return Err(
            error=RetrievalError.model_construct(kind=current_error_kind, detail="Forced error")
        )

    monkeypatch.setattr(writes_retrieve_stream, "run_orchestration_retrieve", failing_retrieve)

    # 1. Known mapping (bad_query -> 400 BAD_REQUEST)
    r1 = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "query_text": "trigger error",
            "mode": "fast",
        },
    )
    assert r1.status_code == 400
    assert r1.json()["error"]["code"] == "BAD_REQUEST"
    assert r1.json()["error"]["detail"] == "Forced error"

    # 2. Unknown mapping -> 500 INTERNAL
    current_error_kind = "some_unknown_weird_error"
    r2 = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "query_text": "trigger error",
            "mode": "fast",
        },
    )
    assert r2.status_code == 500
    assert r2.json()["error"]["code"] == "INTERNAL"


def test_streaming_retrieval_forwards_all_query_parameters(
    client: TestClient, valid_token: str, monkeypatch: MonkeyPatch
) -> None:
    import musubi.api.routers.writes_retrieve_stream as writes_retrieve_stream
    from musubi.retrieve.orchestration import RetrievalEnvelope
    from musubi.types.common import Ok

    captured_query = {}

    async def mock_retrieve(
        client: object, embedder: object, reranker: object, query: dict[str, object]
    ) -> object:
        nonlocal captured_query
        captured_query = query
        return Ok(value=RetrievalEnvelope(results=[], warnings=()))

    monkeypatch.setattr(writes_retrieve_stream, "run_orchestration_retrieve", mock_retrieve)

    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code/episodic",
            "query_text": "param test",
            "mode": "recent",
            "limit": 7,
            "since": 12345.0,
            "tags": ["tag1"],
            "state_filter": ["matured"],
            "include_archived": True,
            "include_lineage": False,
        },
    )
    assert r.status_code == 200

    assert captured_query["mode"] == "recent"
    assert captured_query["since"] == 12345.0
    assert captured_query["tags"] == ["tag1"]
    assert captured_query["state_filter"] == ["matured"]
    assert captured_query["include_archived"] is True
    assert captured_query["include_lineage"] is False
    assert captured_query["namespace_targets"] == [
        {"namespace": "eric/claude-code/episodic", "plane": "episodic"}
    ]


def test_streaming_retrieval_multi_plane_fanout(
    client: TestClient, valid_token: str, monkeypatch: MonkeyPatch
) -> None:
    import musubi.api.routers.writes_retrieve_stream as writes_retrieve_stream
    from musubi.retrieve.orchestration import RetrievalEnvelope, RetrievalResult
    from musubi.types.common import Ok

    async def mock_retrieve(
        client: object, embedder: object, reranker: object, query: dict[str, object]
    ) -> object:
        assert query["namespace_targets"] == [
            {"namespace": "eric/claude-code/episodic", "plane": "episodic"},
            {"namespace": "eric/claude-code/curated", "plane": "curated"},
        ]
        return Ok(
            value=RetrievalEnvelope(
                results=[
                    RetrievalResult(
                        object_id="ep-1",
                        namespace="eric/claude-code/episodic",
                        plane="episodic",
                        snippet="ep content",
                        score=0.9,
                        score_components={
                            "relevance": 0.9,
                            "recency": 0.1,
                            "importance": 0.2,
                            "provenance": 0.3,
                            "reinforcement": 0.4,
                        },
                        lineage={},
                        state="matured",
                        importance=3,
                    ),
                    RetrievalResult(
                        object_id="cur-1",
                        namespace="eric/claude-code/curated",
                        plane="curated",
                        snippet="cur content",
                        score=0.8,
                        score_components={
                            "relevance": 0.8,
                            "recency": 0.1,
                            "importance": 0.5,
                            "provenance": 0.6,
                            "reinforcement": 0.0,
                        },
                        lineage={},
                        state="promoted",
                        importance=5,
                    ),
                ],
                warnings=(),
            )
        )

    monkeypatch.setattr(writes_retrieve_stream, "run_orchestration_retrieve", mock_retrieve)

    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": "eric/claude-code",
            "planes": ["episodic", "curated"],
            "query_text": "fanout",
            "mode": "fast",
        },
    )
    assert r.status_code == 200

    lines = [line for line in r.text.split("\n") if line]
    assert len(lines) == 2

    row1 = json.loads(lines[0])
    row2 = json.loads(lines[1])

    assert row1["object_id"] == "ep-1"
    assert row1["plane"] == "episodic"

    assert row2["object_id"] == "cur-1"
    assert row2["plane"] == "curated"
