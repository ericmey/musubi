"""Idempotency-key cache for write endpoints.

Per [[07-interfaces/canonical-api]] § Idempotency, a POST may carry an
``Idempotency-Key`` header. Two requests with the same key + same body
return the same response (cache hit, marked via the
``X-Idempotent-Replay: true`` header). Same key + different body is a
``CONFLICT`` (409). The cache TTL is 24h.

This implementation is in-memory and process-local — fine for a single
worker. A deployment with multiple workers will move this to Redis or a
shared cache (a future ``slice-api-v0-write-distributed-idempotency``);
the swap-out is a Protocol-keyed dependency so callers don't change.

The cache is exposed to tests via ``_GLOBAL_CACHE`` so they can
``expire_for_test(key)`` to exercise the TTL path without sleeping
24h.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

_DEFAULT_TTL_S = 24 * 3600

# The four exhaustive acquire outcomes. Typed so callers handle every case and an unknown status
# can never fall through as "acquired".
AcquireStatus = Literal["acquired", "in_flight", "hit", "conflict"]


@dataclass(frozen=True)
class CompletedResponse:
    """An immutable captured 2xx response, stored for replay. ``raw_headers`` preserves duplicate
    headers exactly; ``body`` is the exact response bytes. All fields are immutable, so the cached
    value can never be mutated by a caller and a replay exposes no shared mutable reference."""

    status: int
    raw_headers: tuple[tuple[bytes, bytes], ...]
    body: bytes


@dataclass(frozen=True)
class IdempotencyRequestState:
    """The per-request idempotency decision the dependency publishes for the store-only observer.
    Frozen so neither side can mutate it after the dependency establishes it."""

    identity: tuple[Any, ...]
    owner: str
    digest: bytes


@dataclass
class _Entry:
    """One cached idempotent response."""

    body_hash: str
    response_status: int
    response_body: dict[str, Any]
    expires_at: float


class IdempotencyCache:
    """In-memory TTL'd cache keyed by ``Idempotency-Key`` header value.

    Thread-safe. Cache hits return the original response body; same-key
    + different-body returns ``"conflict"``; misses return ``None``.
    """

    def __init__(self, *, ttl_s: float = _DEFAULT_TTL_S) -> None:
        self._ttl = ttl_s
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def hash_body(body: object) -> str:
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def lookup(self, key: str, body: object) -> tuple[str, dict[str, Any] | None, int | None]:
        """Return (status, response_body, response_status).

        ``status`` is one of:

        - ``"hit"``    — same key + same body; ``response_*`` populated.
        - ``"conflict"`` — same key + different body.
        - ``"miss"``   — no entry, or entry expired.
        """
        body_hash = self.hash_body(body)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.expires_at < time.time():
                if entry is not None:
                    del self._entries[key]
                return "miss", None, None
            if entry.body_hash != body_hash:
                return "conflict", None, None
            return "hit", entry.response_body, entry.response_status

    def store(
        self,
        key: str,
        body: object,
        *,
        response_status: int,
        response_body: dict[str, Any],
    ) -> None:
        body_hash = self.hash_body(body)
        with self._lock:
            self._entries[key] = _Entry(
                body_hash=body_hash,
                response_status=response_status,
                response_body=response_body,
                expires_at=time.time() + self._ttl,
            )

    def expire_for_test(self, key: str) -> None:
        """Force-expire an entry. Tests use this to cover the TTL path."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.expires_at = 0.0


_GLOBAL_CACHE = IdempotencyCache()
"""Process-wide cache instance. Tests reach in for ``expire_for_test``."""


def get_idempotency_cache() -> IdempotencyCache:
    """FastAPI dependency provider — overridable in tests."""
    return _GLOBAL_CACHE


# --------------------------------------------------------------------------- #
# Phase B — the routed post-authz idempotency LEASE cache.
# --------------------------------------------------------------------------- #


_DIGEST_LEN = 32  # SHA-256


@dataclass
class _LeaseEntry:
    owner: str
    digest: bytes  # non-optional: every eligible idempotent request carries a canonical digest
    done: bool = False
    completed_at: float | None = None
    response: CompletedResponse | None = None


class IdempotencyLeaseCache:
    """In-flight lease + completed-response store for post-authz idempotent writes.

    Semantics are independent of route parsing: the caller passes an opaque ``identity`` (built
    from the validated principal + operation + authorized namespace + key), an ``owner`` token,
    and the canonical request ``digest``. The single atomic ``acquire`` gates execution:

    - ``"acquired"`` — the caller owns the in-flight slot; it must ``store`` then the outer wrapper
      releases (or ``release`` on error/cancel).
    - ``"in_flight"`` — another owner holds a live lease; the caller 409s (never executes). A live
      lease is NEVER reclaimed by elapsed time — see below.
    - ``"hit"``      — a completed response with the SAME digest exists; replay it.
    - ``"conflict"`` — a completed response with a DIFFERENT digest exists (same key, different
      body); NO lease is acquired (no leak) — the caller returns 409.

    **No time-based reclaim of a live lease.** An in-flight lease is freed ONLY by its owner —
    ``store`` (success) or ``release`` (error/cancel). It is deliberately NOT reclaimed after any
    elapsed-time window: this cache is process-local and single-worker (REQ-10), so a process crash
    destroys the whole cache — a time-based "crash recovery" reclaim could never recover crash state
    and would only let a legitimately SLOW live request be re-executed into a duplicate mutation.
    Fail closed on a hung owner (the key 409s until the process restarts) is strictly safer than a
    double write. Durable, cross-process crash recovery is a named FUTURE concern
    (``slice-api-v0-write-distributed-idempotency``), not this in-memory primitive. Only COMPLETED
    entries expire — after ``ttl_s`` — so replay is bounded; the clock is injectable for
    deterministic tests and defaults to :func:`time.monotonic`.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = _DEFAULT_TTL_S,
        max_entries: int = 10_000,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be > 0")
        if ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")
        self._clock = clock
        self._ttl = ttl_s
        self._max = max_entries
        self._entries: OrderedDict[Any, _LeaseEntry] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _require_digest(digest: bytes) -> None:
        if not isinstance(digest, bytes) or len(digest) != _DIGEST_LEN:
            raise TypeError(
                f"digest must be {_DIGEST_LEN} bytes (SHA-256); got {type(digest).__name__}"
            )

    def acquire(
        self, identity: Any, owner: str, *, digest: bytes
    ) -> tuple[AcquireStatus, CompletedResponse | None]:
        """Atomic gate. Returns ``(status, completed_response)``; the response is populated only on
        a ``"hit"``. ``digest`` is the canonical request digest (mandatory) — a malformed/omitted
        digest raises and NO lease is created."""
        self._require_digest(digest)
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            entry = self._entries.get(identity)
            if entry is None:
                self._entries[identity] = _LeaseEntry(owner=owner, digest=digest)
                self._entries.move_to_end(identity)
                return "acquired", None
            if entry.done:
                if entry.digest != digest:
                    # same identity, DIFFERENT body — conflict; do NOT touch the lease (no leak).
                    return "conflict", None
                self._entries.move_to_end(identity)  # LRU touch on replay
                return "hit", entry.response
            # in-flight: another owner holds a LIVE lease. Never reclaim by elapsed time — a
            # slow-but-live request must not be re-executed into a duplicate mutation, and a
            # process-local cache cannot recover crash state anyway. Fail closed: 409 until the
            # owner completes (store) or releases (error/cancel).
            return "in_flight", None

    def store(self, identity: Any, owner: str, *, response: CompletedResponse) -> None:
        """Complete the caller's lease with its immutable response. Owner-only; raises otherwise.
        Marks the entry newest-by-COMPLETION and evicts oldest completed entries beyond
        ``max_entries``."""
        with self._lock:
            entry = self._entries.get(identity)
            if entry is None or entry.owner != owner or entry.done:
                raise PermissionError("idempotency store by a non-owner or after completion")
            entry.done = True
            entry.completed_at = self._clock()
            entry.response = response
            self._entries.move_to_end(identity)  # newest-by-completion at the tail
            self._evict_completed_locked()

    def release(self, identity: Any, owner: str) -> bool:
        """Release an INCOMPLETE lease (error / cancel path). Owner-only; raises on a mismatched
        owner. A completed lease is kept for replay and is NOT released."""
        with self._lock:
            entry = self._entries.get(identity)
            if entry is None:
                return False
            if entry.owner != owner:
                raise PermissionError("idempotency release by a non-owner")
            if entry.done:
                return False
            del self._entries[identity]
            return True

    def cleanup(self) -> None:
        with self._lock:
            self._cleanup_locked(self._clock())

    def _cleanup_locked(self, now: float) -> None:
        expired = [
            k
            for k, v in self._entries.items()
            if v.done and v.completed_at is not None and now - v.completed_at > self._ttl
        ]
        for k in expired:
            del self._entries[k]

    def _evict_completed_locked(self) -> None:
        # Bound the COMPLETED (stored-response) set to max_entries, evicting the OLDEST by
        # completion first (completed entries are move_to_end'd on store, so their OrderedDict
        # order is completion order). In-flight leases are NEVER counted or evicted — dropping a
        # live lease would lose it and allow double execution. Runs on store (the only place the
        # completed set grows), so it converges even when acquire traffic stops.
        completed = [k for k, v in self._entries.items() if v.done]
        for victim in completed[: max(0, len(completed) - self._max)]:
            del self._entries[victim]


_GLOBAL_LEASE_CACHE = IdempotencyLeaseCache()


def get_idempotency_lease_cache() -> IdempotencyLeaseCache:
    """FastAPI dependency provider for the Phase B lease cache — overridable in tests."""
    return _GLOBAL_LEASE_CACHE


__all__ = [
    "AcquireStatus",
    "CompletedResponse",
    "IdempotencyCache",
    "IdempotencyLeaseCache",
    "IdempotencyRequestState",
    "get_idempotency_cache",
    "get_idempotency_lease_cache",
]
