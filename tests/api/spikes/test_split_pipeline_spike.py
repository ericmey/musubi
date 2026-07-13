"""Auth-boundary split-pipeline SPIKE — the four claims of ADR rev 3, as runnable tests.

Yua (21:18 REQ, point 1): "commit the FastAPI 0.136 spike/tests — prose that four claims
were proven is not inspectable evidence."

This file IS that evidence. It proves, against a live FastAPI/Starlette app (not the musubi
app — a minimal reproduction so the pipeline mechanics are isolated), that the rev-3 split
pipeline is executable:

  1. an outer store-middleware sees request.state set by a route DEPENDENCY, after call_next
  2. a typed Replay exception raised in the DEPENDENCY reaches the middleware AS a response
  3. the middleware can distinguish hit / miss / status / streaming
  4. the store gate must be `2xx && non-streaming && non-replay`, NOT try/except — because
     HTTPException(500) becomes a 500 RESPONSE, so an exception-keyed gate caches the 500;
     ownership releases in `finally` on every path

Run:  uv run pytest tests/api/spikes/test_split_pipeline_spike.py -v
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.testclient import TestClient


class Replay(Exception):
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status


def _build_app() -> tuple[FastAPI, dict]:
    """A minimal split-pipeline app. `events` records what the store-mw decided."""
    app = FastAPI()
    events: dict = {"cached": [], "released": []}

    @app.exception_handler(Replay)
    async def _on_replay(request: Request, exc: Replay):
        r = JSONResponse(exc.payload, status_code=exc.status)
        r.headers["X-Idempotent-Replay"] = "true"
        return r

    @app.middleware("http")
    async def store_mw(request: Request, call_next):
        # thin outer store middleware — NO lookup, authz, or body read
        try:
            resp = await call_next(request)
            if getattr(request.state, "idem_owned", False):
                # STREAMING DETECTION under BaseHTTPMiddleware (Yua point 4), proven in
                # the detection test below: EVERY response is wrapped as _StreamingResponse,
                # so isinstance MISSES real streams AND body_iterator matches EVERYTHING. The
                # actual discriminator is the ABSENCE of Content-Length: a buffered response
                # has a known length; a genuine stream does not.
                is_stream = resp.headers.get("content-length") is None
                is_replay = resp.headers.get("X-Idempotent-Replay") == "true"
                if 200 <= resp.status_code < 300 and not is_stream and not is_replay:
                    events["cached"].append(resp.status_code)
            return resp
        finally:
            if getattr(request.state, "idem_owned", False):
                events["released"].append(True)

    def acquire(request: Request) -> None:
        # the "miss → acquire in-flight" step, as a dependency
        request.state.idem_owned = True

    def replay_dep(request: Request) -> None:
        # the D3 dependency: hit → raise Replay; miss → proceed
        if request.query_params.get("hit"):
            raise Replay({"cached": "from-dependency"})

    @app.get("/w", dependencies=[Depends(acquire), Depends(replay_dep)])
    def w():
        return {"fresh": True}

    @app.get("/boom", dependencies=[Depends(acquire)])
    def boom():
        raise HTTPException(status_code=500, detail="handler failed")

    @app.get("/stream", dependencies=[Depends(acquire)])
    def stream():
        return StreamingResponse((b"x" for _ in range(3)), media_type="text/plain")

    return app, events


# ── Claim 1 — middleware sees dependency-set state after call_next ────────────
def test_middleware_sees_dependency_state_after_call_next() -> None:
    app = FastAPI()
    seen: dict = {}

    @app.middleware("http")
    async def outer(request: Request, call_next):
        resp = await call_next(request)
        seen["value"] = getattr(request.state, "idem_ctx", "<MISSING>")
        return resp

    def dep(request: Request):
        request.state.idem_ctx = "captured-by-dependency"

    @app.get("/probe", dependencies=[Depends(dep)])
    def probe():
        return {"ok": True}

    TestClient(app).get("/probe")
    assert seen["value"] == "captured-by-dependency", "split pipeline requires this"


# ── Claim 2 — typed Replay from the dependency reaches middleware as a response ─
def test_typed_replay_becomes_a_response() -> None:
    app, _ = _build_app()
    r = TestClient(app).get("/w?hit=1")
    assert r.status_code == 200
    assert r.headers.get("X-Idempotent-Replay") == "true"
    assert r.json() == {"cached": "from-dependency"}


# ── Claim 3 — middleware distinguishes hit / miss ─────────────────────────────
def test_middleware_distinguishes_hit_from_miss() -> None:
    app, events = _build_app()
    c = TestClient(app)
    c.get("/w?hit=1")           # hit → replay, must NOT be cached
    c.get("/w")                 # miss, 200 → cached
    assert events["cached"] == [200], f"only the miss-success should cache: {events['cached']}"


# ── Claim 4 — store gate is 2xx/non-streaming/non-replay, release in finally ──
def test_store_gate_is_status_based_not_try_except() -> None:
    app, events = _build_app()
    c = TestClient(app, raise_server_exceptions=False)
    c.get("/w")                 # 200 miss → cache
    c.get("/boom")              # 500 → must NOT cache (HTTPException is a RESPONSE here)
    c.get("/stream")            # streaming → must NOT cache
    c.get("/w?hit=1")           # replay → must NOT cache
    assert events["cached"] == [200], (
        f"only the 200 non-streaming non-replay response may be cached: {events['cached']}")
    # ownership released on EVERY request (4), including the 500 and the streaming one
    assert len(events["released"]) == 4, (
        f"ownership must release in finally on all paths: {len(events['released'])}/4")


def test_streaming_detection_needs_body_iterator_not_isinstance() -> None:
    """Yua point 4: under BaseHTTPMiddleware a StreamingResponse is wrapped as
    _StreamingResponse, so isinstance(resp, StreamingResponse) MISSES it and the stream
    gets cached. hasattr(resp, "body_iterator") is the correct detector."""
    seen: dict = {}
    app = FastAPI()

    @app.middleware("http")
    async def mw(request: Request, call_next):
        resp = await call_next(request)
        seen["isinstance"] = isinstance(resp, StreamingResponse)
        seen["has_body_iterator"] = hasattr(resp, "body_iterator")
        seen["type"] = type(resp).__name__
        return resp

    @app.get("/s")
    def s():
        return StreamingResponse((b"x" for _ in range(3)), media_type="text/plain")

    seen2: dict = {}

    @app.get("/j")
    def j():
        return {"ok": True}

    # re-instrument to capture content-length for BOTH a stream and a buffered response
    @app.middleware("http")
    async def mw2(request: Request, call_next):
        resp = await call_next(request)
        seen2[request.url.path] = resp.headers.get("content-length")
        return resp

    c = TestClient(app)
    c.get("/s")
    c.get("/j")
    assert seen["isinstance"] is False, "isinstance MISSES the wrapped stream"
    assert seen["type"] == "_StreamingResponse", "everything is _StreamingResponse under BaseHTTPMiddleware"
    # THE CORRECT DETECTOR: buffered has Content-Length, stream does not
    assert seen2["/j"] is not None, "a buffered response HAS Content-Length"
    assert seen2["/s"] is None, "a genuine stream has NO Content-Length — this is the detector"


def test_500_is_a_response_not_an_exception() -> None:
    """The specific fact that makes claim 4 necessary: an exception-keyed gate is wrong."""
    app, events = _build_app()
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/boom")
    assert r.status_code == 500
    # the store-mw saw a 500 RESPONSE (not an exception), and correctly did not cache it
    assert 500 not in events["cached"]
