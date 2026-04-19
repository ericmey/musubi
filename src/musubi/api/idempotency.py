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
from dataclasses import dataclass
from typing import Any

_DEFAULT_TTL_S = 24 * 3600


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


__all__ = ["IdempotencyCache", "get_idempotency_cache"]
