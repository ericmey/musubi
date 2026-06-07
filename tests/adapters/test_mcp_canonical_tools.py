"""Test contract for slice-mcp-canonical-tools.

Implements the canonical agent-tools contract from
[[07-interfaces/agent-tools]] for the MCP adapter. The five canonical
tools (`musubi_recent`, `musubi_search`, `musubi_get`, `musubi_remember`,
`musubi_think`) are exercised directly against a stub `AsyncMusubiClient`
so the tool wiring is verified without spinning up the real backend.

`musubi_recent` ships as a clearly-deferred stub in this slice — its
backend dependency (`mode=recent`, [[_slices/slice-retrieve-recent]])
is blocked. The stub test asserts the deferred-message shape; the
contract's full recency semantics test moves into the slice that
finishes the wiring once the backend mode lands.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from musubi.adapters.mcp.tools import attach_tools

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _PlaneStub:
    def __init__(self, *, name: str, store: dict[str, dict[str, Any]] | None = None) -> None:
        self.name = name
        self.store = store if store is not None else {}
        self.captured: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, str]] = []

    async def capture(
        self,
        *,
        namespace: str,
        content: str,
        importance: int = 5,
        tags: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        oid = f"obj-{len(self.captured) + 1}"
        record = {
            "object_id": oid,
            "namespace": namespace,
            "content": content,
            "importance": importance,
            "tags": list(tags or []),
            "idempotency_key": idempotency_key,
        }
        self.captured.append(record)
        self.store[oid] = record
        return {"object_id": oid, "state": "provisional"}

    async def get(self, *, namespace: str, object_id: str) -> dict[str, Any]:
        self.get_calls.append({"namespace": namespace, "object_id": object_id})
        if object_id not in self.store:
            from musubi.sdk.exceptions import NotFound

            raise NotFound(
                code="NOT_FOUND",
                detail=f"not found: {object_id} in {namespace}",
                status_code=404,
            )
        return self.store[object_id]


class _ThoughtsStub:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(
        self,
        *,
        namespace: str,
        from_presence: str,
        to_presence: str,
        content: str,
        channel: str = "default",
        importance: int = 5,
    ) -> dict[str, Any]:
        oid = f"thought-{len(self.sent) + 1}"
        self.sent.append(
            {
                "object_id": oid,
                "namespace": namespace,
                "from_presence": from_presence,
                "to_presence": to_presence,
                "content": content,
                "channel": channel,
                "importance": importance,
            }
        )
        return {"object_id": oid}


class _ClientStub:
    """Mimics the surface of `AsyncMusubiClient` the MCP adapter uses."""

    def __init__(self) -> None:
        # Mirror the real `AsyncMusubiClient` accessor names exactly:
        # singular `episodic`/`curated`, plural `concepts`/`artifacts`
        # (matching the canonical-API path prefixes). Stub names
        # diverging from real names was the trap behind the original
        # PLANE_ATTR pluralization bug — keep them locked in step.
        self.episodic = _PlaneStub(name="episodic")
        self.curated = _PlaneStub(name="curated")
        self.concepts = _PlaneStub(name="concepts")
        self.artifacts = _PlaneStub(name="artifacts")
        self.thoughts = _ThoughtsStub()
        self._retrieve_calls: list[dict[str, Any]] = []
        self._retrieve_response: dict[str, Any] = {"results": []}
        self._retrieve_raise: Exception | None = None

    async def retrieve(
        self,
        *,
        namespace: str,
        query_text: str = "",
        mode: str = "fast",
        limit: int = 10,
        planes: list[str] | None = None,
        since: float | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self._retrieve_calls.append(
            {
                "namespace": namespace,
                "query_text": query_text,
                "mode": mode,
                "limit": limit,
                "planes": planes,
                "since": since,
                "tags": tags,
            }
        )
        if self._retrieve_raise is not None:
            raise self._retrieve_raise
        return self._retrieve_response


# --------------------------------------------------------------------------
# Helper — invoke a registered tool by name
# --------------------------------------------------------------------------


def _invoke(mcp: FastMCP, name: str, **kwargs: Any) -> Any:
    """Call a tool registered on the FastMCP instance directly.

    FastMCP keeps the registered functions in a ``_tool_manager`` registry
    keyed by name (private impl detail). We dig in rather than going
    through the JSON-RPC transport for these unit tests.
    """
    tool_manager = mcp._tool_manager
    tool = tool_manager._tools[name]
    return tool.fn(**kwargs)


def _make_server() -> tuple[FastMCP, _ClientStub]:
    client = _ClientStub()
    mcp = FastMCP("musubi-test")
    attach_tools(mcp, client)  # type: ignore[arg-type]
    return mcp, client


# --------------------------------------------------------------------------
# musubi_search
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_invokes_retrieve_deep_mode() -> None:
    mcp, client = _make_server()
    client._retrieve_response = {
        "results": [
            {
                "plane": "episodic",
                "object_id": "ep-1",
                "namespace": "eric/claude-code/episodic",
                "score": 0.92,
                "content": "Eric was working on the canonical agent-tools spec.",
                "title": None,
            }
        ]
    }
    result = await _invoke(
        mcp,
        "musubi_search",
        namespace="eric/claude-code",
        query="canonical agent tools spec",
        limit=5,
    )

    assert client._retrieve_calls, "retrieve was not invoked"
    call = client._retrieve_calls[0]
    assert call["namespace"] == "eric/claude-code"
    assert call["query_text"] == "canonical agent tools spec"
    assert call["mode"] == "deep"
    assert call["limit"] == 5
    assert "Eric was working on" in result
    assert "[episodic]" in result


@pytest.mark.asyncio
async def test_search_passes_planes_filter() -> None:
    mcp, client = _make_server()
    client._retrieve_response = {"results": []}
    await _invoke(
        mcp,
        "musubi_search",
        namespace="eric/claude-code",
        query="x",
        planes=["episodic", "curated"],
    )
    assert client._retrieve_calls[0]["planes"] == ["episodic", "curated"]


@pytest.mark.asyncio
async def test_search_no_results_returns_clear_message() -> None:
    mcp, _ = _make_server()
    result = await _invoke(mcp, "musubi_search", namespace="eric/claude-code", query="nothing")
    assert "No memories matched" in result
    assert "nothing" in result


@pytest.mark.asyncio
async def test_search_backend_error_returns_tool_error_string() -> None:
    mcp, client = _make_server()
    client._retrieve_raise = RuntimeError("backend down")
    result = await _invoke(mcp, "musubi_search", namespace="eric/claude-code", query="x")
    assert isinstance(result, str)
    assert "backend down" in result or "couldn't" in result.lower()


# --------------------------------------------------------------------------
# musubi_get
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_round_trip_episodic() -> None:
    mcp, client = _make_server()
    client.episodic.store["ep-1"] = {
        "object_id": "ep-1",
        "namespace": "eric/claude-code/episodic",
        "content": "Full episodic body — the source the agent will cite.",
        "importance": 7,
        "title": None,
    }
    result = await _invoke(
        mcp,
        "musubi_get",
        plane="episodic",
        namespace="eric/claude-code/episodic",
        object_id="ep-1",
    )
    assert "Full episodic body" in result
    assert "[episodic]" in result
    assert "eric/claude-code/episodic/ep-1" in result


@pytest.mark.asyncio
async def test_get_routes_each_plane_to_its_stub() -> None:
    mcp, client = _make_server()
    client.curated.store["cur-1"] = {"object_id": "cur-1", "content": "curated body"}
    client.concepts.store["con-1"] = {"object_id": "con-1", "content": "concept body"}
    client.artifacts.store["art-1"] = {"object_id": "art-1", "content": "artifact body"}

    for plane, oid, expected in [
        ("curated", "cur-1", "curated body"),
        ("concept", "con-1", "concept body"),
        ("artifact", "art-1", "artifact body"),
    ]:
        result = await _invoke(
            mcp,
            "musubi_get",
            plane=plane,
            namespace=f"eric/_shared/{plane}",
            object_id=oid,
        )
        assert expected in result, f"{plane} routing wrong"


@pytest.mark.asyncio
async def test_get_unknown_id_returns_tool_error_with_id_and_namespace() -> None:
    mcp, _ = _make_server()
    result = await _invoke(
        mcp,
        "musubi_get",
        plane="episodic",
        namespace="eric/claude-code/episodic",
        object_id="missing-xyz",
    )
    assert "missing-xyz" in result
    assert "eric/claude-code/episodic" in result


def test_normalize_get_namespace_composes_two_part_root_with_plane() -> None:
    from musubi.adapters.mcp.tools import _normalize_get_namespace

    # 2-part presence root (what musubi_search takes) + plane → canonical 3-part
    assert _normalize_get_namespace("aoi/command-chair", "episodic") == "aoi/command-chair/episodic"
    # trailing slash on the root is tolerated, not double-slashed
    assert _normalize_get_namespace("aoi/command-chair/", "curated") == "aoi/command-chair/curated"
    # an already-3-part namespace is trusted as-is — never double-suffixed
    assert (
        _normalize_get_namespace("aoi/command-chair/episodic", "episodic")
        == "aoi/command-chair/episodic"
    )
    # an explicit 3-part namespace is left alone even if its tail differs from `plane`
    assert (
        _normalize_get_namespace("aoi/command-chair/episodic", "curated")
        == "aoi/command-chair/episodic"
    )


@pytest.mark.asyncio
async def test_get_accepts_two_part_root_and_composes_plane() -> None:
    # Regression: an agent that splits a search result row into the 2-part presence
    # root + a plane (the natural reading) must still resolve to the stored 3-part
    # namespace — not a false NOT_FOUND. (2026-06-07 namespace foot-gun.)
    mcp, client = _make_server()
    client.episodic.store["ep-2"] = {
        "object_id": "ep-2",
        "namespace": "eric/claude-code/episodic",
        "content": "Body fetched via the 2-part presence root.",
        "title": None,
    }
    result = await _invoke(
        mcp,
        "musubi_get",
        plane="episodic",
        namespace="eric/claude-code",  # 2-part root, NOT the full 3-part namespace
        object_id="ep-2",
    )
    assert "Body fetched via the 2-part presence root." in result
    # the SDK get() was actually called with the composed canonical namespace
    assert client.episodic.get_calls[-1]["namespace"] == "eric/claude-code/episodic"
    # and the rendered header reflects it
    assert "eric/claude-code/episodic/ep-2" in result


# --------------------------------------------------------------------------
# musubi_remember
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_writes_to_episodic_with_modality_tag() -> None:
    mcp, client = _make_server()
    result = await _invoke(
        mcp,
        "musubi_remember",
        namespace="eric/claude-code/episodic",
        content="Eric decided to ship the spec PR before the slice PR.",
        importance=8,
        topics=["spec", "ship-order"],
    )
    assert client.episodic.captured, "episodic.capture was not called"
    write = client.episodic.captured[0]
    assert write["namespace"] == "eric/claude-code/episodic"
    assert write["content"].startswith("Eric decided")
    assert write["importance"] == 8
    assert "spec" in write["tags"]
    assert "ship-order" in write["tags"]
    # Required modality tag per [[07-interfaces/agent-tools#modality-tagging]]
    assert "src:mcp-agent-remember" in write["tags"]
    # Returns a confirmation string with the new id
    assert "obj-1" in result


@pytest.mark.asyncio
async def test_remember_default_importance_is_seven() -> None:
    mcp, client = _make_server()
    await _invoke(
        mcp,
        "musubi_remember",
        namespace="eric/claude-code/episodic",
        content="x",
    )
    assert client.episodic.captured[0]["importance"] == 7


# --------------------------------------------------------------------------
# musubi_think
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_think_sends_thought_to_recipient_presence() -> None:
    mcp, client = _make_server()
    result = await _invoke(
        mcp,
        "musubi_think",
        namespace="eric/claude-code/thought",
        from_presence="eric/claude-code",
        to_presence="eric/aoi",
        content="Heads up: I shipped the canonical spec PR.",
        channel="default",
    )
    assert client.thoughts.sent, "thoughts.send was not called"
    sent = client.thoughts.sent[0]
    assert sent["from_presence"] == "eric/claude-code"
    assert sent["to_presence"] == "eric/aoi"
    assert "canonical spec PR" in sent["content"]
    assert "thought-1" in result


# --------------------------------------------------------------------------
# musubi_recent
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_invokes_retrieve_recent_mode_without_query() -> None:
    """`musubi_recent` runs retrieve in recent mode with no query_text, and
    renders each row newest-first with a timestamp from the created_epoch
    (carried as `score`), not the raw float."""
    mcp, client = _make_server()
    client._retrieve_response = {
        "results": [
            {
                "plane": "episodic",
                "object_id": "ep-9",
                "namespace": "aoi/command-chair/episodic",
                "title": None,
                "snippet": "the freshest thing that happened",
                "score": 1780794147.0,  # created_epoch → 2026-06-07 01:02 UTC
            }
        ]
    }
    result = await _invoke(
        mcp,
        "musubi_recent",
        namespace="aoi/command-chair",
        limit=5,
    )
    call = client._retrieve_calls[-1]
    assert call["mode"] == "recent"
    assert call["query_text"] == ""  # recent omits the query
    assert call["limit"] == 5
    assert "the freshest thing that happened" in result
    assert "aoi/command-chair/episodic/ep-9" in result
    assert "2026-06-07 01:02" in result  # epoch rendered as a timestamp, not 1780794147.0


@pytest.mark.asyncio
async def test_recent_passes_tags_filter() -> None:
    mcp, client = _make_server()
    await _invoke(
        mcp,
        "musubi_recent",
        namespace="aoi/command-chair",
        tags=["src:mcp-agent-remember"],
    )
    assert client._retrieve_calls[-1]["tags"] == ["src:mcp-agent-remember"]


@pytest.mark.asyncio
async def test_recent_no_results_returns_clear_message() -> None:
    mcp, client = _make_server()
    client._retrieve_response = {"results": []}
    result = await _invoke(mcp, "musubi_recent", namespace="aoi/command-chair")
    assert "no recent activity" in result.lower()


@pytest.mark.asyncio
async def test_recent_backend_error_returns_tool_error_string() -> None:
    mcp, client = _make_server()
    client._retrieve_raise = RuntimeError("boom")
    result = await _invoke(mcp, "musubi_recent", namespace="aoi/command-chair")
    assert result.startswith("Error:")


# --------------------------------------------------------------------------
# Deprecation aliases
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_capture_alias_forwards_to_remember_and_logs_deprecation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mcp, client = _make_server()
    caplog.set_level(logging.WARNING, logger="musubi.adapters.mcp.tools")
    await _invoke(
        mcp,
        "memory_capture",
        namespace="eric/claude-code/episodic",
        content="test",
        importance=5,
    )
    assert client.episodic.captured, "alias did not forward to canonical impl"
    assert any(
        "memory_capture" in rec.message.lower() and "deprecated" in rec.message.lower()
        for rec in caplog.records
    ), "no deprecation warning logged for memory_capture alias"


@pytest.mark.asyncio
async def test_memory_recall_alias_forwards_to_search_and_logs_deprecation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mcp, client = _make_server()
    caplog.set_level(logging.WARNING, logger="musubi.adapters.mcp.tools")
    client._retrieve_response = {"results": []}
    await _invoke(
        mcp,
        "memory_recall",
        namespace="eric/claude-code",
        query="x",
        limit=5,
    )
    assert client._retrieve_calls, "alias did not forward to canonical impl"
    assert any(
        "memory_recall" in rec.message.lower() and "deprecated" in rec.message.lower()
        for rec in caplog.records
    ), "no deprecation warning logged for memory_recall alias"


# --------------------------------------------------------------------------
# Tool registration completeness
# --------------------------------------------------------------------------


def test_attach_tools_registers_all_canonical_plus_aliases() -> None:
    mcp, _ = _make_server()
    tools = mcp._tool_manager._tools
    # Five canonical tools per [[07-interfaces/agent-tools]]
    for name in (
        "musubi_recent",
        "musubi_search",
        "musubi_get",
        "musubi_remember",
        "musubi_think",
    ):
        assert name in tools, f"canonical tool {name!r} not registered"
    # Two deprecation aliases for one minor release
    for name in ("memory_capture", "memory_recall"):
        assert name in tools, f"deprecated alias {name!r} missing"


def test_plane_attr_map_targets_match_real_sdk_accessors() -> None:
    """Regression test for the original PLANE_ATTR bug.

    The Python SDK uses singular `episodic`/`curated` and plural
    `concepts`/`artifacts`. An earlier draft had the map's *values*
    in singular form (`"concept"`/`"artifact"`), which passed every
    stub-based test but would `AttributeError` against the real
    `AsyncMusubiClient` in production. Lock the map's values to the
    real SDK class's attributes so this can't regress.
    """
    from musubi.adapters.mcp.tools import _PLANE_ATTR
    from musubi.sdk.async_client import AsyncMusubiClient

    # Construct a real client to introspect — base_url + token are
    # required by the constructor but no I/O happens at __init__.
    client = AsyncMusubiClient(base_url="https://musubi.test", token="t")
    try:
        for plane, attr in _PLANE_ATTR.items():
            assert hasattr(client, attr), (
                f"_PLANE_ATTR maps {plane!r} → {attr!r}, but AsyncMusubiClient "
                f"has no such attribute. The SDK uses singular `episodic`/`curated` "
                f"and plural `concepts`/`artifacts`."
            )
    finally:
        # AsyncMusubiClient holds an httpx.AsyncClient — close it so the
        # test doesn't trigger an "unclosed resource" warning under
        # asyncio strict mode.
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(client.close())
        finally:
            loop.close()
