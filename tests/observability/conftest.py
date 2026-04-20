"""Shared fixtures for the observability test suite.

Pattern mirrors tests/api/conftest.py — build a FastAPI app via
``create_app`` then attach ``dependency_overrides`` for the ops router's
deps so /ops/status + /ops/metrics resolve against in-memory test
fixtures without contacting real Qdrant / TEI / Ollama.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import AnyHttpUrl, SecretStr

from musubi.api.app import create_app
from musubi.api.dependencies import get_qdrant_client, get_settings_dep
from musubi.settings import Settings

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient


@pytest.fixture
def obs_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "qdrant_host": "qdrant",
            "qdrant_api_key": SecretStr("test-qdrant-key"),
            "tei_dense_url": AnyHttpUrl("http://tei-dense.test"),
            "tei_sparse_url": AnyHttpUrl("http://tei-sparse.test"),
            "tei_reranker_url": AnyHttpUrl("http://tei-reranker.test"),
            "ollama_url": AnyHttpUrl("http://ollama.test"),
            "embedding_model": "BAAI/bge-m3",
            "sparse_model": "naver/splade-v3",
            "reranker_model": "BAAI/bge-reranker-v2-m3",
            "llm_model": "qwen2.5:7b-instruct-q4_K_M",
            "vault_path": tmp_path / "vault",
            "artifact_blob_path": tmp_path / "artifacts",
            "lifecycle_sqlite_path": tmp_path / "lifecycle.sqlite",
            "log_dir": tmp_path / "logs",
            "jwt_signing_key": SecretStr("a-very-long-test-signing-key-for-hs256-tokens-32+bytes"),
            "oauth_authority": AnyHttpUrl("https://auth.example.test"),
            # Skip the production bootstrap — see tests/api/conftest.py.
            "musubi_skip_bootstrap": True,
        }
    )


@pytest.fixture
def obs_qdrant() -> Iterator[QdrantClient]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def obs_app(
    obs_settings: Settings,
    obs_qdrant: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """A TestClient over `create_app` with /ops/* deps wired to in-memory
    fixtures + every TEI/Ollama probe stubbed to a 200 OK transport."""
    from musubi.api.routers import ops as ops_router_mod
    from musubi.config import get_settings as _get_settings
    from musubi.observability import health as health_mod

    # /ops/status pulls Settings via musubi.config.get_settings(); patch
    # the cached accessor for the duration of the test.
    _get_settings.cache_clear()
    monkeypatch.setattr("musubi.config.get_settings", lambda: obs_settings)
    monkeypatch.setattr("musubi.api.routers.ops.get_settings", lambda: obs_settings)

    # Health probes — replace the import the router holds.
    real_check = health_mod.check_component_health

    def _faked_check(
        *,
        name: str,
        url: str,
        transport: object | None = None,
        timeout: float = 1.5,
    ) -> object:
        ok_transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"}))
        return real_check(name=name, url=url, transport=ok_transport, timeout=timeout)

    monkeypatch.setattr(ops_router_mod, "check_component_health", _faked_check)

    app = create_app(settings=obs_settings)
    app.dependency_overrides[get_qdrant_client] = lambda: obs_qdrant
    app.dependency_overrides[get_settings_dep] = lambda: obs_settings

    with TestClient(app) as c:
        yield c

    _get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_global_middleware_state() -> Iterator[None]:
    """Same isolation pattern the api/ suite uses — reset rate-limit +
    idempotency between tests so create_app() is fresh."""
    from musubi.api.idempotency import _GLOBAL_CACHE
    from musubi.api.rate_limit import _GLOBAL_LIMITER

    _GLOBAL_LIMITER.reset_for_test()
    _GLOBAL_CACHE._entries.clear()
    yield
    _GLOBAL_LIMITER.reset_for_test()
    _GLOBAL_CACHE._entries.clear()
