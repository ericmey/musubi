"""Phase B — the routed post-authz idempotency pipeline, end-to-end on the REAL capture routes.

The dependency (:mod:`musubi.api.idempotency_dependency`) + the store-only observer
(:class:`~musubi.api.idempotency_observer.IdempotencyObserver`) wired onto POST /v1/episodic,
/v1/episodic/batch, /v1/curated. These are the exact real-route concurrency and
cross-route/principal/namespace/body tests Yua required for the observer+wiring commit; SEC-002 and
IDEM-001 assert the security holes are closed, this file proves the pipeline's positive behaviour.

The final two blocks unit-test the observer's failure/exit contract directly (req 4/5): release on
every non-store exit, and a store failure AFTER the client bytes are committed that is swallowed,
metered, and releases the lease so the retry re-executes.

    uv run pytest tests/api/test_idempotency_pipeline_integration.py -v
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from musubi.api.idempotency import CompletedResponse, IdempotencyLeaseCache, IdempotencyRequestState
from musubi.api.idempotency_observer import IdempotencyObserver
from musubi.settings import Settings
from tests.api.conftest import mint_token

CAPTURE = "/v1/episodic"
CURATED = "/v1/curated"
IDEM = "Idempotency-Key"
REPLAY = "X-Idempotent-Replay"
NS = "eric/claude-code/episodic"


def _body(content: str = "pipeline probe") -> dict[str, Any]:
    return {"namespace": NS, "content": content, "tags": ["kind:episode"], "importance": 3}


def _auth(token: str, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", IDEM: key}


# --------------------------------------------------------------------------- #
# positive: the owner replays its own write, byte-exact
# --------------------------------------------------------------------------- #


def test_owner_replay_is_byte_exact_same_object_id(client: TestClient, valid_token: str) -> None:
    first = client.post(CAPTURE, json=_body(), headers=_auth(valid_token, "owner-replay"))
    assert first.status_code == 202 and first.headers.get(REPLAY) != "true"
    oid = first.json()["object_id"]

    second = client.post(CAPTURE, json=_body(), headers=_auth(valid_token, "owner-replay"))
    assert second.status_code == 202, "the rightful owner must be replayed, not re-executed"
    assert second.headers.get(REPLAY) == "true", "a replay must carry the marker"
    assert second.json() == first.json(), "replay must serve the exact cached body"
    assert second.json()["object_id"] == oid


def test_conflict_same_key_different_body_is_409(client: TestClient, valid_token: str) -> None:
    a = client.post(CAPTURE, json=_body("original"), headers=_auth(valid_token, "conflict-key"))
    assert a.status_code == 202
    b = client.post(CAPTURE, json=_body("DIFFERENT"), headers=_auth(valid_token, "conflict-key"))
    assert b.status_code == 409 and b.headers.get(REPLAY) != "true"


# --------------------------------------------------------------------------- #
# real-route concurrency: N simultaneous identical requests → the mutation runs exactly ONCE
# --------------------------------------------------------------------------- #


def test_real_route_concurrency_executes_exactly_once(
    client: TestClient, valid_token: str
) -> None:
    n = 12
    headers = _auth(valid_token, "race-key")
    body = _body("race")

    def fire(_: int) -> tuple[int, str | None, str | None]:
        r = client.post(CAPTURE, json=body, headers=headers)
        oid = r.json().get("object_id") if r.headers.get("content-type", "").startswith(
            "application/json"
        ) else None
        return r.status_code, r.headers.get(REPLAY), oid

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(fire, range(n)))

    # Every 2xx must describe the SAME single object — one distinct object_id across all successes.
    success_ids = {oid for code, _, oid in results if code == 202 and oid is not None}
    assert len(success_ids) == 1, (
        f"{n} concurrent identical requests produced {len(success_ids)} distinct object_ids "
        f"(must be exactly 1 — the mutation ran more than once): {success_ids}"
    )
    # No caller may execute a SECOND mutation: non-2xx are the in-flight/duplicate 409s.
    for code, replay_hdr, _ in results:
        assert code in (202, 409), f"unexpected status under concurrency: {code}"
        if code == 409:
            assert replay_hdr != "true"


# --------------------------------------------------------------------------- #
# identity scoping: replay must NOT cross route / principal / namespace / body
# --------------------------------------------------------------------------- #


def test_no_replay_across_routes(client: TestClient, api_settings: Settings) -> None:
    """Same key + a body valid for BOTH shapes must not let /v1/curated be served /v1/episodic's
    cached response — identity binds the operation. (The curated schema differs, so the second call
    is handled fresh / 422s; either way it is never episodic's replay.)"""
    token = mint_token(
        api_settings,
        scopes=["eric/claude-code/episodic:rw", "eric/claude-code/curated:rw"],
        presence="eric/claude-code",
    )
    ep = client.post(CAPTURE, json=_body(), headers=_auth(token, "cross-route"))
    assert ep.status_code == 202 and ep.headers.get(REPLAY) != "true"
    cur = client.post(CURATED, json=_body(), headers=_auth(token, "cross-route"))
    assert cur.headers.get(REPLAY) != "true", "curated was served episodic's cached response"


def test_no_replay_across_principals(client: TestClient, api_settings: Settings) -> None:
    """Same key + same body + same namespace, but a DIFFERENT principal (presence) must be handled
    fresh, never replayed the first principal's write — identity binds (issuer, subject, presence)."""
    tok_a = mint_token(api_settings, scopes=[f"{NS}:rw"], presence="eric/claude-code")
    tok_b = mint_token(api_settings, scopes=[f"{NS}:rw"], presence="eric/other-agent")
    a = client.post(CAPTURE, json=_body(), headers=_auth(tok_a, "cross-principal"))
    assert a.status_code == 202 and a.headers.get(REPLAY) != "true"
    b = client.post(CAPTURE, json=_body(), headers=_auth(tok_b, "cross-principal"))
    assert b.status_code == 202, "a different principal must execute its own write"
    # The absence of the replay marker is the proof of isolation: B was executed fresh (through
    # auth + handler), never served A's cached response. (The object_id can match A's — the
    # episodic plane is content-addressed, so identical content yields the same deterministic id;
    # that is dedup, NOT a replay, and would carry no X-Idempotent-Replay header.)
    assert b.headers.get(REPLAY) != "true", "a different principal must NOT replay another's write"


def test_no_replay_across_namespaces(client: TestClient, api_settings: Settings) -> None:
    """Same key, same principal, DIFFERENT namespace — identity binds the authorized namespace, so
    the second write is fresh, not a replay of the first namespace's response."""
    token = mint_token(
        api_settings,
        scopes=["eric/claude-code/episodic:rw", "eric/other/episodic:rw"],
        presence="eric/claude-code",
    )
    a = client.post(
        CAPTURE,
        json={"namespace": "eric/claude-code/episodic", "content": "x", "importance": 3},
        headers=_auth(token, "cross-ns"),
    )
    assert a.status_code == 202 and a.headers.get(REPLAY) != "true"
    b = client.post(
        CAPTURE,
        json={"namespace": "eric/other/episodic", "content": "x", "importance": 3},
        headers=_auth(token, "cross-ns"),
    )
    assert b.headers.get(REPLAY) != "true", "a different namespace must not replay another's write"


# --------------------------------------------------------------------------- #
# observer contract (req 4/5), unit-level over a controllable ASGI app + cache
# --------------------------------------------------------------------------- #

_ID = ("issuer", "subject", "presence", "POST", "op", "ns", "k")
_D = bytes(32)


def _run(cache: IdempotencyLeaseCache, status: int) -> list[Message]:
    """Drive the observer once over a tiny inner app that publishes the acquired lease (as the
    dependency would) against ``cache`` and returns ``status``. Returns the client-received
    messages. The caller pre-acquires the lease on ``cache``."""
    sent: list[Message] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        scope["state"]["idem"] = IdempotencyRequestState(identity=_ID, owner="o1", digest=_D)
        scope["state"]["idem_cache"] = cache
        await send({"type": "http.response.start", "status": status, "headers": [(b"x", b"y")]})
        await send({"type": "http.response.body", "body": b'{"ok":1}', "more_body": False})

    observer = IdempotencyObserver(inner)

    async def go() -> None:
        scope: Scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"idempotency-key", b"k")],
            "state": {},
        }

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m: Message) -> None:
            sent.append(m)

        await observer(scope, receive, send)

    asyncio.run(go())
    return sent


def test_observer_stores_on_clean_2xx_then_replayable() -> None:
    cache = IdempotencyLeaseCache()
    cache.acquire(_ID, "o1", digest=_D)  # the dependency acquired; observer must complete it
    _run(cache, status=202)
    # a completed entry now exists → a fresh caller replays it (hit), the lease is NOT leaked
    status, completed = cache.acquire(_ID, "o2", digest=_D)
    assert status == "hit" and completed is not None and completed.status == 202


def test_observer_releases_on_non_2xx() -> None:
    cache = IdempotencyLeaseCache()
    cache.acquire(_ID, "o1", digest=_D)
    _run(cache, status=500)
    # non-2xx → lease released → a fresh caller ACQUIRES (no completed entry, no wedged lease)
    assert cache.acquire(_ID, "o2", digest=_D)[0] == "acquired"


def test_observer_store_failure_after_send_is_swallowed_and_retry_reexecutes() -> None:
    """Req 5: a store that raises AFTER the client bytes are committed must not raise, must not
    alter the client response, and must release the incomplete lease so the retry re-executes
    (no completed entry). The client already got its 2xx."""

    class _FailingStore(IdempotencyLeaseCache):
        def store(self, identity: Any, owner: str, *, response: CompletedResponse) -> None:
            raise RuntimeError("simulated store backend failure")

    cache = _FailingStore()
    cache.acquire(_ID, "o1", digest=_D)
    sent = _run(cache, status=202)  # must NOT raise despite store() raising

    # 1. the client response is unchanged and NOT claimed as a replay
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 202
    assert not any(k == b"x-idempotent-replay" for k, _ in start["headers"])
    # 2. the incomplete lease was released → the retry re-executes (a fresh acquire, no hit)
    assert cache.acquire(_ID, "o2", digest=_D)[0] == "acquired", (
        "after a post-send store failure the lease must be released so the retry re-executes"
    )
