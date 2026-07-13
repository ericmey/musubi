"""Route/operation identity availability — WHERE the canonical endpoint identity exists.

Yua blocker 2 (2026-07-12T21:48): "Do not patch req9 into the current pre-auth middleware
using raw URL path. Prove how the canonical route-template/operation_id becomes available
behind parsed authz."

This spike proves, against a live Starlette/FastAPI app (same stack as Musubi), exactly where
the canonical route identity is and is not available — so the req-9 fix (idempotency identity
must include the endpoint) is placed correctly, in a DEPENDENCY, not the middleware.

Observed facts (all asserted below):

  - The pre-auth middleware (BaseHTTPMiddleware, runs BEFORE routing) sees
    `request.scope["route"] is None` and only `request.url.path` — the RAW, concrete path
    (`/v1/items/42`), with no template and no operation_id. Musubi's own code already knows
    this: `app.py:_bucket_for_path` says *"Routing hasn't happened yet at middleware time, so
    we match the URL path prefix directly instead of reading the route's operation_id."*

  - A route DEPENDENCY (runs after routing) sees `request.scope["route"]` as the `APIRoute`,
    exposing `.path_format` (the canonical template `/v1/items/{item_id}`) and `.operation_id`
    (`capture_episodic.bucket=capture`).

Why the raw path is the WRONG identity source: two requests to the SAME endpoint with
different path params (`/v1/items/1` vs `/v1/items/2`) have DIFFERENT `url.path` — so raw path
over-discriminates; and two DIFFERENT operations that share a URL prefix cannot be told apart
without the operation_id the middleware cannot see. The canonical identity is
`(path_format, operation_id)`, available only in a dependency.

Tests/docs only. No src. Synthetic app; no Musubi imports needed for the mechanic.

    uv run pytest tests/api/spikes/test_route_identity_availability.py -v
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute
from starlette.testclient import TestClient


def _build_app(record: dict) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def pre_auth_mw(request: Request, call_next):
        # This is the stage Musubi's idempotency/rate-limit middleware runs at.
        record["mw_route"] = request.scope.get("route")
        record["mw_url_path"] = request.url.path
        return await call_next(request)

    async def capture_identity(request: Request) -> None:
        route = request.scope.get("route")
        record["dep_route_type"] = type(route).__name__ if route is not None else None
        record["dep_path_format"] = getattr(route, "path_format", None)
        record["dep_operation_id"] = getattr(route, "operation_id", None)

    @app.post(
        "/v1/items/{item_id}",
        operation_id="capture_episodic.bucket=capture",
        dependencies=[Depends(capture_identity)],
    )
    async def handler(item_id: str) -> dict:
        return {"item_id": item_id}

    return app


def test_middleware_cannot_see_canonical_route() -> None:
    record: dict = {}
    TestClient(_build_app(record)).post("/v1/items/42")
    # Pre-auth middleware: routing has NOT happened yet.
    assert record["mw_route"] is None, (
        "middleware unexpectedly saw a route — if this changed, the pipeline order changed"
    )
    # It only has the RAW concrete path — no template, no operation_id.
    assert record["mw_url_path"] == "/v1/items/42", record["mw_url_path"]


def test_dependency_sees_canonical_route_and_operation_id() -> None:
    record: dict = {}
    TestClient(_build_app(record)).post("/v1/items/42")
    # A dependency runs AFTER routing → the APIRoute is on the scope.
    assert record["dep_route_type"] == "APIRoute", record["dep_route_type"]
    # The canonical, param-independent identity:
    assert record["dep_path_format"] == "/v1/items/{item_id}", record["dep_path_format"]
    assert record["dep_operation_id"] == "capture_episodic.bucket=capture", record[
        "dep_operation_id"
    ]


def test_raw_path_over_discriminates_same_endpoint() -> None:
    """Two requests to the SAME endpoint with different path params have DIFFERENT url.path but
    the SAME path_format — proving the raw path (all the middleware has) is the wrong identity:
    it would treat two calls to one endpoint as two different endpoints."""
    r1: dict = {}
    r2: dict = {}
    TestClient(_build_app(r1)).post("/v1/items/1")
    TestClient(_build_app(r2)).post("/v1/items/2")
    assert r1["mw_url_path"] != r2["mw_url_path"], "raw paths differ (1 vs 2)"
    assert r1["dep_path_format"] == r2["dep_path_format"], (
        "canonical template is identical — raw path over-discriminates the same endpoint"
    )


def test_app_exposes_path_format_and_operation_id_on_apiroute() -> None:
    """Static control: the attributes the fix relies on exist on FastAPI's APIRoute, so the
    dependency-level identity is a real API, not an accident of this test."""
    record: dict = {}
    app = _build_app(record)
    route = next(
        r for r in app.routes if isinstance(r, APIRoute) and r.path == "/v1/items/{item_id}"
    )
    assert route.path_format == "/v1/items/{item_id}"
    assert route.operation_id == "capture_episodic.bucket=capture"
