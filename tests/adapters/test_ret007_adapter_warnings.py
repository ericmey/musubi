"""RET-007 — MCP adapter degradation surfacing.

The LiveKit tests moved to sourceblender/musubi-livekit with the adapter.

Contract §5: the adapters MUST surface the allowlisted ``warnings`` to the agent, not discard them.
The MCP result must keep the allowlisted warning codes visible to the agent.

    uv run pytest tests/adapters/test_ret007_adapter_warnings.py -v
"""

from typing import Any, cast

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


class _RerankerCauseClient(_WarningClient):
    async def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        response = await super().retrieve(**kwargs)
        response["warnings"] = ["reranker_failed", "reranker_failed_request_rejected"]
        return response


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #


async def test_mcp_adapter_surfaces_warnings() -> None:
    out = await _do_search(
        cast(Any, _WarningClient()), namespace="test/ns", query="q", limit=5, planes=["episodic"]
    )
    # Contract §5: MCP must prepend a strict fixed-prefix system note carrying the allowlisted code.
    if "[SYSTEM: Retrieval degraded:" not in out or "sparse_embedding_failed" not in out:
        raise DefectStillPresent(
            f"MCP dropped the degradation warning from the LLM string — no fixed-prefix note. Got: {out!r}"
        )


async def test_mcp_preserves_reranker_cause_detail() -> None:
    client = cast(Any, _RerankerCauseClient())
    mcp_output = await _do_search(
        client, namespace="test/ns", query="q", limit=5, planes=["episodic"]
    )
    assert "reranker_failed" in mcp_output
    assert "reranker_failed_request_rejected" in mcp_output
