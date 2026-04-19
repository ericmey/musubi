"""Demotion sweeps — filtering unreinforced/old memories from default retrieval.

Matured memories and concepts that haven't earned their place over time are
demoted (not deleted). Demoted objects stay in the index for lineage but are
filtered out of default retrieval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast

from qdrant_client import QdrantClient, models

from musubi.lifecycle.events import LifecycleEventSink
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
    """Demote mature concepts that haven't been reinforced recently."""
    # concept demotion rule:
    # state == "matured" AND last_reinforced_at < now - 30 days
    # (Since last_reinforced_at is missing, we use updated_epoch for now, but
    # the ticket is logged).

    now = utc_now()
    cutoff_epoch = epoch_of(now) - (DEMOTION_CONCEPT_NO_REINFORCE_DAYS * 24 * 3600)

    must_conditions: list[Any] = [
        models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
        models.FieldCondition(key="updated_epoch", range=models.Range(lt=cutoff_epoch)),
    ]

    offset = None
    demoted_count = 0
    from musubi.store.names import collection_for_plane

    coll_name = collection_for_plane("concept")

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
            return
    except LookupError:
        pass

    raise LookupError(
        f"Object {object_id} not found in episodic or concept planes for namespace {namespace}"
    )
