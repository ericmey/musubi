"""Shared fixtures for the API read-side test suite.

Spins up an in-memory Qdrant + bootstrapped collections + the four planes
the read endpoints depend on (episodic, curated, concept, artifact),
plus a token-minting helper that signs HS256 against a test
:class:`Settings` instance. The FastAPI app is built via ``create_app``
with explicit dependency overrides so no real Qdrant / Ollama / TEI is
ever contacted.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import AnyHttpUrl, SecretStr

from musubi.api.app import create_app
from musubi.api.dependencies import (
    get_artifact_plane,
    get_concept_plane,
    get_curated_plane,
    get_episodic_plane,
    get_qdrant_client,
    get_settings_dep,
)
from musubi.embedding import FakeEmbedder
from musubi.planes.artifact import ArtifactPlane
from musubi.planes.concept import ConceptPlane
from musubi.planes.curated import CuratedPlane
from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.store import bootstrap

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient


_TEST_ISSUER = "https://auth.example.test"


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    """Settings instance for the API test suite — HS256 signing key, dummy
    URLs for every external. Enough that ``validate_token`` accepts our
    minted tokens and the FastAPI app boots."""
    return Settings.model_validate(
        {
            "qdrant_host": "qdrant",
            "qdrant_api_key": SecretStr("test-qdrant-key"),
            "tei_dense_url": AnyHttpUrl("http://tei-dense"),
            "tei_sparse_url": AnyHttpUrl("http://tei-sparse"),
            "tei_reranker_url": AnyHttpUrl("http://tei-reranker"),
            "ollama_url": AnyHttpUrl("http://ollama:11434"),
            "embedding_model": "BAAI/bge-m3",
            "sparse_model": "naver/splade-v3",
            "reranker_model": "BAAI/bge-reranker-v2-m3",
            "llm_model": "qwen2.5:7b-instruct-q4_K_M",
            "vault_path": tmp_path / "vault",
            "artifact_blob_path": tmp_path / "artifacts",
            "lifecycle_sqlite_path": tmp_path / "lifecycle.sqlite",
            "log_dir": tmp_path / "logs",
            "jwt_signing_key": SecretStr("a-very-long-test-signing-key-for-hs256-tokens-32+bytes"),
            "oauth_authority": AnyHttpUrl(_TEST_ISSUER),
        }
    )


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def episodic(qdrant: QdrantClient) -> EpisodicPlane:
    return EpisodicPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def curated(qdrant: QdrantClient) -> CuratedPlane:
    return CuratedPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def concept(qdrant: QdrantClient) -> ConceptPlane:
    return ConceptPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def artifact(qdrant: QdrantClient) -> ArtifactPlane:
    return ArtifactPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def app_factory(
    api_settings: Settings,
    qdrant: QdrantClient,
    episodic: EpisodicPlane,
    curated: CuratedPlane,
    concept: ConceptPlane,
    artifact: ArtifactPlane,
) -> object:
    """Returns the FastAPI app with all DI overrides wired to the
    in-memory test instances."""
    app = create_app(settings=api_settings)
    app.dependency_overrides[get_settings_dep] = lambda: api_settings
    app.dependency_overrides[get_qdrant_client] = lambda: qdrant
    app.dependency_overrides[get_episodic_plane] = lambda: episodic
    app.dependency_overrides[get_curated_plane] = lambda: curated
    app.dependency_overrides[get_concept_plane] = lambda: concept
    app.dependency_overrides[get_artifact_plane] = lambda: artifact
    return app


@pytest.fixture
def client(app_factory: object) -> Iterator[TestClient]:
    with TestClient(app_factory) as c:  # type: ignore[arg-type]
        yield c


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def mint_token(
    settings: Settings,
    *,
    scopes: list[str] | None = None,
    presence: str = "eric/claude-code",
    expires_delta: timedelta = timedelta(hours=1),
) -> str:
    """Mint an HS256 JWT against ``settings.jwt_signing_key``."""
    now = datetime.now(UTC)
    payload = {
        "iss": _TEST_ISSUER,
        "sub": "eric-claude-code",
        "aud": "musubi",
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": "test-token",
        "scope": " ".join(scopes or ["eric/claude-code/episodic:r"]),
        "presence": presence,
    }
    return jwt.encode(
        payload,
        settings.jwt_signing_key.get_secret_value(),
        algorithm="HS256",
    )


@pytest.fixture
def valid_token(api_settings: Settings) -> str:
    return mint_token(
        api_settings,
        scopes=[
            "eric/claude-code/episodic:r",
            "eric/claude-code/curated:r",
            "eric/claude-code/concept:r",
            "eric/claude-code/artifact:r",
            "eric/claude-code/thought:r",
        ],
    )


@pytest.fixture
def operator_token(api_settings: Settings) -> str:
    return mint_token(api_settings, scopes=["operator"])


@pytest.fixture
def out_of_scope_token(api_settings: Settings) -> str:
    """Valid token but for a different namespace than test calls use."""
    return mint_token(
        api_settings,
        scopes=["other-tenant/other-presence/episodic:r"],
        presence="other-tenant/other-presence",
    )


@pytest.fixture
def auth(valid_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {valid_token}"}
