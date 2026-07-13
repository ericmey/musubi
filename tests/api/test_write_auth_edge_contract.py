"""Phase B — required reds for the body-derived authorization dependency EDGE (Option A).

Yua REQ 2026-07-13T00:27 + 00:30. Tests-first, before implementation. Locks the contract for
moving the capture routes' body-namespace authz out of the handler (`_check_body_scope`) into an
explicit `AuthorizedWrite` dependency edge, with the idempotency dependency `Depends`-ing on it.

Eligible body-derived captures (proven inventory): POST /v1/episodic, /v1/episodic/batch,
/v1/curated. Concept/query-derived mutations are a NAMED out-of-scope follow-up.

CONTROLS (green now, must stay green through the refactor):
  - no token -> 401; foreign namespace -> 403 (no plane mutation); malformed body -> 422 envelope;
    created_at operator guard still enforced (reads request.state.auth); OpenAPI requestBody
    material fields + requiredness stable; exactly ONE body parse per route (structural).
RED / xfail(strict) until the pipeline is wired:
  - every eligible route carries the idempotency dependency (mechanical inventory, no omission);
  - the handler is unreachable without the AuthorizedWrite/idempotency context (route graph).

    uv run pytest tests/api/test_write_auth_edge_contract.py -v
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.testclient import TestClient

from musubi.settings import Settings
from tests.api.conftest import mint_token

CAPTURE_PATHS = {"/v1/episodic", "/v1/episodic/batch", "/v1/curated"}
NS = "eric/claude-code"


def _post_capture_routes(app: FastAPI) -> list[APIRoute]:
    return [
        r
        for r in app.routes
        if isinstance(r, APIRoute) and r.path in CAPTURE_PATHS and "POST" in r.methods
    ]


def _all_subdep_names(dependant: Any) -> list[str]:
    names: list[str] = []
    for d in dependant.dependencies:
        call = getattr(d, "call", None)
        if call is not None:
            names.append(call.__name__)
        names += _all_subdep_names(d)
    return names


def _all_body_param_names(dependant: Any) -> list[str]:
    """Body params across the WHOLE dependency tree — FastAPI keeps a dependency's Body(...) in the
    sub-dependant, not the route's top-level body_params, so 'one parse' must count recursively."""
    names = [b.name for b in dependant.body_params]
    for d in dependant.dependencies:
        names += _all_body_param_names(d)
    return names


def _valid_body(path: str) -> dict[str, Any]:
    if path == "/v1/episodic":
        return {"namespace": f"{NS}/episodic", "content": "x", "tags": ["kind:episode"], "importance": 3}
    if path == "/v1/episodic/batch":
        return {"namespace": f"{NS}/episodic", "items": [{"content": "x"}]}
    return {  # /v1/curated
        "namespace": f"{NS}/curated",
        "title": "t",
        "content": "c",
        "vault_path": "Eric/x.md",
        "body_hash": "a" * 64,  # curated body_hash is a 64-char sha256 hex
    }


def _tok(api_settings: Settings, subject_ns: str) -> str:
    return mint_token(
        api_settings,
        scopes=[f"{subject_ns}/episodic:rw", f"{subject_ns}/curated:rw"],
        presence=subject_ns,
    )


# --------------------------------------------------------------------------- #
# structural — exactly one body parse (control, must stay green)
# --------------------------------------------------------------------------- #


def test_each_capture_route_parses_body_exactly_once(app_factory: FastAPI) -> None:
    routes = _post_capture_routes(app_factory)
    assert len(routes) == 3, f"expected exactly 3 body-derived captures, got {[r.path for r in routes]}"
    for r in routes:
        names = _all_body_param_names(r.dependant)  # across the whole dependency tree
        assert len(names) == 1, f"{r.path}: must parse the body exactly once, got body_params={names}"


# --------------------------------------------------------------------------- #
# behaviour controls — must hold before AND after the refactor
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", sorted(CAPTURE_PATHS))
def test_no_token_is_401_no_mutation(client: TestClient, path: str) -> None:
    r = client.post(path, json=_valid_body(path))
    assert r.status_code == 401, f"{path}: unauthenticated capture must be 401, got {r.status_code}"


@pytest.mark.parametrize("path", sorted(CAPTURE_PATHS))
def test_foreign_namespace_is_403_no_mutation(client: TestClient, api_settings: Settings, path: str) -> None:
    token = _tok(api_settings, "mallory/evil")  # authorized ONLY on mallory/evil
    r = client.post(path, json=_valid_body(path), headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, f"{path}: cross-tenant capture must be 403, got {r.status_code}"


@pytest.mark.parametrize("path", sorted(CAPTURE_PATHS))
def test_malformed_body_is_422_envelope(client: TestClient, api_settings: Settings, path: str) -> None:
    token = _tok(api_settings, NS)
    r = client.post(path, json={"unexpected": 1}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422, f"{path}: malformed body must be 422, got {r.status_code}"
    env = r.json()
    assert env.get("error", {}).get("code") == "BAD_REQUEST", (
        f"422 must carry the standard nested error envelope, got {env}"
    )


def test_created_at_operator_guard_still_enforced(client: TestClient, api_settings: Settings) -> None:
    """created_at override is operator-only; a non-operator token must be 403 — proving the guard
    still sees request.state.auth after auth moves into the dependency."""
    token = _tok(api_settings, NS)
    body = {**_valid_body("/v1/episodic"), "created_at": "2020-01-01T00:00:00Z"}
    r = client.post("/v1/episodic", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, f"non-operator created_at override must be 403, got {r.status_code}"


def test_openapi_requestbody_material_fields_stable(app_factory: FastAPI) -> None:
    """The requestBody schema + requiredness for all 3 routes must stay structurally equivalent as
    the body moves into a dependency (FastAPI flattens a dependency Body(...) into the operation)."""
    spec = app_factory.openapi()
    expected_required = {
        "/v1/episodic": {"namespace", "content"},
        "/v1/episodic/batch": {"namespace", "items"},
        "/v1/curated": {"namespace", "title", "content", "vault_path", "body_hash"},
    }
    for path, must_have in expected_required.items():
        rb = spec["paths"][path]["post"].get("requestBody")
        assert rb is not None and rb.get("required") is True, f"{path}: requestBody must be required"
        ref = rb["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
        model = spec["components"]["schemas"][ref]
        assert must_have <= set(model.get("required", [])), (
            f"{path}: material required fields changed: {model.get('required')} missing {must_have}"
        )


# --------------------------------------------------------------------------- #
# RED — pending the pipeline (xfail strict; flip when wired)
# --------------------------------------------------------------------------- #


def test_every_eligible_capture_route_carries_idempotency_dependency(app_factory: FastAPI) -> None:
    routes = _post_capture_routes(app_factory)
    assert len(routes) == 3
    for r in routes:
        names = _all_subdep_names(r.dependant)
        assert any("idempotenc" in n.lower() for n in names), (
            f"{r.path} omits the idempotency dependency (route-inventory): {names}"
        )


def test_handler_unreachable_without_authorized_write(app_factory: FastAPI) -> None:
    for r in _post_capture_routes(app_factory):
        names = _all_subdep_names(r.dependant)
        assert any(("authorized" in n.lower()) or ("write_auth" in n.lower()) for n in names), (
            f"{r.path}: handler reachable without the AuthorizedWrite edge: {names}"
        )
