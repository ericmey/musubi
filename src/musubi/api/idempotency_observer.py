"""Store-only idempotency observer (Phase B).

A pure-ASGI middleware. It makes NO replay/conflict/in-flight decision — the routed dependency
(:func:`musubi.api.idempotency_dependency.make_idempotency_dependency`) does that BEFORE the
handler runs, and for an ACQUIRED lease publishes an
:class:`~musubi.api.idempotency.IdempotencyRequestState` (plus the exact lease cache it used) onto
``request.state``. This observer only completes that lease:

  - it captures the terminal response (status, raw headers, exact body bytes) as the app streams;
  - on a CLEAN terminal 2xx for an acquired lease it stores the immutable
    :class:`~musubi.api.idempotency.CompletedResponse` — that entry IS the replay cache, so it is
    NOT released;
  - on EVERY other exit — non-2xx, a handler exception, a client/send failure, cancellation, or a
    store that itself raises — it releases the incomplete lease in ``finally`` so an acquired slot
    can never leak.

A store failure AFTER the client bytes are already committed must never become a raised request
failure: the client already has its response. It is logged (identity hash + correlation only —
never the identity or body), metered, and the incomplete lease is released so the NEXT retry
re-executes (no completed entry exists). The client response is left untouched and is NOT claimed
as a replay.

Why pure ASGI and not ``@app.middleware`` / ``BaseHTTPMiddleware``: the latter collapses the
downstream response into a lossy ``_StreamingResponse``, losing exact bytes/headers. A pure-ASGI
send-wrapper observes ``http.response.start`` / ``http.response.body`` events verbatim.

The observer only buffers a response body for requests that could possibly carry a lease — a write
method AND an ``Idempotency-Key`` header — so read traffic is never buffered.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from musubi.api.idempotency import CompletedResponse, IdempotencyLeaseCache, IdempotencyRequestState
from musubi.observability.registry import default_registry

log = logging.getLogger("musubi.api.idempotency")

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_store_failure_total = default_registry().counter(
    "musubi_idempotency_store_failures_total",
    "Idempotency completed-response stores that failed AFTER the client response was committed "
    "(the lease is released and the next retry re-executes; the client response is unchanged).",
)


def _identity_hash(identity: tuple[Any, ...]) -> str:
    """A short, non-reversible tag for the lease identity — safe to log (the identity itself
    carries principal + namespace + key and must never be logged in the clear)."""
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:16]


def _lease(scope: Scope) -> tuple[IdempotencyRequestState, IdempotencyLeaseCache] | None:
    """The (state, cache) the dependency published for an acquired lease, or ``None`` — for a
    replay/conflict/in-flight/non-idempotent request the dependency never publishes state, so the
    observer is a no-op."""
    state = scope.get("state") or {}
    idem = state.get("idem")
    cache = state.get("idem_cache")
    if isinstance(idem, IdempotencyRequestState) and isinstance(cache, IdempotencyLeaseCache):
        return idem, cache
    return None


def _is_candidate(scope: Scope) -> bool:
    """Only write requests carrying an ``Idempotency-Key`` can ever acquire a lease — buffer the
    response for those alone, never for reads."""
    if scope.get("method") not in _WRITE_METHODS:
        return False
    return any(k.lower() == b"idempotency-key" for k, _ in scope.get("headers", []))


class IdempotencyObserver:
    """Store-only ASGI wrapper. Mount OUTERMOST so it observes the exact terminal response the
    client receives."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_candidate(scope):
            await self.app(scope, receive, send)
            return

        status: int | None = None
        raw_headers: tuple[tuple[bytes, bytes], ...] = ()
        chunks: list[bytes] = []
        terminal = False

        async def _send(message: Message) -> None:
            nonlocal status, raw_headers, chunks, terminal
            if message["type"] == "http.response.start":
                status = message["status"]
                raw_headers = tuple(
                    (bytes(k), bytes(v)) for k, v in message.get("headers", [])
                )
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    chunks.append(bytes(body))
                if not message.get("more_body", False):
                    terminal = True
            await send(message)

        stored = False
        try:
            await self.app(scope, receive, _send)
            lease = _lease(scope)
            if lease is not None and terminal and status is not None and 200 <= status < 300:
                idem, cache = lease
                completed = CompletedResponse(
                    status=status, raw_headers=raw_headers, body=b"".join(chunks)
                )
                try:
                    cache.store(idem.identity, idem.owner, response=completed)
                    stored = True  # the completed entry IS the replay cache — do NOT release it
                except Exception:
                    # The client bytes are already committed. Never raise / alter the response /
                    # claim a replay. Log (identity hash + correlation only) + meter, and fall
                    # through to the finally release so the next retry re-executes.
                    _store_failure_total.inc()
                    log.exception(
                        "idempotency completed-response store failed after response send",
                        extra={
                            "idem_identity_hash": _identity_hash(idem.identity),
                            "correlation_id": (scope.get("state") or {}).get("correlation_id"),
                        },
                    )
        finally:
            lease = _lease(scope)
            if lease is not None and not stored:
                idem, cache = lease
                try:
                    cache.release(idem.identity, idem.owner)
                except Exception:
                    # Release must never surface as a request failure either — the response has
                    # already gone (or is being torn down). Log and move on; a stale in-flight
                    # lease is reclaimed by the stale-owner window.
                    log.exception(
                        "idempotency lease release failed",
                        extra={"idem_identity_hash": _identity_hash(idem.identity)},
                    )


__all__ = ["IdempotencyObserver"]
