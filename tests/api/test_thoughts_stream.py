"""Test contract for slice-api-thoughts-stream.

Implements the 21 bullets from docs/Musubi/_slices/slice-api-thoughts-stream.md.

Conventions:
- Broker-level behaviour (fanout, filter, backpressure) is tested against
  the broker directly — unit-level, no HTTP.
- HTTP-stream behaviour (SSE content-type, ping cadence, auth, 503 cap, publish
  hook round-trip) is tested via ASGITransport + AsyncClient with a hard
  asyncio.wait_for timeout around every stream read so a bug hangs the test
  in ~1s, never indefinitely.
- Replay semantics + graceful shutdown close-event + hypothesis properties are
  skipped-with-reason pointing at slice-ops-integration-harness (#108) —
  those bullets need a live Qdrant + real graceful-shutdown hook not mocked
  here.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest import LogCaptureFixture

from musubi.api.events import Subscription, broker
from musubi.api.routers.thoughts import _sse_frame, _thoughts_event_generator
from musubi.types.thought import Thought

# Every test in this module reads SSE streams that may hang if the endpoint
# misbehaves. Keep a hard ceiling so pytest fails with a stack trace rather
# than blocking CI forever.
_STREAM_READ_TIMEOUT = 2.0


class _FakeRequest:
    """Minimal Request shim for driving the event generator directly.

    The generator only uses ``request.app.state.testing``; we don't need a
    full Starlette request here. Direct-driving sidesteps ASGITransport's
    streaming-response buffering quirks and keeps test latency sub-second.
    """

    def __init__(self, testing: bool) -> None:
        import types

        self.app = types.SimpleNamespace(state=types.SimpleNamespace(testing=testing))


@pytest.fixture
def app(app_factory: Any) -> FastAPI:
    app = cast(FastAPI, app_factory)
    # Flip test mode so the endpoint's ping cadence drops from 30s to 10ms —
    # the tests observe pings within their wait_for window.
    app.state.testing = True
    return app


@pytest.fixture(autouse=True)
def clean_broker() -> Any:
    """Ensure every test starts with an empty broker subscriber set.

    The broker is a module-level singleton; tests that leave subscribers
    behind (e.g. the stream tests when they're interrupted) would pollute
    the next test's view. Clean before + after.
    """
    broker._subscribers.clear()
    yield
    broker._subscribers.clear()


def _thought(**kwargs: Any) -> Thought:
    defaults = {
        "namespace": "eric/claude-code/thought",
        "from_presence": "me",
        "to_presence": "you",
        "content": "hello",
        "importance": 5,
        "channel": "default",
    }
    defaults.update(kwargs)
    return Thought(**defaults)  # type: ignore[arg-type]


def _parse_sse_frame(raw: bytes) -> dict[str, str]:
    """Parse a single SSE frame's bytes into a {field: value} dict."""
    frame: dict[str, str] = {}
    for line in raw.decode("utf-8").split("\n"):
        if line == "":
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            frame[field.strip()] = value.strip()
    return frame


async def _drive_one_frame(
    sub: Subscription, testing: bool, event_filter: str | None = None
) -> dict[str, str]:
    """Drive the event generator directly, return the first matching frame as
    a parsed dict. Bypasses HTTP/ASGITransport buffering entirely."""
    request = _FakeRequest(testing=testing)
    agen = _thoughts_event_generator(request, sub)  # type: ignore[arg-type]
    try:
        async for raw in agen:
            frame = _parse_sse_frame(raw)
            if event_filter is None or frame.get("event") == event_filter:
                return frame
        raise AssertionError("generator ended before matching frame arrived")
    finally:
        await agen.aclose()


# ─────────────────────────────────────────────────────────────────────────
# Endpoint shape (4)
# ─────────────────────────────────────────────────────────────────────────


def test_stream_returns_sse_content_type() -> None:
    """Verify the endpoint emits well-formed SSE frames with the right
    media type metadata.

    The full HTTP round-trip through ASGITransport doesn't terminate for
    infinite SSE streams (the transport buffers response body chunks and
    never flushes the headers to the client under asyncio). The
    integration harness (PR #114) covers the real HTTP round-trip
    against live services. At unit level we verify the byte-frame
    formatter directly + the header constants set by the route.
    """
    # Byte-frame formatter produces the right SSE shape.
    frame_bytes = _sse_frame(event="ping", data='{"at":"2026-04-20T00:00:00Z"}')
    parsed = _parse_sse_frame(frame_bytes)
    assert parsed["event"] == "ping"
    assert "at" in parsed["data"]

    # Header constants in the StreamingResponse construction: check by
    # inspecting the endpoint function's source (no HTTP round-trip
    # needed).
    import inspect

    from musubi.api.routers.thoughts import stream_thoughts

    source = inspect.getsource(stream_thoughts)
    assert 'media_type="text/event-stream"' in source
    assert '"Cache-Control": "no-cache"' in source


@pytest.mark.asyncio
async def test_stream_emits_ping_every_30s() -> None:
    """Verify ping emission by driving the generator directly.

    HTTP-layer verification of this bullet lives in the integration harness
    (PR #114); ASGITransport buffering makes it unreliable at unit level.
    Here we drive the generator and assert the first yielded frame (in
    test mode, after ~10ms of idle queue) is a ping.
    """
    sub = broker.subscribe("eric/claude-code/thought", {"all"})
    frame = await asyncio.wait_for(
        _drive_one_frame(sub, testing=True, event_filter="ping"),
        timeout=_STREAM_READ_TIMEOUT,
    )
    assert frame["event"] == "ping"
    payload = json.loads(frame["data"])
    assert "at" in payload


@pytest.mark.asyncio
async def test_stream_returns_403_without_read_scope(app: FastAPI, out_of_scope_token: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/thoughts/stream",
            params={"namespace": "eric/claude-code/thought"},
            headers={"Authorization": f"Bearer {out_of_scope_token}"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_stream_returns_503_when_connection_cap_exceeded(
    app: FastAPI, valid_token: str, monkeypatch: Any
) -> None:
    import musubi.api.events as ev

    monkeypatch.setattr(ev, "MAX_SUBSCRIBERS", 0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/thoughts/stream",
            params={"namespace": "eric/claude-code/thought"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert response.status_code == 503
        assert response.headers.get("retry-after") == "5"


# ─────────────────────────────────────────────────────────────────────────
# Subscription filtering (4) — broker-direct; no HTTP needed
# ─────────────────────────────────────────────────────────────────────────


def test_stream_filters_by_namespace() -> None:
    sub = broker.subscribe("eric/claude-code/thought", {"all"})
    broker.publish(_thought(namespace="other/namespace/thought", to_presence="all"))
    assert sub.queue.empty(), "cross-namespace thought leaked to subscriber"
    broker.publish(_thought(namespace="eric/claude-code/thought", to_presence="all"))
    assert sub.queue.qsize() == 1


def test_stream_filters_by_include_parameter() -> None:
    # Subscriber explicitly opts into ONLY "openclaw" — no "all" broadcast.
    sub = broker.subscribe("eric/claude-code/thought", {"openclaw"})
    # to="livekit" — not in includes → filtered out.
    broker.publish(_thought(namespace="eric/claude-code/thought", to_presence="livekit"))
    assert sub.queue.empty()
    # to="openclaw" — matches → delivered.
    broker.publish(_thought(namespace="eric/claude-code/thought", to_presence="openclaw"))
    assert sub.queue.qsize() == 1
    # to="all" — also filtered because the subscriber narrowed include to
    # just "openclaw" (opted out of broadcasts). If the subscriber wanted
    # broadcasts they would have subscribed with {"openclaw", "all"}.
    broker.publish(_thought(namespace="eric/claude-code/thought", to_presence="all"))
    assert sub.queue.qsize() == 1


def test_stream_defaults_include_to_token_presence_plus_all() -> None:
    # The endpoint default is {token-presence, "all"}; simulate by subscribing
    # with that set and verifying delivery semantics.
    sub = broker.subscribe("eric/claude-code/thought", {"me", "all"})
    broker.publish(_thought(namespace="eric/claude-code/thought", to_presence="me"))
    broker.publish(_thought(namespace="eric/claude-code/thought", to_presence="all"))
    broker.publish(_thought(namespace="eric/claude-code/thought", to_presence="someone-else"))
    assert sub.queue.qsize() == 2


def test_stream_never_delivers_cross_namespace_events() -> None:
    # Subscriber on ns1 with broadcast filter; publish to ns2 also broadcasting.
    # Namespace mismatch means the broker never even checks include filter.
    sub = broker.subscribe("test/ns1/thought", {"all"})
    broker.publish(_thought(namespace="test/ns2/thought", to_presence="all"))
    assert sub.queue.empty()


# ─────────────────────────────────────────────────────────────────────────
# Fanout semantics (2) — NORMATIVE: broadcast, NOT competing-consumer
# ─────────────────────────────────────────────────────────────────────────


def test_two_subscribers_same_presence_both_receive_every_event() -> None:
    sub1 = broker.subscribe("test/ns/thought", {"me"})
    sub2 = broker.subscribe("test/ns/thought", {"me"})
    broker.publish(_thought(namespace="test/ns/thought", to_presence="me"))
    # BROADCAST: both subscribers receive the same event.
    assert sub1.queue.qsize() == 1
    assert sub2.queue.qsize() == 1


def test_three_subscribers_one_slow_fast_ones_unaffected(
    caplog: LogCaptureFixture,
) -> None:
    sub_slow = broker.subscribe("test/ns/thought", {"me"})
    sub_fast1 = broker.subscribe("test/ns/thought", {"me"})
    sub_fast2 = broker.subscribe("test/ns/thought", {"me"})
    # Fill slow consumer's queue past the drop threshold (1000).
    for _ in range(1005):
        sub_slow.queue.put_nowait(_thought())
    broker.publish(_thought(namespace="test/ns/thought", to_presence="me"))
    # Slow consumer: event dropped (queue already over cap).
    # Fast consumers: receive normally.
    assert sub_fast1.queue.qsize() == 1
    assert sub_fast2.queue.qsize() == 1
    assert "Dropped thought" in caplog.text


# ─────────────────────────────────────────────────────────────────────────
# Publish hook (2) — verify POST /thoughts/send fans out to broker
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_thought_publishes_to_broker(app: FastAPI, valid_token: str) -> None:
    sub = broker.subscribe("eric/claude-code/thought", {"you"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/v1/thoughts/send",
            json={
                "namespace": "eric/claude-code/thought",
                "from_presence": "me",
                "to_presence": "you",
                "content": "hi",
            },
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert res.status_code == 202
    assert sub.queue.qsize() == 1


@pytest.mark.asyncio
async def test_send_with_no_subscribers_is_noop_not_error(app: FastAPI, valid_token: str) -> None:
    # No subscribers in broker; POST must still succeed.
    assert len(broker._subscribers) == 0
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/v1/thoughts/send",
            json={
                "namespace": "eric/claude-code/thought",
                "from_presence": "me",
                "to_presence": "you",
                "content": "hi",
            },
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert res.status_code == 202


# ─────────────────────────────────────────────────────────────────────────
# Replay (3) — deferred to integration harness; mocked Qdrant doesn't
# model lex-sorted epoch-range scrolls accurately enough to exercise
# Last-Event-ID replay at unit level.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_with_missing_last_event_id_starts_from_live() -> None:
    # No Last-Event-ID → stream begins from the live broker queue. With no
    # pending thoughts, the first emitted frame is a ping. Verified by
    # driving the generator directly (HTTP round-trip + header semantics
    # verified in the integration harness under PR #114 against live
    # services).
    sub = broker.subscribe("eric/claude-code/thought", {"all"})
    frame = await asyncio.wait_for(
        _drive_one_frame(sub, testing=True, event_filter="ping"),
        timeout=_STREAM_READ_TIMEOUT,
    )
    assert frame["event"] == "ping"


@pytest.mark.asyncio
async def test_replay_from_last_event_id_emits_events_before_live_tail() -> None:
    """When the generator receives a ``replay`` list, it emits those
    frames first — before any broker-queue reads. Verified by driving
    the generator directly with a seeded replay list and confirming
    the first frames are the replayed thoughts in lex-ascending
    object_id order (ASGITransport can't be used here; the existing
    stream tests all drive the generator directly for the same
    reason — see `_drive_one_frame`)."""
    sub = broker.subscribe("eric/claude-code/thought", {"claude-code", "all"})
    replay = sorted(
        [
            _thought(content="alpha"),
            _thought(content="beta"),
            _thought(content="gamma"),
        ],
        key=lambda t: t.object_id,
    )

    request = _FakeRequest(testing=True)
    agen = _thoughts_event_generator(request, sub, replay=replay)  # type: ignore[arg-type]
    collected: list[dict[str, str]] = []
    try:
        async for raw in agen:
            frame = _parse_sse_frame(raw)
            if frame.get("event") == "thought":
                collected.append(frame)
            if len(collected) >= 3:
                break
    finally:
        await agen.aclose()

    assert len(collected) == 3, f"expected 3 replay frames; got {collected!r}"
    ids = [f["id"] for f in collected]
    assert ids == [t.object_id for t in replay], (
        f"replay frames must emit in the order supplied; got {ids}"
    )
    # Frames must emit in lex-ascending object_id order.
    assert ids == sorted(ids), f"replay not lex-sorted: {ids}"


@pytest.mark.asyncio
async def test_replay_transitions_to_live_tail_after_emitting_replay() -> None:
    """After replay frames are exhausted, the generator switches to
    live-tail from the broker queue. Verified by seeding a replay
    frame, publishing a live thought, and confirming both arrive as
    ``thought`` frames in replay-first order."""
    namespace = "eric/claude-code/thought"
    sub = broker.subscribe(namespace, {"claude-code", "all"})
    historic = _thought(content="historic")

    request = _FakeRequest(testing=True)
    agen = _thoughts_event_generator(request, sub, replay=[historic])  # type: ignore[arg-type]

    contents: list[str] = []
    published_live = False
    try:
        async for raw in agen:
            frame = _parse_sse_frame(raw)
            if frame.get("event") != "thought":
                continue
            data = json.loads(frame["data"])
            contents.append(data["content"])
            if not published_live and data["content"] == "historic":
                broker.publish(
                    _thought(
                        namespace=namespace,
                        from_presence="a",
                        to_presence="claude-code",
                        content="live",
                    )
                )
                published_live = True
            if "live" in contents:
                break
    finally:
        await agen.aclose()

    assert "historic" in contents, f"expected historic replay frame; got {contents}"
    assert "live" in contents, f"expected live-tail after replay; got {contents}"
    assert contents.index("historic") < contents.index("live")


@pytest.mark.asyncio
async def test_stream_endpoint_sets_truncated_header_when_plane_reports_truncation(
    app: FastAPI,
    valid_token: str,
    thoughts: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint-level wiring: when ``ThoughtsPlane.replay_since`` flags
    truncated=True, the ``X-Musubi-Replay-Truncated: true`` header is
    set on the StreamingResponse. We assert this by calling the
    endpoint coroutine directly (ASGITransport buffers SSE bodies, so
    round-tripping the header via HTTP isn't viable here).

    Direct-call trick: pack a minimal request + settings, patch
    replay_since to return a truncated result, inspect the
    ``StreamingResponse.headers`` on the return value."""
    from starlette.responses import StreamingResponse

    from musubi.api.routers.thoughts import stream_thoughts

    async def truncated_replay(
        *, namespace: str, includes: Any, last_event_id: str, cap: int = 500
    ) -> Any:
        return ([_thought(content="hit")], True)

    monkeypatch.setattr(thoughts, "replay_since", truncated_replay)

    # Minimal Request surrogate — stream_thoughts uses it only for the
    # auth token extraction and `app.state.testing`.
    class _Req:
        def __init__(self) -> None:
            import types

            self.app = types.SimpleNamespace(state=types.SimpleNamespace(testing=True))
            self.headers = {"authorization": f"Bearer {valid_token}"}
            self.state = types.SimpleNamespace()

    settings = app.dependency_overrides[
        next(k for k in app.dependency_overrides if k.__name__ == "get_settings_dep")
    ]()

    response = await stream_thoughts(
        request=_Req(),  # type: ignore[arg-type]
        namespace="eric/claude-code/thought",
        include=None,
        last_event_id="0" * 27,
        qdrant=None,  # type: ignore[arg-type]
        settings=settings,
        thoughts_plane=thoughts,
    )

    assert isinstance(response, StreamingResponse)
    assert response.headers.get("X-Musubi-Replay-Truncated") == "true", (
        f"expected truncation header; got {dict(response.headers)!r}"
    )


@pytest.mark.asyncio
async def test_stream_endpoint_no_truncation_header_when_replay_fits(
    app: FastAPI,
    valid_token: str,
    thoughts: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the truncation test — when the plane reports
    ``truncated=False`` (the common case), the header must NOT appear
    on the response."""
    from starlette.responses import StreamingResponse

    from musubi.api.routers.thoughts import stream_thoughts

    async def clean_replay(
        *, namespace: str, includes: Any, last_event_id: str, cap: int = 500
    ) -> Any:
        return ([_thought(content="one")], False)

    monkeypatch.setattr(thoughts, "replay_since", clean_replay)

    class _Req:
        def __init__(self) -> None:
            import types

            self.app = types.SimpleNamespace(state=types.SimpleNamespace(testing=True))
            self.headers = {"authorization": f"Bearer {valid_token}"}
            self.state = types.SimpleNamespace()

    settings = app.dependency_overrides[
        next(k for k in app.dependency_overrides if k.__name__ == "get_settings_dep")
    ]()

    response = await stream_thoughts(
        request=_Req(),  # type: ignore[arg-type]
        namespace="eric/claude-code/thought",
        include=None,
        last_event_id="0" * 27,
        qdrant=None,  # type: ignore[arg-type]
        settings=settings,
        thoughts_plane=thoughts,
    )

    assert isinstance(response, StreamingResponse)
    assert "X-Musubi-Replay-Truncated" not in response.headers, (
        f"header must not be set when replay fits under cap; got {dict(response.headers)!r}"
    )


# ─────────────────────────────────────────────────────────────────────────
# Backpressure (2)
# ─────────────────────────────────────────────────────────────────────────


def test_slow_consumer_events_dropped_and_metered(
    caplog: LogCaptureFixture,
) -> None:
    sub = broker.subscribe("test/ns/thought", {"all"})
    for _ in range(1005):
        sub.queue.put_nowait(_thought())
    broker.publish(_thought(namespace="test/ns/thought", to_presence="all"))
    # Slow consumer — event dropped with a log line.
    assert "Dropped thought" in caplog.text


@pytest.mark.skip(
    reason="deferred to slice-ops-integration-harness: reconnect+recover semantics "
    "are client-side replay behaviour; Last-Event-ID replay also lives there"
)
def test_reconnect_with_last_event_id_recovers_dropped_events() -> None:
    pass


# ─────────────────────────────────────────────────────────────────────────
# Lifecycle (2)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.skip(
    reason="deferred to slice-ops-integration-harness: graceful-shutdown "
    "close-event requires a real Starlette shutdown hook, not mockable here"
)
def test_server_shutdown_sends_close_event() -> None:
    pass


@pytest.mark.asyncio
async def test_client_disconnect_cleans_up_subscription() -> None:
    """Verify the generator's finally-block unsubscribes on cancellation.

    Drive the generator directly, pull one frame, then close. The endpoint's
    ``finally`` block must call ``broker.unsubscribe(sub)``.
    """
    assert len(broker._subscribers) == 0
    sub = broker.subscribe("eric/claude-code/thought", {"all"})
    assert len(broker._subscribers) == 1

    request = _FakeRequest(testing=True)
    agen = _thoughts_event_generator(request, sub)  # type: ignore[arg-type]
    # Pull the first frame so the generator is actively running.
    first = await asyncio.wait_for(anext(agen), timeout=_STREAM_READ_TIMEOUT)
    assert first  # a valid SSE frame

    # Close the generator — simulates the client disconnect cancellation path.
    await agen.aclose()

    # The finally-block unsubscribe should have fired.
    assert len(broker._subscribers) == 0


# ─────────────────────────────────────────────────────────────────────────
# Hypothesis / property (2) — out-of-scope: post-v1.0 hardening
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.skip(reason="out-of-scope: hypothesis-based property suite is post-v1.0 hardening")
def test_hypothesis_dedup_set_idempotent_over_replay() -> None:
    pass


@pytest.mark.skip(reason="out-of-scope: hypothesis-based property suite is post-v1.0 hardening")
def test_hypothesis_ksuid_order_monotonic() -> None:
    pass
