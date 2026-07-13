"""RET-007 Blocker 4 — the LiveKit degradation channel is LIVE, not dead.

Owner slice: slice-ret007-degradation-impl (#422).

Proves: the slow→cache→fast path preserves degradation status; a total failure is visible on the
agent-facing channel (``LiveKitAdapter.retrieval_status``); and no stale warning survives a
healthy/error transition. Previously SlowThinker cached results only, a FastTalker cache-hit never
updated ``last_warnings``, and ``last_warnings`` had no consumer.

    uv run pytest tests/adapters/test_ret007_livekit_channel.py -v
"""

from typing import Any

from musubi.adapters.livekit.adapter import LiveKitAdapter
from musubi.adapters.livekit.cache import RETRIEVAL_UNAVAILABLE, ContextCache
from musubi.adapters.livekit.config import LiveKitAdapterConfig
from musubi.adapters.livekit.fast_talker import FastTalker
from musubi.adapters.livekit.slow_thinker import SlowThinker

_QUERY = "how do i configure cuda for the deep path"


class _Client:
    """Minimal SDK stand-in whose ``retrieve`` returns a fixed dict or raises."""

    def __init__(self, response: Any = None, raises: bool = False) -> None:
        self._response = response
        self._raises = raises

    async def retrieve(self, **kwargs: Any) -> Any:
        if self._raises:
            raise RuntimeError("backend down")
        return self._response


def _degraded(warnings: list[str]) -> dict[str, Any]:
    return {"results": [{"object_id": "1", "plane": "episodic"}], "warnings": warnings}


async def test_slow_to_cache_to_fast_preserves_degradation() -> None:
    """The Slow Thinker records a degraded pre-fetch → caches it WITH warnings → a Fast Talker
    cache-hit surfaces the SAME degradation (even though the fast client would answer healthy)."""
    cache = ContextCache()
    slow = SlowThinker(
        client=_Client(response=_degraded(["sparse_embedding_failed"])),
        namespace="ns",
        cache=cache,
    )
    await slow._prefetch(_QUERY)
    assert slow.last_warnings == ["sparse_embedding_failed"]

    # the fast client would answer HEALTHY — but the cache-hit must win and carry the degradation
    fast = FastTalker(
        client=_Client(response={"results": [], "warnings": []}), namespace="ns", cache=cache
    )
    await fast.get_context(_QUERY)
    assert fast.last_warnings == ["sparse_embedding_failed"], (
        "a cache-hit must preserve the cached degradation status, not go stale/healthy"
    )


async def test_total_failure_visible_on_agent_channel() -> None:
    fast = FastTalker(client=_Client(raises=True), namespace="ns", cache=ContextCache())
    results = await fast.get_context(_QUERY)
    assert results == []
    assert fast.last_warnings == [RETRIEVAL_UNAVAILABLE], (
        "a total failure must be visible, not silent"
    )


async def test_no_stale_warning_after_healthy_transition() -> None:
    fast = FastTalker(
        client=_Client(response={"results": [], "warnings": []}),
        namespace="ns",
        cache=ContextCache(),
    )
    fast.last_warnings = ["sparse_embedding_failed"]  # stale from a prior degraded turn
    await fast.get_context("a completely different fresh query")
    assert fast.last_warnings == [], (
        "a healthy retrieval must clear the previous degradation status"
    )


async def test_no_stale_warning_after_error_transition() -> None:
    fast = FastTalker(client=_Client(raises=True), namespace="ns", cache=ContextCache())
    fast.last_warnings = ["sparse_embedding_failed"]  # stale from a prior degraded turn
    await fast.get_context("another fresh query")
    assert fast.last_warnings == [RETRIEVAL_UNAVAILABLE], (
        "an error must replace the stale degradation with the failure status, not retain it"
    )


async def test_slow_thinker_total_failure_is_visible() -> None:
    slow = SlowThinker(client=_Client(raises=True), namespace="ns", cache=ContextCache())
    await slow._prefetch(_QUERY)
    assert slow.last_warnings == [RETRIEVAL_UNAVAILABLE]


def test_adapter_retrieval_status_is_the_consumer() -> None:
    """The agent-facing consumer: ``LiveKitAdapter.retrieval_status`` reads the live Fast Talker
    channel — so the degradation status is actually reachable, not a dead attribute."""
    adapter = LiveKitAdapter(
        client=_Client(),
        namespace="ns",
        artifact_namespace="ns/_shared/artifact",
        config=LiveKitAdapterConfig(),
    )
    assert adapter.retrieval_status == []  # healthy at construction
    adapter.fast_talker.last_warnings = ["plane_timeout_episodic"]
    assert adapter.retrieval_status == ["plane_timeout_episodic"]
    adapter.fast_talker.last_warnings = [RETRIEVAL_UNAVAILABLE]
    assert adapter.retrieval_status == [RETRIEVAL_UNAVAILABLE]
