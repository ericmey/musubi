"""DATA-001 Phase 2 coordinator-seam regressions (Yua ruling, Option B).

The generic `enqueue_custom_intent` + `patch_json` threading generalize the artifact-only custom-intent
path. These pin: the artifact wrapper is unchanged, a generic kind round-trips its patch through a
FRESH coordinator process (replay-from-disk, no caller memory), a malformed/oversized patch fails
truthfully at admission, and cap/already-active semantics are unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from musubi.lifecycle.coordinator import CustomIntentContext, LifecycleTransitionCoordinator


@pytest.fixture
def client() -> Iterator[QdrantClient]:
    c = QdrantClient(":memory:")
    try:
        yield c
    finally:
        c.close()


def test_artifact_index_wrapper_unchanged(client: QdrantClient, tmp_path: Path) -> None:
    coord = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "c.db")
    seen: list[str | None] = []

    def _h(ctx: CustomIntentContext) -> str:
        seen.append(ctx.patch_json)
        return "confirmed"

    coord.register_intent_handler("artifact_index", _h)
    assert coord.enqueue_index_intent(object_id="a1", namespace="t/p/e") == "admitted"
    assert coord.enqueue_index_intent(object_id="a1", namespace="t/p/e") == "already_active"
    coord.reconcile_once()
    assert seen == [None], (
        "the artifact wrapper carries no patch payload (re-derives from Qdrant/blob)"
    )


def test_generic_kind_round_trips_patch_through_fresh_coordinator(
    client: QdrantClient, tmp_path: Path
) -> None:
    db = tmp_path / "c.db"
    coord1 = LifecycleTransitionCoordinator(client=client, db_path=db)
    payload = '{"content":"hello","tags":["p2"],"fingerprint":"fake-v1"}'
    assert (
        coord1.enqueue_custom_intent(
            kind="immutable_vector_publish",
            object_id="o1",
            namespace="t/p/e",
            collection="c",
            patch_json=payload,
        )
        == "admitted"
    )
    # crash: drop coord1, rebuild a FRESH coordinator from disk only, register the handler, reconcile.
    del coord1
    coord2 = LifecycleTransitionCoordinator(client=client, db_path=db)
    got: dict[str, CustomIntentContext] = {}

    def _handler(ctx: CustomIntentContext) -> str:
        got["ctx"] = ctx
        return "confirmed"

    coord2.register_intent_handler("immutable_vector_publish", _handler)
    coord2.reconcile_once()
    assert got["ctx"].patch_json == payload, (
        "the handler must replay the exact persisted patch from disk"
    )
    assert got["ctx"].object_id == "o1" and got["ctx"].namespace == "t/p/e"


def test_malformed_or_oversized_patch_fails_truthfully(
    client: QdrantClient, tmp_path: Path
) -> None:
    coord = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "c.db")
    with pytest.raises(ValueError, match="not valid JSON"):
        coord.enqueue_custom_intent(
            kind="immutable_vector_publish",
            object_id="o",
            namespace="t/p/e",
            collection="c",
            patch_json="{not json",
        )
    big = '{"content":"' + "x" * (64 * 1024) + '"}'
    with pytest.raises(ValueError, match="exceeds"):
        coord.enqueue_custom_intent(
            kind="immutable_vector_publish",
            object_id="o",
            namespace="t/p/e",
            collection="c",
            patch_json=big,
        )
    with pytest.raises(ValueError, match="non-transition"):
        coord.enqueue_custom_intent(
            kind="lifecycle_transition", object_id="o", namespace="t/p/e", collection="c"
        )


def test_cap_and_already_active_unchanged(client: QdrantClient, tmp_path: Path) -> None:
    # already_active (idempotency) is only reachable BELOW the cap — the cap gate is checked FIRST
    # (unchanged ordering): a duplicate admitted while over cap returns at_capacity, not already_active.
    below = LifecycleTransitionCoordinator(
        client=client, db_path=tmp_path / "below.db", pending_cap=10
    )
    assert (
        below.enqueue_custom_intent(kind="k", object_id="o1", namespace="t/p/e", collection="c")
        == "admitted"
    )
    assert (
        below.enqueue_custom_intent(kind="k", object_id="o1", namespace="t/p/e", collection="c")
        == "already_active"
    )
    # at cap=1 a DIFFERENT object is refused with at_capacity (never raises).
    atcap = LifecycleTransitionCoordinator(
        client=client, db_path=tmp_path / "atcap.db", pending_cap=1
    )
    assert (
        atcap.enqueue_custom_intent(kind="k", object_id="o1", namespace="t/p/e", collection="c")
        == "admitted"
    )
    assert (
        atcap.enqueue_custom_intent(kind="k", object_id="o2", namespace="t/p/e", collection="c")
        == "at_capacity"
    )


def test_drive_intent_touches_only_the_named_operation(
    client: QdrantClient, tmp_path: Path
) -> None:
    """DATA-001 P2 named-inline seam: drive_intent(opk) claims + drives ONLY that operation via the
    same handler path, and NEVER touches an unrelated queued intent (proof for the synchronous
    create()/update() path)."""
    coord = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "c.db")
    driven: list[str] = []

    def _h(ctx: CustomIntentContext) -> str:
        driven.append(ctx.operation_key)
        return "confirmed"

    coord.register_intent_handler("k", _h)
    coord.enqueue_custom_intent(
        kind="k",
        object_id="oA",
        namespace="t/p/e",
        collection="c",
        patch_json="{}",
        operation_key="op-A",
    )
    coord.enqueue_custom_intent(
        kind="k",
        object_id="oB",
        namespace="t/p/e",
        collection="c",
        patch_json="{}",
        operation_key="op-B",
    )
    coord.drive_intent("op-A")
    assert driven == ["op-A"], f"drive_intent must touch ONLY the named op; drove {driven}"
    # op-B stays PENDING (untouched) — the worker reconcile drives it.
    coord.reconcile_once()
    assert set(driven) == {"op-A", "op-B"}


def test_drive_intent_bypasses_retry_backoff(client: QdrantClient, tmp_path: Path) -> None:
    """DATA-001 P2, Yua item 4: an explicit inline ``drive_intent`` must re-apply a just-retried intent
    IMMEDIATELY, bypassing the ``next_attempt_epoch`` backoff a background worker paces on — otherwise
    the synchronous publish loop can never re-drive its own 'retry' without a production sleep. With a
    huge backoff the future ``next_attempt_epoch`` would block a non-forced claim, so a second drive that
    still FINALIZES proves the bypass (and that no lease guard was relaxed)."""
    coord = LifecycleTransitionCoordinator(
        client=client, db_path=tmp_path / "c.db", backoff_base_s=3600.0, backoff_max_s=3600.0
    )
    outcomes = iter(["retry", "confirmed"])

    def _h(ctx: CustomIntentContext) -> str:
        return next(outcomes)

    coord.register_intent_handler("k", _h)
    coord.enqueue_custom_intent(
        kind="k", object_id="o1", namespace="t/p/e", collection="c", operation_key="op-1"
    )
    first = coord.drive_intent("op-1")  # handler returns 'retry' -> schedules next_attempt ~1h out
    assert first.pending == 1 and first.finalized == 0
    # a plain worker pass would skip it (backoff not elapsed) — prove that, then prove drive bypasses it.
    assert coord.reconcile_once().finalized == 0, "worker must still honor the backoff window"
    second = coord.drive_intent(
        "op-1"
    )  # explicit inline drive: bypass backoff, claim, re-apply NOW
    assert second.finalized == 1, (
        "drive_intent must bypass the retry backoff and finalize immediately"
    )
