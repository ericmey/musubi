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
from musubi.planes.artifact.plane import ArtifactPlane
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
    artifact_plane: ArtifactPlane | None = None
    artifact_archival_enabled: bool = False


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
    """Archive old, unreferenced artifacts.

    Off by default. Set `DemotionDeps.artifact_archival_enabled = True`
    (driven by `settings.musubi_artifact_archival_enabled`) to opt in.

    When enabled: scrolls `musubi_artifact` for rows older than
    `DEMOTION_ARTIFACT_AGE_DAYS` that aren't referenced by any memory
    (no episodic/curated/concept row carries the artifact's id in its
    `supported_by`). Transitions those to `state=archived` via
    `ArtifactPlane.transition`. The blob itself is preserved — archival
    is a soft-delete, not storage reclamation.
    """
    if not deps.artifact_archival_enabled:
        return 0

    if deps.artifact_plane is None:
        log.warning(
            "Artifact archival is enabled but artifact_plane is not configured; "
            "skipping artifact demotion sweep. Wire `DemotionDeps.artifact_plane` "
            "in the lifecycle-worker bootstrap."
        )
        return 0

    now = utc_now()
    cutoff_epoch = epoch_of(now) - (DEMOTION_ARTIFACT_AGE_DAYS * 24 * 3600)

    # Pre-compute the full set of referenced artifact ids once per sweep.
    # Nested-list field filters (`supported_by.artifact_id`) aren't
    # uniformly supported across Qdrant back-ends (notably the in-memory
    # client), so we scroll every memory-carrying plane and accumulate
    # the ids in Python. O(m) once, then O(n) membership checks — saves
    # the O(n*m) per-artifact probe a naive implementation would do.
    referenced_ids = _collect_referenced_artifact_ids(deps.qdrant)

    from musubi.store.names import collection_for_plane

    coll_name = collection_for_plane("artifact")

    must_conditions: list[Any] = [
        models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
        models.FieldCondition(key="created_epoch", range=models.Range(lt=cutoff_epoch)),
    ]

    offset = None
    archived_count = 0

    while True:
        resp = deps.qdrant.scroll(
            collection_name=coll_name,
            scroll_filter=models.Filter(must=must_conditions),
            limit=batch_size,
            offset=offset,
            # Narrow payload: we only need the ids here, not the full
            # artifact row (content-type, blob metadata, etc.). On
            # sizable collections this saves real bytes per sweep.
            with_payload=["namespace", "object_id"],
            with_vectors=False,
        )
        points, offset = resp[0], resp[1]

        for point in points:
            if not point.payload:
                continue

            object_id_str = point.payload.get("object_id")
            namespace = point.payload.get("namespace")
            if not object_id_str or not namespace:
                continue

            if object_id_str in referenced_ids:
                continue

            try:
                _, _event = await deps.artifact_plane.transition(
                    namespace=cast(Any, namespace),
                    object_id=cast(Any, object_id_str),
                    to_state="archived",
                    actor=_LIFECYCLE_ACTOR,
                    reason="decay-rule:unreferenced-expired",
                )
                archived_count += 1
            except Exception as e:
                log.error(
                    "Failed to archive artifact %s: %s",
                    object_id_str,
                    e,
                    exc_info=True,
                )

        if not offset:
            break

    return archived_count


def _collect_referenced_artifact_ids(client: QdrantClient) -> set[str]:
    """Return the set of artifact ids referenced by any live memory.

    Scrolls `musubi_episodic`, `musubi_curated`, `musubi_concept` — the
    three planes whose payloads carry `supported_by`. Demoted / archived
    rows still pin their artifacts (we don't reclaim retroactively);
    per-row state filtering lives at retrieval, not here.
    """
    from musubi.store.names import collection_for_plane

    referenced: set[str] = set()
    for plane_name in ("episodic", "curated", "concept"):
        coll = collection_for_plane(plane_name)
        offset: Any = None
        while True:
            points, offset = client.scroll(
                collection_name=coll,
                limit=500,
                offset=offset,
                # Narrow payload: we only read `supported_by` here, not
                # the full row (which carries multi-KB `content` fields
                # on curated/concept). Saves O(total-bytes) per sweep.
                with_payload=["supported_by"],
                with_vectors=False,
            )
            for point in points:
                if not point.payload:
                    continue
                for ref in point.payload.get("supported_by") or []:
                    artifact_id = ref.get("artifact_id") if isinstance(ref, dict) else None
                    if artifact_id:
                        referenced.add(str(artifact_id))
            if not offset:
                break
    return referenced


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

    Three jobs — ``demotion_episodic`` (weekly Sun 03:45 UTC),
    ``demotion_concept`` (daily 05:00 UTC), and ``demotion_artifact``
    (weekly Sun 04:15 UTC). The artifact sweep self-gates on
    ``deps.artifact_archival_enabled``; scheduling it unconditionally
    keeps the cron registry stable while letting the feature flag
    decide whether it does any work.

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
        elif name == "demotion_artifact":
            # Runs right after the episodic sweep so the referenced-by
            # probe sees any fresh demotions before deciding which
            # artifacts are orphans.
            kwargs = {"day_of_week": "sun", "hour": 4, "minute": 15}
            grace = 3600
        else:  # pragma: no cover — all names enumerated above
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
        _wrap(
            "demotion_artifact",
            lambda: demotion_artifact(deps, batch_size=batch_size),
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
