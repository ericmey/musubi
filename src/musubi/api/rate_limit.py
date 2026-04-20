"""Per-token rate-limit middleware for write endpoints.

Per [[07-interfaces/canonical-api]] § Rate limits, write endpoints are
bucketed per (token, endpoint-bucket). Default buckets:

- ``capture`` — 100/min
- ``thought`` — 100/min (shares the "send" allowance)
- ``artifact-upload`` — 20/min
- ``batch-write`` — 50/min
- ``transition`` — 50/min (operator path)

Operator-scoped tokens get a 10x multiplier per the spec.

Implementation: in-memory rolling window per (token-jti or bearer hash,
bucket). Production with multiple workers will move this to Kong or
Redis (a future ``slice-api-rate-limit-distributed``); the swap is a
Protocol-keyed dependency so callers don't change.

Headers emitted on every write response:

- ``X-RateLimit-Limit``     — bucket capacity
- ``X-RateLimit-Remaining`` — tokens left in the current window
- ``Retry-After``           — seconds until the window resets (429 only)

Bucket selection is per route: the router decorator declares its
bucket; the middleware reads the route's metadata to pick the cap.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

_WINDOW_S = 60.0


@dataclass(frozen=True)
class BucketSpec:
    name: str
    capacity_per_min: int


# Default buckets. Operator scope multiplies these by 10.
DEFAULT_BUCKETS: dict[str, BucketSpec] = {
    "capture": BucketSpec(name="capture", capacity_per_min=100),
    "thought": BucketSpec(name="thought", capacity_per_min=100),
    "artifact-upload": BucketSpec(name="artifact-upload", capacity_per_min=20),
    "batch-write": BucketSpec(name="batch-write", capacity_per_min=50),
    "transition": BucketSpec(name="transition", capacity_per_min=50),
    "default": BucketSpec(name="default", capacity_per_min=200),
}

_OPERATOR_MULTIPLIER = 10


@dataclass
class _BucketState:
    used: int = 0
    window_start: float = field(default_factory=time.time)


class RateLimiter:
    """Per-(token, bucket) rolling-window counter.

    Thread-safe. ``allow()`` returns the per-window tally + whether the
    request fits; the caller emits the headers + 429 if not.
    """

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _BucketState] = {}
        self._lock = threading.Lock()

        try:
            from musubi.config import get_settings

            settings = get_settings()
            self._capture_rate = getattr(settings, "rate_limit_capture", 10.0)
            self._retrieve_rate = getattr(settings, "rate_limit_retrieve", 20.0)
            self._thought_rate = getattr(settings, "rate_limit_thought", 5.0)
        except Exception:
            self._capture_rate = 10.0
            self._retrieve_rate = 20.0
            self._thought_rate = 5.0

    @staticmethod
    def token_key(bearer: str | None) -> str:
        """Hash the bearer to a stable per-token key. Avoids storing the
        raw token in memory beyond the request lifetime."""
        if bearer is None:
            return "anonymous"
        return hashlib.sha256(bearer.encode("utf-8")).hexdigest()[:24]

    def allow(
        self,
        *,
        token_key: str,
        bucket: BucketSpec,
        operator: bool,
    ) -> tuple[bool, int, int, int]:
        """Return ``(allowed, limit, remaining, retry_after_s)``.

        ``allowed`` is False iff the bucket is full for the current
        window.
        """
        capacity = bucket.capacity_per_min * (_OPERATOR_MULTIPLIER if operator else 1)
        now = time.time()
        key = (token_key, bucket.name)
        with self._lock:
            state = self._buckets.get(key)
            if state is None or now - state.window_start >= _WINDOW_S:
                state = _BucketState(used=0, window_start=now)
                self._buckets[key] = state
            elapsed = now - state.window_start
            retry_after = max(1, int(_WINDOW_S - elapsed))
            if state.used >= capacity:
                return False, capacity, 0, retry_after
            state.used += 1
            remaining = capacity - state.used
            return True, capacity, remaining, retry_after

    def reset_for_test(self) -> None:
        with self._lock:
            self._buckets.clear()


_GLOBAL_LIMITER = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    """FastAPI dependency provider — overridable in tests."""
    return _GLOBAL_LIMITER


# ---------------------------------------------------------------------------
# Bucket-tagged route helper
# ---------------------------------------------------------------------------


def with_bucket(bucket_name: str) -> Callable[..., object]:
    """Tag a route handler with a rate-limit bucket name.

    Used as a parameter on each write router decorator's ``operation_id``
    metadata; the middleware reads it from the route to pick the
    bucket. Routes without a tag fall back to the ``default`` bucket.
    """
    # Returned as-is for now; the bucket is read from the route's
    # ``operation_id`` set on the decorator. The function exists so
    # callers can spell their intent declaratively.
    return bucket_name  # type: ignore[return-value]


__all__ = [
    "DEFAULT_BUCKETS",
    "BucketSpec",
    "RateLimiter",
    "get_rate_limiter",
    "with_bucket",
]
