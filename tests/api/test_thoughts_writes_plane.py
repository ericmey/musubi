from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from musubi.api.dependencies import get_qdrant_client, get_thoughts_plane
from musubi.embedding.base import Embedder
from musubi.planes.thoughts import ThoughtsPlane
from musubi.store.specs import DENSE_VECTOR_NAME


class DummyEmbedder(Embedder):
    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        # deterministic non-zero, different from FakeEmbedder
        # Embeds length-dependent vectors to discriminate different content directionally
        return [[1.0, float((sum(map(ord, t)) % 7) + 1)] + [0.0] * 1022 for t in texts]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return [{} for _ in texts]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return [1.0 for _ in candidates]


def test_thought_send_uses_configured_plane_and_embedder(
    client: TestClient, app_factory: FastAPI, api_settings: Any
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["eric/ns/thought:w"])

    qdrant = app_factory.dependency_overrides[get_qdrant_client]()
    spy_plane = ThoughtsPlane(client=qdrant, embedder=DummyEmbedder())
    spy_plane.send = AsyncMock(wraps=spy_plane.send)  # type: ignore[method-assign]

    app_factory.dependency_overrides[get_thoughts_plane] = lambda: spy_plane

    r = client.post(
        "/v1/thoughts/send",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "eric/ns/thought",
            "from_presence": "eric/me",
            "to_presence": "eric/other",
            "content": "hello world",
            "channel": "default",
            "importance": 5,
        },
    )
    assert r.status_code == 202

    spy_plane.send.assert_called_once()

    # Also verify that the vector stored is from DummyEmbedder, not FakeEmbedder
    object_id = r.json()["object_id"]
    from musubi.planes.thoughts.plane import _point_id

    point_id = _point_id(object_id)

    # Retrieve the raw point from Qdrant to check the vector
    points = qdrant.retrieve(collection_name="musubi_thought", ids=[point_id], with_vectors=True)
    assert len(points) == 1

    vector = points[0].vector
    assert DENSE_VECTOR_NAME in vector
    assert any(v != 0 for v in vector[DENSE_VECTOR_NAME])

    # Assert second distinct content produces distinct vector
    r2 = client.post(
        "/v1/thoughts/send",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "eric/ns/thought",
            "from_presence": "eric/me",
            "to_presence": "eric/other",
            "content": "different length content",
            "channel": "default",
            "importance": 5,
        },
    )
    object_id2 = r2.json()["object_id"]
    point_id2 = _point_id(object_id2)
    points2 = qdrant.retrieve(collection_name="musubi_thought", ids=[point_id2], with_vectors=True)
    vector2 = points2[0].vector

    assert any(v != 0 for v in vector2[DENSE_VECTOR_NAME])
    assert vector[DENSE_VECTOR_NAME] != vector2[DENSE_VECTOR_NAME]


def test_thought_read_uses_configured_plane(
    client: TestClient, app_factory: FastAPI, api_settings: Any
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["eric/ns/thought:w"])

    qdrant = app_factory.dependency_overrides[get_qdrant_client]()
    spy_plane = ThoughtsPlane(client=qdrant, embedder=DummyEmbedder())
    spy_plane.read = AsyncMock(wraps=spy_plane.read)  # type: ignore[method-assign]

    app_factory.dependency_overrides[get_thoughts_plane] = lambda: spy_plane

    # First ID raises LookupError, second succeeds
    async def mock_read(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("object_id") == "invalid-id":
            raise LookupError("Not found")
        # second call succeeds (does nothing)

    spy_plane.read.side_effect = mock_read

    r = client.post(
        "/v1/thoughts/read",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "eric/ns/thought",
            "ids": ["invalid-id", "valid-id"],
            "reader": "eric/other",
        },
    )
    assert r.status_code == 200

    # Assert both were called, meaning loop continued after the first exception
    assert spy_plane.read.call_count == 2

    # Count returned should be 1 (only the second succeeded)
    assert r.json()["count"] == 1


def test_production_router_has_no_fake_embedder() -> None:
    import pathlib

    src_file = pathlib.Path("src/musubi/api/routers/writes_thoughts.py").read_text()
    assert "FakeEmbedder" not in src_file


def test_missing_dependency_fails_loud(
    client: TestClient, app_factory: FastAPI, api_settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["eric/ns/thought:w"])

    def raise_dependency() -> None:
        raise RuntimeError("loud failure injected")

    app_factory.dependency_overrides[get_thoughts_plane] = raise_dependency

    from fastapi.testclient import TestClient as _TestClient

    err_client = _TestClient(app_factory, raise_server_exceptions=False)

    import logging

    with caplog.at_level(logging.ERROR, logger="musubi.api.app"):
        r = err_client.post(
            "/v1/thoughts/send",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "namespace": "eric/ns/thought",
                "from_presence": "eric/me",
                "to_presence": "eric/other",
                "content": "hello world",
                "channel": "default",
                "importance": 5,
            },
        )
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL"
    assert any("loud failure injected" in rec.exc_text for rec in caplog.records if rec.exc_text)
