"""Demotion sweeps — filtering unreinforced/old memories from default retrieval.

Matured memories and concepts that haven't earned their place over time are
demoted (not deleted). Demoted objects stay in the index for lineage but are
filtered out of default retrieval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from qdrant_client import QdrantClient, models

from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.scheduler import Job, file_lock
from musubi.planes.concept.plane import ConceptPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.types.common import epoch_of, utc_now

log = logging.getLogger(__name__)

_LIFECYCLE_ACTOR = "lifecycle-worker"

# Tunables (usually from config)
DEMOTION_EPISODIC_AGE_DAYS = 60
DEMOTION_EPISODIC_MAX_IMPORTANCE = 4
DEMOTION_CONCEPT_NO_REINFORCE_DAYS = 30
DEMOTION_ARTIFACT_AGE_DAYS = 180
DEMOTION_ARTIFACT_MIN_SIZE = 1_000_000


class ThoughtEmitter(Protocol):
    async def emit(self, channel: str, content: str, title: str | None = None) -> None: ...


@dataclass
class DemotionDeps:
    qdrant: QdrantClient
    episodic_plane: EpisodicPlane
    concept_plane: ConceptPlane
    events: LifecycleEventSink
    thoughts: ThoughtEmitter


async def demotion_episodic(deps: DemotionDeps, batch_size: int = 100) -> int:
    """Demote mature, untouched, low-importance episodic memories."""
    # Episodic demotion rule is:
    # state == "matured" AND access_count == 0 AND reinforcement_count == 0 AND age > 60d AND importance < 4
    now = utc_now()
    cutoff_epoch = epoch_of(now) - (DEMOTION_EPISODIC_AGE_DAYS * 24 * 3600)

    must_conditions: list[Any] = [
        models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
        models.FieldCondition(key="access_count", match=models.MatchValue(value=0)),
        models.FieldCondition(key="reinforcement_count", match=models.MatchValue(value=0)),
        models.FieldCondition(key="updated_epoch", range=models.Range(lt=cutoff_epoch)),
        models.FieldCondition(
            key="importance", range=models.Range(lt=DEMOTION_EPISODIC_MAX_IMPORTANCE)
        ),
    ]

    offset = None
    demoted_count = 0
    from musubi.store.names import collection_for_plane

    coll_name = collection_for_plane("episodic")

    while True:
        resp = deps.qdrant.scroll(
            collection_name=coll_name,
            scroll_filter=models.Filter(must=must_conditions),
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, offset = resp[0], resp[1]

        for point in points:
            if not point.payload:
                continue

            try:
                namespace = point.payload.get("namespace")
                object_id_str = point.payload.get("object_id")
                await deps.episodic_plane.transition(
                    namespace=cast(Any, namespace),
                    object_id=cast(Any, object_id_str),
                    to_state="demoted",
                    actor=_LIFECYCLE_ACTOR,
                    reason="decay-rule:untouched-low-importance",
                )
                demoted_count += 1
            except Exception as e:
                log.error(
                    "Failed to demote episodic %s: %s",
                    point.payload.get("object_id"),
                    e,
                    exc_info=True,
                )

        if not offset:
            break

    return demoted_count


async def demotion_concept(deps: DemotionDeps, batch_size: int = 100) -> int:
    """Demote mature concepts that haven't been reinforced recently.

    Selects on `last_reinforced_epoch < cutoff` when present, else
    falls back to `created_epoch < cutoff` for concepts that have
    never been reinforced. The fallback is qdrant-side via a `should`
    clause (Qdrant OR); per-point re-verification in Python keeps the
    logic honest against payload-schema drift.
    """
    now = utc_now()
    cutoff_epoch = epoch_of(now) - (DEMOTION_CONCEPT_NO_REINFORCE_DAYS * 24 * 3600)

    # Qdrant filter semantics: `must` is AND across its entries, `should`
    # is OR. Combining must + should requires both to match — so
    # `state == matured` must hold AND one of the two epoch branches
    # must match.
    must_conditions: list[Any] = [
        models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
    ]
    should_conditions: list[Any] = [
        models.FieldCondition(key="last_reinforced_epoch", range=models.Range(lt=cutoff_epoch)),
        models.Filter(
            must=[
                models.IsNullCondition(is_null=models.PayloadField(key="last_reinforced_epoch")),
                models.FieldCondition(key="created_epoch", range=models.Range(lt=cutoff_epoch)),
            ]
        ),
    ]

    offset = None
    demoted_count = 0
    from musubi.store.names import collection_for_plane

    coll_name = collection_for_plane("concept")

    while True:
        resp = deps.qdrant.scroll(
            collection_name=coll_name,
            scroll_filter=models.Filter(must=must_conditions, should=should_conditions),
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, offset = resp[0], resp[1]

        for point in points:
            if not point.payload:
                continue

            # Re-verify in Python: the qdrant `should` clause gates which
            # payloads we see, but a concept with null last_reinforced
            # and fresh created_at would slip through if the fallback
            # clause were mis-wired. Belt-and-braces.
            last_reinforced_epoch = point.payload.get("last_reinforced_epoch")
            reference_epoch = (
                last_reinforced_epoch
                if last_reinforced_epoch is not None
                else point.payload.get("created_epoch")
            )
            if reference_epoch is None or reference_epoch >= cutoff_epoch:
                continue

            try:
                namespace = point.payload.get("namespace")
                object_id_str = point.payload.get("object_id")
                await deps.concept_plane.transition(
                    namespace=cast(Any, namespace),
                    object_id=cast(Any, object_id_str),
                    to_state="demoted",
                    actor=_LIFECYCLE_ACTOR,
                    reason="decay-rule:no-reinforcement",
                )
                await deps.thoughts.emit(
                    "ops-alerts", f"Concept {object_id_str} demoted; reinforcement tapered off"
                )
                demoted_count += 1
            except Exception as e:
                log.error(
                    "Failed to demote concept %s: %s",
                    point.payload.get("object_id"),
                    e,
                    exc_info=True,
                )

        if not offset:
            break

    return demoted_count


async def demotion_artifact(deps: DemotionDeps, batch_size: int = 100) -> int:
    """Archive old, unreferenced, large artifacts.
    Off by default; opt-in per-namespace.
    """
    return 0


async def reinstate(deps: DemotionDeps, namespace: str, object_id: str, reason: str) -> None:
    from musubi.types.common import KSUID

    obj_ksuid = KSUID(object_id)

    # Try episodic
    try:
        e_mem = await deps.episodic_plane.get(namespace=cast(Any, namespace), object_id=obj_ksuid)
        if e_mem:
            await deps.episodic_plane.transition(
                namespace=cast(Any, namespace),
                object_id=obj_ksuid,
                to_state="matured",
                actor=_LIFECYCLE_ACTOR,
                reason=f"reinstatement: {reason}",
            )
            return
    except LookupError:
        pass

    # Try concept
    try:
        c_mem = await deps.concept_plane.get(namespace=cast(Any, namespace), object_id=obj_ksuid)
        if c_mem:
            await deps.concept_plane.transition(
                namespace=cast(Any, namespace),
                object_id=obj_ksuid,
                to_state="matured",
                actor=_LIFECYCLE_ACTOR,
                reason=f"reinstatement: {reason}",
            )
            # Reset the reinforcement clock — otherwise the concept's
            # old `last_reinforced_epoch` (which triggered demotion in
            # the first place) still sits below the cutoff and the
            # next sweep re-demotes it immediately. Reinstatement is
            # an explicit operator call to give the concept a fresh
            # chance, so set the clock to now.
            now = utc_now()
            now_epoch = epoch_of(now)
            from musubi.planes.concept.plane import _point_id
            from musubi.store.names import collection_for_plane

            deps.qdrant.set_payload(
                collection_name=collection_for_plane("concept"),
                payload={
                    "last_reinforced_at": now.isoformat(),
                    "last_reinforced_epoch": now_epoch,
                },
                points=[_point_id(object_id)],
            )
            return
    except LookupError:
        pass

    raise LookupError(
        f"Object {object_id} not found in episodic or concept planes for namespace {namespace}"
    )


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


def build_demotion_jobs(
    *,
    deps: DemotionDeps,
    lock_dir: Path,
    batch_size: int = 100,
) -> list[Job]:
    """Return :class:`Job` objects for the demotion sweeps registered in
    :func:`musubi.lifecycle.scheduler.build_default_jobs`.

    Two jobs today — ``demotion_episodic`` (weekly Sun 03:45 UTC) and
    ``demotion_concept`` (daily 05:00 UTC). ``demotion_artifact`` stays
    opt-in per-namespace and is not scheduled globally.

    The wrapper grabs a file lock per job so two workers on the same
    host can't double-execute, then runs the sweep via ``asyncio.run``
    — matching the shape the runner's ``asyncio.to_thread`` dispatch
    expects.
    """
    import asyncio as _asyncio

    def _wrap(name: str, run_coro_factory: Any) -> Job:
        lock_path = lock_dir / f"{name}.lock"

        def _runner() -> None:
            with file_lock(lock_path) as acquired:
                if not acquired:
                    log.info("lifecycle-job=%s lock-held; skipping run", name)
                    return
                _asyncio.run(run_coro_factory())

        if name == "demotion_episodic":
            kwargs: dict[str, Any] = {"day_of_week": "sun", "hour": 3, "minute": 45}
            grace = 3600
        elif name == "demotion_concept":
            kwargs = {"hour": 5, "minute": 0}
            grace = 3600
        else:  # pragma: no cover — both names enumerated above
            raise ValueError(f"unknown demotion job name: {name}")
        return Job(
            name=name,
            trigger_kind="cron",
            trigger_kwargs=kwargs,
            func=_runner,
            grace_time_s=grace,
        )

    return [
        _wrap(
            "demotion_episodic",
            lambda: demotion_episodic(deps, batch_size=batch_size),
        ),
        _wrap(
            "demotion_concept",
            lambda: demotion_concept(deps, batch_size=batch_size),
        ),
    ]


__all__ = [
    "DemotionDeps",
    "ThoughtEmitter",
    "build_demotion_jobs",
    "demotion_artifact",
    "demotion_concept",
    "demotion_episodic",
    "reinstate",
]
