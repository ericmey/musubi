from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from musubi.api.dependencies import get_qdrant_client, get_thoughts_plane
from musubi.embedding.base import Embedder
from musubi.planes.thoughts import ThoughtsPlane
from musubi.store.specs import DENSE_VECTOR_NAME


class DummyEmbedder(Embedder):
    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        # deterministic non-zero, different from FakeEmbedder
        # Single 1.0 to avoid complex normalization math
        return [[1.0] + [0.0] * 1023 for _ in texts]

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
    # DummyEmbedder returns 0.1, FakeEmbedder returns gauss(0,1) normalized or zeros.
    assert vector[DENSE_VECTOR_NAME][0] == 1.0


def test_thought_read_uses_configured_plane(
    client: TestClient, app_factory: FastAPI, api_settings: Any
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["eric/ns/thought:w"])

    qdrant = app_factory.dependency_overrides[get_qdrant_client]()
    spy_plane = ThoughtsPlane(client=qdrant, embedder=DummyEmbedder())
    spy_plane.read = AsyncMock(wraps=spy_plane.read)  # type: ignore[method-assign]

    app_factory.dependency_overrides[get_thoughts_plane] = lambda: spy_plane

    r = client.post(
        "/v1/thoughts/read",
        headers={"Authorization": f"Bearer {token}"},
        json={"namespace": "eric/ns/thought", "ids": ["invalid-id"], "reader": "eric/other"},
    )
    assert r.status_code == 200

    spy_plane.read.assert_called_once_with(
        namespace="eric/ns/thought", object_id="invalid-id", reader="eric/other"
    )


def test_production_router_has_no_fake_embedder() -> None:
    import pathlib

    src_file = pathlib.Path("src/musubi/api/routers/writes_thoughts.py").read_text()
    assert "FakeEmbedder" not in src_file


def test_missing_dependency_fails_loud(
    client: TestClient, app_factory: FastAPI, api_settings: Any
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["eric/ns/thought:w"])

    # Remove the dependency from app
    app_factory.dependency_overrides[get_thoughts_plane] = lambda: Exception(
        "loud failure injected"
    )

    # It should raise HTTP 500 when it hits the overridden dependency Exception

    try:
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
        assert r.status_code == 500
    except Exception as exc:
        assert "loud failure injected" in str(exc)
