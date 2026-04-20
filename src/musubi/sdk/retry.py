"""Retry policy for the SDK's HTTP calls.

Per [[07-interfaces/sdk]] § Retry policy:

- Retries on: 429, 503, 504, ``NetworkError``.
- Exponential backoff: 0.5s, 1s, 2s, 4s (max 4 attempts).
- Honors ``Retry-After`` header on 429/503.
- Idempotency-Key auto-mint on POST (handled in the client, not here).
"""

from __future__ import annotations

from dataclasses import dataclass

_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry, how long to sleep, and which statuses
    qualify."""

    max_attempts: int = 4
    base_backoff: float = 0.5
    max_backoff: float = 4.0
    retry_after_cap_s: float = 30.0
    retryable_statuses: frozenset[int] = _RETRYABLE_STATUSES

    @classmethod
    def default(cls) -> RetryPolicy:
        return cls()

    @classmethod
    def none(cls) -> RetryPolicy:
        return cls(max_attempts=1, base_backoff=0.0)

    def backoff_for(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Compute the sleep duration before attempt ``attempt`` (1-indexed
        for the SECOND call). Honours ``Retry-After`` if supplied,
        capped at :attr:`retry_after_cap_s` so a misbehaving server can't
        stall the client indefinitely."""
        if retry_after is not None:
            return min(max(0.0, retry_after), self.retry_after_cap_s)
        if attempt <= 1:
            return 0.0
        # Exponential: base * 2^(attempt-2) so attempt=2 → base, attempt=3 → 2*base.
        delay = self.base_backoff * (2.0 ** max(0, attempt - 2))
        return min(delay, self.max_backoff)


__all__ = ["RetryPolicy"]
