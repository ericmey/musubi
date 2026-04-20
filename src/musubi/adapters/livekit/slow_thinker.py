"""Slow Thinker — pre-fetches deep-path retrieval between turns.

Per [[07-interfaces/livekit-adapter]] § The dual-agent pattern. Runs
its own asyncio task that the caller cancels and restarts whenever
new transcript material arrives. Cancellation is silent: the cancelled
task does not propagate ``CancelledError`` into the LiveKit event
loop, so a fast user-interrupt never crashes the speech path.

Read-only against Musubi (calls ``client.retrieve(mode="deep")``);
result writes land in the shared :class:`ContextCache` for the Fast
Talker to consume on the next 200ms tick.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from musubi.adapters.livekit.cache import ContextCache

log = logging.getLogger("musubi.adapters.livekit.slow_thinker")


class SlowThinker:
    """Per-session deep-pre-fetch worker."""

    def __init__(
        self,
        *,
        client: Any,
        namespace: str,
        cache: ContextCache,
        deep_limit: int = 15,
        cache_ttl_s: float = 120.0,
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.cache = cache
        self._deep_limit = deep_limit
        self._cache_ttl_s = cache_ttl_s
        self._task: asyncio.Task[None] | None = None

    async def on_user_utterance_segment(self, transcript_so_far: str) -> None:
        """Cancel any in-flight pre-fetch, then start a new one with
        the latest transcript. Idempotent if the same string lands
        twice in a row — the new task simply replays the deep call."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._prefetch(transcript_so_far))

    async def _prefetch(self, transcript: str) -> None:
        try:
            response = await self.client.retrieve(
                namespace=self.namespace,
                query_text=transcript,
                mode="deep",
                limit=self._deep_limit,
            )
        except asyncio.CancelledError:
            # Surfacing this as a normal cancel keeps the LiveKit loop
            # quiet — the new task replaces us.
            raise
        except Exception:
            log.warning("slow-thinker pre-fetch failed", exc_info=True)
            return
        results = response.get("results", []) if isinstance(response, dict) else []
        self.cache.put(transcript, results, ttl=self._cache_ttl_s)
