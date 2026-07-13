"""RET-007 Blocker 4 — the LiveKit degradation channel is LIVE, not dead.

Owner slice: slice-ret007-degradation-impl (#422).

Proves: the slow→cache→fast path preserves degradation status; a total failure is visible on the
agent-facing channel (``LiveKitAdapter.retrieval_status``); and no stale warning survives a
healthy/error transition. Previously SlowThinker cached results only, a FastTalker cache-hit never
updated ``last_warnings``, and ``last_warnings`` had no consumer.

    uv run pytest tests/adapters/test_ret007_livekit_channel.py -v
"""

import asyncio
from typing import Any

from musubi.adapters.livekit.adapter import LiveKitAdapter
from musubi.adapters.livekit.cache import RETRIEVAL_UNAVAILABLE, ContextCache, RetrievalStatus
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


class _GatedClient:
    """A client whose ``retrieve`` blocks on an :class:`asyncio.Event` until released — so a test can
    force an out-of-order COMPLETION (an older request finishing after a newer one)."""

    def __init__(self, gate: asyncio.Event, response: Any = None, raises: bool = False) -> None:
        self._gate = gate
        self.response = response
        self.raises = raises

    async def retrieve(self, **kwargs: Any) -> Any:
        await self._gate.wait()
        if self.raises:
            raise RuntimeError("backend down")
        return self.response


def _degraded(warnings: list[str]) -> dict[str, Any]:
    return {"results": [{"object_id": "1", "plane": "episodic"}], "warnings": warnings}


async def test_overlap_old_slow_failure_must_not_clobber_newer_fast() -> None:
    """RACE (Yua): a Slow Thinker request STARTED first but completing LAST (with a failure) must NOT
    overwrite the status of a newer Fast Talker turn that already delivered healthy context."""
    status = RetrievalStatus()
    gate = asyncio.Event()
    slow = SlowThinker(
        client=_GatedClient(gate, raises=True), namespace="ns", cache=ContextCache(), status=status
    )
    fast = FastTalker(
        client=_Client(response={"results": [], "warnings": []}),
        namespace="ns",
        cache=ContextCache(),
        status=status,
    )
    slow_task = asyncio.create_task(
        slow._prefetch(_QUERY)
    )  # gen 1 — begins, then blocks on the gate
    await asyncio.sleep(0)  # let the slow request allocate its generation and park
    await fast.get_context("a newer live query")  # gen 2 — healthy, publishes []
    assert status.warnings == []
    gate.set()  # release the OLDER slow request → it fails, but must not win
    await slow_task
    assert status.warnings == [], "an older slow failure must not clobber the newer fast status"


async def test_overlap_old_fast_healthy_must_not_clobber_newer_slow() -> None:
    """The mirror race: a Fast Talker started first but completing last (healthy) must not overwrite a
    newer Slow Thinker pre-fetch that recorded degradation."""
    status = RetrievalStatus()
    gate = asyncio.Event()
    fast = FastTalker(
        client=_GatedClient(gate, response={"results": [], "warnings": []}),
        namespace="ns",
        cache=ContextCache(),
        status=status,
    )
    slow = SlowThinker(
        client=_Client(response=_degraded(["sparse_embedding_failed"])),
        namespace="ns",
        cache=ContextCache(),
        status=status,
    )
    fast_task = asyncio.create_task(fast.get_context(_QUERY))  # gen 1 — begins, then blocks
    await asyncio.sleep(0)
    await slow._prefetch("a newer transcript")  # gen 2 — degraded, publishes
    assert status.warnings == ["sparse_embedding_failed"]
    gate.set()  # release the OLDER fast request → healthy, but must not win
    await fast_task
    assert status.warnings == ["sparse_embedding_failed"], (
        "an older fast healthy result must not clobber the newer slow degradation status"
    )


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
