"""Test contract for slice-retrieve-recent — backend (recent.py).

Covers the unit-level contract bullets:
- Results ordered newest-first (delegated to Qdrant `order_by`; we assert
  the call shape).
- `since` filter composes (assert filter membership).
- `tags` AND filter composes (assert one FieldCondition per tag).
- Limit cap (>50 clamped server-side).
- `state_filter` default includes `provisional`.
- No embedder construction or call (no embedder in the signature).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from qdrant_client import QdrantClient, models

from musubi.retrieve.recent import (
    _MAX_LIMIT,
    RecentRetrievalError,
    run_recent_retrieve,
)
from musubi.types.common import Err, Ok

NAMESPACE = "aoi/command-chair/episodic"
COLLECTION = "musubi_episodic"


def _payload(object_id: str, *, created_epoch: float, **extra: Any) -> dict[str, Any]:
    return {
        "object_id": object_id,
        "namespace": NAMESPACE,
        "plane": "episodic",
        "state": "matured",
        "content": f"content {object_id}",
        "created_epoch": created_epoch,
        **extra,
    }


class _SpyQdrantClient:
    """Records the last scroll call's args; returns canned points.

    ``last_kwargs`` is initialized to ``{}`` rather than ``None`` so
    tests can index into it without first narrowing through an
    ``assert is not None``. Every test calls ``run_recent_retrieve``
    before inspecting, so the empty default is never observed in
    practice — it's a type-friendliness shim.
    """

    def __init__(self, points: list[Any] | None = None) -> None:
        self.points = points or []
        self.last_kwargs: dict[str, Any] = {}
        self.scroll_calls = 0

    def scroll(self, **kwargs: Any) -> tuple[list[Any], Any]:
        self.scroll_calls += 1
        self.last_kwargs = kwargs
        return (self.points, None)


def _client(points: list[Any] | None = None) -> tuple[QdrantClient, _SpyQdrantClient]:
    spy = _SpyQdrantClient(points=points)
    return cast(QdrantClient, spy), spy


# ---------------------------------------------------------------------------
# Filter-shape contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orders_by_created_epoch_desc() -> None:
    """Result order is delegated to Qdrant's `order_by`; assert call shape."""
    client, spy = _client(points=[])
    res = await run_recent_retrieve(
        client=client, namespace=NAMESPACE, collection=COLLECTION, limit=5
    )
    assert isinstance(res, Ok)
    assert spy.last_kwargs is not None
    order_by = spy.last_kwargs["order_by"]
    assert isinstance(order_by, models.OrderBy)
    assert order_by.key == "created_epoch"
    assert order_by.direction == models.Direction.DESC


@pytest.mark.asyncio
async def test_namespace_match_in_filter() -> None:
    client, spy = _client(points=[])
    await run_recent_retrieve(client=client, namespace=NAMESPACE, collection=COLLECTION)
    must = spy.last_kwargs["scroll_filter"].must
    ns_cond = next(c for c in must if c.key == "namespace")
    assert ns_cond.match == models.MatchValue(value=NAMESPACE)


@pytest.mark.asyncio
async def test_since_filter_composes() -> None:
    client, spy = _client(points=[])
    await run_recent_retrieve(
        client=client,
        namespace=NAMESPACE,
        collection=COLLECTION,
        since=1779000000.0,
    )
    must = spy.last_kwargs["scroll_filter"].must
    since_cond = next(c for c in must if c.key == "created_epoch")
    assert since_cond.range == models.Range(gte=1779000000.0)


@pytest.mark.asyncio
async def test_since_unset_omits_range_condition() -> None:
    client, spy = _client(points=[])
    await run_recent_retrieve(client=client, namespace=NAMESPACE, collection=COLLECTION)
    must = spy.last_kwargs["scroll_filter"].must
    assert not any(c.key == "created_epoch" for c in must)


@pytest.mark.asyncio
async def test_tags_filter_is_and_across_entries() -> None:
    """Each listed tag becomes its own FieldCondition — AND semantics."""
    client, spy = _client(points=[])
    await run_recent_retrieve(
        client=client,
        namespace=NAMESPACE,
        collection=COLLECTION,
        tags=["voice", "important"],
    )
    must = spy.last_kwargs["scroll_filter"].must
    tag_conds = [c for c in must if c.key == "tags"]
    assert len(tag_conds) == 2
    values = {c.match.value for c in tag_conds}
    assert values == {"voice", "important"}


@pytest.mark.asyncio
async def test_default_state_filter_includes_provisional() -> None:
    """Recent's default differs from fast/deep — includes provisional.

    This is the slice-retrieve-recent design decision: recent mode is "what
    just happened," provisional is the freshest tier, excluding it defeats
    the use case.
    """
    client, spy = _client(points=[])
    await run_recent_retrieve(client=client, namespace=NAMESPACE, collection=COLLECTION)
    must = spy.last_kwargs["scroll_filter"].must
    state_cond = next(c for c in must if c.key == "state")
    assert set(state_cond.match.any) == {"provisional", "matured", "promoted"}


@pytest.mark.asyncio
async def test_explicit_state_filter_overrides_default() -> None:
    client, spy = _client(points=[])
    await run_recent_retrieve(
        client=client,
        namespace=NAMESPACE,
        collection=COLLECTION,
        state_filter=["matured"],
    )
    must = spy.last_kwargs["scroll_filter"].must
    state_cond = next(c for c in must if c.key == "state")
    assert set(state_cond.match.any) == {"matured"}


# ---------------------------------------------------------------------------
# Limit cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_above_cap_is_clamped() -> None:
    client, spy = _client(points=[])
    await run_recent_retrieve(client=client, namespace=NAMESPACE, collection=COLLECTION, limit=999)
    assert spy.last_kwargs["limit"] == _MAX_LIMIT


@pytest.mark.asyncio
async def test_limit_under_cap_passes_through() -> None:
    client, spy = _client(points=[])
    await run_recent_retrieve(client=client, namespace=NAMESPACE, collection=COLLECTION, limit=7)
    assert spy.last_kwargs["limit"] == 7


@pytest.mark.asyncio
async def test_limit_zero_returns_typed_error() -> None:
    client, _ = _client(points=[])
    res = await run_recent_retrieve(
        client=client, namespace=NAMESPACE, collection=COLLECTION, limit=0
    )
    assert isinstance(res, Err)
    assert res.error.code == "invalid_limit"
    assert res.error.status_code == 400


# ---------------------------------------------------------------------------
# Result packing
# ---------------------------------------------------------------------------


class _FakePoint:
    def __init__(self, payload: dict[str, Any], point_id: str = "fakeid") -> None:
        self.payload = payload
        self.id = point_id


@pytest.mark.asyncio
async def test_returns_hits_in_qdrant_returned_order() -> None:
    """Trust Qdrant's order — we hand it `order_by=DESC` and return what
    it gives back without re-sorting client-side."""
    points = [
        _FakePoint(_payload("newer", created_epoch=2000.0)),
        _FakePoint(_payload("older", created_epoch=1000.0)),
    ]
    client, _ = _client(points=points)
    res = await run_recent_retrieve(
        client=client, namespace=NAMESPACE, collection=COLLECTION, limit=5
    )
    assert isinstance(res, Ok)
    assert [h.object_id for h in res.value.results] == ["newer", "older"]


@pytest.mark.asyncio
async def test_hit_snippet_truncates_content() -> None:
    long = "x" * 1000
    points = [_FakePoint(_payload("o1", created_epoch=1.0, content=long))]
    client, _ = _client(points=points)
    res = await run_recent_retrieve(client=client, namespace=NAMESPACE, collection=COLLECTION)
    assert isinstance(res, Ok)
    assert len(res.value.results[0].snippet) == 300


@pytest.mark.asyncio
async def test_scroll_exception_returns_503() -> None:
    class _BoomClient:
        def scroll(self, **kwargs: Any) -> Any:
            raise RuntimeError("qdrant unreachable")

    client = cast(QdrantClient, _BoomClient())
    res = await run_recent_retrieve(client=client, namespace=NAMESPACE, collection=COLLECTION)
    assert isinstance(res, Err)
    assert isinstance(res.error, RecentRetrievalError)
    assert res.error.code == "index_unavailable"
    assert res.error.status_code == 503
