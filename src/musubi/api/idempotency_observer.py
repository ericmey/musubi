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
  - when a durable receipt store is configured, it commits the receipt BEFORE releasing any 2xx
    bytes to the client;
  - on EVERY other exit — non-2xx, a handler exception, a client/send failure, cancellation, or an
    ordinary replay-cache store failure — it releases the incomplete lease in ``finally``.

A durable-receipt failure occurs before client success is committed: it becomes a typed 503 and
holds the process-local lease fail-closed so a retry cannot duplicate the mutation. Durable mode
also publishes its ordinary replay entry before sending success, so an immediate transport retry
replays safely. Ordinary mode keeps the existing post-send replay-cache behavior.

Why pure ASGI and not ``@app.middleware`` / ``BaseHTTPMiddleware``: the latter collapses the
downstream response into a lossy ``_StreamingResponse``, losing exact bytes/headers. A pure-ASGI
send-wrapper observes ``http.response.start`` / ``http.response.body`` events verbatim.

The observer only buffers a response body for requests that could possibly carry a lease — a write
method AND an ``Idempotency-Key`` header — so read traffic is never buffered.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from musubi.api.idempotency import CompletedResponse, IdempotencyLeaseCache, IdempotencyRequestState
from musubi.observability.registry import default_registry

log = logging.getLogger("musubi.api.idempotency")

_store_failure_total = default_registry().counter(
    "musubi_idempotency_store_failures_total",
    "Idempotency completed-response replay-cache stores that failed. Ordinary mode releases the "
    "lease after the client response; durable mode retains it fail-closed because its receipt "
    "already exists.",
)

_receipt_store_failure_total = default_registry().counter(
    "musubi_idempotency_receipt_store_failures_total",
    "Durable idempotency receipt commits that failed BEFORE the client success response was sent "
    "(a typed 503 is returned and the process-local lease remains held fail-closed).",
)


def _identity_hash(identity: tuple[Any, ...]) -> str:
    """A short, non-reversible tag for the lease identity — safe to log (the identity itself
    carries principal + namespace + key and must never be logged in the clear)."""
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:16]


def _lease(
    scope: Scope,
) -> (
    tuple[IdempotencyRequestState, IdempotencyLeaseCache, Any | None, str | None, str | None] | None
):
    """The (state, cache) the dependency published for an acquired lease, or ``None`` — for a
    replay/conflict/in-flight/non-idempotent request the dependency never publishes state, so the
    observer is a no-op."""
    state = scope.get("state") or {}
    idem = state.get("idem")
    cache = state.get("idem_cache")
    if isinstance(idem, IdempotencyRequestState) and isinstance(cache, IdempotencyLeaseCache):
        return (
            idem,
            cache,
            state.get("idem_receipt_store"),
            state.get("idem_namespace"),
            state.get("idem_operation"),
        )
    return None


class IdempotencyObserver:
    """Store-only ASGI wrapper. Mount OUTERMOST so it observes the exact terminal response the
    client receives."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        active = False  # buffer ONLY when the routed dependency acquired a lease for this request
        status: int | None = None
        raw_headers: tuple[tuple[bytes, bytes], ...] = ()
        messages: list[Message] = []
        chunks: list[bytes] = []
        terminal = False
        hold_lease = False

        async def _send(message: Message) -> None:
            nonlocal active, status, raw_headers, chunks, terminal
            if message["type"] == "http.response.start":
                # Eligibility is only knowable HERE: the routed idempotency dependency publishes the
                # acquired-lease state during dependency resolution — strictly before the handler
                # produces a response.start — so its presence now means this request holds a lease
                # and its (small, JSON) response must be captured for replay. An INELIGIBLE route
                # (no idempotency dependency, e.g. the retrieve StreamingResponse) never publishes
                # that state, so `active` stays False and NOT ONE body chunk is retained — the
                # stream flows straight through. Gating on method + header instead would buffer any
                # keyed POST to an ineligible streaming route: an unbounded-memory DoS.
                active = _lease(scope) is not None
                if active:
                    status = message["status"]
                    raw_headers = tuple((bytes(k), bytes(v)) for k, v in message.get("headers", []))
                    messages.append(message)
            elif message["type"] == "http.response.body" and active:
                messages.append(message)
                body = message.get("body", b"")
                if body:
                    chunks.append(bytes(body))
                if not message.get("more_body", False):
                    terminal = True
            if not active:
                await send(message)

        stored = False
        try:
            await self.app(scope, receive, _send)
            lease = _lease(scope)
            if lease is not None and terminal and status is not None and 200 <= status < 300:
                idem, cache, receipt_store, namespace, operation = lease
                completed = CompletedResponse(
                    status=status, raw_headers=raw_headers, body=b"".join(chunks)
                )
                if receipt_store is not None:
                    try:
                        if not isinstance(namespace, str) or not isinstance(operation, str):
                            raise RuntimeError("idempotency receipt scope is incomplete")
                        receipt_store.store(
                            identity=idem.identity,
                            digest=idem.digest,
                            response=completed,
                            namespace=namespace,
                            operation=operation,
                        )
                    except Exception:
                        hold_lease = True
                        _receipt_store_failure_total.inc()
                        log.exception(
                            "durable idempotency receipt commit failed before response send",
                            extra={
                                "idem_identity_hash": _identity_hash(idem.identity),
                                "correlation_id": (scope.get("state") or {}).get("correlation_id"),
                            },
                        )
                        error_body = json.dumps(
                            {
                                "error": {
                                    "code": "BACKEND_UNAVAILABLE",
                                    "detail": "durable idempotency receipt commit failed",
                                    "hint": "do not re-POST; inspect the receipt status",
                                }
                            },
                            separators=(",", ":"),
                        ).encode()
                        error_headers = [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(error_body)).encode()),
                        ]
                        correlation_id = (scope.get("state") or {}).get("correlation_id")
                        if isinstance(correlation_id, str):
                            error_headers.append((b"x-request-id", correlation_id.encode()))
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 503,
                                "headers": error_headers,
                            }
                        )
                        await send(
                            {"type": "http.response.body", "body": error_body, "more_body": False}
                        )
                        return

                    # Publish the ordinary replay entry before success bytes too. If the transport
                    # then fails, an automatic immediate POST retry replays instead of executing a
                    # second mutation. The durable receipt remains the restart/expiry recovery path.
                    try:
                        cache.store(idem.identity, idem.owner, response=completed)
                        stored = True
                    except Exception:
                        # The durable receipt is authoritative, but without a replay entry an
                        # immediate POST retry could execute again. Keep the lease held fail-closed;
                        # the client can recover through receipt lookup.
                        hold_lease = True
                        _store_failure_total.inc()
                        log.exception(
                            "idempotency replay-cache store failed after durable receipt commit",
                            extra={
                                "idem_identity_hash": _identity_hash(idem.identity),
                                "correlation_id": (scope.get("state") or {}).get("correlation_id"),
                            },
                        )
                        error_body = json.dumps(
                            {
                                "error": {
                                    "code": "BACKEND_UNAVAILABLE",
                                    "detail": "durable idempotency replay publication failed",
                                    "hint": "do not re-POST; inspect the receipt status",
                                }
                            },
                            separators=(",", ":"),
                        ).encode()
                        error_headers = [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(error_body)).encode()),
                        ]
                        correlation_id = (scope.get("state") or {}).get("correlation_id")
                        if isinstance(correlation_id, str):
                            error_headers.append((b"x-request-id", correlation_id.encode()))
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 503,
                                "headers": error_headers,
                            }
                        )
                        await send(
                            {"type": "http.response.body", "body": error_body, "more_body": False}
                        )
                        return

                # A durable receipt and replay entry (when requested) now exist before any success
                # bytes leave this process. Ordinary mode retains its existing post-send behavior.
                for message in messages:
                    await send(message)
                if receipt_store is None:
                    try:
                        cache.store(idem.identity, idem.owner, response=completed)
                        stored = True  # completed entry IS the replay cache — do NOT release it
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
            elif active:
                for message in messages:
                    await send(message)
        finally:
            lease = _lease(scope)
            if lease is not None and not stored and not hold_lease:
                idem, cache, _receipt_store, _namespace, _operation = lease
                try:
                    cache.release(idem.identity, idem.owner)
                except Exception:
                    # Release must never surface as a request failure either — the response has
                    # already gone (or is being torn down). Log and move on. (A live lease is only
                    # ever freed by its owner's request exit; there is no time-based reclaim, so a
                    # failed release fails closed — the key 409s until the process restarts, which
                    # is safer than risking a duplicate mutation.)
                    log.exception(
                        "idempotency lease release failed",
                        extra={"idem_identity_hash": _identity_hash(idem.identity)},
                    )


__all__ = ["IdempotencyObserver"]
