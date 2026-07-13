"""D3 spike — hybrid routed-dependency state + pure-ASGI send observer for idempotent JSON.

Yua REV2 GO (2026-07-12T23:44). Design-only spike, ZERO src, on the isolated design worktree
(branch slice/auth-boundary-design-spikes from Phase A a1c916e). Proves the storage/lease
mechanics of the chosen hybrid:

  - a ROUTED DEPENDENCY (here: modelled as route-declared eligibility + a lease acquired before
    the app runs) sets authorized-identity + lease + cache-eligible state,
  - a PURE ASGI SEND-OBSERVER (not @app.middleware, which is the lossy BaseHTTPMiddleware) wraps
    `send`, captures the exact http.response.start (status + raw header list, duplicates intact)
    and every http.response.body (exact bytes + more_body), and STORES only after a clean
    terminal more_body=False AND 2xx AND eligible AND non-replay; the lease RELEASES in `finally`
    on EVERY exit.

The apps under test are raw ASGI callables so send-failure and cancellation can be injected
exactly. No FastAPI/Starlette response objects are needed for the mechanics (a separate test
uses Starlette Response only to prove background-once + duplicate Set-Cookie shape).

REQUIRED ADVERSARIAL (Yua): a multi-body NON-streaming response whose intermediate event has
more_body=True must STILL be cached (eligibility is route-declared; storage waits for the clean
terminal event) — `more_body` ever being true must NOT exclude it.

    UV_PROJECT_ENVIRONMENT=/Users/ericmey/Projects/musubi/.venv uv run --no-sync \
      pytest tests/api/spikes/test_d3_asgi_send_observer.py -v
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# --------------------------------------------------------------------------- #
# The pure-ASGI send observer (the thing being designed). Not src — a spike.
# --------------------------------------------------------------------------- #


@dataclass
class _Capture:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""
    saw_more_body: bool = False
    terminal: bool = False


class SendObserver:
    """Wraps `send`. Eligibility is ROUTE-DECLARED (never inferred from more_body). Stores only on
    a clean terminal event; releases the lease on every exit."""

    def __init__(self, app: ASGIApp, *, eligible_paths: set[str]) -> None:
        self.app = app
        self.eligible_paths = eligible_paths
        self.cache: dict[str, tuple[int, list[tuple[bytes, bytes]], bytes]] = {}
        self.stored: list[str] = []
        self.leases_acquired: list[str] = []
        self.leases_released: list[str] = []

    def _idem_key(self, scope: Scope) -> str | None:
        for k, v in scope.get("headers", []):
            if k == b"idempotency-key":
                return str(v.decode())
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        eligible = scope["path"] in self.eligible_paths  # ROUTE-DECLARED, not more_body-inferred
        key = self._idem_key(scope)

        # replay: the dependency's lease/lookup would short-circuit; modelled here.
        if eligible and key is not None and key in self.cache:
            status, headers, body = self.cache[key]
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [*headers, (b"x-idempotent-replay", b"true")],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return  # NO lease acquired, NO handler executed → no side effects on replay

        owner: str | None = None
        if eligible:
            owner = f"lease:{key}"
            self.leases_acquired.append(owner)

        cap = _Capture()

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                cap.status = message["status"]
                cap.headers = list(message["headers"])  # preserves duplicates + order
            elif message["type"] == "http.response.body":
                cap.body += message.get("body", b"")
                if message.get("more_body", False):
                    cap.saw_more_body = True
                else:
                    cap.terminal = True
            await send(message)  # may raise (send-failure) or be cancelled

        try:
            await self.app(scope, receive, wrapped_send)
            if (
                eligible
                and key is not None
                and cap.terminal  # ONLY a clean terminal event
                and cap.status is not None
                and 200 <= cap.status < 300
            ):
                self.cache[key] = (cap.status, cap.headers, cap.body)
                self.stored.append(key)
        finally:
            if owner is not None:
                self.leases_released.append(owner)


# --------------------------------------------------------------------------- #
# helpers to drive a raw ASGI app with precise control over send
# --------------------------------------------------------------------------- #


def _scope(path: str, key: str | None = None) -> Scope:
    headers: list[tuple[bytes, bytes]] = []
    if key is not None:
        headers.append((b"idempotency-key", key.encode()))
    return {"type": "http", "path": path, "method": "POST", "headers": headers}


async def _drive(app: ASGIApp, scope: Scope, send: Send) -> None:
    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(scope, receive, send)


def _collector() -> tuple[list[Message], Send]:
    events: list[Message] = []

    async def send(m: Message) -> None:
        events.append(m)

    return events, send


# --------------------------------------------------------------------------- #
# endpoint apps (raw ASGI) — each models a case
# --------------------------------------------------------------------------- #


def _json_once(
    body: bytes, status: int = 200, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(b"content-type", b"application/json"), *(extra_headers or [])]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return app


def _multi_frame(frames: list[bytes], status: int = 200) -> ASGIApp:
    """A NON-streaming response emitted in multiple body events (intermediate more_body=True)."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        for i, f in enumerate(frames):
            await send({"type": "http.response.body", "body": f, "more_body": i < len(frames) - 1})

    return app


def _raises_before_terminal() -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("handler blew up mid-response")

    return app


def _error_response() -> ASGIApp:
    return _json_once(b'{"error":1}', status=500)


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #

ELIGIBLE = {"/w"}


def test_eligible_json_is_captured_byte_exact_and_stored() -> None:
    obs = SendObserver(_json_once(b'{"z":1,"a":2}'), eligible_paths=ELIGIBLE)
    _events, send = _collector()
    asyncio.run(_drive(obs, _scope("/w", "k1"), send))
    assert obs.stored == ["k1"]
    _status, _headers, body = obs.cache["k1"]
    assert body == b'{"z":1,"a":2}', "exact bytes captured, no re-serialisation"
    assert obs.leases_acquired == obs.leases_released == ["lease:k1"], (
        "lease acquired then released"
    )


def test_duplicate_headers_preserved_exact() -> None:
    dup = [(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")]
    obs = SendObserver(_json_once(b"{}", extra_headers=dup), eligible_paths=ELIGIBLE)
    _e, send = _collector()
    asyncio.run(_drive(obs, _scope("/w", "k2"), send))
    _s, headers, _b = obs.cache["k2"]
    cookies = [v for k, v in headers if k == b"set-cookie"]
    assert cookies == [b"a=1", b"b=2"], f"duplicate Set-Cookie must survive exactly, got {cookies}"


def test_multi_frame_nonstreaming_is_still_cached_adversarial() -> None:
    """REQUIRED ADVERSARIAL: intermediate more_body=True must NOT exclude from cache. Eligibility
    is route-declared; storage waits for the clean terminal event, accumulating all frames."""
    obs = SendObserver(_multi_frame([b'{"part":', b'"one"}']), eligible_paths=ELIGIBLE)
    _e, send = _collector()
    asyncio.run(_drive(obs, _scope("/w", "k3"), send))
    assert obs.stored == ["k3"], "a multi-body eligible 2xx must be cached despite more_body=True"
    _s, _h, body = obs.cache["k3"]
    assert body == b'{"part":"one"}', "all frames accumulated in order"


def test_streaming_route_not_declared_eligible_is_not_cached() -> None:
    """A true stream is simply NOT route-declared eligible → not cached. No detection needed."""
    obs = SendObserver(
        _multi_frame([b"chunk1", b"chunk2"]), eligible_paths=set()
    )  # /w NOT eligible
    _e, send = _collector()
    asyncio.run(_drive(obs, _scope("/stream", "k4"), send))
    assert obs.stored == [], "ineligible route must never be cached"
    assert obs.leases_acquired == [] and obs.leases_released == [], "no lease for ineligible route"


def test_exception_response_not_cached_lease_released() -> None:
    obs = SendObserver(_error_response(), eligible_paths=ELIGIBLE)
    _e, send = _collector()
    asyncio.run(_drive(obs, _scope("/w", "k5"), send))
    assert obs.stored == [], "a 5xx must not be cached"
    assert obs.leases_released == ["lease:k5"], "lease released even on error status"


def test_handler_exception_before_terminal_not_cached_lease_released() -> None:
    obs = SendObserver(_raises_before_terminal(), eligible_paths=ELIGIBLE)
    _e, send = _collector()
    with pytest.raises(RuntimeError):
        asyncio.run(_drive(obs, _scope("/w", "k6"), send))
    assert obs.stored == [], "partial (no terminal) must not be cached"
    assert obs.leases_released == ["lease:k6"], "lease released on handler exception"


def test_send_failure_not_cached_lease_released() -> None:
    """send() raises (client disconnect) after start — nothing cached; lease released; no partial."""
    obs = SendObserver(_json_once(b"{}"), eligible_paths=ELIGIBLE)

    async def failing_send(m: Message) -> None:
        if m["type"] == "http.response.body":
            raise ConnectionError("client disconnected")

    with pytest.raises(ConnectionError):
        asyncio.run(_drive(obs, _scope("/w", "k7"), failing_send))
    assert obs.stored == [], "a send failure must not produce a cached success"
    assert obs.leases_released == ["lease:k7"], "lease released on send failure"


def test_cancellation_not_cached_lease_released() -> None:
    """asyncio cancellation mid-response — nothing cached; lease released in finally."""
    obs = SendObserver(_json_once(b"{}"), eligible_paths=ELIGIBLE)

    async def cancelling_send(m: Message) -> None:
        if m["type"] == "http.response.body":
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_drive(obs, _scope("/w", "k8"), cancelling_send))
    assert obs.stored == [], "a cancelled response must not be cached"
    assert obs.leases_released == ["lease:k8"], "lease released on cancellation"


def test_replay_serves_cache_without_reexecuting_or_reacquiring() -> None:
    """Store then replay: the replay serves the cached bytes, does NOT re-acquire a lease, and
    does NOT execute the handler (so side effects run once)."""
    executions = {"n": 0}

    async def counting_app(scope: Scope, receive: Receive, send: Send) -> None:
        executions["n"] += 1
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":1}', "more_body": False})

    obs = SendObserver(counting_app, eligible_paths=ELIGIBLE)
    _e1, s1 = _collector()
    asyncio.run(_drive(obs, _scope("/w", "k9"), s1))  # store
    events2, s2 = _collector()
    asyncio.run(_drive(obs, _scope("/w", "k9"), s2))  # replay
    assert executions["n"] == 1, (
        "handler executed once; replay must not re-execute (side-effect once)"
    )
    assert obs.leases_acquired == ["lease:k9"], "replay must not acquire a second lease"
    replay_hdr = [
        v
        for m in events2
        if m["type"] == "http.response.start"
        for k, v in m["headers"]
        if k == b"x-idempotent-replay"
    ]
    assert replay_hdr == [b"true"], "replay is marked"
    body = b"".join(m.get("body", b"") for m in events2 if m["type"] == "http.response.body")
    assert body == b'{"ok":1}', "replay serves byte-identical body"


def test_store_failure_does_not_fake_a_cached_success() -> None:
    """If the cache store itself fails, a COMPLETED mutation must not become an ambiguous cached
    success: the store raises, nothing is recorded as stored, and the lease still releases."""

    class FailingStoreObserver(SendObserver):
        def __init__(self, app: ASGIApp, **kw: Any) -> None:
            super().__init__(app, **kw)

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            # force cache assignment to fail by making .cache reject writes after capture
            class _RejectDict(dict):  # type: ignore[type-arg]
                def __setitem__(self, k: Any, v: Any) -> None:
                    raise RuntimeError("cache backend down")

            self.cache = _RejectDict()
            await super().__call__(scope, receive, send)

    obs = FailingStoreObserver(_json_once(b"{}"), eligible_paths=ELIGIBLE)
    with pytest.raises(RuntimeError, match="cache backend down"):
        _e, send = _collector()
        asyncio.run(_drive(obs, _scope("/w", "k10"), send))
    assert obs.stored == [], "store failure must not be recorded as a cached success"
    assert obs.leases_released == ["lease:k10"], "lease released even when the store fails"


class _UppercaseTransform:
    """A response-transforming ASGI middleware (models GZip/transform): mutates the body bytes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def wrapped(m: Message) -> None:
            if m["type"] == "http.response.body" and m.get("body"):
                m = {**m, "body": m["body"].upper()}
            await send(m)

        await self.app(scope, receive, wrapped)


def test_middleware_placement_determines_captured_bytes() -> None:
    """The observer must sit BELOW (inside) response-transforming middleware to capture the
    ORIGINAL bytes; placed ABOVE (outside) it captures the TRANSFORMED bytes. Pins placement."""
    body = b'{"v":"abc"}'

    # observer OUTSIDE the transform -> sees transformed (uppercased) bytes
    outside = SendObserver(_UppercaseTransform(_json_once(body)), eligible_paths=ELIGIBLE)
    _e, s = _collector()
    asyncio.run(_drive(outside, _scope("/w", "p1"), s))
    assert outside.cache["p1"][2] == body.upper(), (
        "observer above transform captures transformed bytes"
    )

    # observer INSIDE the transform -> sees original bytes (correct placement)
    inner = SendObserver(_json_once(body), eligible_paths=ELIGIBLE)
    stack = _UppercaseTransform(inner)
    _e2, s2 = _collector()
    asyncio.run(_drive(stack, _scope("/w", "p2"), s2))
    assert inner.cache["p2"][2] == body, "observer below transform captures original bytes"

    assert outside.cache["p1"][2] != inner.cache["p2"][2], "placement changes what is stored"


# --------------------------------------------------------------------------- #
# D3-12 / D3-13 — the SAME observer wrapping a REAL pinned FastAPI app
# --------------------------------------------------------------------------- #


def _fastapi_app(counters: dict[str, int]) -> FastAPI:
    """Real FastAPI app: a routed dependency sets request.state (scope['state']) and is counted;
    one handler returns a dict, one returns a raw Response; one raises → real exception handler."""
    app = FastAPI()

    async def idem_dependency(request: Request) -> None:
        counters["dep"] += 1
        request.state.idem_identity = "declared"  # dependency sets state (scope['state'])

    @app.post("/eligible-dict", dependencies=[Depends(idem_dependency)])
    async def eligible_dict() -> dict[str, int]:
        counters["dict_handler"] += 1
        return {"z": 1, "a": 2}

    @app.post("/eligible-response", dependencies=[Depends(idem_dependency)])
    async def eligible_response() -> Response:
        counters["resp_handler"] += 1
        r = PlainTextResponse("col1,col2\n1,2\n", media_type="text/csv")
        r.set_cookie("s", "1")
        return r

    @app.post("/boom", dependencies=[Depends(idem_dependency)])
    async def boom() -> Response:
        counters["boom_handler"] += 1
        raise HTTPException(status_code=503, detail="backend down")

    return app


def test_d3_12_real_fastapi_dict_and_response_capture_and_replay_byte_exact() -> None:
    counters = {"dep": 0, "dict_handler": 0, "resp_handler": 0, "boom_handler": 0}
    obs = SendObserver(
        _fastapi_app(counters), eligible_paths={"/eligible-dict", "/eligible-response"}
    )
    client = TestClient(obs)

    for path, key, expect_ct in [
        ("/eligible-dict", "d1", "application/json"),
        ("/eligible-response", "r1", "text/csv"),
    ]:
        first = client.post(path, headers={"Idempotency-Key": key})
        assert first.status_code == 200
        assert first.headers.get("x-idempotent-replay") is None, "first call is not a replay"
        replay = client.post(path, headers={"Idempotency-Key": key})
        assert replay.headers.get("x-idempotent-replay") == "true", "second call is a replay"
        assert replay.content == first.content, f"{path}: replay bytes must be identical"
        assert replay.headers.get("content-type", "").startswith(expect_ct), (
            "media type preserved on replay"
        )

    # dependency + handlers each ran exactly ONCE (miss); replay never re-executed either.
    assert counters["dep"] == 2, f"dependency ran once per distinct key, not on replay: {counters}"
    assert counters["dict_handler"] == 1 and counters["resp_handler"] == 1, counters
    # replay preserves the raw Set-Cookie from the Response handler
    replay2 = client.post("/eligible-response", headers={"Idempotency-Key": "r1"})
    assert any("s=1" in c for c in replay2.headers.get_list("set-cookie")), (
        "Set-Cookie preserved on replay"
    )


def test_d3_13_real_exception_handler_5xx_not_cached_lease_released() -> None:
    counters = {"dep": 0, "dict_handler": 0, "resp_handler": 0, "boom_handler": 0}
    obs = SendObserver(_fastapi_app(counters), eligible_paths={"/boom"})
    client = TestClient(obs)
    r = client.post("/boom", headers={"Idempotency-Key": "b1"})
    assert r.status_code == 503, "real exception handler produced the 5xx"
    assert obs.stored == [], "a 5xx from a real exception handler must not be cached"
    assert obs.leases_released == ["lease:b1"], "lease released on the exception-handler path"
    # a second identical request re-executes (nothing was cached) — proves no false replay
    client.post("/boom", headers={"Idempotency-Key": "b1"})
    assert counters["boom_handler"] == 2, "no cache → handler runs again, not served a stale replay"
