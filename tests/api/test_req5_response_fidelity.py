"""REQ-5 — the idempotency replay must reproduce the response FAITHFULLY, no lossy rebuild.

Yua req 5 (21:18): "preserve raw duplicate headers, cookies, media type, background task
semantics, and exact response bytes — no lossy Response reconstruction."

These reds run against the REAL middleware: a custom write route is mounted on a real
`create_app` instance (every POST goes through the idempotency middleware in app.py), primed
with an Idempotency-Key, then replayed. Each red asserts the SECURE/faithful behaviour and
FAILS today because the current pipeline is lossy:

  store  (app.py ~296): reads the body, `json.loads` it to a dict, `cache.store(response_body=
          dict)`, and rebuilds with `headers=dict(response.headers)` — the dict() COLLAPSES
          duplicate headers, and only a JSON dict body is kept.
  replay (app.py ~250): `Response(json.dumps(cached_body), media_type="application/json")` +
          only `X-Idempotent-Replay` — so it FORCES json media, RE-SERIALISES the bytes, and
          carries NONE of the original headers/cookies. Non-JSON 2xx is not cached at all, so it
          silently RE-EXECUTES on the "replay".

Observed on the real path (documented in the reds):
  cookie sess=secret -> gone on replay · X-Multi:['one','two'] -> ['one'] on first, [] on replay
  body {"z":1,"a":2} -> {"z": 1, "a": 2} (bytes changed) · text/csv 2xx -> never cached, re-runs

`xfail(strict=True)` on the holes; plain controls for what must stay true (a normal JSON replay
still works; a background task runs exactly once and is NOT re-run on replay). Tests/docs only,
no src.

    uv run pytest tests/api/test_req5_response_fidelity.py -v
"""

from __future__ import annotations

import pytest
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.testclient import TestClient

from musubi.api.app import create_app
from musubi.settings import Settings

IDEM = "Idempotency-Key"
REPLAY = "X-Idempotent-Replay"


def _client_with(api_settings: Settings, route: str, handler) -> TestClient:
    app = create_app(settings=api_settings)
    app.add_api_route(route, handler, methods=["POST"])
    return TestClient(app)


def _prime_and_replay(client: TestClient, route: str, key: str) -> tuple:
    h = {IDEM: key}
    first = client.post(route, headers=h)
    replay = client.post(route, headers=h)
    return first, replay


# --------------------------------------------------------------------------- #
# reds — faithful replay (fail today)
# --------------------------------------------------------------------------- #

@pytest.mark.xfail(strict=True, reason="REQ-5: replay drops Set-Cookie — lossy rebuild, fix pending")
def test_replay_preserves_set_cookie(api_settings: Settings) -> None:
    async def h() -> Response:
        r = JSONResponse({"ok": 1})
        r.set_cookie("sess", "secret")
        return r
    c = _client_with(api_settings, "/_fid/cookie", h)
    _first, replay = _prime_and_replay(c, "/_fid/cookie", "fid-cookie")
    assert replay.headers.get(REPLAY) == "true", "precondition: second call must be a replay"
    assert replay.headers.get_list("set-cookie"), "replay dropped Set-Cookie — a faithful replay must keep it"


@pytest.mark.xfail(strict=True, reason="REQ-5: replay drops custom response headers — fix pending")
def test_replay_preserves_custom_header(api_settings: Settings) -> None:
    async def h() -> Response:
        r = JSONResponse({"ok": 1})
        r.headers["X-Trace"] = "abc123"
        return r
    c = _client_with(api_settings, "/_fid/header", h)
    _first, replay = _prime_and_replay(c, "/_fid/header", "fid-header")
    assert replay.headers.get(REPLAY) == "true"
    assert replay.headers.get("x-trace") == "abc123", "replay dropped a custom header"


@pytest.mark.xfail(strict=True, reason="REQ-5: dict(headers) collapses duplicate headers even on the first response — fix pending")
def test_first_response_preserves_duplicate_headers(api_settings: Settings) -> None:
    async def h() -> Response:
        r = JSONResponse({"ok": 1})
        r.headers.append("X-Multi", "one")
        r.headers.append("X-Multi", "two")
        return r
    c = _client_with(api_settings, "/_fid/dup", h)
    first, _replay = _prime_and_replay(c, "/_fid/dup", "fid-dup")
    assert first.headers.get_list("x-multi") == ["one", "two"], (
        f"duplicate headers collapsed to {first.headers.get_list('x-multi')} — dict(headers) is lossy")


@pytest.mark.xfail(strict=True, reason="REQ-5: replay re-serialises the body, changing the bytes — fix pending")
def test_replay_preserves_exact_body_bytes(api_settings: Settings) -> None:
    # compact separators + non-sorted keys; a re-serialisation will change the bytes.
    async def h() -> Response:
        return Response(content=b'{"z":1,"a":2}', media_type="application/json")
    c = _client_with(api_settings, "/_fid/bytes", h)
    first, replay = _prime_and_replay(c, "/_fid/bytes", "fid-bytes")
    assert replay.headers.get(REPLAY) == "true"
    assert replay.content == first.content, (
        f"replay bytes {replay.content!r} != original {first.content!r} — re-serialisation is lossy")


@pytest.mark.xfail(strict=True, reason="REQ-5: non-JSON 2xx is not cached — replay re-executes and loses media type — fix pending")
def test_non_json_response_is_idempotent_and_keeps_media(api_settings: Settings) -> None:
    runs = {"n": 0}

    async def h() -> Response:
        runs["n"] += 1
        return PlainTextResponse("col1,col2\n1,2\n", media_type="text/csv")

    c = _client_with(api_settings, "/_fid/csv", h)
    _first, replay = _prime_and_replay(c, "/_fid/csv", "fid-csv")
    # SECURE: the second identical write must be a replay (handler ran once), with media kept.
    assert replay.headers.get(REPLAY) == "true", (
        f"non-JSON write re-executed instead of replaying (handler ran {runs['n']}x) — not idempotent")
    assert replay.headers.get("content-type", "").startswith("text/csv"), "replay lost the media type"


# --------------------------------------------------------------------------- #
# controls — must hold before AND after the fix
# --------------------------------------------------------------------------- #

def test_json_replay_still_works(api_settings: Settings) -> None:
    async def h() -> Response:
        return JSONResponse({"ok": 1})
    c = _client_with(api_settings, "/_fid/json", h)
    _first, replay = _prime_and_replay(c, "/_fid/json", "fid-json")
    assert replay.headers.get(REPLAY) == "true", "the happy-path JSON replay must keep working"


def test_background_task_runs_once_and_not_on_replay(api_settings: Settings) -> None:
    """Background-task semantics: the side effect runs exactly once (on the original) and is NOT
    re-executed on replay. The faithful-replay fix must NOT re-attach the task to the replay."""
    runs = {"n": 0}

    async def h() -> Response:
        return JSONResponse({"ok": 1}, background=BackgroundTask(lambda: runs.__setitem__("n", runs["n"] + 1)))

    c = _client_with(api_settings, "/_fid/bg", h)
    with c:
        c.post("/_fid/bg", headers={IDEM: "fid-bg"})
        after_first = runs["n"]
        replay = c.post("/_fid/bg", headers={IDEM: "fid-bg"})
    assert after_first == 1, f"background task must run once on the original, ran {after_first}x"
    assert runs["n"] == 1, f"background task re-ran on replay (now {runs['n']}x) — replay must not repeat side effects"
    assert replay.headers.get(REPLAY) == "true"
