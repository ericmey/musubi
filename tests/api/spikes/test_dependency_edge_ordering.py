"""Dependency-edge ordering — authz must gate idempotency by an EDGE, not declaration order.

Yua req 2 (2026-07-12T21:18): "prove authz is a dependency EDGE, not sibling declaration
order." This is the structural guarantee that the SEC-002 bug (idempotency answering BEFORE
authentication) cannot silently reappear.

Observed against a live FastAPI app:

  - SIBLING dependencies run in DECLARATION ORDER. `dependencies=[Depends(idem), Depends(authz)]`
    runs idem BEFORE authz. So ordering-by-list-position is fragile: a future reorder of the
    list — or a new dependency inserted above authz — silently puts the idempotency answer
    before the auth check again. That is exactly the SEC-002 shape.

  - An EDGE is robust. When the idempotency dependency takes the auth result as a
    sub-dependency (`def idem(ctx = Depends(authz))`), authz is GUARANTEED to run first, even
    when idem is declared first — and idem RECEIVES the authorized principal, so it can build
    the replay identity from validated auth, never from unverified request data.

Conclusion for the src fix: the idempotency/identity dependency must `Depends(authz)` (consume
the AuthContext), making authz-before-idempotency a property of the dependency GRAPH, not of a
list position a later edit can reorder.

Tests/docs only. No src. Synthetic app.

    uv run pytest tests/api/spikes/test_dependency_edge_ordering.py -v
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from starlette.testclient import TestClient

AUTHZ = "authz"
IDEM = "idem"


def _sibling_app(order: list[str], *, idem_first: bool) -> FastAPI:
    """authz and idem as SIBLING dependencies; caller controls declaration order."""
    app = FastAPI()

    async def authz() -> None:
        order.append(AUTHZ)

    async def idem() -> None:
        order.append(IDEM)

    deps = [Depends(idem), Depends(authz)] if idem_first else [Depends(authz), Depends(idem)]

    @app.post("/x", dependencies=deps)
    async def handler() -> dict:
        return {}

    return app


def _edge_app(order: list[str]) -> FastAPI:
    """idem takes authz's result as a SUB-DEPENDENCY (the edge). idem is declared first to
    prove the edge — not the declaration order — decides execution."""
    app = FastAPI()

    async def authz() -> dict:
        order.append(AUTHZ)
        return {"principal": "eric/claude-code"}  # the validated AuthContext

    async def idem(request: Request, ctx: dict = Depends(authz)) -> dict:
        order.append(IDEM)
        request.state.identity_principal = ctx["principal"]  # identity built FROM validated auth
        return ctx

    @app.post("/x", dependencies=[Depends(idem)])
    async def handler() -> dict:
        return {}

    return app


def test_sibling_order_follows_declaration_and_is_fragile() -> None:
    """Sibling ordering is declaration-order — so it can put idempotency BEFORE auth."""
    idem_first: list[str] = []
    TestClient(_sibling_app(idem_first, idem_first=True)).post("/x")
    assert idem_first == [IDEM, AUTHZ], (
        f"expected declaration order to drive execution, got {idem_first}"
    )

    authz_first: list[str] = []
    TestClient(_sibling_app(authz_first, idem_first=False)).post("/x")
    assert authz_first == [AUTHZ, IDEM], (
        f"reversing the declaration reversed execution, got {authz_first}"
    )

    # THE FRAGILITY, stated as an assertion: the SAME two dependencies produce OPPOSITE
    # orderings purely from list position. Relying on this to keep auth before idempotency is
    # one careless reorder away from the SEC-002 bug.
    assert idem_first != authz_first, "sibling order is position-dependent — not a guarantee"


def test_edge_forces_authz_before_idem_regardless_of_declaration() -> None:
    """The edge holds the invariant no matter the declaration order."""
    order: list[str] = []
    TestClient(_edge_app(order)).post("/x")
    assert order == [AUTHZ, IDEM], (
        f"edge must run authz before idem even though idem is declared first, got {order}"
    )


def test_edge_passes_validated_principal_into_identity() -> None:
    """Beyond ordering: the edge DELIVERS the authorized principal to the idempotency layer, so
    the replay identity is built from validated auth — not from unverified request fields."""
    app = FastAPI()
    captured: dict = {}

    async def authz() -> dict:
        return {"principal": "eric/claude-code"}

    async def idem(request: Request, ctx: dict = Depends(authz)) -> None:
        captured["principal"] = ctx["principal"]

    @app.post("/x", dependencies=[Depends(idem)])
    async def handler() -> dict:
        return {}

    TestClient(app).post("/x")
    assert captured["principal"] == "eric/claude-code", (
        "the idempotency dependency must receive the validated principal via the edge"
    )
