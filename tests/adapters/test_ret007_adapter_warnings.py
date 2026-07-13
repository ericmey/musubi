"""RET-007 — adapter degradation surfacing (MCP + LiveKit reds).

Owner slice: slice-ret007-degradation (Musubi MCP + LiveKit adapters). Tests/docs only, no src.

Contract §5: the adapters MUST surface the allowlisted ``warnings`` to the agent, not discard them.
Today every adapter does ``res.get("results")`` and throws the rest of the response dict away, so the
agent is blind to retrieval degradation. Each red fails for its named contract reason; the fix flips
it. For LiveKit, **this red contract DEFINES ``last_warnings`` (a list of allowlisted codes) as the
minimal surfacing channel** the fix must populate on FastTalker/SlowThinker after each retrieval; the
ChatContext non-memory status-message rendering is layered on top of that channel and is out of scope
for this red.

    uv run pytest tests/adapters/test_ret007_adapter_warnings.py -v
"""

from typing import Any, cast

import pytest

from musubi.adapters.livekit.cache import ContextCache
from musubi.adapters.livekit.fast_talker import FastTalker
from musubi.adapters.livekit.slow_thinker import SlowThinker
from musubi.adapters.mcp.tools import _do_search


class DefectStillPresent(Exception):
    """Raised when the current adapter still discards the warnings the contract requires surfaced."""


class _WarningClient:
    """A minimal AsyncMusubiClient stand-in whose retrieve returns a degraded (warnings-bearing)
    response — one hit plus the allowlisted ``sparse_embedding_failed`` code."""

    async def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "results": [
                {
                    "plane": "episodic",
                    "object_id": "1",
                    "namespace": "test/ns",
                    "score": 1.0,
                    "content": "hit",
                }
            ],
            "warnings": ["sparse_embedding_failed"],
        }


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="MCP: _do_search extracts res['results'] and discards `warnings` — no degradation note reaches the LLM string",
)
async def test_mcp_adapter_surfaces_warnings() -> None:
    out = await _do_search(
        cast(Any, _WarningClient()), namespace="test/ns", query="q", limit=5, planes=["episodic"]
    )
    # Contract §5: MCP must prepend a strict fixed-prefix system note carrying the allowlisted code.
    if "[SYSTEM: Retrieval degraded:" not in out or "sparse_embedding_failed" not in out:
        raise DefectStillPresent(
            f"MCP dropped the degradation warning from the LLM string — no fixed-prefix note. Got: {out!r}"
        )


# --------------------------------------------------------------------------- #
# LiveKit — FastTalker + SlowThinker
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="LiveKit FastTalker: get_context returns response['results'] only; warnings are discarded, never reach ChatContext",
)
async def test_livekit_fast_talker_surfaces_warnings() -> None:
    ft = FastTalker(client=cast(Any, _WarningClient()), namespace="test/ns", cache=ContextCache())
    await ft.get_context("q")
    # Contract §5: the warnings must be reachable so the fix can inject a non-memory runtime status
    # message into ChatContext. Today FastTalker exposes no warnings channel.
    surfaced = getattr(ft, "last_warnings", None)
    if not surfaced or "sparse_embedding_failed" not in surfaced:
        raise DefectStillPresent(
            "FastTalker discarded the retrieval warnings — no channel surfaces them to ChatContext"
        )


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="LiveKit SlowThinker: _prefetch caches response['results'] only; warnings are discarded before ChatContext",
)
async def test_livekit_slow_thinker_surfaces_warnings() -> None:
    st = SlowThinker(client=cast(Any, _WarningClient()), namespace="test/ns", cache=ContextCache())
    await st._prefetch("q")
    surfaced = getattr(st, "last_warnings", None)
    if not surfaced or "sparse_embedding_failed" not in surfaced:
        raise DefectStillPresent(
            "SlowThinker discarded the retrieval warnings — no channel surfaces them to ChatContext"
        )
