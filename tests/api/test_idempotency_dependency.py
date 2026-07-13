"""Routed idempotency dependency — decision logic, identity/digest, owner, in_flight, release.

Unit-level (the dependency is NOT yet wired onto the real capture routes — that lands with the
store-only observer). A minimal app wires the dependency with a controllable lease cache so the
hit / conflict / in_flight / acquired decisions can be driven directly.

    uv run pytest tests/api/test_idempotency_dependency.py -v
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Request
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.testclient import TestClient

from musubi.api.errors import APIError, api_error_handler
from musubi.api.idempotency import (
    CompletedResponse,
    IdempotencyLeaseCache,
    get_idempotency_lease_cache,
)
from musubi.api.idempotency_dependency import (
    IdempotentContext,
    Replay,
    build_identity,
    canonical_digest,
    make_idempotency_dependency,
)
from musubi.api.write_auth import AuthorizedWrite
from musubi.auth.tokens import AuthContext

OP = "capture_episodic.bucket=capture"
NS = "eric/claude-code/episodic"


def _auth() -> AuthContext:
    return AuthContext(
        subject="eric-claude-code",
        issuer="https://auth.test",
        audience="musubi",
        scopes=(f"{NS}:rw",),
        presence="eric/claude-code",
        token_id="t",
    )


@dataclass
class _Body:
    namespace: str = NS
    content: str = "c"


async def _authz(request: Request) -> AuthorizedWrite[_Body]:
    request.state.auth = _auth()
    return AuthorizedWrite(auth=_auth(), namespace=NS, body=_Body())


def _app(cache: IdempotencyLeaseCache) -> FastAPI:
    app = FastAPI()
    idem = make_idempotency_dependency(_authz)
    app.add_exception_handler(APIError, api_error_handler)

    @app.exception_handler(Replay)
    async def _replay(request: Request, exc: Replay) -> Response:
        r = Response(content=exc.completed.body, status_code=exc.completed.status)
        r.raw_headers = [*exc.completed.raw_headers, (b"x-idempotent-replay", b"true")]
        return r

    @app.post("/v1/episodic", operation_id=OP)
    async def handler(ctx: IdempotentContext = Depends(idem)) -> dict[str, Any]:
        # NOTE: no observer yet — this test app does not store; it reports the dependency's decision.
        return {"executed": True, "owner": ctx.owner, "has_identity": ctx.identity is not None}

    app.dependency_overrides[get_idempotency_lease_cache] = lambda: cache
    return app


def _client(cache: IdempotencyLeaseCache) -> TestClient:
    return TestClient(_app(cache), raise_server_exceptions=False)


def _expected_identity(key: str) -> tuple[Any, ...]:
    return build_identity(_auth(), "POST", OP, NS, key)


# --------------------------------------------------------------------------- #


def test_no_key_is_not_idempotent_handler_runs() -> None:
    c = _client(IdempotencyLeaseCache())
    r = c.post("/v1/episodic", json={"namespace": NS, "content": "c"})
    assert (
        r.status_code == 200 and r.json()["executed"] is True and r.json()["has_identity"] is False
    )


def test_duplicate_key_header_is_400() -> None:
    c = _client(IdempotencyLeaseCache())
    r = c.post(
        "/v1/episodic",
        json={"namespace": NS, "content": "c"},
        headers=[("idempotency-key", "a"), ("idempotency-key", "b")],
    )
    assert r.status_code == 400


def test_acquired_runs_handler_with_a_fresh_owner_and_holds_the_lease() -> None:
    cache = IdempotencyLeaseCache()
    c = _client(cache)
    r = c.post(
        "/v1/episodic", json={"namespace": NS, "content": "c"}, headers={"Idempotency-Key": "k"}
    )
    assert (
        r.status_code == 200 and r.json()["executed"] is True and r.json()["has_identity"] is True
    )
    owner = r.json()["owner"]
    assert isinstance(owner, str) and len(owner) == 32
    # the lease is held (in-flight) for a concurrent caller — proving acquire took effect.
    assert cache.acquire(_expected_identity("k"), "other", digest=bytes(32))[0] == "in_flight"


def test_owner_tokens_are_unique_per_request() -> None:
    c = _client(IdempotencyLeaseCache())
    o1 = c.post(
        "/v1/episodic", json={"namespace": NS, "content": "c"}, headers={"Idempotency-Key": "k1"}
    ).json()["owner"]
    o2 = c.post(
        "/v1/episodic", json={"namespace": NS, "content": "c"}, headers={"Idempotency-Key": "k2"}
    ).json()["owner"]
    assert o1 != o2, (
        "each request must get a collision-resistant owner, not one derived from the key"
    )


def test_in_flight_is_visible_conflict_handler_not_executed() -> None:
    cache = IdempotencyLeaseCache()
    cache.acquire(_expected_identity("k"), "someone-else", digest=bytes(32))  # pre-held lease
    c = _client(cache)
    r = c.post(
        "/v1/episodic", json={"namespace": NS, "content": "c"}, headers={"Idempotency-Key": "k"}
    )
    assert r.status_code == 409, (
        "an in-flight duplicate must be a visible conflict, not a second execution"
    )
    assert "in flight" in r.text


def test_different_body_same_identity_is_409_conflict() -> None:
    cache = IdempotencyLeaseCache()
    # complete an entry with a digest for body A
    digest_a = canonical_digest(
        b'{"namespace":"' + NS.encode() + b'","content":"A"}', "application/json"
    )
    cache.acquire(_expected_identity("k"), "o1", digest=digest_a)
    cache.store(
        _expected_identity("k"),
        "o1",
        response=CompletedResponse(status=202, raw_headers=(), body=b"stored"),
    )
    c = _client(cache)
    r = c.post(
        "/v1/episodic",
        json={"namespace": NS, "content": "DIFFERENT"},
        headers={"Idempotency-Key": "k"},
    )
    assert r.status_code == 409 and "different" in r.text.lower()


def test_same_digest_is_replay_served_without_executing() -> None:
    cache = IdempotencyLeaseCache()
    # pre-store the EXACT bytes the client will send so digests match
    body = b'{"namespace":"' + NS.encode() + b'","content":"c"}'
    digest = canonical_digest(body, "application/json")
    cache.acquire(_expected_identity("k"), "o1", digest=digest)
    cache.store(
        _expected_identity("k"),
        "o1",
        response=CompletedResponse(
            status=202,
            raw_headers=((b"content-type", b"application/json"),),
            body=b'{"object_id":"cached"}',
        ),
    )
    c = _client(cache)
    r = c.post(
        "/v1/episodic",
        content=body,
        headers={"Idempotency-Key": "k", "content-type": "application/json"},
    )
    assert r.status_code == 202 and r.headers.get("X-Idempotent-Replay") == "true"
    assert r.json() == {"object_id": "cached"}, "replay serves the cached response, handler not run"


# --------------------------------------------------------------------------- #
# release guarantee: a raise AFTER acquire but BEFORE observer state is established frees the lease
# --------------------------------------------------------------------------- #


class _SpyCache:
    def __init__(self) -> None:
        self.released: list[tuple[Any, str]] = []

    def acquire(
        self, identity: Any, owner: str, *, digest: bytes
    ) -> tuple[str, CompletedResponse | None]:
        return "acquired", None

    def release(self, identity: Any, owner: str) -> bool:
        self.released.append((identity, owner))
        return True


class _BoomState:
    def __setattr__(self, name: str, value: Any) -> None:
        raise RuntimeError("cannot establish observer state")


class _FakeRoute:
    operation_id = OP
    path = "/v1/episodic"


class _FakeRequest:
    method = "POST"

    def __init__(self) -> None:
        object.__setattr__(self, "scope", {"route": _FakeRoute()})
        object.__setattr__(self, "state", _BoomState())

    @property
    def headers(self) -> Headers:
        return Headers(raw=[(b"idempotency-key", b"k"), (b"content-type", b"application/json")])

    async def body(self) -> bytes:
        return b'{"namespace":"eric/claude-code/episodic","content":"c"}'


def test_release_when_dependency_raises_after_acquire_before_state() -> None:
    spy = _SpyCache()
    dep = make_idempotency_dependency(_authz)
    aw = AuthorizedWrite(auth=_auth(), namespace=NS, body=_Body())
    import asyncio

    with pytest.raises(RuntimeError, match="observer state"):
        asyncio.run(dep(_FakeRequest(), authorized=aw, cache=spy))  # type: ignore[arg-type]
    assert len(spy.released) == 1, (
        "a lease acquired but not published must be released on the raise"
    )
