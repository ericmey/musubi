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
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from musubi.api.dependencies import get_episodic_plane
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
# real-route concurrency: N simultaneous identical requests → the MUTATION runs exactly ONCE
# --------------------------------------------------------------------------- #


class _CountingEpisodicPlane:
    """An EpisodicPlane stand-in that COUNTS ``create`` calls and holds the FIRST call in-flight
    deterministically via two Events: ``entered`` fires when a create begins; the create then parks
    on ``release`` (offloaded to a worker thread so the event loop stays free to serve the racing
    retries). Counting the mutation directly is the only sound exactly-once proof — EpisodicPlane is
    content-addressed, so N executions of an identical body all return the SAME object_id, and an
    object_id-distinctness check would pass even when the mutation ran N times."""

    def __init__(self) -> None:
        self.creates = 0
        self._lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()

    async def create(self, memory: Any, *, preserve_created_at: bool = False) -> Any:
        with self._lock:
            self.creates += 1
            first = self.creates == 1
        # ONLY the first create parks (holding the winner's lease in-flight); any subsequent create
        # returns immediately, so a bypass-acquire red where every retry reaches create cannot
        # deadlock — it just records creates > 1 and returns.
        if first:
            self.entered.set()
            # Park WITHOUT blocking the event loop (offload the wait to a worker thread) so the 11
            # retry requests still run and race the held lease.
            await asyncio.get_event_loop().run_in_executor(None, self.release.wait)
        return SimpleNamespace(object_id="fixed-object-id", state="provisional")


def test_real_route_concurrency_mutation_runs_exactly_once(
    app_factory: Any, valid_token: str
) -> None:
    """A winner capture request whose ``create`` is held in-flight, then 11 identical retries fired
    while its lease is held: all 11 must be visible in_flight 409s and ``plane.create`` must have
    run EXACTLY ONCE (the lease acquire is the gate). Deterministic — the overlap is proven by Events,
    not scheduler timing. Asserts the real create call count through the real route + dependency +
    observer (only the plane is a counting spy). Red-proven by bypassing acquire (an
    always-``acquired`` cache), which lets every retry reach ``create`` → creates > 1."""
    plane = _CountingEpisodicPlane()
    app_factory.dependency_overrides[get_episodic_plane] = lambda: plane

    headers = _auth(valid_token, "race-key")
    body = _body("race")

    with TestClient(app_factory) as client, ThreadPoolExecutor(max_workers=12) as pool:
        winner = pool.submit(lambda: client.post(CAPTURE, json=body, headers=headers))
        try:
            assert plane.entered.wait(timeout=5.0), "winner never entered plane.create"
            # The winner's lease is now held IN-FLIGHT. Fire 11 identical retries — every one must
            # be a visible in_flight conflict and NONE may reach the mutation.
            retries = [
                pool.submit(lambda: client.post(CAPTURE, json=body, headers=headers))
                for _ in range(11)
            ]
            retry_codes = [f.result(timeout=10.0).status_code for f in retries]
            assert retry_codes == [409] * 11, (
                f"retries must all be in_flight 409s, got {retry_codes}"
            )
            assert plane.creates == 1, (
                f"a retry executed the mutation while the winner held the lease (create ran "
                f"{plane.creates}x) — the acquire gate failed"
            )
        finally:
            # ALWAYS free the winner, even on assertion failure, so the executor can shut down
            # instead of hanging on a parked request.
            plane.release.set()

        won = winner.result(timeout=10.0)
        assert won.status_code == 202 and won.headers.get(REPLAY) != "true"
        assert plane.creates == 1, "the winner must be the only execution"


# --------------------------------------------------------------------------- #
# B1 — the observer must NOT buffer an INELIGIBLE response (no acquired lease), even one carrying an
# Idempotency-Key on a streaming route. Buffering it would reintroduce an unbounded-memory DoS.
# --------------------------------------------------------------------------- #


def test_observer_does_not_buffer_ineligible_stream() -> None:
    """An ineligible route (no idempotency dependency → no published lease state) that streams a
    large body while carrying an Idempotency-Key must flow through the observer WITHOUT retention.
    Measured, not asserted-by-output: tracemalloc peak during the stream must stay a small fraction
    of the streamed size. Red-proven by the method+header candidacy gate, which buffers the whole
    stream (peak ≈ full size)."""
    chunk_size = 256 * 1024  # 256 KiB
    n_chunks = 128  # 32 MiB total
    delivered = 0

    async def streaming_app(scope: Scope, receive: Receive, send: Send) -> None:
        # NOTE: this app publishes NO idem state → ineligible, exactly like /v1/retrieve/stream.
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/octet-stream")],
            }
        )
        for i in range(n_chunks):
            # A DISTINCT object per chunk (fresh allocation, not one reused buffer) so that a
            # buffering observer genuinely accumulates memory — otherwise 128 references to one
            # immutable bytes object would cost only one chunk and mask the DoS.
            body = bytes([i % 256]) * chunk_size
            await send({"type": "http.response.body", "body": body, "more_body": i < n_chunks - 1})

    observer = IdempotencyObserver(streaming_app)

    async def drive() -> int:
        nonlocal delivered
        scope: Scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/retrieve/stream",
            "headers": [(b"idempotency-key", b"k"), (b"content-type", b"application/json")],
            "state": {},
        }

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m: Message) -> None:
            nonlocal delivered
            if m["type"] == "http.response.body":
                delivered += len(
                    m.get("body", b"")
                )  # count LENGTH only — the harness retains nothing

        await observer(scope, receive, send)
        return delivered

    tracemalloc.start()
    try:
        total = asyncio.run(drive())
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    streamed = n_chunks * chunk_size
    assert total == streamed, "every chunk must be delivered downstream (the stream is not dropped)"
    # Non-retention: peak must be a small fraction of the streamed size. A buffering observer peaks
    # at ≈ the full 32 MiB; a non-retaining one peaks at ≈ one chunk + overhead.
    assert peak < streamed // 8, (
        f"observer retained {peak} bytes streaming {streamed} — an ineligible stream must NOT be "
        f"buffered (memory DoS)"
    )


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
