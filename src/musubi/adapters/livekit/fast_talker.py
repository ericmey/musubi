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

from musubi.adapters.livekit.cache import ContextCache

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
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.cache = cache
        self._fast_limit = fast_limit
        self._match_threshold = match_threshold

    async def get_context(self, query_text: str) -> list[dict[str, Any]]:
        """Return retrieval results for the live query — cache first,
        SDK fast path on miss. Returns ``[]`` on any error so the
        speech loop is never blocked."""
        cached = self.cache.get_best_match(query_text, threshold=self._match_threshold)
        if cached is not None:
            return cached
        try:
            response = await self.client.retrieve(
                namespace=self.namespace,
                query_text=query_text,
                mode="fast",
                limit=self._fast_limit,
            )
        except Exception:
            log.warning("fast-talker fallback failed", exc_info=True)
            return []
        if not isinstance(response, dict):
            return []
        results = response.get("results", [])
        return results if isinstance(results, list) else []
