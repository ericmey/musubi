"""Phase B idempotency pipeline — TRANSCRIBED design contract + closure matrix.

Owned production-facing contract for the routed post-authz idempotency pipeline (SEC-002 +
IDEM-001). Transcribed verbatim from the ACCEPTED D3 design spike at
`slice/auth-boundary-design-spikes` @ 239029a (kept on origin as reference; this copy prevents
the evidence from orphaning). These are the design-contract tests the src implementation must
satisfy; the real-app behaviour is separately proven by flipping the SEC-002 / IDEM-001 strict
reds (test_sec002_idempotency_auth.py, test_idem001_replay_and_race.py, spikes/
test_idem_lease_contract.py).

CLOSURE MATRIX (Yua requirement -> contract test here -> real red it flips):
  principal tuple (iss,sub,presence) in identity, not token text
      -> test_d3_12_full_identity_replay_reauth_and_principal_tuple        (real: sec002 subject binding)
  identity = principal + method + operation + authorized namespace + key
      -> test_d3_same_key_across_namespace_is_distinct_execution           (real: idem001 cross-endpoint)
      -> test_d3_control_key_collision_across_principal_and_operation_does_not_replay
  body digest exact received bytes + content-type; byte-exact, not semantic
      -> test_d3_json_byte_exact_whitespace_or_keyorder_change_is_409_not_replay
  same identity + same digest => replay
      -> test_d3_12_full_identity_replay_reauth_and_principal_tuple        (real: sec002 owner replay)
  same identity + different digest => 409, no handler
      -> test_d3_same_key_same_identity_different_body_is_409_no_handler
  duplicate Idempotency-Key header => 400 (not first-wins)
      -> test_d3_duplicate_idempotency_key_header_is_400
  dependency-edge authz BEFORE lookup; no lookup/acquire on invalid-auth/foreign-ns
      -> test_d3_control_invalid_auth_never_looks_up_or_acquires
      -> test_d3_control_foreign_namespace_never_looks_up_or_acquires
  replay re-authenticates + re-authorizes every time (no pre-auth replay)
      -> test_d3_12_full_identity_replay_reauth_and_principal_tuple        (real: sec002 no-bearer/bad-bearer)
  in-flight lease released on ALL exits (2xx, 5xx, send-fail, cancel, store-fail)
      -> test_d3_control_5xx_not_cached_lease_released + the SendObserver mechanics tests
         (real: idem001 race / test_idem_lease_contract acquire/release)
  store-only observer: clean terminal 2xx miss only; raw headers/body preserved; streams excluded
      -> test_eligible_json_* / test_duplicate_headers_* / test_multi_frame_* / placement / etc.

DESIGN FINDING carried: replay MUST live in the routed dependency (post-authz); the outer wrapper
is store-only — a wrapper cannot replay before the dependency (no pre-route auth in Musubi).


Yua REV2 GO (2026-07-12T23:44); real-stack correction (2026-07-13). Design-only spike, ZERO src,
on the isolated design worktree (branch slice/auth-boundary-design-spikes from Phase A a1c916e).

The pipeline is a HYBRID: a PURE ASGI SEND-OBSERVER (not @app.middleware, which is the lossy
BaseHTTPMiddleware) wraps `send`, captures the exact http.response.start (status + raw header
list, duplicates intact) and every http.response.body (exact bytes + more_body), and STORES only
after a clean terminal more_body=False AND 2xx AND eligible AND non-replay; the lease RELEASES on
every exit. The eligibility/identity/lease and the REPLAY come from a routed dependency AFTER
authz — see the real-stack section for the authoritative model and why the wrapper cannot replay.

The raw-ASGI apps are used only to inject send-failure and cancellation exactly (the synthetic
mechanics tier).

REQUIRED ADVERSARIAL (Yua): a multi-body NON-streaming response whose intermediate event has
more_body=True must STILL be cached (eligibility is route-declared; storage waits for the clean
terminal event) — `more_body` ever being true must NOT exclude it.

TWO TIERS, and the KEY design finding (Yua 2026-07-13):
  - `SendObserver` + raw ASGI apps: SYNTHETIC MECHANICS ONLY — capture, store-gate, lease-release,
    send-failure, cancellation, multi-frame, placement. Its pre-call_next replay is a
    simplification, NOT the design.
  - `StoreOnlyObserver` + `_auth_app`: the AUTHORITATIVE real-stack model. Replay CANNOT happen in
    the outer wrapper: a safe replay needs the principal-bound identity that only exists after
    routing + authz, and Musubi has no pre-route auth. So replay MUST live in the routed dependency
    (post-authz); the outer wrapper is store-only. Controls prove no cross-principal / cross-
    operation replay, no lookup/acquire on invalid-auth or foreign-namespace, and re-auth on replay.

IDENTITY CLOSURE (Yua 2026-07-13): identity = validated principal tuple (issuer, subject,
presence) + method + operation + AUTHORIZED NAMESPACE + key; the canonical body DIGEST is
persisted separately. Same identity+same digest => replay; same identity+different body =>
409 (no handler); same key across namespaces => distinct execution. JSON canonicalisation is
BYTE-EXACT (whitespace/key-order change => 409, not a semantic-equivalence replay); multipart
uses the D5 scheme. Duplicate Idempotency-Key headers => 400 (not first-wins).

    UV_PROJECT_ENVIRONMENT=/Users/ericmey/Projects/musubi/.venv uv run --no-sync \
      pytest tests/api/spikes/test_d3_asgi_send_observer.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
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
    """SYNTHETIC MECHANICS ONLY — isolates the store-gate / capture / lease-release behaviour with
    raw ASGI apps so send-failure and cancellation can be injected exactly. Its path-based
    eligibility and its pre-`call_next` replay are a SIMPLIFICATION that does NOT model the auth
    gate: doing the replay in the wrapper before call_next is the SEC-002 pre-auth replay bug. The
    AUTHORITATIVE design — replay in the routed dependency AFTER authz, outer wrapper store-only —
    is proven in the corrected real-stack section (`StoreOnlyObserver` + `_auth_app`) below. Use
    this class only for the capture/gate/lease mechanics, never as the replay design."""

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
    """SYNTHETIC store-then-serve MECHANIC only (no auth gate): the cache lookup serves the bytes
    without re-executing the app. The AUTH-GATED replay (which must run post-authz, in the routed
    dependency) is proven by the real-stack controls below — this test is NOT the replay design."""
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
# D3-12/13 (CORRECTED) — real FastAPI: replay lives in the routed dependency
# (post-authz); the outer observer is STORE-ONLY and reads dependency state.
#
# DESIGN FINDING (Yua 2026-07-13): the outer ASGI wrapper CANNOT replay before
# the dependency. A safe replay requires the principal-bound identity, which only
# exists AFTER routing + authz (a routed dependency). Musubi has no pre-route auth
# middleware, so there is no authenticated pre-route state the wrapper could use.
# Therefore replay MUST be performed by the dependency (post-authz); the outer
# wrapper only STORES on a clean 2xx miss and RELEASES the lease. Doing the replay
# in the wrapper before call_next is exactly the SEC-002 pre-auth replay bug.
# --------------------------------------------------------------------------- #


class Replay(Exception):
    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body


class StoreOnlyObserver:
    """Outer pure-ASGI wrapper. It NEVER authorizes, computes eligibility, acquires a lease, or
    replays. It reads the idempotency decision the routed dependency wrote into
    scope['state']['idem'] (post-authz) and stores the captured bytes only on a clean 2xx MISS;
    it releases the lease the dependency acquired. Shares the cache dict with the dependency."""

    def __init__(self, app: ASGIApp, *, cache: dict[Any, Any]) -> None:
        self.app = app
        self.cache = cache  # identity tuple -> (digest, status, headers, body)
        self.stored: list[Any] = []
        self.released: list[Any] = []
        self.acquired_seen: list[Any] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cap = _Capture()

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                cap.status = message["status"]
                cap.headers = list(message["headers"])
            elif message["type"] == "http.response.body":
                cap.body += message.get("body", b"")
                if not message.get("more_body", False):
                    cap.terminal = True
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
            state = scope.get("state", {}).get("idem")
            if state and state.get("lease_owner"):
                self.acquired_seen.append(state["lease_owner"])
            if (
                state
                and state.get("eligible")
                and not state.get("is_replay")
                and cap.terminal
                and cap.status is not None
                and 200 <= cap.status < 300
            ):
                # persist the canonical request digest ALONGSIDE the response (separate from the
                # identity tuple) so a same-identity replay can compare body digests.
                self.cache[state["identity"]] = (state["digest"], cap.status, cap.headers, cap.body)
                self.stored.append(state["identity"])
        finally:
            state = scope.get("state", {}).get("idem")
            if state and state.get("lease_owner"):
                self.released.append(state["lease_owner"])


@dataclass(frozen=True)
class _Principal:
    """The STABLE validated principal (D6/req7): issuer + subject + presence — NOT display token
    text. The idempotency identity binds all three."""

    issuer: str
    subject: str
    presence: str


def _canonical_digest(body: bytes, content_type: str) -> bytes:
    """JSON/default canonicalisation is BYTE-EXACT: digest the exact received body bytes plus the
    content-type. NO semantic JSON equivalence — whitespace / key-order differences MUST change the
    digest (locked by test_d3_json_byte_exact_*). Multipart bodies use the D5 canonical scheme
    (test_d5_multipart_digest_ingress.py::test_identity_*), not this path."""
    return hashlib.sha256(content_type.encode("latin-1") + b"\x00" + body).digest()


def _auth_app(cache: dict[Any, Any], counters: dict[str, int]) -> FastAPI:
    """authenticate (authn + namespace authz) → idem (EDGE, post-authz). The idem identity is
    (issuer, subject, presence, method, operation, authorized-namespace, key); the canonical body
    digest is persisted SEPARATELY and compared: same identity+same digest => replay; same
    identity+different digest => 409 (no handler). Duplicate Idempotency-Key headers => 400."""
    app = FastAPI()

    async def authenticate(request: Request) -> _Principal:
        counters["auth"] += 1
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer tok-"):
            raise HTTPException(status_code=401, detail="invalid token")
        subject = auth[len("Bearer tok-") :]
        ns = request.query_params.get("namespace")
        if ns is not None and not ns.startswith(subject + "/"):
            raise HTTPException(status_code=403, detail="foreign namespace")
        return _Principal(
            issuer="https://auth.test", subject=subject, presence=f"{subject}/claude-code"
        )

    async def idem(request: Request, principal: _Principal = Depends(authenticate)) -> None:
        keys = request.headers.getlist("idempotency-key")
        if len(keys) > 1:
            raise HTTPException(status_code=400, detail="duplicate Idempotency-Key header")
        if not keys:
            return
        key = keys[0]
        ns = request.query_params.get("namespace")
        identity = (
            principal.issuer,
            principal.subject,
            principal.presence,
            request.method,
            request.scope["route"].path,  # normalized operation
            ns,  # authorized namespace
            key,
        )
        digest = _canonical_digest(await request.body(), request.headers.get("content-type", ""))
        entry = cache.get(identity)
        if entry is not None:
            stored_digest, status, headers, body = entry
            if stored_digest == digest:
                request.state.idem = {"is_replay": True}
                raise Replay(status, headers, body)
            # same identity, DIFFERENT body → conflict; NO handler, NO store, NO replay.
            raise HTTPException(
                status_code=409, detail="Idempotency-Key reused with a different body"
            )
        counters["acquire"] += 1
        request.state.idem = {
            "eligible": True,
            "identity": identity,
            "digest": digest,
            "lease_owner": ("lease", identity),
        }

    @app.exception_handler(Replay)
    async def _replay_handler(request: Request, exc: Replay) -> Response:
        r = Response(content=exc.body, status_code=exc.status)
        r.raw_headers = [*exc.headers, (b"x-idempotent-replay", b"true")]
        return r

    @app.post("/dict", dependencies=[Depends(idem)])
    async def dict_route() -> dict[str, int]:
        counters["dict_handler"] += 1
        return {"z": 1, "a": 2}

    @app.post("/resp", dependencies=[Depends(idem)])
    async def resp_route() -> Response:
        counters["resp_handler"] += 1
        r = PlainTextResponse("col1,col2\n1,2\n", media_type="text/csv")
        r.set_cookie("s", "1")
        return r

    @app.post("/boom", dependencies=[Depends(idem)])
    async def boom_route() -> Response:
        counters["boom_handler"] += 1
        raise HTTPException(status_code=503, detail="backend down")

    @app.post("/no-idem", dependencies=[Depends(authenticate)])  # authenticated, NO idem dep
    async def no_idem_route() -> dict[str, int]:
        counters["no_idem_handler"] += 1
        return {"ok": 1}

    return app


def _mk() -> tuple[dict[Any, Any], dict[str, int], StoreOnlyObserver, TestClient]:
    cache: dict[Any, Any] = {}
    counters = dict.fromkeys(
        ("auth", "acquire", "dict_handler", "resp_handler", "boom_handler", "no_idem_handler"), 0
    )
    obs = StoreOnlyObserver(_auth_app(cache, counters), cache=cache)
    return cache, counters, obs, TestClient(obs, raise_server_exceptions=False)


def _h(subject: str, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer tok-{subject}", "Idempotency-Key": key}


def test_d3_12_full_identity_replay_reauth_and_principal_tuple() -> None:
    _cache, counters, obs, client = _mk()
    for path, key, ct in [("/dict", "d1", "application/json"), ("/resp", "r1", "text/csv")]:
        first = client.post(path, headers=_h("eric", key), json={"payload": 1})
        assert first.status_code == 200 and first.headers.get("x-idempotent-replay") is None
        replay = client.post(path, headers=_h("eric", key), json={"payload": 1})
        assert replay.headers.get("x-idempotent-replay") == "true", "same identity+digest replays"
        assert replay.content == first.content and replay.headers.get(
            "content-type", ""
        ).startswith(ct)
    assert counters["auth"] == 4, "re-authenticated on every request incl the two replays"
    assert (
        counters["acquire"] == 2 and counters["dict_handler"] == 1 and counters["resp_handler"] == 1
    )
    # identity binds the validated principal TUPLE (issuer, subject, presence), not token text.
    assert obs.stored[0][:3] == ("https://auth.test", "eric", "eric/claude-code")


def test_d3_same_key_same_identity_different_body_is_409_no_handler() -> None:
    _cache, counters, _obs, client = _mk()
    a = client.post("/dict", headers=_h("eric", "k"), json={"payload": "A"})
    assert a.status_code == 200
    b = client.post(
        "/dict", headers=_h("eric", "k"), json={"payload": "B"}
    )  # same identity, diff body
    assert b.status_code == 409, "same identity + different body must be 409, not a replay"
    assert b.headers.get("x-idempotent-replay") is None
    assert counters["dict_handler"] == 1, "the 409 must NOT run the handler (no second mutation)"


def test_d3_same_key_across_namespace_is_distinct_execution() -> None:
    _cache, counters, _obs, client = _mk()
    r_a = client.post("/dict?namespace=eric/a", headers=_h("eric", "k"), json={"p": 1})
    r_b = client.post(
        "/dict?namespace=eric/b", headers=_h("eric", "k"), json={"p": 1}
    )  # same key, other ns
    assert r_a.status_code == 200 and r_b.status_code == 200
    assert r_b.headers.get("x-idempotent-replay") is None, (
        "cross-namespace must NOT replay/disclose"
    )
    assert counters["dict_handler"] == 2, "distinct authorized namespaces execute distinctly"


def test_d3_json_byte_exact_whitespace_or_keyorder_change_is_409_not_replay() -> None:
    """Locks the canonicalisation contract as BYTE-EXACT (not semantic JSON equivalence): the same
    logical JSON with different whitespace/key-order is a DIFFERENT digest → 409, never a replay."""
    _cache, counters, _obs, client = _mk()
    ct = {"content-type": "application/json"}
    first = client.post("/dict", headers={**_h("eric", "k"), **ct}, content=b'{"a":1,"b":2}')
    assert first.status_code == 200
    reorder = client.post("/dict", headers={**_h("eric", "k"), **ct}, content=b'{"b":2, "a":1}')
    assert reorder.status_code == 409, (
        "different bytes (key-order+whitespace) → 409, NOT semantic replay"
    )
    assert counters["dict_handler"] == 1, "byte-different body did not re-run the handler"


def test_d3_duplicate_idempotency_key_header_is_400() -> None:
    _cache, counters, _obs, client = _mk()
    r = client.post(
        "/dict",
        headers=[
            ("authorization", "Bearer tok-eric"),
            ("idempotency-key", "a"),
            ("idempotency-key", "b"),
        ],
        json={"p": 1},
    )
    assert r.status_code == 400, "duplicate Idempotency-Key headers must fail 400 (not first-wins)"
    assert counters["dict_handler"] == 0 and counters["acquire"] == 0


def test_d3_control_eligible_path_without_state_never_stores_or_acquires() -> None:
    _cache, counters, obs, client = _mk()
    client.post("/no-idem", headers=_h("eric", "k"), json={"p": 1})
    client.post("/no-idem", headers=_h("eric", "k"), json={"p": 1})
    assert obs.stored == [] and obs.acquired_seen == [] and obs.released == []
    assert counters["no_idem_handler"] == 2, (
        "no dependency state → no replay; handler runs both times"
    )


def test_d3_control_invalid_auth_never_looks_up_or_acquires() -> None:
    cache, counters, _obs, client = _mk()
    client.post("/dict", headers=_h("eric", "d1"), json={"p": 1})  # seed
    before = dict(cache)
    r = client.post(
        "/dict", headers={"Authorization": "Bearer BAD", "Idempotency-Key": "d1"}, json={"p": 1}
    )
    assert r.status_code == 401
    assert counters["acquire"] == 1 and cache == before, (
        "idem edge never ran; no lookup/acquire/store"
    )


def test_d3_control_foreign_namespace_never_looks_up_or_acquires() -> None:
    _cache, counters, _obs, client = _mk()
    client.post("/dict?namespace=eric/x", headers=_h("eric", "d1"), json={"p": 1})
    acq = counters["acquire"]
    r = client.post("/dict?namespace=eric/x", headers=_h("mallory", "d1"), json={"p": 1})
    assert r.status_code == 403 and counters["acquire"] == acq, "authz blocks before idem runs"


def test_d3_control_5xx_not_cached_lease_released() -> None:
    _cache, counters, obs, client = _mk()
    r = client.post("/boom", headers=_h("eric", "b1"), json={"p": 1})
    assert r.status_code == 503 and obs.stored == []
    assert len(obs.released) == 1, "lease released on the 5xx path"
    client.post("/boom", headers=_h("eric", "b1"), json={"p": 1})
    assert counters["boom_handler"] == 2, "nothing cached → handler runs again (no false replay)"


def test_d3_control_key_collision_across_principal_and_operation_does_not_replay() -> None:
    _cache, counters, _obs, client = _mk()
    client.post("/dict", headers=_h("eric", "shared"), json={"p": 1})
    other_principal = client.post("/dict", headers=_h("mallory", "shared"), json={"p": 1})
    assert other_principal.headers.get("x-idempotent-replay") is None, "no cross-principal replay"
    other_op = client.post("/resp", headers=_h("eric", "shared"), json={"p": 1})
    assert other_op.headers.get("x-idempotent-replay") is None, "no cross-operation replay"
    assert counters["dict_handler"] == 2 and counters["resp_handler"] == 1
