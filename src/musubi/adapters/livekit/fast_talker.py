"""Fast Talker — speech-generation context fetch with hard 200ms budget.

Per [[07-interfaces/livekit-adapter]] § FastTalker. Reads from the
shared :class:`ContextCache` first; on miss, falls back to a
fast-path SDK call (mode="fast", ~150ms p50). Never blocks speech
on Musubi: any retrieval failure surfaces as an empty result list
and the agent speaks generically.
"""

from __future__ import annotations

import logging
from typing import Any

from musubi.adapters.livekit.cache import (
    RETRIEVAL_UNAVAILABLE,
    ContextCache,
    RetrievalStatus,
)

log = logging.getLogger("musubi.adapters.livekit.fast_talker")


class FastTalker:
    """Per-session fast-path retrieval wrapper."""

    def __init__(
        self,
        *,
        client: Any,
        namespace: str,
        cache: ContextCache,
        fast_limit: int = 5,
        match_threshold: float = 0.5,
        status: RetrievalStatus | None = None,
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.cache = cache
        self._fast_limit = fast_limit
        self._match_threshold = match_threshold
        #: RET-007 — the allowlisted degradation codes from the most recent retrieval, surfaced so the
        #: agent can render a non-memory status message. Empty when the last retrieval was healthy.
        self.last_warnings: list[str] = []
        #: The shared authoritative agent channel (set by the adapter). The Fast Talker drives the turn.
        self._status = status

    def _publish(self, warnings: list[str], generation: int) -> None:
        self.last_warnings = list(warnings)
        if self._status is not None:
            self._status.publish(generation, self.last_warnings)

    async def get_context(self, query_text: str) -> list[dict[str, Any]]:
        """Return retrieval results for the live query — cache first,
        SDK fast path on miss. Returns ``[]`` on any error so the
        speech loop is never blocked."""
        # Allocate the generation at the START so an out-of-order completion cannot clobber a newer turn.
        generation = self._status.begin() if self._status is not None else 0
        cached = self.cache.match(query_text, threshold=self._match_threshold)
        if cached is not None:
            # RET-007: a cache-hit must reflect the CACHED degradation status, not a stale one from a
            # previous turn — the Slow Thinker stored the warnings alongside the results.
            self._publish(list(cached.warnings), generation)
            return cached.results
        try:
            response = await self.client.retrieve(
                namespace=self.namespace,
                query_text=query_text,
                mode="fast",
                limit=self._fast_limit,
            )
        except Exception:
            log.warning("fast-talker fallback failed", exc_info=True)
            # RET-007: a total failure is VISIBLE on the agent-facing channel — never a silent [] that
            # leaves a stale "healthy" (or stale-degraded) status from the previous turn.
            self._publish([RETRIEVAL_UNAVAILABLE], generation)
            return []
        if not isinstance(response, dict):
            self._publish([RETRIEVAL_UNAVAILABLE], generation)
            return []
        warnings = response.get("warnings", [])
        self._publish(warnings if isinstance(warnings, list) else [], generation)
        results = response.get("results", [])
        return results if isinstance(results, list) else []
