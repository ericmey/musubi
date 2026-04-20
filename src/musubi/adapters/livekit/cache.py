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
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _CacheEntry:
    key: str
    results: list[dict[str, Any]]
    expires_at: float


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
    ) -> None:
        """Store results under ``key`` with a TTL. When full, the oldest
        entry is evicted (FIFO via ``deque(maxlen=...)``)."""
        self._entries.append(
            _CacheEntry(
                key=key,
                results=results,
                expires_at=time.monotonic() + ttl,
            )
        )

    def get_best_match(self, query: str, threshold: float) -> list[dict[str, Any]] | None:
        """Return the highest-overlap unexpired entry's results, if any
        beat ``threshold``. ``None`` otherwise — the Fast Talker reads
        this and falls back to the SDK's fast path on miss."""
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
        if best is not None and best_score >= threshold:
            return best.results
        return None
