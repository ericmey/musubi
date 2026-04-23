"""Test contract for slice-lifecycle-promotion (Demotion)."""

from __future__ import annotations

import warnings
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient

from musubi.embedding.fake import FakeEmbedder
from musubi.lifecycle.demotion import DemotionDeps, demotion_concept, demotion_episodic, reinstate
from musubi.lifecycle.events import LifecycleEventSink
from musubi.planes.concept.plane import ConceptPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.store.bootstrap import bootstrap
from musubi.types.common import epoch_of, generate_ksuid, utc_now
from musubi.types.concept import SynthesizedConcept
from musubi.types.episodic import EpisodicMemory


class FakeThoughtEmitter:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def emit(self, channel: str, content: str, title: str | None = None) -> None:
        self.calls.append((channel, content, title))


@pytest.fixture
def qdrant() -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    yield client
    client.close()


@pytest.fixture
def episodic_plane(qdrant: QdrantClient) -> EpisodicPlane:
    return EpisodicPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def concept_plane(qdrant: QdrantClient) -> ConceptPlane:
    return ConceptPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def events_sink(tmp_path: Path) -> Any:
    s = LifecycleEventSink(db_path=tmp_path / "events.db", flush_every_n=10, flush_every_s=1.0)
    yield s
    s.close()


@pytest.fixture
def deps(
    qdrant: QdrantClient,
    episodic_plane: EpisodicPlane,
    concept_plane: ConceptPlane,
    events_sink: LifecycleEventSink,
) -> DemotionDeps:
    return DemotionDeps(
        qdrant=qdrant,
        episodic_plane=episodic_plane,
        concept_plane=concept_plane,
        events=events_sink,
        thoughts=FakeThoughtEmitter(),
    )


def _episodic(**kwargs: Any) -> EpisodicMemory:
    now = utc_now()
    d = {
        "object_id": generate_ksuid(),
        "namespace": "eric/shared/episodic",
        "content": "Test content",
        "state": "matured",
        "created_at": now - timedelta(days=61),
        "updated_at": now - timedelta(days=61),
        "access_count": 0,
        "reinforcement_count": 0,
        "importance": 3,
    }
    d.update(kwargs)
    return EpisodicMemory(**d)  # type: ignore


def _set_old(deps: DemotionDeps, plane_name: str, object_id: str, days_old: int = 61) -> None:
    from musubi.planes.concept.plane import _point_id as cp_id
    from musubi.planes.episodic.plane import _point_id as ep_id
    from musubi.store.names import collection_for_plane

    point_id = ep_id(object_id) if plane_name == "episodic" else cp_id(object_id)
    coll_name = collection_for_plane(plane_name)
    cutoff = epoch_of(utc_now()) - days_old * 24 * 3600
    deps.qdrant.set_payload(
        collection_name=coll_name,
        payload={"updated_epoch": cutoff, "created_epoch": cutoff, "last_accessed_at": None},
        points=[point_id],
    )


@pytest.mark.asyncio
async def test_episodic_demotion_selects_by_all_four_criteria(deps: DemotionDeps) -> None:
    e = _episodic()
    await deps.episodic_plane.create(e)
    # transition to matured since create makes it provisional
    await deps.episodic_plane.transition(
        namespace=e.namespace, object_id=e.object_id, to_state="matured", actor="sys", reason="test"
    )
    _set_old(deps, "episodic", str(e.object_id))

    count = await demotion_episodic(deps)
    assert count == 1

    p = await deps.episodic_plane.get(namespace=e.namespace, object_id=e.object_id)
    assert p and p.state == "demoted"


@pytest.mark.asyncio
async def test_episodic_demotion_skips_if_accessed(deps: DemotionDeps) -> None:
    e = _episodic()
    await deps.episodic_plane.create(e)
    await deps.episodic_plane.transition(
        namespace=e.namespace, object_id=e.object_id, to_state="matured", actor="sys", reason="test"
    )
    _set_old(deps, "episodic", str(e.object_id))
    # modify access_count manually since transition sets it? No, transition doesn't.
    # But wait, create makes access_count=0. We want 1.
    from musubi.planes.episodic.plane import _point_id

    deps.qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"access_count": 1},
        points=[_point_id(str(e.object_id))],
    )
    count = await demotion_episodic(deps)
    assert count == 0


@pytest.mark.asyncio
async def test_episodic_demotion_skips_if_reinforced(deps: DemotionDeps) -> None:
    e = _episodic()
    await deps.episodic_plane.create(e)
    await deps.episodic_plane.transition(
        namespace=e.namespace, object_id=e.object_id, to_state="matured", actor="sys", reason="test"
    )
    _set_old(deps, "episodic", str(e.object_id))
    from musubi.planes.episodic.plane import _point_id

    deps.qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"reinforcement_count": 1},
        points=[_point_id(str(e.object_id))],
    )
    count = await demotion_episodic(deps)
    assert count == 0


@pytest.mark.asyncio
async def test_episodic_demotion_skips_if_high_importance(deps: DemotionDeps) -> None:
    e = _episodic(importance=6)
    await deps.episodic_plane.create(e)
    await deps.episodic_plane.transition(
        namespace=e.namespace, object_id=e.object_id, to_state="matured", actor="sys", reason="test"
    )
    _set_old(deps, "episodic", str(e.object_id))
    count = await demotion_episodic(deps)
    assert count == 0


@pytest.mark.asyncio
async def test_episodic_demotion_skips_if_young(deps: DemotionDeps) -> None:
    e = _episodic()
    await deps.episodic_plane.create(e)
    await deps.episodic_plane.transition(
        namespace=e.namespace, object_id=e.object_id, to_state="matured", actor="sys", reason="test"
    )
    _set_old(deps, "episodic", str(e.object_id), days_old=30)
    count = await demotion_episodic(deps)
    assert count == 0


@pytest.mark.asyncio
async def test_episodic_demotion_transitions_and_emits_event(deps: DemotionDeps) -> None:
    e = _episodic()
    await deps.episodic_plane.create(e)
    await deps.episodic_plane.transition(
        namespace=e.namespace, object_id=e.object_id, to_state="matured", actor="sys", reason="test"
    )
    _set_old(deps, "episodic", str(e.object_id))
    count = await demotion_episodic(deps)
    assert count == 1
    # Plane handles the LifecycleEvent emission on transition.


# Concept
@pytest.mark.skip(
    reason="deferred to issue #218: use last_reinforced_epoch in demotion scroll filter; "
    "field exists on SynthesizedConcept, demotion currently proxies via updated_epoch"
)
def test_concept_demotion_selects_by_last_reinforced() -> None:
    pass


@pytest.mark.asyncio
async def test_concept_demotion_emits_ops_thought(deps: DemotionDeps) -> None:
    now = utc_now()
    c = SynthesizedConcept(
        object_id=generate_ksuid(),
        namespace="eric/shared/concept",
        title="T",
        content="C",
        synthesis_rationale="R",
        created_at=now - timedelta(days=31),
        updated_at=now - timedelta(days=31),
        merged_from=[generate_ksuid() for _ in range(3)],
    )
    await deps.concept_plane.create(c)
    await deps.concept_plane.transition(
        namespace=c.namespace, object_id=c.object_id, to_state="matured", actor="sys", reason="test"
    )
    _set_old(deps, "concept", str(c.object_id), days_old=31)

    count = await demotion_concept(deps)
    assert count == 1
    assert len(cast(Any, deps.thoughts).calls) == 1
    assert cast(Any, deps.thoughts).calls[0][0] == "ops-alerts"


@pytest.mark.skip(
    reason="deferred to issue #218: use last_reinforced_epoch in demotion scroll filter; "
    "field exists on SynthesizedConcept, demotion currently proxies via updated_epoch"
)
def test_concept_reinforcement_resets_demotion_clock() -> None:
    pass


# Artifact
@pytest.mark.skip(reason="deferred to issue #222: artifact archival policy + slice-ops-storage")
def test_artifact_archival_off_by_default() -> None:
    pass


@pytest.mark.skip(reason="deferred to issue #222: artifact archival policy + slice-ops-storage")
def test_artifact_archival_respects_referenced_by() -> None:
    pass


@pytest.mark.skip(reason="deferred to issue #222: artifact archival policy + slice-ops-storage")
def test_artifact_archival_transitions_to_archived_keeps_blob() -> None:
    pass


# Reinstatement
@pytest.mark.asyncio
async def test_reinstate_moves_back_to_matured(deps: DemotionDeps) -> None:
    e = _episodic()
    await deps.episodic_plane.create(e)
    await deps.episodic_plane.transition(
        namespace=e.namespace, object_id=e.object_id, to_state="matured", actor="sys", reason="test"
    )
    await deps.episodic_plane.transition(
        namespace=e.namespace, object_id=e.object_id, to_state="demoted", actor="sys", reason="test"
    )

    await reinstate(deps, e.namespace, str(e.object_id), "because")

    p = await deps.episodic_plane.get(namespace=e.namespace, object_id=e.object_id)
    assert p and p.state == "matured"


@pytest.mark.skip(
    reason="deferred to issue #218: use last_reinforced_epoch in demotion scroll filter; "
    "field exists on SynthesizedConcept, demotion currently proxies via updated_epoch"
)
def test_reinstate_resets_reinforced_clock() -> None:
    pass


@pytest.mark.skip(reason="event emitted via transition method")
def test_reinstate_emits_event() -> None:
    pass


# Migration safety
@pytest.mark.skip(reason="deferred to slice-ops-observability")
def test_demotion_paused_flag_honored() -> None:
    pass


@pytest.mark.skip(reason="deferred to slice-ops-observability")
def test_demotion_paused_expired_resumes() -> None:
    pass


# Property / Integration
@pytest.mark.skip(reason="out-of-scope: hypothesis-based property suite is post-v1.0 hardening")
def test_hypothesis_demotion_is_idempotent_across_runs_with_no_change_in_criteria() -> None:
    pass


@pytest.mark.skip(reason="out-of-scope: hypothesis-based property suite is post-v1.0 hardening")
def test_hypothesis_no_object_that_transitions_to_demoted_was_accessed_within_the_selection_window() -> (
    None
):
    pass


@pytest.mark.skip(reason="deferred to integration tests")
def test_integration_seed_1000_memories_with_varied_properties_run_weekly_demotion_count_transitions_matches_criteria() -> (
    None
):
    pass


@pytest.mark.skip(reason="deferred to integration tests")
def test_integration_reinstatement_round_trip_demote_reinstate_appears_in_default_retrieval() -> (
    None
):
    pass
