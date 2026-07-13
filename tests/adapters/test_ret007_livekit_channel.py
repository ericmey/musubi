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
        self.response = response
        self.raises = raises

    async def retrieve(self, **kwargs: Any) -> Any:
        if self.raises:
            raise RuntimeError("backend down")
        return self.response


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


async def test_slow_thinker_failure_visible_on_agent_channel() -> None:
    """The REAL seam (Yua): a Slow Thinker total failure must reach the AGENT channel
    (``adapter.retrieval_status``), not merely ``slow_thinker.last_warnings`` in isolation."""
    adapter = LiveKitAdapter(
        client=_Client(raises=True),
        namespace="ns",
        artifact_namespace="ns/_shared/artifact",
        config=LiveKitAdapterConfig(),
    )
    await adapter.slow_thinker._prefetch(_QUERY)
    assert adapter.retrieval_status == [RETRIEVAL_UNAVAILABLE], (
        "a slow-thinker total failure must be visible on the agent-facing channel"
    )


async def test_agent_channel_current_turn_transitions() -> None:
    """Current-turn semantics on the one authoritative channel: a slow failure is visible, then a
    later healthy Fast Talker turn CLEARS it (no stale failure), and a degraded turn shows the codes."""
    client = _Client(raises=True)
    adapter = LiveKitAdapter(
        client=client,
        namespace="ns",
        artifact_namespace="ns/_shared/artifact",
        config=LiveKitAdapterConfig(),
    )
    await adapter.slow_thinker._prefetch(_QUERY)
    assert adapter.retrieval_status == [RETRIEVAL_UNAVAILABLE]

    # recovery — a healthy fast turn on the shared channel clears the stale failure
    client.raises = False
    client.response = {"results": [], "warnings": []}
    await adapter.fast_talker.get_context("a fresh unrelated query")
    assert adapter.retrieval_status == [], "a healthy current turn must clear the stale failure"

    # a subsequent degraded fast turn shows the bounded codes
    client.response = {"results": [], "warnings": ["sparse_embedding_failed"]}
    await adapter.fast_talker.get_context("another fresh query")
    assert adapter.retrieval_status == ["sparse_embedding_failed"]


async def test_adapter_retrieval_status_is_the_consumer() -> None:
    """The agent-facing consumer: a real Fast Talker turn publishes onto ``retrieval_status`` — the
    status is reachable through the actual retrieval path, not a directly-mutated dead attribute."""
    adapter = LiveKitAdapter(
        client=_Client(response=_degraded(["plane_timeout_episodic"])),
        namespace="ns",
        artifact_namespace="ns/_shared/artifact",
        config=LiveKitAdapterConfig(),
    )
    assert adapter.retrieval_status == []  # nothing retrieved yet
    await adapter.fast_talker.get_context(_QUERY)
    assert adapter.retrieval_status == ["plane_timeout_episodic"]
