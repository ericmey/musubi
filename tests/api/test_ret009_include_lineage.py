"""RET-009 — public retrieval must forward include_lineage.

The public ``POST /v1/retrieve`` model currently lacks ``include_lineage``;
Pydantic's default ``extra='ignore'`` silently drops the field, and the router
never forwards it. This slice adds the field with default ``True``, forwards the
caller's exact value through the existing canonical orchestration seam, and proves
the contract at the HTTP/OpenAPI boundary.

Tests first, zero source in this commit. All 7 reds must fail for their named
missing behavior only; the implementation will satisfy them.
"""

from __future__ import annotations

from typing import Any

import pytest

from musubi.settings import Settings
from musubi.types.common import Ok


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


# Module-level capture list. Each test appends the forwarded query dict here.
_CAPTURED: list[dict[str, Any]] = []


def _make_app(monkeypatch: pytest.MonkeyPatch, api_settings: Settings) -> Any:
    """Build a TestClient with the orchestration seam captured to module-level list."""
    from fastapi.testclient import TestClient

    from musubi.api.app import create_app
    from musubi.api.dependencies import (
        get_embedder,
        get_qdrant_client,
        get_reranker,
        get_settings_dep,
    )
    from musubi.auth.tokens import AuthContext

    def mock_auth(*args: Any, **kwargs: Any) -> Any:
        return Ok(
            value=AuthContext(
                subject="test",
                scopes=("**:rw",),
                presence="test",
                issuer="test",
                audience="test",
                token_id="t",
            )
        )

    async def mock_run_orchestration(*args: Any, **kwargs: Any) -> Any:
        # args: (client, embedder, reranker, query)
        query = kwargs.get("query")
        if isinstance(query, dict):
            _CAPTURED.append(query)

        class MockOrchResult:
            def __init__(self) -> None:
                self.results: list[Any] = []
                self.warnings: list[Any] = []

            def __iter__(self) -> Any:
                return iter(self.results)

        return Ok(value=MockOrchResult())

    monkeypatch.setattr("musubi.api.routers.retrieve.authenticate_request", mock_auth)
    monkeypatch.setattr(
        "musubi.api.routers.retrieve.run_orchestration_retrieve", mock_run_orchestration
    )

    app = create_app(settings=api_settings)
    app.dependency_overrides[get_settings_dep] = lambda: api_settings
    app.dependency_overrides[get_qdrant_client] = lambda: None
    app.dependency_overrides[get_embedder] = lambda: None
    app.dependency_overrides[get_reranker] = lambda: None

    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_capture() -> None:
    """Clear the module-level capture list before each test."""
    _CAPTURED.clear()


def test_omitted_include_lineage_forwards_true(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """Omitting include_lineage must forward the default ``True`` to orchestration."""
    client = _make_app(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": "nyla/test/episodic", "query_text": "x", "mode": "fast", "limit": 5},
    )
    assert response.status_code == 200, response.text
    if not _CAPTURED:
        raise DefectStillPresent("orchestration was not called")
    q = _CAPTURED[-1]
    if "include_lineage" not in q:
        raise DefectStillPresent(
            "include_lineage missing from forwarded query dict (Pydantic dropped the unknown key)"
        )
    if q["include_lineage"] is not True:
        raise DefectStillPresent(
            f"omitted include_lineage must forward True, got {q['include_lineage']!r}"
        )


def test_explicit_include_lineage_false_forwards_false(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """Explicit ``include_lineage: false`` must reach orchestration as ``False``."""
    client = _make_app(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "nyla/test/episodic",
            "query_text": "x",
            "mode": "fast",
            "limit": 5,
            "include_lineage": False,
        },
    )
    assert response.status_code == 200, response.text
    if not _CAPTURED or _CAPTURED[-1].get("include_lineage") is not False:
        raise DefectStillPresent(
            f"explicit include_lineage=False must forward False, got "
            f"{(_CAPTURED[-1].get('include_lineage', '<missing>') if _CAPTURED else '<not-called>')!r}"
        )


def test_explicit_include_lineage_true_forwards_true(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """Explicit ``include_lineage: true`` must reach orchestration as ``True``."""
    client = _make_app(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "nyla/test/episodic",
            "query_text": "x",
            "mode": "fast",
            "limit": 5,
            "include_lineage": True,
        },
    )
    assert response.status_code == 200, response.text
    if not _CAPTURED or _CAPTURED[-1].get("include_lineage") is not True:
        raise DefectStillPresent(
            f"explicit include_lineage=True must forward True, got "
            f"{(_CAPTURED[-1].get('include_lineage', '<missing>') if _CAPTURED else '<not-called>')!r}"
        )


def test_concrete_namespace_preserves_include_lineage(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """3-segment concrete namespace must preserve the include_lineage value."""
    client = _make_app(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "nyla/test/episodic",
            "query_text": "x",
            "mode": "fast",
            "limit": 5,
            "include_lineage": False,
        },
    )
    assert response.status_code == 200, response.text
    if not _CAPTURED or _CAPTURED[-1].get("include_lineage") is not False:
        raise DefectStillPresent("concrete namespace: include_lineage=False not forwarded")


def test_fanout_namespace_preserves_include_lineage(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """2-segment fanout namespace must preserve the include_lineage value."""
    client = _make_app(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "nyla/test",
            "query_text": "x",
            "mode": "fast",
            "limit": 5,
            "planes": ["episodic", "curated"],
            "include_lineage": False,
        },
    )
    assert response.status_code == 200, response.text
    if not _CAPTURED or _CAPTURED[-1].get("include_lineage") is not False:
        raise DefectStillPresent("fanout namespace: include_lineage=False not forwarded")


def test_non_boolean_include_lineage_rejected_at_wire(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """Non-boolean include_lineage must be rejected at the wire boundary (422)."""
    client = _make_app(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "nyla/test/episodic",
            "query_text": "x",
            "mode": "fast",
            "limit": 5,
            "include_lineage": "not-a-bool",
        },
    )
    if response.status_code != 422:
        raise DefectStillPresent(
            f"non-boolean include_lineage must yield 422, got {response.status_code}"
        )
    if _CAPTURED:
        raise DefectStillPresent("orchestration must not be called when the body is invalid")


def test_openapi_exposes_include_lineage_with_default_true(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """The generated OpenAPI must expose include_lineage with default True."""
    client = _make_app(monkeypatch, api_settings)
    response = client.get("/v1/openapi.json")
    assert response.status_code == 200, response.text
    schema = response.json()
    components = schema.get("components", {}).get("schemas", {})
    retrieve_query = components.get("RetrieveQuery")
    if retrieve_query is None:
        raise DefectStillPresent("RetrieveQuery schema missing from OpenAPI components")
    props = retrieve_query.get("properties", {})
    if "include_lineage" not in props:
        raise DefectStillPresent("include_lineage missing from RetrieveQuery OpenAPI schema")
    field = props["include_lineage"]
    if field.get("type") != "boolean":
        raise DefectStillPresent(
            f"include_lineage must be a boolean in OpenAPI, got {field.get('type')!r}"
        )
    if field.get("default") is not True:
        raise DefectStillPresent(
            f"include_lineage must default to True in OpenAPI, got {field.get('default')!r}"
        )
