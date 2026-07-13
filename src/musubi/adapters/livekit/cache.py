"""In-memory per-session retrieval cache for the LiveKit voice adapter.

Per [[07-interfaces/livekit-adapter]] § ContextCache. Bounded
(`max_entries` defaults to 10), TTL'd, and ages out the oldest
entries first when full. ``get_best_match`` does a cheap token-overlap
match — not a vector lookup, since pre-fetch queries are usually
substrings or rephrasings of the live query.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: RET-007 — the agent-facing status code for a TOTAL retrieval failure (the SDK call raised / returned
#: no usable body). Distinct from the bounded per-plane degradation codes; it means "no memory this turn".
RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"


class RetrievalStatus:
    """The ONE authoritative agent-facing retrieval status for the current turn (RET-007 Blocker 4).

    Both the Slow Thinker (pre-fetch) and the Fast Talker (speech turn) publish to it on EVERY
    retrieval — success, degraded, or total failure — so the most recent retrieval wins (current-turn
    semantics). A Slow Thinker total failure is therefore visible immediately (not only in its own
    ``last_warnings``), and a later healthy Fast Talker turn clears it with no stale carry-over.
    """

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def publish(self, warnings: list[str]) -> None:
        self.warnings = list(warnings)


@dataclass(frozen=True)
class _CacheEntry:
    key: str
    results: list[dict[str, Any]]
    expires_at: float
    #: RET-007 — the retrieval degradation warnings that accompanied these cached results, so a Fast
    #: Talker cache-hit surfaces the SAME degradation status the Slow Thinker recorded (not a stale one).
    warnings: list[str] = field(default_factory=list)


def _token_overlap(a: str, b: str) -> float:
    """Cheap Jaccard-ish overlap of lowercased token sets. Returns 0.0
    if either side has no tokens — keeps the threshold gate honest
    instead of a divide-by-zero."""
    ta = {t for t in a.lower().split() if t}
    tb = {t for t in b.lower().split() if t}
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


class ContextCache:
    """Bounded TTL'd retrieval cache. One per voice session."""

    def __init__(self, max_entries: int = 10) -> None:
        self._max_entries = max_entries
        self._entries: deque[_CacheEntry] = deque(maxlen=max_entries)

    def put(
        self,
        key: str,
        results: list[dict[str, Any]],
        ttl: float,
        warnings: list[str] | None = None,
    ) -> None:
        """Store results (+ their RET-007 degradation ``warnings``) under ``key`` with a TTL. When
        full, the oldest entry is evicted (FIFO via ``deque(maxlen=...)``)."""
        self._entries.append(
            _CacheEntry(
                key=key,
                results=results,
                expires_at=time.monotonic() + ttl,
                warnings=list(warnings or []),
            )
        )

    def match(self, query: str, threshold: float) -> _CacheEntry | None:
        """Return the highest-overlap unexpired ENTRY (results + warnings), if any beat ``threshold``.
        The Fast Talker uses this so a cache-hit carries the cached degradation status."""
        now = time.monotonic()
        best_score = 0.0
        best: _CacheEntry | None = None
        for entry in self._entries:
            if entry.expires_at <= now:
                continue
            score = _token_overlap(query, entry.key)
            if score > best_score:
                best_score = score
                best = entry
        return best if best is not None and best_score >= threshold else None

    def get_best_match(self, query: str, threshold: float) -> list[dict[str, Any]] | None:
        """Backward-compatible results-only view of :meth:`match` (``None`` on miss)."""
        entry = self.match(query, threshold)
        return entry.results if entry is not None else None
