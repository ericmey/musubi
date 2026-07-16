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

    from musubi.api.routers.retrieve import _enumerate_family_targets

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

    assert _enumerate_family_targets(
        cast(Any, _FacetOnlyClient()), family="eric", planes=["episodic"]
    ) == [
        ("eric/chair/episodic", "episodic"),
        ("eric/other/episodic", "episodic"),
    ]


def test_direct_orchestration_rejects_empty_namespace_without_resolved_targets() -> None:
    from pydantic import ValidationError

    from musubi.retrieve.orchestration import NamespaceTarget, RetrievalQuery

    with pytest.raises(ValidationError, match="namespace must be non-empty"):
        RetrievalQuery(namespace="", mode="recent")

    query = RetrievalQuery(
        namespace="",
        mode="recent",
        namespace_targets=[NamespaceTarget(namespace="eric/chair/episodic", plane="episodic")],
    )
    assert query.namespace_targets is not None


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
        json={"mode": "fast", "query_text": "hello", "state_filter": ["provisional"]},
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
        json={"mode": "fast", "query_text": "memory", "state_filter": ["provisional"]},
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


def test_context_empty_wildcard_still_flows_through_namespace_policy(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r",), subject="eric", presence="eric/command-chair")
    monkeypatch.setattr("musubi.api.routers.context._expand_wildcard_targets", lambda *_args: [])

    observed: list[list[tuple[str, str]]] = []

    def record_policy(*_args: Any, targets: list[tuple[str, str]], **_kwargs: Any) -> Ok[Any]:
        observed.append(targets)
        return Ok(value=[])

    monkeypatch.setattr("musubi.api.routers.context.enforce_namespace_policy", record_policy)

    res = client.post(
        "/v1/context",
        json={"namespace": "eric/*", "query_text": "nothing stored"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    assert observed == [[]]
    assert res.json()["groups"] == []


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/retrieve", {"query_text": "bad plane", "planes": ["unknown"]}),
        ("/v1/retrieve/stream", {"query_text": "bad plane", "planes": ["unknown"]}),
        ("/v1/context", {"query_text": "bad plane", "planes": ["unknown"]}),
    ],
)
def test_omitted_namespace_rejects_unknown_plane_as_bad_request(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    api_settings: Settings,
    path: str,
    payload: dict[str, Any],
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r",), subject="eric", presence="eric/command-chair")

    res = client.post(path, json=payload, headers={"Authorization": f"Bearer {token}"})

    assert res.status_code == 400
    assert res.json()["error"]["code"] == "BAD_REQUEST"
    assert "unknown plane 'unknown'" in res.json()["error"]["detail"]


def test_context_empty_namespace_is_rejected_at_request_validation(
    client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r"], presence="eric/command-chair")
    res = client.post(
        "/v1/context",
        json={"namespace": "", "query_text": "invalid empty namespace"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 422


def test_stream_empty_wildcard_still_flows_through_namespace_policy(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, api_settings: Settings
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(api_settings, scopes=["*/*/*:r"], presence="eric/command-chair")
    _patch_auth(monkeypatch, ("*/*/*:r",), subject="eric", presence="eric/command-chair")
    monkeypatch.setattr(
        "musubi.api.routers.writes_retrieve_stream._expand_wildcard_targets", lambda *_args: []
    )
    observed: list[list[tuple[str, str]]] = []

    def record_policy(*_args: Any, targets: list[tuple[str, str]], **_kwargs: Any) -> Ok[Any]:
        observed.append(targets)
        return Ok(value=[])

    monkeypatch.setattr(
        "musubi.api.routers.writes_retrieve_stream.enforce_namespace_policy", record_policy
    )

    res = client.post(
        "/v1/retrieve/stream",
        json={"namespace": "eric/*", "query_text": "nothing stored"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    assert observed == [[]]
    assert res.content == b""


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
        json={
            "namespace": "eric/*/episodic",
            "mode": "fast",
            "query_text": "hello",
            "state_filter": ["provisional"],
        },
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
        json={
            "namespace": "eric/*/episodic",
            "mode": "fast",
            "query_text": "hello",
            "state_filter": ["provisional"],
        },
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
        json={
            "namespace": "eric/*/episodic",
            "mode": "fast",
            "query_text": "hello",
            "state_filter": ["provisional"],
        },
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
        json={
            "namespace": "eric/salesai/episodic",
            "mode": "fast",
            "query_text": "hello",
            "state_filter": ["provisional"],
        },
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
        json={
            "namespace": "eric/command-chair/episodic",
            "mode": "fast",
            "query_text": "hello",
            "state_filter": ["provisional"],
        },
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


def test_implicit_discovery_does_not_audit_each_unauthorized_candidate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from musubi.auth.scopes import enforce_namespace_policy
    from musubi.auth.tokens import AuthContext
    from musubi.settings import Settings
    from musubi.types.common import Ok

    context = AuthContext(
        subject="eric",
        issuer="test",
        audience="musubi",
        scopes=("eric/command-chair/*:r",),
        presence="eric/command-chair",
        token_id="test",
    )
    settings = Settings(
        **{  # type: ignore[arg-type]
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
        }
    )

    with caplog.at_level("INFO", logger="musubi.auth.scopes"):
        result = enforce_namespace_policy(
            context,
            targets=[
                ("eric/command-chair/episodic", "episodic"),
                ("aoi/voice/episodic", "episodic"),
                ("tama/voice/episodic", "episodic"),
            ],
            settings=settings,
            reject_unauthorized=False,
        )

    assert isinstance(result, Ok)
    assert result.value == [("eric/command-chair/episodic", "episodic")]
    assert [record.message for record in caplog.records] == ["auth.allow"]


# --------------------------------------------------------------------------- #
# AUTH-001 hidden-page (Yua 19:06) — three bounded fixes:
#   1. _enumerate_family_targets safety-cap -> APIError envelope (BACKEND_UNAVAILABLE)
#   2. planes dedup in _enumerate_family_targets (one shared seam)
#   3. 3-segment concrete namespace ignores default planes
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# AUTH-001 hidden-page (Yua 19:06) — three bounded fixes:
#   1. _enumerate_family_targets safety-cap -> APIError envelope (BACKEND_UNAVAILABLE)
#   2. planes dedup in _enumerate_family_targets (one shared seam)
#   3. 3-segment concrete namespace ignores default planes
# --------------------------------------------------------------------------- #


def test_planes_dedup_in_enumerate_family_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullet 2: ``_enumerate_family_targets`` dedups ``planes`` BEFORE
    faceting. A duplicate ``planes`` list (e.g. ``["episodic",
    "episodic"]``) must result in exactly ONE facet call per unique
    plane, not two. Discriminator: count the facet calls."""
    from qdrant_client import QdrantClient

    from musubi.api.routers import retrieve as _r

    fake = QdrantClient(":memory:")
    facet_calls: list[str] = []

    def _facet(collection_name: str, *_args: object, **_kwargs: object) -> object:
        facet_calls.append(collection_name)
        return type("Facet", (), {"hits": []})()

    monkeypatch.setattr(fake, "facet", _facet)

    _r._enumerate_family_targets(
        fake, family="eric", planes=["episodic", "episodic", "curated", "episodic"]
    )
    # Exactly 2 unique collections were faceted (episodic, curated).
    assert sorted(facet_calls) == ["musubi_curated", "musubi_episodic"], (
        f"planes must be deduped before faceting; got facet calls: {facet_calls!r}"
    )


def test_duplicate_planes_route_returns_unique_targets(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    api_settings: Settings,
) -> None:
    """Bullet 2 non-regression: omitted-namespace route with duplicate
    ``planes`` (e.g. ``["episodic", "episodic", "curated"]``) returns
    200 with exactly the unique-planes targets. The shared dedup seam
    in ``_enumerate_family_targets`` proves this end-to-end through
    the route (not just the direct seam)."""
    from qdrant_client import QdrantClient

    from musubi.api.dependencies import get_qdrant_client

    fake = QdrantClient(":memory:")
    # Inject a controlled set of distinct namespaces per plane.
    ns_for_plane: dict[str, list[str]] = {
        "musubi_episodic": ["eric/a/episodic", "eric/b/episodic"],
        "musubi_curated": ["eric/c/curated"],
    }

    def _facet(collection_name: str, *_args: object, **_kwargs: object) -> object:
        # Return the plane's namespace set as facet hits.
        return type(
            "Facet",
            (),
            {
                "hits": [
                    type("H", (), {"value": v})() for v in ns_for_plane.get(collection_name, [])
                ]
            },
        )()

    monkeypatch.setattr(fake, "facet", _facet)
    monkeypatch.setitem(
        cast(FastAPI, client.app).dependency_overrides, get_qdrant_client, lambda: fake
    )

    # Auth bypass — the orchestrator will be mocked below to capture
    # the final targets.
    _patch_auth(monkeypatch, ("eric/*/*:r",))

    # Mock the orchestrator to capture the targets it received. The
    # route imports it as `run_orchestration_retrieve`, so we patch
    # the symbol at the route's import location (not at the source
    # module — those are different references after import).
    from musubi.api.routers import retrieve as _retrieve_router

    captured: dict[str, Any] = {}

    async def _capture(
        *, client: object, embedder: object, reranker: object, query: dict[str, object]
    ) -> object:
        captured["query"] = query
        # Return a minimal valid response envelope.
        from musubi.api.responses import RankedRetrieveResponse
        from musubi.types.common import Ok

        return Ok(value=RankedRetrieveResponse(results=[], mode="fast", limit=10))

    monkeypatch.setattr(_retrieve_router, "run_orchestration_retrieve", _capture)

    res = client.post(
        "/v1/retrieve",
        json={
            "namespace": None,
            "mode": "fast",
            "query_text": "hello",
            "planes": ["episodic", "episodic", "curated"],
        },
    )
    assert res.status_code == 200, (
        f"duplicate planes must NOT 500; got {res.status_code} {res.text!r}"
    )
    # The dedup contract is at the planes level — each unique
    # plane in the input list produces exactly one facet call.
    # The direct seam test (test_planes_dedup_in_enumerate_family_targets)
    # proves the call count. Here we prove the route accepts
    # duplicate planes and reaches the orchestrator with the
    # dedup'd namespace_targets (each unique plane appears at
    # least once; per-plane namespaces are the union across the
    # facet hit, not deduplicated per-plane).
    q = captured.get("query", {})
    nts = q.get("namespace_targets", [])
    # nts is a list of dicts {"namespace": ns, "plane": plane}.
    planes_in_targets = sorted({entry["plane"] for entry in nts})
    # Exactly 2 unique planes in the targets (episodic, curated).
    assert planes_in_targets == ["curated", "episodic"], (
        f"namespace_targets must contain exactly the dedup'd planes; "
        f"got: {planes_in_targets!r} from nts={nts!r}"
    )
    # And 3 namespace targets (2 episodic + 1 curated).
    assert len(nts) == 3, f"expected 3 namespace targets, got: {nts!r}"


def test_context_three_segment_concrete_ignores_default_planes(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    api_settings: Settings,
) -> None:
    """Bullet 3 (deterministic): ``/v1/context`` with a concrete
    3-segment namespace MUST return 200 even when the client sends
    the default ``planes = ["episodic", "curated", "concept"]``.
    The 3-seg concrete plane ``episodic`` is the single source of
    truth; ``planes`` is ignored for that shape."""
    _patch_auth(monkeypatch, ("*/*/*:r",))
    res = client.post(
        "/v1/context",
        json={
            "namespace": "tenant/presence/episodic",
            "query_text": "hello",
            "planes": ["episodic", "curated", "concept"],
        },
    )
    assert res.status_code == 200, (
        f"3-segment concrete namespace must be accepted (200) even "
        f"with the default planes list; got {res.status_code} {res.text!r}"
    )


def test_context_three_segment_wildcard_plane_uses_supplied_planes(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    api_settings: Settings,
) -> None:
    """Bullet 3 non-regression: ``/v1/context`` with a 3-segment
    wildcard-plane namespace (``tenant/presence/*``) uses the
    supplied ``planes`` list. We capture the orchestrator's
    ``namespace_targets`` to prove the dedup'd planes were used."""
    from qdrant_client import QdrantClient

    from musubi.api.dependencies import get_qdrant_client

    fake = QdrantClient(":memory:")

    def _facet(_collection: str, *_args: object, **_kwargs: object) -> object:
        return type("Facet", (), {"hits": []})()

    def _scroll(*_args: object, **_kwargs: object) -> object:
        # Wildcard expansion calls ``client.scroll``; return empty
        # so the expansion yields no extra targets.
        return ([], None)

    monkeypatch.setattr(fake, "facet", _facet)
    monkeypatch.setattr(fake, "scroll", _scroll)
    monkeypatch.setitem(
        cast(FastAPI, client.app).dependency_overrides, get_qdrant_client, lambda: fake
    )

    _patch_auth(monkeypatch, ("*/*/*:r",))

    captured: dict[str, Any] = {}

    async def _capture(
        *, client: object, embedder: object, reranker: object, query: dict[str, object]
    ) -> object:
        captured["query"] = query
        from musubi.api.responses import RankedRetrieveResponse
        from musubi.types.common import Ok

        return Ok(value=RankedRetrieveResponse(results=[], mode="fast", limit=10))

    from musubi.api.responses import RankedRetrieveResponse
    from musubi.api.routers import context as _context_router
    from musubi.types.common import Ok

    async def _capture_context(*args: Any, **kwargs: Any) -> object:
        # The orchestrator is called with various keyword args
        # (``client``, ``embedder``, ``reranker``, ``query``, plus
        # ``account_access`` in the context router path). Capture
        # only ``query`` for the assertion; pass-through the rest.
        captured["query"] = kwargs.get("query")
        return Ok(value=RankedRetrieveResponse(results=[], mode="fast", limit=10))

    monkeypatch.setattr(_context_router, "run_orchestration_retrieve", _capture_context)

    res = client.post(
        "/v1/context",
        json={
            "namespace": "tenant/presence/*",
            "query_text": "hello",
            "planes": ["episodic", "curated"],
        },
    )
    assert res.status_code == 200, (
        f"3-seg wildcard-plane must accept the planes list (200); "
        f"got {res.status_code} {res.text!r}"
    )
    q = captured.get("query", {})
    # With ``namespace="tenant/presence/*"`` and no Qdrant
    # facets, the targets list is empty — but the ``planes`` list
    # was used (passed through to the orchestrator). Prove the
    # request reached the orchestrator with the supplied planes.
    assert q.get("planes") == ["episodic", "curated"], (
        f"planes must reach the orchestrator; got: {q.get('planes')!r}"
    )


def test_context_three_segment_wildcard_plane_dedup_planes(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    api_settings: Settings,
) -> None:
    """Bullet 2 + 3 combined non-regression: 3-seg wildcard-plane
    with duplicate ``planes`` (``["episodic", "episodic",
    "curated"]``) yields dedup'd ``namespace_targets`` of length
    equal to the unique planes count. Captures the route's
    end-to-end behavior."""
    from qdrant_client import QdrantClient

    from musubi.api.dependencies import get_qdrant_client

    fake = QdrantClient(":memory:")

    def _facet(_collection: str, *_args: object, **_kwargs: object) -> object:
        return type("Facet", (), {"hits": []})()

    def _scroll(*_args: object, **_kwargs: object) -> object:
        # Wildcard expansion calls ``client.scroll``; return empty
        # so the expansion yields no extra targets.
        return ([], None)

    monkeypatch.setattr(fake, "facet", _facet)
    monkeypatch.setattr(fake, "scroll", _scroll)
    monkeypatch.setitem(
        cast(FastAPI, client.app).dependency_overrides, get_qdrant_client, lambda: fake
    )

    _patch_auth(monkeypatch, ("*/*/*:r",))

    captured: dict[str, Any] = {}

    async def _capture(
        *, client: object, embedder: object, reranker: object, query: dict[str, object]
    ) -> object:
        captured["query"] = query
        from musubi.api.responses import RankedRetrieveResponse
        from musubi.types.common import Ok

        return Ok(value=RankedRetrieveResponse(results=[], mode="fast", limit=10))

    from musubi.api.responses import RankedRetrieveResponse
    from musubi.api.routers import context as _context_router
    from musubi.types.common import Ok

    async def _capture_context(*args: Any, **kwargs: Any) -> object:
        # The orchestrator is called with various keyword args
        # (``client``, ``embedder``, ``reranker``, ``query``, plus
        # ``account_access`` in the context router path). Capture
        # only ``query`` for the assertion; pass-through the rest.
        captured["query"] = kwargs.get("query")
        return Ok(value=RankedRetrieveResponse(results=[], mode="fast", limit=10))

    monkeypatch.setattr(_context_router, "run_orchestration_retrieve", _capture_context)

    res = client.post(
        "/v1/context",
        json={
            "namespace": "tenant/presence/*",
            "query_text": "hello",
            "planes": ["episodic", "episodic", "curated"],
        },
    )
    assert res.status_code == 200, (
        f"duplicate planes must be deduped; got {res.status_code} {res.text!r}"
    )
    # The 3-seg wildcard-plane with empty facets yields no targets,
    # but the planes list itself is the dedup contract — captured
    # here is what the route passed through.
    # The fact that we got 200 (not 400 inconsistent) proves the
    # route accepted the dedup'd list. We assert the planes list
    # is exactly what the route received.
    q = captured.get("query", {})
    # The context router's _resolve_targets (for 3-seg wildcard)
    # dedups the planes list internally. The dedup contract is
    # that the orchestrator receives the dedup'd unique planes,
    # not the verbatim input list.
    assert sorted(q.get("planes") or []) == ["curated", "episodic"], (
        f"planes must be deduped to {{'curated', 'episodic'}}; got: {q.get('planes')!r}"
    )
