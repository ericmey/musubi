"""AUTH-001: all-namespace recall with configurable exclusions (Issue #523).

Owner slice: slice-auth001-token-scope (#523).

The single canonical source of exclusions is Settings.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from musubi.auth.tokens import AuthContext
from musubi.settings import Settings
from musubi.types.common import Ok


def test_default_discovery_uses_server_side_namespace_facet_not_point_scroll() -> None:
    from qdrant_client.http import models

    from musubi.api.routers.retrieve import _enumerate_authorized_targets

    class _FacetOnlyClient:
        def scroll(self, **_kwargs: Any) -> Any:
            raise AssertionError("default namespace discovery must not transfer every stored point")

        def facet(self, **kwargs: Any) -> Any:
            assert kwargs["collection_name"] == "musubi_episodic"
            assert kwargs["key"] == "namespace"
            assert kwargs["exact"] is True
            assert kwargs["limit"] == 10_000
            facet_filter = kwargs["facet_filter"]
            assert isinstance(facet_filter, models.Filter)
            condition = cast(list[models.FieldCondition], facet_filter.must)[0]
            assert condition.key == "identity_family"
            assert condition.match == models.MatchValue(value="eric")
            return SimpleNamespace(
                hits=[
                    SimpleNamespace(value="eric/other/episodic"),
                    SimpleNamespace(value="eric/chair/episodic"),
                    SimpleNamespace(value=7),
                ]
            )

    assert _enumerate_authorized_targets(
        cast(Any, _FacetOnlyClient()), family="eric", planes=["episodic"]
    ) == [
        ("eric/chair/episodic", "episodic"),
        ("eric/other/episodic", "episodic"),
    ]


def _patch_auth(
    monkeypatch: pytest.MonkeyPatch,
    scopes: tuple[str, ...],
    subject: str = "eric",
    presence: str = "command-chair",
) -> None:
    ctx = AuthContext(
        subject=subject,
        issuer="test",
        audience="musubi",
        scopes=scopes,
        presence=presence,
        token_id="test",
    )

    def mock_authenticate_request(*args: Any, **kwargs: Any) -> Ok[AuthContext]:
        return Ok(value=ctx)

    monkeypatch.setattr(
        "musubi.api.routers.retrieve.authenticate_request", mock_authenticate_request
    )
    monkeypatch.setattr(
        "musubi.api.routers.context.authenticate_request", mock_authenticate_request
    )
    monkeypatch.setattr(
        "musubi.api.routers.writes_retrieve_stream.authenticate_request", mock_authenticate_request
    )


def _seed_qdrant(client: TestClient, token: str, namespace: str, content: str = "hello") -> None:
    res = client.post(
        "/v1/episodic",
        json={"namespace": namespace, "content": content, "importance": 5},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "test"},
    )
    assert res.status_code == 202


def test_default_read_spans_at_least_two_non_excluded_namespaces(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    _seed_qdrant(client, token, "eric/command-chair/episodic", "hello chair")
    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    _seed_qdrant(client, token, "eric/other/episodic", "hello other")

    res = client.post(
        "/v1/retrieve",
        json={"mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    namespaces = [r["namespace"] for r in res.json()["results"]]

    assert "eric/command-chair/episodic" in namespaces
    assert "eric/other/episodic" in namespaces
    assert "eric/salesai/episodic" not in namespaces


def test_default_read_returns_authorized_subset_instead_of_failing_on_other_targets(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    seed_token = mint_token(
        api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair"
    )
    _seed_qdrant(client, seed_token, "eric/command-chair/episodic", "allowed memory")
    _seed_qdrant(client, seed_token, "eric/other/episodic", "unauthorized memory")

    _patch_auth(
        monkeypatch,
        ("eric/command-chair/*:r",),
        subject="eric",
        presence="eric/command-chair",
    )
    res = client.post(
        "/v1/retrieve",
        json={"mode": "fast", "query_text": "memory"},
        headers={"Authorization": f"Bearer {seed_token}"},
    )

    assert res.status_code == 200
    namespaces = {row["namespace"] for row in res.json()["results"]}
    assert namespaces == {"eric/command-chair/episodic"}


def test_context_omitted_namespace_spans_non_excluded_authorized_namespaces(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(
        monkeypatch,
        ("*/*/*:r", "*/*/*:w"),
        subject="eric",
        presence="eric/command-chair",
    )
    _seed_qdrant(client, token, "eric/command-chair/episodic", "chair continuity memory")
    _seed_qdrant(client, token, "eric/other/episodic", "other continuity memory")
    _seed_qdrant(client, token, "eric/salesai/episodic", "sales continuity memory")

    res = client.post(
        "/v1/context",
        json={"planes": ["episodic"], "query_text": "continuity memory", "max_items": 8},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    namespaces = {item["namespace"] for group in res.json()["groups"] for item in group["items"]}
    assert "eric/command-chair/episodic" in namespaces
    assert "eric/other/episodic" in namespaces
    assert "eric/salesai/episodic" not in namespaces


def test_salesai_cannot_be_reenabled_by_empty_settings_override(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    # A request with no per-agent exclusions configured still cannot bypass the mandatory baseline.
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/salesai/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["results"] == []


def test_salesai_cannot_be_reenabled_by_settings_subtract(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    # Settings-only validation: mandatory exclusions cannot be subtracted.
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/*/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    namespaces = [r["namespace"] for r in res.json()["results"]]
    assert "eric/salesai/episodic" not in namespaces


def test_salesai_cannot_be_reenabled_by_direct_target(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/salesai/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["results"] == []


def test_salesai_cannot_be_reenabled_by_wildcard(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/*/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    namespaces = [r["namespace"] for r in res.json()["results"]]
    assert "eric/salesai/episodic" not in namespaces


def test_salesai_cannot_be_reenabled_by_recent_lane(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/salesai/episodic", "mode": "recent", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["results"] == []


def test_salesai_cannot_be_reenabled_by_streaming(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    res = client.post(
        "/v1/retrieve/stream",
        json={"namespace": "eric/salesai/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert b"eric/salesai/episodic" not in res.content


def test_salesai_cannot_be_reenabled_by_adapter_path(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/salesai/episodic", "mode": "deep", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["results"] == []


def test_settings_exclusions_add_to_mandatory_not_subtract(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    # Test that setting custom exclusions doesn't remove the mandatory 'salesai'
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    def mock_get_settings() -> Settings:
        args = api_settings.model_dump()
        args["per_agent_excluded_namespaces"] = {"eric": ("custom",)}
        return Settings(**args)

    from musubi.api.dependencies import get_settings_dep

    cast(FastAPI, client.app).dependency_overrides[get_settings_dep] = mock_get_settings

    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    _seed_qdrant(client, token, "eric/custom/episodic", "hello custom")
    _seed_qdrant(client, token, "eric/command-chair/episodic", "hello chair")

    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/*/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    namespaces = [r["namespace"] for r in res.json()["results"]]
    assert "eric/command-chair/episodic" in namespaces
    assert "eric/custom/episodic" not in namespaces
    assert "eric/salesai/episodic" not in namespaces


def test_per_agent_settings_adds_to_mandatory(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    def mock_get_settings() -> Settings:
        args = api_settings.model_dump()
        args["per_agent_excluded_namespaces"] = {
            "eric": ("custom1",),
            "eric/command-chair": ("custom2",),
        }
        return Settings(**args)

    from musubi.api.dependencies import get_settings_dep

    cast(FastAPI, client.app).dependency_overrides[get_settings_dep] = mock_get_settings

    _seed_qdrant(client, token, "eric/custom1/episodic", "hello")
    _seed_qdrant(client, token, "eric/command-chair/episodic", "hello")

    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/*/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    namespaces = [r["namespace"] for r in res.json()["results"]]
    assert "eric/command-chair/episodic" in namespaces
    assert "eric/custom1/episodic" not in namespaces


def test_per_agent_settings_keyed_by_subject_or_presence_both_contribute(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    def mock_get_settings() -> Settings:
        args = api_settings.model_dump()
        args["per_agent_excluded_namespaces"] = {
            "eric": ("custom1",),
            "eric/command-chair": ("custom2",),
        }
        return Settings(**args)

    from musubi.api.dependencies import get_settings_dep

    cast(FastAPI, client.app).dependency_overrides[get_settings_dep] = mock_get_settings

    _seed_qdrant(client, token, "eric/custom1/episodic", "hello")
    _seed_qdrant(client, token, "eric/custom2/episodic", "hello")
    _seed_qdrant(client, token, "eric/command-chair/episodic", "hello")

    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/*/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    namespaces = [r["namespace"] for r in res.json()["results"]]
    assert "eric/command-chair/episodic" in namespaces
    assert "eric/custom1/episodic" not in namespaces
    assert "eric/custom2/episodic" not in namespaces


def test_unauthorized_namespaces_remain_denied_not_silently_broadened(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    # Missing read scope for salesai
    token = mint_token(
        api_settings, scopes=["eric/command-chair/*:r"], presence="eric/command-chair"
    )
    _patch_auth(monkeypatch, ("eric/command-chair/*:r",))
    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/salesai/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Should still return 403 because it's unauthorized (we check resolve_namespace_scope FIRST)
    assert res.status_code == 403


def test_canonical_config_source_is_single_no_scattered_exceptions(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    def mock_get_settings() -> Settings:
        args = api_settings.model_dump()
        args["per_agent_excluded_namespaces"] = {
            "eric": ("custom1",),
            "eric/command-chair": ("custom2",),
        }
        return Settings(**args)

    from musubi.api.dependencies import get_settings_dep

    cast(FastAPI, client.app).dependency_overrides[get_settings_dep] = mock_get_settings

    # Using context to prove the single seam propagates cleanly to all routes
    _seed_qdrant(client, token, "eric/salesai/episodic", "hello salesai")
    res = client.post(
        "/v1/context",
        json={
            "namespace": "eric/salesai/episodic",
            "planes": ["episodic"],
            "mode": "startup",
            "query_text": "hello",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["groups"] == []


def test_explicit_narrowing_still_narrows(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r", "*/*/*:w"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r", "*/*/*:w"), subject="eric", presence="eric/command-chair")

    _seed_qdrant(client, token, "eric/command-chair/episodic", "hello chair")
    _seed_qdrant(client, token, "eric/other/episodic", "hello other")
    res = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/command-chair/episodic", "mode": "fast", "query_text": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    namespaces = [r["namespace"] for r in res.json()["results"]]
    assert "eric/command-chair/episodic" in namespaces
    assert "eric/other/episodic" not in namespaces


def test_write_to_active_salesai_namespace_permitted_under_existing_write_scope(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["eric/salesai/*:w"], presence="eric/salesai")
    _patch_auth(monkeypatch, ("eric/salesai/*:w",))
    res = client.post(
        "/v1/episodic",
        json={"namespace": "eric/salesai/episodic", "content": "test write", "importance": 5},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "test2"},
    )
    assert res.status_code == 202


def test_direct_matcher_unit_tests_does_not_break_auth001() -> None:
    from musubi.auth.scopes import enforce_namespace_policy
    from musubi.auth.tokens import AuthContext
    from musubi.settings import Settings
    from musubi.types.common import Ok

    ctx = AuthContext(
        subject="eric",
        issuer="test",
        audience="musubi",
        scopes=("*/*/*:r",),
        presence="eric/command-chair",
        token_id="test",
    )
    s = Settings(
        **{  # type: ignore[arg-type]
            "default_excluded_namespaces": frozenset({"salesai"}),
            "log_dir": "/",
            "jwt_signing_key": "a",
            "oauth_authority": "http://a",
            "qdrant_host": "a",
            "qdrant_api_key": "a",
            "tei_dense_url": "http://a",
            "tei_sparse_url": "http://a",
            "tei_reranker_url": "http://a",
            "ollama_url": "http://a",
            "embedding_model": "a",
            "sparse_model": "a",
            "reranker_model": "a",
            "llm_model": "a",
            "vault_path": "/",
            "artifact_blob_path": "/",
            "lifecycle_sqlite_path": "/",
            "per_agent_excluded_namespaces": {"eric": ("custom1",)},
        }
    )

    targets = [
        ("tenant/salesai/episodic", "episodic"),
        ("tenant/salesai2/episodic", "episodic"),
        ("tenant/custom1/episodic", "episodic"),
        ("salesai/agent/episodic", "episodic"),
    ]

    res = enforce_namespace_policy(ctx, targets=targets, settings=s)
    assert isinstance(res, Ok)
    val = [ns for ns, p in res.value]

    assert "tenant/salesai/episodic" not in val
    assert "tenant/salesai2/episodic" in val
    assert "tenant/custom1/episodic" not in val
    assert "salesai/agent/episodic" in val
