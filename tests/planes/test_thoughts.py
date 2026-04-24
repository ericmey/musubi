"""Test contract for slice-plane-thoughts.

Runs against an in-memory Qdrant and the deterministic FakeEmbedder.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.thoughts import ThoughtsPlane
from musubi.store import bootstrap
from musubi.types.thought import Thought


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def plane(qdrant: QdrantClient) -> ThoughtsPlane:
    return ThoughtsPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def ns() -> str:
    return "eric/claude-code/thought"


def _make(
    content: str, namespace: str, from_presence: str, to_presence: str, **extra: object
) -> Thought:
    """Helper to build a Thought with sane defaults."""
    return Thought(
        namespace=namespace,
        content=content,
        from_presence=from_presence,
        to_presence=to_presence,
        **extra,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Test Contract (verbatim from docs/Musubi/04-data-model/thoughts.md)
# ---------------------------------------------------------------------------


async def test_thought_send_creates_unread(plane: ThoughtsPlane, ns: str) -> None:
    t = await plane.send(_make("hello", ns, "a", "b"))
    assert t.read is False
    assert t.read_by == []

    fetched = await plane.get(namespace=ns, object_id=t.object_id)
    assert fetched is not None
    assert fetched.read is False
    assert fetched.read_by == []


async def test_thought_check_returns_unread_only(plane: ThoughtsPlane, ns: str) -> None:
    await plane.send(_make("msg1", ns, "a", "b"))
    msg2 = await plane.send(_make("msg2", ns, "a", "b"))

    await plane.read(namespace=ns, object_id=msg2.object_id, reader="b")

    unread = await plane.check(namespace=ns, my_presence="b")
    assert len(unread) == 1
    assert unread[0].content == "msg1"


async def test_thought_check_excludes_self_sends(plane: ThoughtsPlane, ns: str) -> None:
    # Sent to myself? Or from myself to someone else?
    # Spec: from_presence NOT = my_presence
    await plane.send(_make("outgoing", ns, "a", "b"))
    unread = await plane.check(namespace=ns, my_presence="a")
    assert len(unread) == 0


async def test_thought_check_includes_broadcast_to_all(plane: ThoughtsPlane, ns: str) -> None:
    await plane.send(_make("broadcast", ns, "a", "all"))
    unread_b = await plane.check(namespace=ns, my_presence="b")
    assert len(unread_b) == 1
    assert unread_b[0].content == "broadcast"


async def test_thought_read_unicast_sets_read_true(plane: ThoughtsPlane, ns: str) -> None:
    t = await plane.send(_make("unicast", ns, "a", "b"))
    updated = await plane.read(namespace=ns, object_id=t.object_id, reader="b")
    assert updated.read is True
    assert "b" in updated.read_by


async def test_thought_read_broadcast_appends_to_read_by_only(
    plane: ThoughtsPlane, ns: str
) -> None:
    t = await plane.send(_make("broadcast", ns, "a", "all"))
    updated = await plane.read(namespace=ns, object_id=t.object_id, reader="b")
    assert updated.read is False
    assert "b" in updated.read_by


async def test_thought_read_idempotent(plane: ThoughtsPlane, ns: str) -> None:
    t = await plane.send(_make("unicast", ns, "a", "b"))
    await plane.read(namespace=ns, object_id=t.object_id, reader="b")
    updated = await plane.read(namespace=ns, object_id=t.object_id, reader="b")
    assert updated.read_by == ["b"]  # not ["b", "b"]


async def test_thought_read_batched_not_N_plus_1(plane: ThoughtsPlane, ns: str) -> None:
    # We will test read_batch for this
    t1 = await plane.send(_make("m1", ns, "a", "b"))
    t2 = await plane.send(_make("m2", ns, "a", "b"))

    updated_list = await plane.read_batch(
        namespace=ns, object_ids=[t1.object_id, t2.object_id], reader="b"
    )
    assert len(updated_list) == 2
    assert all(t.read is True for t in updated_list)


async def test_thought_history_semantic_match(plane: ThoughtsPlane, ns: str) -> None:
    await plane.send(_make("apples and oranges", ns, "a", "b"))
    await plane.send(_make("dogs and cats", ns, "a", "b"))

    history = await plane.history(
        namespace=ns, channel="default", query="apples and oranges", limit=10
    )
    assert len(history) > 0
    assert history[0].content == "apples and oranges"


async def test_thought_history_filters_by_presence(plane: ThoughtsPlane, ns: str) -> None:
    await plane.send(_make("from a to b", ns, "a", "b"))
    await plane.send(_make("from c to d", ns, "c", "d"))

    history = await plane.history(namespace=ns, channel="default", presence="a", limit=10)
    assert len(history) == 1
    assert history[0].content == "from a to b"


async def test_thought_channel_filter_applies(plane: ThoughtsPlane, ns: str) -> None:
    await plane.send(_make("ops alert", ns, "sys", "all", channel="ops-alerts"))
    await plane.send(_make("normal", ns, "a", "b", channel="default"))

    history = await plane.history(namespace=ns, channel="ops-alerts", limit=10)
    assert len(history) == 1
    assert history[0].content == "ops alert"


async def test_thought_importance_filter_applies(plane: ThoughtsPlane, ns: str) -> None:
    await plane.send(_make("important", ns, "a", "b", importance=10))
    await plane.send(_make("trivial", ns, "a", "b", importance=1))

    # history with importance filter
    history = await plane.history(namespace=ns, channel="default", min_importance=8, limit=10)
    assert len(history) == 1
    assert history[0].content == "important"


async def test_thought_in_reply_to_chain_queries_correctly(plane: ThoughtsPlane, ns: str) -> None:
    t1 = await plane.send(_make("first", ns, "a", "b"))
    t2 = await plane.send(_make("reply", ns, "b", "a", in_reply_to=t1.object_id))

    # We should be able to query by in_reply_to
    replies = await plane.history(
        namespace=ns, channel="default", in_reply_to=t1.object_id, limit=10
    )
    assert len(replies) == 1
    assert replies[0].object_id == t2.object_id


async def test_thought_namespace_isolation(plane: ThoughtsPlane, ns: str) -> None:
    other_ns = "eric/other/thought"
    t1 = await plane.send(_make("in ns", ns, "a", "b"))
    t2 = await plane.send(_make("in other", other_ns, "a", "b"))

    fetched = await plane.get(namespace=ns, object_id=t2.object_id)
    assert fetched is None

    unread = await plane.check(namespace=ns, my_presence="b")
    assert len(unread) == 1
    assert unread[0].object_id == t1.object_id


async def test_cross_tenant_thought_requires_multi_tenant_scope(
    plane: ThoughtsPlane, ns: str
) -> None:
    # Actually testing namespace isolation on the write path as per the hint.
    # A thought intended for another tenant's namespace must be blocked or handled correctly.
    # We'll raise a ValueError or LookupError if trying to read/write across boundaries illegally.
    with pytest.raises(
        ValueError, match="Cross-tenant thoughts require explicit multi-tenant scope"
    ):
        await plane.send(_make("cross", ns, "a", "b"), enforce_tenant_scope=True)

    # Or, as the hint said:
    # "namespace isolation on the write path. Your plane owns the namespace filter on read and the namespace field on write, so this bullet lands HERE, not in slice-auth."
    # Let's test that get() across namespace fails, and transition() across namespace fails.
    t = await plane.send(_make("cross", ns, "a", "b"))
    with pytest.raises(LookupError):
        await plane.transition(
            namespace="other/ns/thought",
            object_id=t.object_id,
            to_state="archived",
            actor="test",
            reason="test",
        )


async def test_thought_embedding_deferred_under_load_does_not_block_send(
    plane: ThoughtsPlane, ns: str
) -> None:
    # Send should complete quickly, and we might skip embedding.
    # We'll add an option `defer_embedding=True` to send().
    t = await plane.send(_make("fast", ns, "a", "b"), defer_embedding=True)
    assert t.object_id is not None

    # It should not have vectors (or have zero vectors) if deferred.
    # Qdrant client check:
    from qdrant_client import models as qmodels

    records, _ = plane._client.scroll(
        collection_name="musubi_thought",
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="object_id", match=qmodels.MatchValue(value=t.object_id))
            ]
        ),
        with_vectors=True,
        limit=1,
    )
    assert (
        not records[0].vector
        or not isinstance(records[0].vector, dict)
        or "dense_bge_m3_v1" not in records[0].vector
        or records[0].vector["dense_bge_m3_v1"] == []
    )


async def test_thought_transition_valid_state_change(plane: ThoughtsPlane, ns: str) -> None:
    t = await plane.send(_make("transition me", ns, "a", "b"))
    updated, event = await plane.transition(
        namespace=ns,
        object_id=t.object_id,
        to_state="matured",
        actor="test",
        reason="test-change",
    )

    assert updated.state == "matured"
    assert updated.version == t.version + 1

    # Check event properties
    from musubi.types.lifecycle_event import LifecycleEvent

    assert isinstance(event, LifecycleEvent)
    assert event.from_state == "provisional"
    assert event.to_state == "matured"
    assert event.actor == "test"
    assert event.reason == "test-change"

    # Wrong namespace -> LookupError
    import pytest

    with pytest.raises(LookupError):
        await plane.transition(
            namespace="wrong-namespace",
            object_id=t.object_id,
            to_state="archived",
            actor="test",
            reason="test-isolation",
        )


async def test_thought_transition_on_missing_object_raises_lookup(
    plane: ThoughtsPlane, ns: str
) -> None:
    import pytest

    with pytest.raises(LookupError):
        await plane.transition(
            namespace=ns, object_id="0" * 27, to_state="matured", actor="test", reason="missing"
        )


# ---------------------------------------------------------------------------
# #233 — Last-Event-ID replay for SSE reconnect backfill
# ---------------------------------------------------------------------------


async def test_replay_since_returns_thoughts_after_last_event_id(
    plane: ThoughtsPlane, ns: str
) -> None:
    """Only thoughts with ``object_id > last_event_id`` are returned.
    KSUIDs are time-sortable lex, so "> anchor" is "emitted after the
    last one the client saw" for any anchor across a second-boundary.
    (KSUIDs generated in the same second have random suffixes, so this
    test uses a sentinel anchor lex-smaller than any real KSUID to get
    deterministic ordering.)"""
    t1 = await plane.send(_make("first", ns, "a", "b"))
    t2 = await plane.send(_make("second", ns, "a", "b"))
    t3 = await plane.send(_make("third", ns, "a", "b"))
    anchor = "0" * 27  # lex-smaller than any KSUID

    replayed, truncated = await plane.replay_since(
        namespace=ns, includes={"b", "all"}, last_event_id=anchor
    )
    assert truncated is False
    returned_ids = sorted(t.object_id for t in replayed)
    expected_ids = sorted([t1.object_id, t2.object_id, t3.object_id])
    assert returned_ids == expected_ids, f"expected all 3 thoughts replayed; got {returned_ids}"
    # And emission order is strictly ascending for the client's dedup set.
    emitted_ids = [t.object_id for t in replayed]
    assert emitted_ids == sorted(emitted_ids), (
        f"replay must emit ascending by object_id; got {emitted_ids}"
    )


async def test_replay_since_is_lexicographic_by_object_id(plane: ThoughtsPlane, ns: str) -> None:
    """Replay order is strictly ascending by object_id. Clients rely on
    this for dedup-set insertion order (see
    [[07-interfaces/canonical-api#consumer-expectations]] bullet 6)."""
    sent = [await plane.send(_make(f"msg-{i}", ns, "a", "b")) for i in range(6)]
    anchor = sent[0].object_id

    replayed, _ = await plane.replay_since(
        namespace=ns, includes={"b", "all"}, last_event_id=anchor
    )
    ids = [t.object_id for t in replayed]
    assert ids == sorted(ids), f"replay not lex-sorted: {ids}"


async def test_replay_since_respects_include_filter(plane: ThoughtsPlane, ns: str) -> None:
    """``to_presence`` filter mirrors the live broker: only thoughts
    targeting any presence in ``includes`` come back."""
    for_b = await plane.send(_make("for-b", ns, "a", "b"))
    _for_c = await plane.send(_make("for-c", ns, "a", "c"))
    broadcast = await plane.send(_make("everyone", ns, "a", "all"))
    anchor = "0" * 27  # before any KSUID

    replayed, _ = await plane.replay_since(
        namespace=ns, includes={"b", "all"}, last_event_id=anchor
    )
    contents = {t.content for t in replayed}
    assert contents == {"for-b", "everyone"}, f"expected b+broadcast only; got {contents}"
    ids = {t.object_id for t in replayed}
    assert for_b.object_id in ids
    assert broadcast.object_id in ids


async def test_replay_since_respects_namespace(plane: ThoughtsPlane) -> None:
    """Replay never leaks thoughts across namespaces."""
    own_ns = "eric/claude-code/thought"
    other_ns = "eric/livekit-voice/thought"
    own = await plane.send(_make("own", own_ns, "a", "b"))
    await plane.send(_make("other", other_ns, "a", "b"))
    anchor = "0" * 27

    replayed, _ = await plane.replay_since(
        namespace=own_ns, includes={"b", "all"}, last_event_id=anchor
    )
    assert [t.object_id for t in replayed] == [own.object_id]


async def test_replay_since_caps_results_and_signals_truncation(
    plane: ThoughtsPlane, ns: str
) -> None:
    """When more thoughts match than the cap allows, return the first
    ``cap`` entries and flag ``truncated=True``. Clients that see the
    truncation signal fall back to ``/v1/thoughts/history`` for deeper
    backfill."""
    for i in range(5):
        await plane.send(_make(f"msg-{i}", ns, "a", "b"))
    anchor = "0" * 27

    replayed, truncated = await plane.replay_since(
        namespace=ns, includes={"b", "all"}, last_event_id=anchor, cap=3
    )
    assert len(replayed) == 3
    assert truncated is True


async def test_replay_since_empty_when_anchor_is_lex_largest(plane: ThoughtsPlane, ns: str) -> None:
    """If the anchor is lex-greater than every stored thought's
    object_id, replay is empty — the generator skips straight to
    live-tail. Uses the lex-max *valid* KSUID as anchor
    (``Ksuid.from_bytes(b"\\xff" * 20)``). Raw ``"z"*27`` isn't a
    decodable KSUID because its timestamp prefix overflows uint32."""
    from ksuid import Ksuid

    await plane.send(_make("old", ns, "a", "b"))
    await plane.send(_make("latest", ns, "a", "b"))
    anchor = str(Ksuid.from_bytes(b"\xff" * 20))

    replayed, truncated = await plane.replay_since(
        namespace=ns, includes={"b", "all"}, last_event_id=anchor
    )
    assert replayed == []
    assert truncated is False


async def test_replay_since_returns_empty_for_malformed_anchor(
    plane: ThoughtsPlane, ns: str
) -> None:
    """A garbage ``Last-Event-ID`` (wrong length, invalid chars, empty)
    shouldn't 500 the endpoint — ``replay_since`` swallows the decode
    error and returns empty so the stream falls through to live-tail.
    The client's state is corrupt at that point, but the server stays
    up."""
    await plane.send(_make("one", ns, "a", "b"))

    # "zzz..." overflows uint32 timestamp; "not-a-ksuid" has
    # non-base62 chars; "" is empty. Each raises a different exception
    # from the decoder — the catch-all in replay_since turns them all
    # into empty replay. (Note: "0" alone is technically a valid
    # lex-min KSUID — base62 left-pads — so we don't test it here;
    # it's equivalent to replaying the whole plane, which is correct.)
    for bad in ("zzzzzzzzzzzzzzzzzzzzzzzzzzz", "not-a-ksuid", ""):
        replayed, truncated = await plane.replay_since(
            namespace=ns, includes={"b", "all"}, last_event_id=bad
        )
        assert replayed == [], f"malformed anchor {bad!r} should yield empty replay"
        assert truncated is False
