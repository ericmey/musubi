"""An in-flight sweep must never enrich a row that left `matured` under it.

`_apply_enrichment` wrote `importance`, `updated_at` and `updated_epoch` fenced on
`object_id` ALONE — no state, no version, no lease. The sweep transitions a row to
`matured` and enriches it as two separate writes, so anything that moves the row in
between is overwritten by the second one.

The case that made this urgent (Copilot on musubi#732, 2026-09-20): a retraction
quarantines the row to `archived`/importance 1 in that gap. The enrichment write then
lands anyway and the retraction finishes with a **rescored importance and a
post-retraction `updated_at`** — breaking RET-012's terminal-state guarantee and
IDEM-008's "the retraction timestamp is the retraction's" in one `set_payload`.

The fence is `state == "matured"`: only a row still in the state this sweep just put
it in may be enriched. That single condition also excludes v2 immutable content points
for free, because they carry no `state` field at all — a `FieldCondition` cannot match
a point that lacks the key. `test_v2_content_point_is_never_enriched` pins that, so if
`state` is ever added to content payloads the exclusion stops being accidental.

Version was considered and deliberately not used: the sweep does not hold the
post-transition version without an extra read, and state is the property that actually
matters — an archived row must never be enriched at ANY version.

Shiori, 2026-09-20.
"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient, models

from musubi.lifecycle import maturation
from musubi.lifecycle.maturation import episodic_maturation_sweep
from musubi.planes.episodic import EpisodicPlane
from musubi.types.common import epoch_of, utc_now
from musubi.types.episodic import EpisodicMemory

from tests.lifecycle.test_maturation import (  # noqa: F401 -- fixtures
    FakeOllama,
    _config,
    _coordinator,
    _seed_provisional,
    cursor,
    ns,
    plane,
    qdrant,
    sink,
)


def _payload(client: QdrantClient, object_id: str) -> list[dict[str, Any]]:
    rows, _ = client.scroll(
        collection_name="musubi_episodic",
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=8,
        with_payload=True,
    )
    return [dict(r.payload or {}) for r in rows]


async def test_a_row_archived_mid_sweep_is_not_enriched(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
    cursor: Any,
) -> None:
    """THE DEFECT. Archive the row in the exact gap between the sweep's transition
    and its enrichment write, which is the window a retraction quarantine occupies."""
    row = await _seed_provisional(plane, ns, content="retract me", age_seconds=7200)

    real_transition = maturation.transition
    archived_at: dict[str, Any] = {}

    def transition_then_archive(*args: Any, **kwargs: Any) -> Any:
        result = real_transition(*args, **kwargs)
        # The quarantine CAS lands here: after the sweep matured the row, before it
        # enriches. Written directly so the test does not depend on the retraction API.
        quarantined = utc_now()
        archived_at["updated_at"] = quarantined.isoformat()
        qdrant.set_payload(
            collection_name="musubi_episodic",
            payload={"state": "archived", "importance": 1,
                     "updated_at": quarantined.isoformat(),
                     "updated_epoch": epoch_of(quarantined)},
            points=models.Filter(must=[models.FieldCondition(
                key="object_id", match=models.MatchValue(value=str(row.object_id)))]),
            wait=True,
        )
        return result

    maturation.transition = transition_then_archive  # type: ignore[assignment]
    try:
        await episodic_maturation_sweep(
            client=qdrant, sink=sink, coordinator=_coordinator(qdrant, sink),
            ollama=FakeOllama(topic_map={}), cursor=cursor, config=_config(min_age_sec=3600),
        )
    finally:
        maturation.transition = real_transition  # type: ignore[assignment]

    after = (await plane.get(namespace=ns, object_id=row.object_id))
    assert after is not None
    assert after.state == "archived", "the quarantine itself did not hold"
    assert after.importance == 1, (
        f"the sweep re-scored a retracted row to importance {after.importance}; "
        f"RET-012's terminal state is not terminal")
    assert after.updated_at.isoformat() == archived_at["updated_at"], (
        f"the sweep stamped a post-retraction updated_at ({after.updated_at}); "
        f"the retraction timestamp must remain the retraction's "
        f"({archived_at['updated_at']})")


async def test_an_ordinary_row_is_still_enriched(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
    cursor: Any,
) -> None:
    """THE POSITIVE CONTROL. Without it, a fence that refuses everything passes the
    cell above — and the sweep would silently stop enriching anything at all."""
    row = await _seed_provisional(plane, ns, content="GPU pin: nvidia driver 575",
                                  age_seconds=7200)

    report = await episodic_maturation_sweep(
        client=qdrant, sink=sink, coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor, config=_config(min_age_sec=3600),
    )

    assert report.transitioned == 1
    assert report.enriched == 1, "the fence refused an ordinary, still-matured row"
    after = await plane.get(namespace=ns, object_id=row.object_id)
    assert after is not None and after.state == "matured"
    assert "hardware/gpu" in after.linked_to_topics


async def test_v2_content_point_is_never_enriched(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
    cursor: Any,
) -> None:
    """The enrichment filter matched EVERY point carrying the object_id, so for a v2
    row it wrote to the immutable content point as well as the anchor.

    The `state` fence excludes content points because they carry no `state` key. That
    is correct but incidental, so it is pinned here: if `state` is ever added to a
    content payload, this cell fails rather than the immutability guarantee."""
    row = await _seed_provisional(plane, ns, content="two-point row", age_seconds=7200)
    # A content-shaped sibling: same object_id, no `state`, as v2 publishes.
    qdrant.upsert(
        collection_name="musubi_episodic",
        points=[models.PointStruct(
            id="00000000-0000-4000-8000-00000000c0de",
            vector={}, payload={"object_id": str(row.object_id), "namespace": ns,
                                "point_kind": "content", "importance": 8},
        )],
        wait=True,
    )
    before = [p for p in _payload(qdrant, str(row.object_id))
              if p.get("point_kind") == "content"][0]

    await episodic_maturation_sweep(
        client=qdrant, sink=sink, coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={}), cursor=cursor, config=_config(min_age_sec=3600),
    )

    after = [p for p in _payload(qdrant, str(row.object_id))
             if p.get("point_kind") == "content"][0]
    assert after == before, "enrichment wrote to the immutable content point"
