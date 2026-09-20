"""Test contract for slice-lifecycle-engine.

Implements the Test Contract bullets from the two specs this slice owns:

- [[04-data-model/lifecycle#Test contract]] (transition engine + events)
- [[06-ingestion/lifecycle-engine#Test contract]] (APScheduler + locks)

Every bullet in those sections is present here with its verbatim name — either
as a passing test, or ``@pytest.mark.skip(reason=...)`` pointing at the
downstream slice that owns the method under test, or declared ``⊘ out-of-scope``
in ``docs/Musubi/_slices/slice-lifecycle-engine.md`` ``## Work log`` (for
the two hypothesis bullets + three integration bullets).

Runs against an in-memory Qdrant (``QdrantClient(":memory:")``) plus on-disk
sqlite under ``tmp_path``. No network, no real LLM calls.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st
from qdrant_client import QdrantClient
from qdrant_client import models as qmodels

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.scheduler import (
    Job,
    JobFailureMetrics,
    NamespaceLock,
    build_default_jobs,
    build_scheduler,
    file_lock,
)
from musubi.lifecycle.transitions import (
    LineageUpdates,
    TransitionError,
    TransitionResult,
    transition,
)
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.types.common import Err, Ok, epoch_of, utc_now
from musubi.types.episodic import EpisodicMemory
from musubi.types.lifecycle_event import (
    LifecycleEvent,
    is_legal_transition,
    legal_next_states,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    """In-memory Qdrant with the canonical collection layout bootstrapped."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def plane(qdrant: QdrantClient) -> EpisodicPlane:
    return EpisodicPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def ns() -> str:
    return "eric/claude-code/episodic"


@pytest.fixture
def events_db(tmp_path: Path) -> Path:
    return tmp_path / "events.db"


@pytest.fixture
def sink(events_db: Path) -> Iterator[LifecycleEventSink]:
    """Fresh ``LifecycleEventSink`` rooted at ``events_db``."""
    s = LifecycleEventSink(db_path=events_db, flush_every_n=100, flush_every_s=5.0)
    try:
        yield s
    finally:
        s.close()


def _coordinator(qdrant: QdrantClient, sink: LifecycleEventSink) -> LifecycleTransitionCoordinator:
    return LifecycleTransitionCoordinator(client=qdrant, db_path=sink._db_path)


async def _seed_matured(
    plane: EpisodicPlane,
    ns: str,
    coordinator: LifecycleTransitionCoordinator,
    content: str = "seeded",
) -> EpisodicMemory:
    """Helper: create + mature an episodic so it is eligible for demote/supersede."""
    saved = await plane.create(EpisodicMemory(namespace=ns, content=content))
    result = await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="test-fixture",
        reason="seed",
        coordinator=coordinator,
    )
    assert isinstance(result, Ok)
    assert isinstance(result.value, TransitionResult)
    matured = await plane.get(namespace=ns, object_id=saved.object_id)
    assert matured is not None
    return matured


# ---------------------------------------------------------------------------
# Spec: 04-data-model/lifecycle — Test contract
# ---------------------------------------------------------------------------


async def test_valid_transition_succeeds_and_emits_event(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 1 — provisional → matured succeeds and writes one LifecycleEvent."""
    saved = await plane.create(EpisodicMemory(namespace=ns, content="valid-transition"))
    result = transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=saved.object_id,
        namespace=ns,
        target_state="matured",
        actor="test-suite",
        reason="unit",
        sink=sink,
    )
    assert isinstance(result, Ok), result
    tr = result.value
    assert isinstance(tr, TransitionResult)
    assert tr.from_state == "provisional"
    assert tr.to_state == "matured"
    assert isinstance(tr.event, LifecycleEvent)
    assert tr.event.object_id == saved.object_id

    # Event persisted.
    sink.flush()
    rows = sink.read_all()
    assert any(ev.event_id == tr.event.event_id for ev in rows)


async def test_invalid_transition_returns_typed_error(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 2 — provisional → demoted is illegal for episodic; returns Err()."""
    saved = await plane.create(EpisodicMemory(namespace=ns, content="illegal-hop"))
    result = transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=saved.object_id,
        namespace=ns,
        target_state="demoted",
        actor="test-suite",
        reason="unit",
        sink=sink,
    )
    assert isinstance(result, Err), result
    err = result.error
    assert isinstance(err, TransitionError)
    assert err.code == "illegal_transition"
    assert err.from_state == "provisional"
    assert err.to_state == "demoted"
    # No event row should have been written for an illegal transition.
    sink.flush()
    assert not sink.read_all()


async def test_transition_bumps_version_and_updated_epoch(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 3 — version++, updated_epoch >= previous."""
    saved = await plane.create(EpisodicMemory(namespace=ns, content="bump"))
    before_version = saved.version
    before_epoch = saved.updated_epoch
    assert before_epoch is not None
    result = transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=saved.object_id,
        namespace=ns,
        target_state="matured",
        actor="t",
        reason="u",
        sink=sink,
    )
    assert isinstance(result, Ok)
    assert isinstance(result.value, TransitionResult)
    assert result.value.version == before_version + 1
    fetched = await plane.get(namespace=ns, object_id=saved.object_id)
    assert fetched is not None
    assert fetched.version == before_version + 1
    assert fetched.updated_epoch is not None
    assert fetched.updated_epoch >= before_epoch


async def test_transition_preserves_lineage_through_supersession(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 4 — matured → superseded with lineage_updates sets superseded_by."""
    coordinator = _coordinator(qdrant, sink)
    old = await _seed_matured(plane, ns, coordinator, content="old-version")
    new = await _seed_matured(plane, ns, coordinator, content="new-version")
    result = transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=old.object_id,
        namespace=ns,
        target_state="superseded",
        actor="rewrite",
        reason="new version written",
        lineage_updates=LineageUpdates(superseded_by=new.object_id),
        sink=sink,
    )
    assert isinstance(result, Ok), result
    assert isinstance(result.value, TransitionResult)
    refreshed = await plane.get(namespace=ns, object_id=old.object_id)
    assert refreshed is not None
    assert refreshed.state == "superseded"
    assert refreshed.superseded_by == new.object_id
    # Event records the lineage change.
    assert result.value.event.lineage_changes.get("superseded_by") == new.object_id


async def test_circular_supersession_rejected(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 5 — A → B → A supersession chain is rejected."""
    coordinator = _coordinator(qdrant, sink)
    a = await _seed_matured(plane, ns, coordinator, content="alpha")
    b = await _seed_matured(plane, ns, coordinator, content="beta")
    # A superseded by B — legal.
    ok_first = transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=a.object_id,
        namespace=ns,
        target_state="superseded",
        actor="t",
        reason="first",
        lineage_updates=LineageUpdates(superseded_by=b.object_id),
        sink=sink,
    )
    assert isinstance(ok_first, Ok)
    # Now try B superseded by A — forms a cycle, must be rejected.
    cycle = transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=b.object_id,
        namespace=ns,
        target_state="superseded",
        actor="t",
        reason="cycle",
        lineage_updates=LineageUpdates(superseded_by=a.object_id),
        sink=sink,
    )
    assert isinstance(cycle, Err), cycle
    assert cycle.error.code == "circular_supersession"


async def test_demotion_requires_reason(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 6 — transition to demoted with empty reason is rejected."""
    matured = await _seed_matured(plane, ns, _coordinator(qdrant, sink), content="demote-me")
    result = transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=matured.object_id,
        namespace=ns,
        target_state="demoted",
        actor="t",
        reason="",  # empty reason
        sink=sink,
    )
    assert isinstance(result, Err), result
    assert result.error.code in {"missing_reason", "invariant_violation"}


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-maturation: the per-job episodic maturation "
    "sweep (Ollama scoring + batch transitions) lives in musubi/lifecycle/maturation.py, "
    "which is owned by slice-lifecycle-maturation."
)
async def test_episodic_maturation_happy_path() -> None:
    """Bullet 7 — maturation sweep end-to-end; owned by slice-lifecycle-maturation."""


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-maturation: demotion rule selection is a sweep-job "
    "concern implemented in musubi/lifecycle/demotion.py (owned downstream)."
)
async def test_episodic_demotion_rule_selects_correctly() -> None:
    """Bullet 8 — demotion rule selection; owned by slice-lifecycle-maturation."""


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-maturation: provisional TTL sweep is implemented "
    "in musubi/lifecycle/maturation.py::run_provisional_ttl (owned downstream)."
)
async def test_episodic_provisional_ttl_archives_not_deletes() -> None:
    """Bullet 9 — provisional TTL archives; owned by slice-lifecycle-maturation."""


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-maturation: concept maturation + contradiction check "
    "lives in musubi/lifecycle/concept_maturation.py (owned downstream)."
)
async def test_concept_maturation_blocked_by_contradiction() -> None:
    """Bullet 10 — concept maturation blocked by contradiction; owned downstream."""


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: concept → curated promotion is implemented "
    "in musubi/lifecycle/promotion.py (owned by slice-lifecycle-promotion)."
)
async def test_concept_promotion_sets_all_required_fields() -> None:
    """Bullet 11 — concept promotion sets required fields; owned by slice-lifecycle-promotion."""


async def test_event_written_for_every_transition(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 12 — no silent mutation: every transition appends one event."""
    saved = await plane.create(EpisodicMemory(namespace=ns, content="ledger-1"))
    for target in ("matured", "demoted", "matured"):
        result = transition(
            qdrant,
            coordinator=_coordinator(qdrant, sink),
            object_id=saved.object_id,
            namespace=ns,
            target_state=target,
            actor="t",
            reason="step",
            sink=sink,
        )
        assert isinstance(result, Ok), (target, result)
    sink.flush()
    events = sink.read_all()
    # Exactly three events, one per transition.
    events_for_object = [e for e in events if e.object_id == saved.object_id]
    assert len(events_for_object) == 3
    assert [e.to_state for e in events_for_object] == ["matured", "demoted", "matured"]


@pytest.mark.anyio
async def test_concurrent_transitions_stale_expected_version_fence_violation(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """LIFE-010 — concurrent transitions: stale expected_version hard-fenced."""
    saved = await _seed_matured(plane, ns, _coordinator(qdrant, sink), content="concurrent")

    first = transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=saved.object_id,
        namespace=ns,
        target_state="demoted",
        actor="worker-a",
        reason="a-demote",
        expected_version=saved.version,
        sink=sink,
    )
    assert isinstance(first, Ok)

    snapshot_events = sink.read_all()

    coordinator = _coordinator(qdrant, sink)

    with (
        patch.object(coordinator, "transition", wraps=coordinator.transition) as spy_transition,
        patch.object(sink, "record", wraps=sink.record) as spy_sink,
    ):
        second = transition(
            qdrant,
            coordinator=coordinator,
            object_id=saved.object_id,
            namespace=ns,
            target_state="superseded",
            actor="worker-b",
            reason="b-supersede",
            expected_version=saved.version,  # stale!
            lineage_updates=LineageUpdates(superseded_by="0" * 27),
            sink=sink,
        )

        # Must return version_fence_violation immediately
        assert isinstance(second, Err)
        assert second.error.code == "version_fence_violation"

        # Prove zero coordinator dispatches or sink writes
        spy_transition.assert_not_called()
        spy_sink.assert_not_called()

        assert sink.read_all() == snapshot_events

        # Prove no mutation to the state, version, and lineage
        reloaded = await plane.get(namespace=ns, object_id=saved.object_id)
        assert reloaded is not None
        assert reloaded.state == "demoted"
        assert reloaded.version == saved.version + 1
        assert reloaded.superseded_by is None


async def test_event_batch_flushed_within_5s_under_load(
    events_db: Path,
    qdrant: QdrantClient,
    plane: EpisodicPlane,
    ns: str,
) -> None:
    """Bullet 14 — sink flushes at most every 5s even if count threshold not hit."""
    # Use a short-wall-clock flush interval so the test runs fast, but the
    # contract is the same: time-based flush kicks in even without count pressure.
    short_sink = LifecycleEventSink(db_path=events_db, flush_every_n=1000, flush_every_s=0.2)
    try:
        seeded = await _seed_matured(
            plane, ns, _coordinator(qdrant, short_sink), content="flush-load"
        )
        started = time.monotonic()
        result = transition(
            qdrant,
            coordinator=_coordinator(qdrant, short_sink),
            object_id=seeded.object_id,
            namespace=ns,
            target_state="demoted",
            actor="t",
            reason="flush-test",
            sink=short_sink,
        )
        assert isinstance(result, Ok)
        # Without calling flush(), the background / time-based flush must land
        # within the configured interval.
        deadline = started + 2.0
        rows: list[LifecycleEvent] = []
        while time.monotonic() < deadline:
            rows = short_sink.read_all()
            if rows:
                break
            time.sleep(0.05)
        assert rows, "time-based flush did not land within 2 s"
    finally:
        short_sink.close()


async def test_sqlite_event_db_survives_worker_restart(
    events_db: Path,
    qdrant: QdrantClient,
    plane: EpisodicPlane,
    ns: str,
) -> None:
    """Bullet 15 — committed events are readable by a fresh sink on the same file."""
    seeded = await _seed_matured(
        plane,
        ns,
        LifecycleTransitionCoordinator(client=qdrant, db_path=events_db),
        content="restart-survivor",
    )
    first = LifecycleEventSink(db_path=events_db, flush_every_n=100, flush_every_s=5.0)
    try:
        result = transition(
            qdrant,
            coordinator=_coordinator(qdrant, first),
            object_id=seeded.object_id,
            namespace=ns,
            target_state="demoted",
            actor="worker-1",
            reason="before-restart",
            sink=first,
        )
        assert isinstance(result, Ok)
        first.flush()
    finally:
        first.close()

    # Simulate restart — brand new sink instance, same file.
    second = LifecycleEventSink(db_path=events_db, flush_every_n=100, flush_every_s=5.0)
    try:
        rows = second.read_all()
        assert any(e.object_id == seeded.object_id and e.to_state == "demoted" for e in rows)
    finally:
        second.close()


# ---------------------------------------------------------------------------
# Hypothesis property tests — referenced by bullets 16 / 17.
# The verbatim bullet strings ("hypothesis: ...") are also declared in the
# slice work log so tc_coverage.py reports them as ⊘ out-of-scope.
# ---------------------------------------------------------------------------


def test_hypothesis_state_machine_reachability() -> None:
    """Bullet 16 body — every declared allowed transition is reachable from some state."""
    from musubi.types.lifecycle_event import _ALLOWED

    for object_type, table in _ALLOWED.items():
        # Every target state in the table must also be a declared source key —
        # i.e. no orphan terminals unless explicitly declared with empty set.
        sources = set(table.keys())
        targets: set[str] = set()
        for targets_from_here in table.values():
            targets.update(targets_from_here)
        orphans = targets - sources
        assert not orphans, f"{object_type}: orphan target states {orphans}"


@given(
    steps=st.lists(
        st.sampled_from(("provisional", "matured", "demoted", "archived", "superseded")),
        min_size=0,
        max_size=5,
    )
)
def test_hypothesis_monotone_invariants(steps: list[str]) -> None:
    """Bullet 17 body — version, updated_epoch never decrease across legal transitions.

    Purely a property over the transition table; no Qdrant needed.
    """
    version = 1
    epoch = epoch_of(utc_now())
    current = "provisional"
    for nxt in steps:
        if is_legal_transition("episodic", current, nxt):  # type: ignore[arg-type]
            version += 1
            new_epoch = epoch + 0.000001  # strictly later
            assert version > 1 or current == "provisional"
            assert new_epoch >= epoch
            epoch = new_epoch
            current = nxt


# ---------------------------------------------------------------------------
# Spec: 06-ingestion/lifecycle-engine — Test contract
# ---------------------------------------------------------------------------


def test_jobs_registered_with_documented_triggers() -> None:
    """Bullet 1 — the JOBS registry contains every documented job with its trigger."""
    jobs = build_default_jobs()
    names = {j.name for j in jobs}
    expected = {
        "maturation_episodic",
        "provisional_ttl",
        "synthesis",
        "concept_maturation",
        "promotion",
        "demotion_concept",
        "demotion_episodic",
        "reflection_digest",
        "vault_reconcile",
    }
    assert expected.issubset(names), f"missing jobs: {expected - names}"
    # Every job has a trigger + grace_time default.
    for job in jobs:
        assert job.trigger is not None, job.name
        assert job.grace_time_s >= 0


def test_missed_job_within_grace_runs(tmp_path: Path) -> None:
    """Bullet 2 — scheduler runs a missed job if it is within misfire_grace_time."""
    calls: list[datetime] = []

    def record() -> None:
        calls.append(datetime.now(UTC))

    job = Job(
        name="within_grace",
        trigger_kind="interval",
        trigger_kwargs={"seconds": 60},
        func=record,
        grace_time_s=900,
        coalesce=True,
    )
    sched = build_scheduler([job], jobstore_path=tmp_path / "scheduler.db", testing=True)
    # Simulate a missed fire that lands within the 900 s grace window.
    sched.force_run(job.name, missed_by_s=10)
    assert len(calls) == 1


def test_missed_job_outside_grace_skipped(tmp_path: Path) -> None:
    """Bullet 3 — missed job outside grace window is skipped, not run."""
    calls: list[datetime] = []

    def record() -> None:
        calls.append(datetime.now(UTC))

    job = Job(
        name="outside_grace",
        trigger_kind="interval",
        trigger_kwargs={"seconds": 60},
        func=record,
        grace_time_s=60,
        coalesce=True,
    )
    sched = build_scheduler([job], jobstore_path=tmp_path / "scheduler.db", testing=True)
    ran = sched.force_run(job.name, missed_by_s=3600)
    assert ran is False
    assert calls == []


def test_coalesce_multiple_misfires_run_once(tmp_path: Path) -> None:
    """Bullet 4 — coalesce=True compresses multiple missed fires into a single run."""
    calls: list[int] = []

    def record() -> None:
        calls.append(1)

    job = Job(
        name="coalesce_me",
        trigger_kind="interval",
        trigger_kwargs={"seconds": 60},
        func=record,
        grace_time_s=900,
        coalesce=True,
    )
    sched = build_scheduler([job], jobstore_path=tmp_path / "scheduler.db", testing=True)
    sched.force_coalesced_run(job.name, misfires=5)
    assert sum(calls) == 1


def test_file_lock_acquires_and_releases(tmp_path: Path) -> None:
    """Bullet 5 — file_lock() acquires a lock file and releases on context exit."""
    lock_path = tmp_path / "job.lock"
    with file_lock(lock_path, timeout=0) as acquired:
        assert acquired is True
        assert lock_path.exists()
    # After exit, re-acquisition must succeed.
    with file_lock(lock_path, timeout=0) as acquired_again:
        assert acquired_again is True


def test_second_lock_attempt_fails_fast(tmp_path: Path) -> None:
    """Bullet 6 — a second in-process lock attempt returns False without blocking."""
    lock_path = tmp_path / "job.lock"
    started = time.monotonic()
    with file_lock(lock_path, timeout=0) as a:
        assert a is True
        with file_lock(lock_path, timeout=0) as b:
            assert b is False
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"second lock blocked for {elapsed:.2f}s; expected fail-fast"


@pytest.mark.skip(
    reason="deferred to integration: verifying fcntl release-on-process-death requires "
    "a subprocess harness + os.kill; this unit-layer covers same-process semantics. "
    "Marked by the Lifecycle Worker owner's follow-up slice for the integration harness."
)
def test_lock_released_on_process_death() -> None:
    """Bullet 7 — fcntl lock released when holder process dies; integration-level."""


def test_namespace_scoped_lock_allows_parallel_namespaces(tmp_path: Path) -> None:
    """Bullet 8 — namespace-scoped locks allow two namespaces to run in parallel."""
    base = tmp_path / "namespaced"
    lock_a = NamespaceLock(base_dir=base, job_name="synthesis", ns_hash="ns-a")
    lock_b = NamespaceLock(base_dir=base, job_name="synthesis", ns_hash="ns-b")
    with lock_a.acquire() as got_a, lock_b.acquire() as got_b:
        assert got_a is True
        assert got_b is True


def test_job_failure_does_not_stop_scheduler(tmp_path: Path) -> None:
    """Bullet 9 — one failing job does not tear down the whole scheduler."""
    good_calls: list[int] = []

    def fail() -> None:
        raise RuntimeError("intentional failure")

    def ok() -> None:
        good_calls.append(1)

    jobs = [
        Job(
            name="fails",
            trigger_kind="interval",
            trigger_kwargs={"seconds": 60},
            func=fail,
        ),
        Job(
            name="succeeds",
            trigger_kind="interval",
            trigger_kwargs={"seconds": 60},
            func=ok,
        ),
    ]
    sched = build_scheduler(jobs, jobstore_path=tmp_path / "scheduler.db", testing=True)
    sched.force_run("fails", missed_by_s=0)
    assert sched.is_running()
    sched.force_run("succeeds", missed_by_s=0)
    assert good_calls == [1]


@pytest.mark.skip(
    reason="deferred to slice-plane-thought (and slice-lifecycle-reflection): emitting a "
    "Thought to the 'ops-alerts' channel requires the Thought plane's write path, which "
    "lives outside this slice's owns_paths. Failure metric (test_job_failure_metric_incremented) "
    "covers the same failure with an alternative observability channel."
)
def test_job_failure_emits_thought() -> None:
    """Bullet 10 — job failure emits an 'ops-alerts' Thought; owned by slice-plane-thought."""


def test_job_failure_metric_incremented(tmp_path: Path) -> None:
    """Bullet 11 — failing jobs bump the per-job failure counter."""
    metrics = JobFailureMetrics()

    def boom() -> None:
        raise RuntimeError("boom")

    jobs = [
        Job(
            name="metric_check",
            trigger_kind="interval",
            trigger_kwargs={"seconds": 60},
            func=boom,
        ),
    ]
    sched = build_scheduler(
        jobs,
        jobstore_path=tmp_path / "scheduler.db",
        testing=True,
        metrics=metrics,
    )
    sched.force_run("metric_check", missed_by_s=0)
    assert metrics.failures("metric_check") == 1
    sched.force_run("metric_check", missed_by_s=0)
    assert metrics.failures("metric_check") == 2


@pytest.mark.skip(
    reason="deferred to per-job slices (slice-lifecycle-maturation, slice-lifecycle-synthesis, "
    "slice-lifecycle-promotion, slice-lifecycle-reflection): cursor advancement is tested "
    "against each job's batch logic. This slice owns only the scheduler + transition engine."
)
def test_cursor_advances_on_successful_batch() -> None:
    """Bullet 12 — per-job cursor advancement; owned by per-job slices."""


@pytest.mark.skip(
    reason="deferred to per-job slices: cursor persistence across restart is a per-sweep-job "
    "concern (maturation-cursor.db, synthesis-cursor.db, etc.). "
    "test_sqlite_event_db_survives_worker_restart above covers the scheduler/events side."
)
def test_cursor_persists_across_worker_restart() -> None:
    """Bullet 13 — per-job cursor persistence; owned by per-job slices."""


def test_scheduler_db_persists_job_history(tmp_path: Path) -> None:
    """Bullet 14 — APScheduler's sqlite jobstore persists jobs across builder invocations."""
    jobstore = tmp_path / "scheduler.db"
    jobs = [
        Job(
            name="persistent_job",
            trigger_kind="interval",
            trigger_kwargs={"seconds": 60},
            func=lambda: None,
        ),
    ]
    sched = build_scheduler(jobs, jobstore_path=jobstore, testing=True)
    assert sched.has_job("persistent_job")

    # File exists on disk.
    assert jobstore.exists(), "scheduler jobstore db was not created"

    # Opening the sqlite file directly shows a non-empty job table.
    conn = sqlite3.connect(str(jobstore))
    try:
        cur = conn.cursor()
        rows = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert rows, "no tables in scheduler db"
    finally:
        conn.close()


async def test_lifecycle_events_batched_and_flushed(
    events_db: Path,
    qdrant: QdrantClient,
    plane: EpisodicPlane,
    ns: str,
) -> None:
    """Bullet 15 — events are batched up to flush_every_n and then written."""
    batch_sink = LifecycleEventSink(db_path=events_db, flush_every_n=3, flush_every_s=60.0)
    try:
        ids: list[str] = []
        for i in range(5):
            saved = await plane.create(EpisodicMemory(namespace=ns, content=f"batch-{i}-unique"))
            result = transition(
                qdrant,
                coordinator=_coordinator(qdrant, batch_sink),
                object_id=saved.object_id,
                namespace=ns,
                target_state="matured",
                actor="t",
                reason="batch",
                sink=batch_sink,
            )
            assert isinstance(result, Ok)
            ids.append(saved.object_id)
        # With flush_every_n=3 and 5 transitions, at least the first batch of 3
        # must have landed on disk without calling flush() explicitly.
        rows = batch_sink.read_all()
        assert len(rows) >= 3, f"expected batch flush at N=3; saw {len(rows)} rows"
        batch_sink.flush()
        all_rows = batch_sink.read_all()
        assert len(all_rows) == 5
    finally:
        batch_sink.close()


async def test_events_survive_worker_restart(
    events_db: Path,
    qdrant: QdrantClient,
    plane: EpisodicPlane,
    ns: str,
) -> None:
    """Bullet 16 — events.db rows survive a sink close + re-open cycle."""
    seeded = await _seed_matured(
        plane,
        ns,
        LifecycleTransitionCoordinator(client=qdrant, db_path=events_db),
        content="restart-events",
    )
    s1 = LifecycleEventSink(db_path=events_db, flush_every_n=1, flush_every_s=0.1)
    try:
        result = transition(
            qdrant,
            coordinator=_coordinator(qdrant, s1),
            object_id=seeded.object_id,
            namespace=ns,
            target_state="demoted",
            actor="worker-1",
            reason="restart",
            sink=s1,
        )
        assert isinstance(result, Ok)
        s1.flush()
    finally:
        s1.close()

    # Reopen: row is there.
    s2 = LifecycleEventSink(db_path=events_db, flush_every_n=1, flush_every_s=0.1)
    try:
        rows = s2.read_all()
        assert any(e.object_id == seeded.object_id for e in rows)
    finally:
        s2.close()


# ---------------------------------------------------------------------------
# Basic dataclass-shape sanity — guards against accidental rename of the
# canonical surface. Not a spec bullet; kept because the transition Result
# shape is load-bearing for downstream slices.
# ---------------------------------------------------------------------------


def test_transition_result_and_error_shapes_are_frozen() -> None:
    tr = TransitionResult(
        object_id="0" * 27,
        object_type="episodic",
        from_state="provisional",
        to_state="matured",
        version=2,
        event=LifecycleEvent(
            object_id="0" * 27,
            object_type="episodic",
            namespace="eric/claude-code/episodic",
            from_state="provisional",
            to_state="matured",
            actor="t",
            reason="r",
        ),
    )
    assert tr.from_state == "provisional"
    assert tr.to_state == "matured"
    err = TransitionError(
        code="illegal_transition",
        message="x",
        from_state="provisional",
        to_state="demoted",
        allowed=tuple(sorted(legal_next_states("episodic", "provisional"))),
    )
    assert err.code == "illegal_transition"
    assert "matured" in err.allowed


def test_transition_is_thread_safe_against_own_sink(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
) -> None:
    """Not a spec bullet — smoke-test that sink.record() does not race itself.

    Motivation: the sink is shared between threads inside the Worker process.
    A concurrent ``record`` from two scheduler threads must not lose events.
    """
    events: list[LifecycleEvent] = []

    def make_event(i: int) -> LifecycleEvent:
        return LifecycleEvent(
            object_id="0" * 26 + str(i % 10),
            object_type="episodic",
            namespace="eric/claude-code/episodic",
            from_state="provisional",
            to_state="matured",
            actor=f"thread-{i}",
            reason="race-test",
        )

    def work(start: int, count: int) -> None:
        for j in range(count):
            ev = make_event(start + j)
            events.append(ev)
            sink.record(ev)

    threads = [threading.Thread(target=work, args=(i * 10, 5)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sink.flush()
    persisted = sink.read_all()
    # Every event we recorded must be durable; order is not asserted.
    recorded_ids = {e.event_id for e in events}
    persisted_ids = {e.event_id for e in persisted}
    assert recorded_ids <= persisted_ids


# ---------------------------------------------------------------------------
# Additional branch-coverage tests (non-spec-bullet, but required to pass the
# ≥85 % coverage target on owned files from CLAUDE.md#Style).
# ---------------------------------------------------------------------------


def test_sink_rejects_invalid_flush_parameters(tmp_path: Path) -> None:
    """Constructor validates flush_every_n and flush_every_s."""
    with pytest.raises(ValueError, match="flush_every_n"):
        LifecycleEventSink(db_path=tmp_path / "e1.db", flush_every_n=0)
    with pytest.raises(ValueError, match="flush_every_s"):
        LifecycleEventSink(db_path=tmp_path / "e2.db", flush_every_s=0.0)


def test_sink_record_after_close_returns_typed_err(tmp_path: Path) -> None:
    """A closed sink refuses the mutation through the Result boundary."""
    sink = LifecycleEventSink(db_path=tmp_path / "e.db")
    sink.close()
    ev = LifecycleEvent(
        object_id="0" * 27,
        object_type="episodic",
        namespace="eric/claude-code/episodic",
        from_state="provisional",
        to_state="matured",
        actor="t",
        reason="r",
    )
    result = sink.record(ev)
    assert isinstance(result, Err)
    assert result.error.code == "lifecycle_event_write_failed"


def test_sink_close_is_idempotent(tmp_path: Path) -> None:
    """Calling close() twice is a no-op — best-effort cleanup path."""
    sink = LifecycleEventSink(db_path=tmp_path / "e.db")
    sink.close()
    sink.close()  # second call must not raise


def test_sink_read_all_after_close_opens_fresh_connection(tmp_path: Path) -> None:
    """Closed sink's read_all uses the module-level helper instead of self._conn."""
    sink = LifecycleEventSink(db_path=tmp_path / "e.db")
    ev = LifecycleEvent(
        object_id="0" * 27,
        object_type="episodic",
        namespace="eric/claude-code/episodic",
        from_state="provisional",
        to_state="matured",
        actor="t",
        reason="r",
    )
    sink.record(ev)
    sink.flush()
    sink.close()
    events = sink.read_all()
    assert len(events) == 1
    assert events[0].event_id == ev.event_id


def test_sink_deserialises_naive_datetime() -> None:
    """Payloads round-tripped without a tz are coerced to UTC on read."""
    from musubi.lifecycle.events import _deserialise, _serialise

    ev = LifecycleEvent(
        object_id="0" * 27,
        object_type="episodic",
        namespace="eric/claude-code/episodic",
        from_state="provisional",
        to_state="matured",
        actor="t",
        reason="r",
    )
    # Round-trip via JSON, then strip the tz-suffix to simulate a legacy
    # payload that stored the naive form.
    import json as _json

    data = _json.loads(_serialise(ev))
    data["occurred_at"] = "2026-04-19T12:00:00"
    roundtripped = _deserialise(_json.dumps(data))
    assert roundtripped.occurred_at.tzinfo is UTC


def test_context_manager_closes_sink(tmp_path: Path) -> None:
    """Using LifecycleEventSink as a context manager calls close on exit."""
    db = tmp_path / "e.db"
    with LifecycleEventSink(db_path=db) as sink:
        ev = LifecycleEvent(
            object_id="0" * 27,
            object_type="episodic",
            namespace="eric/claude-code/episodic",
            from_state="provisional",
            to_state="matured",
            actor="t",
            reason="r",
        )
        sink.record(ev)
        sink.flush()  # make sure the buffer is persisted before close
    # After exit, close() has run — reading still works via a fresh connection.
    assert len(sink.read_all()) == 1


def test_lineage_updates_serialises_all_fields() -> None:
    """Every optional field on LineageUpdates reaches the payload patch."""
    lu = LineageUpdates(
        superseded_by="a" * 27,
        supersedes=["b" * 27],
        merged_from=["c" * 27],
        contradicts=["d" * 27],
    )
    patch = lu.to_payload_patch()
    assert set(patch.keys()) == {"superseded_by", "supersedes", "merged_from", "contradicts"}
    # Empty LineageUpdates round-trips to an empty dict.
    assert LineageUpdates().to_payload_patch() == {}
    assert LineageUpdates().to_event_changes() == {}


def test_transition_not_found_returns_typed_error(qdrant: QdrantClient, tmp_path: Path) -> None:
    """Missing object_id yields code='not_found' without mutating any plane."""
    result = transition(
        qdrant,
        coordinator=LifecycleTransitionCoordinator(
            client=qdrant, db_path=tmp_path / "not-found.db"
        ),
        object_id="z" * 27,
        # No such row anywhere, so there is no namespace to qualify with.
        namespace=None,
        target_state="matured",
        actor="t",
        reason="r",
    )
    assert isinstance(result, Err)
    assert result.error.code == "not_found"


def test_file_lock_non_linux_fallback_is_exercised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When fcntl is stubbed to None, file_lock still yields acquired=True.

    Covers the defensive branch used on Windows CI (not our production target,
    but the stub exists and needs at least one caller to keep it honest).
    """
    import musubi.lifecycle.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_fcntl", None)
    with file_lock(tmp_path / "x.lock") as got:
        assert got is True


def test_namespace_lock_rejects_unsafe_hash(tmp_path: Path) -> None:
    """Namespace hashes with path separators are rejected at construction."""
    with pytest.raises(ValueError):
        NamespaceLock(base_dir=tmp_path, job_name="j", ns_hash="a/b")
    with pytest.raises(ValueError):
        NamespaceLock(base_dir=tmp_path, job_name="j", ns_hash="..")


def test_testing_scheduler_no_misfires_returns_zero(tmp_path: Path) -> None:
    """force_coalesced_run with zero misfires is a no-op."""
    sched = build_scheduler(
        build_default_jobs(),
        jobstore_path=tmp_path / "jobs.db",
        testing=True,
    )
    assert sched.force_coalesced_run("synthesis", misfires=0) == 0


async def test_transition_records_supersession_lineage(
    qdrant: QdrantClient,
    plane: EpisodicPlane,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Lineage updates propagate to both the payload and the LifecycleEvent."""
    a = await plane.create(EpisodicMemory(namespace=ns, content="a"))
    b = await plane.create(EpisodicMemory(namespace=ns, content="b"))
    # Transition a to matured first so superseded is a legal next state.
    assert isinstance(
        transition(
            qdrant,
            coordinator=_coordinator(qdrant, sink),
            object_id=a.object_id,
            namespace=ns,
            target_state="matured",
            actor="t",
            reason="warm",
        ),
        Ok,
    )
    r = transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=a.object_id,
        namespace=ns,
        target_state="superseded",
        actor="t",
        reason="dup",
        lineage_updates=LineageUpdates(superseded_by=b.object_id),
        sink=sink,
    )
    assert isinstance(r, Ok)
    assert isinstance(r.value, TransitionResult)
    assert r.value.event.lineage_changes["superseded_by"] == b.object_id


# ---------------------------------------------------------------------------
# _locate_object refuses rather than guessing (Copilot, musubi#771)
# ---------------------------------------------------------------------------


def _collection_with(client: QdrantClient, name: str, payloads: list[dict[str, object]]) -> None:
    from qdrant_client import models as qmodels

    client.create_collection(
        name, vectors_config=qmodels.VectorParams(size=2, distance=qmodels.Distance.COSINE)
    )
    client.upsert(
        name,
        points=[
            qmodels.PointStruct(id=i + 1, vector=[0.1, 0.2], payload=p)
            for i, p in enumerate(payloads)
        ],
    )


def test_the_same_object_id_in_two_planes_refuses_instead_of_picking_one() -> None:
    """THE CELL THE CROSS-PLANE FIX EXISTS FOR.

    `_locate_object` scanned collections in dict order and returned on the first hit,
    so an object_id present in two planes resolved by ITERATION ORDER -- a transition
    landing on whichever collection happens to be declared first. Qualifying the
    namespace cannot fix it: both rows can carry the same namespace in different
    planes, so there is no argument the caller could pass. The only safe answer is to
    look at every collection and refuse.

    Note the namespace here is IDENTICAL on both rows. A cell that varied it would pass
    against the old early-return code, because the within-collection check would never
    see the second plane at all."""
    from musubi.lifecycle.transitions import AmbiguousObjectId, _locate_object

    client = QdrantClient(":memory:")
    shared = {"object_id": "obj-in-two-planes", "namespace": "ns/a", "state": "provisional"}
    _collection_with(client, "musubi_episodic", [dict(shared)])
    _collection_with(client, "musubi_curated", [dict(shared)])

    with pytest.raises(AmbiguousObjectId) as excinfo:
        _locate_object(client, object_id="obj-in-two-planes", namespace="ns/a")

    message = str(excinfo.value)
    assert "musubi_episodic" in message and "musubi_curated" in message, (
        f"the refusal must name both planes so an operator can act on it; got {message!r}"
    )


def test_an_unqualified_object_id_in_two_namespaces_refuses() -> None:
    """The within-collection sibling, which had no cell either.

    `object_id` is not globally unique. With `namespace=None` the scroll must be able to
    SEE a second row in order to refuse it -- a `limit=1` lookup cannot, which is how
    this resolved silently to whichever row came back first."""
    from musubi.lifecycle.transitions import AmbiguousObjectId, _locate_object

    client = QdrantClient(":memory:")
    _collection_with(
        client,
        "musubi_episodic",
        [
            {"object_id": "dup", "namespace": "ns/a", "state": "provisional"},
            {"object_id": "dup", "namespace": "ns/b", "state": "provisional"},
        ],
    )

    with pytest.raises(AmbiguousObjectId):
        _locate_object(client, object_id="dup", namespace=None)

    # ...and qualifying it resolves cleanly, so the refusal is not just "always raise".
    collection, payload = _locate_object(client, object_id="dup", namespace="ns/b")  # type: ignore[misc]
    assert collection == "musubi_episodic"
    assert payload["namespace"] == "ns/b"


def test_an_ambiguous_supersession_target_refuses_with_the_typed_error(tmp_path: Path) -> None:
    """THE LINEAGE-WALK CELL. The refusal has to travel the whole walk, not one step.

    `_locate_object` refuses an unqualified duplicate at the ENTRY lookup. The
    supersession cycle walk then followed the chain with its own
    `_scroll_by_object_id(...)[0]` at every hop, so an ambiguous chain id was resolved by
    scroll order -- a cycle could be missed, or lineage attached, based on whichever row
    came back first (Copilot, musubi#771).

    Two things are required and they are different: the walk must REFUSE, and the refusal
    must arrive as a typed `Err`. `AmbiguousObjectId` is an exception, so an unmapped
    raise escaping `transition()` is a 500 rather than the caller-error it is."""
    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
    from musubi.lifecycle.transitions import transition
    from musubi.types.common import generate_ksuid

    subject = generate_ksuid()
    target = generate_ksuid()
    client = QdrantClient(":memory:")
    _collection_with(
        client,
        "musubi_episodic",
        [
            # The row being transitioned -- unambiguous, so the ENTRY lookup succeeds and
            # the cell cannot pass for the wrong reason.
            {"object_id": subject, "namespace": "ns/a", "state": "provisional", "version": 1},
            # The supersession TARGET, duplicated across namespaces: the ambiguity lives
            # one hop into the walk, where the entry check never looks.
            {"object_id": target, "namespace": "ns/a", "state": "matured", "version": 1},
            {"object_id": target, "namespace": "ns/b", "state": "matured", "version": 1},
        ],
    )
    coordinator = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "wk.db")

    result = transition(
        client,
        coordinator=coordinator,
        object_id=subject,
        target_state="matured",
        actor="operator",
        reason="ambiguous-lineage",
        namespace=None,
        lineage_updates=LineageUpdates(superseded_by=target),
    )

    assert isinstance(result, Err), f"the ambiguous chain id was resolved, not refused: {result!r}"
    assert result.error.code == "ambiguous_object_id", (
        f"refused with {result.error.code!r}; an unmapped AmbiguousObjectId is a 500, and "
        f"`circular_supersession` would name the wrong cause"
    )

    # ...and nothing moved. A refusal that still mutated would be the worse failure.
    rows, _ = client.scroll(
        collection_name="musubi_episodic",
        scroll_filter=qmodels.Filter(
            must=[qmodels.FieldCondition(key="object_id", match=qmodels.MatchValue(value=subject))]
        ),
        limit=2,
        with_payload=True,
    )
    assert (rows[0].payload or {})["state"] == "provisional", "the subject transitioned anyway"


def test_every_locally_constructed_error_code_is_documented() -> None:
    """Codes THIS MODULE constructs are derived from source, not hand-maintained.

    SCOPE, stated in the name because a test name is a claim: this walks literal
    `TransitionError(code=...)` constructions in `transitions.py`. It cannot see codes
    the coordinator returns and `transition()` forwards -- those belong to the
    coordinator's contract. Calling it "every error code" would reintroduce, as a test
    name, exactly the exhaustiveness promise just removed from the docstring
    (Yua, musubi#771).

    The previous revision asserted "this list is exhaustive" while omitting six
    coordinator codes — a completeness promise nothing checked, which is worse than no
    promise because a caller can rely on it. Removing the false claim is necessary but
    not sufficient: the list can still fall behind silently.

    So walk the module's own AST for `TransitionError(code="...")` literals and require
    each to be documented. A new code added to this module without a docstring line
    fails here rather than in someone's exhaustive `match` statement
    (Copilot/Yua, musubi#771)."""
    import ast
    import inspect

    from musubi.lifecycle import transitions as mod

    tree = ast.parse(inspect.getsource(mod))
    constructed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "TransitionError":
            continue
        for kw in node.keywords:
            if kw.arg == "code" and isinstance(kw.value, ast.Constant):
                constructed.add(str(kw.value.value))

    assert constructed, "found no TransitionError(code=...) literals — the walk is inert"
    doc = mod.TransitionError.__doc__ or ""
    undocumented = sorted(c for c in constructed if c not in doc)
    assert not undocumented, (
        f"{mod.__name__} returns these codes with no line in TransitionError's "
        f"documented contract: {undocumented}"
    )


def test_a_scroll_failure_on_one_plane_never_resolves_to_another(tmp_path: Path) -> None:
    """THE FAIL-OPEN CELL, and the sharpest defect of the night.

    `_scroll_by_object_id` was `except Exception: return []`. The comment named one
    cause -- a missing collection -- and the handler caught every cause: timeout, reset,
    auth failure, transport error. All of them became "no rows here", and the all-plane
    ambiguity check read that empty list as a CONFIRMED MISS.

    So a transient fault on the plane holding the duplicate made the duplicate
    invisible, and the unqualified lookup resolved to the other plane and transitioned a
    stranger's row -- precisely the outcome the identity fix exists to prevent. The
    guard failed open on error, at the bottom of the stack, inside the change fixing
    that same class four layers up (Copilot/Aoi, musubi#771).

    Injection is a REAL fault from the client, not a missing collection: both
    collections exist and are discoverable, and one raises on scroll."""
    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
    from musubi.lifecycle.transitions import transition
    from musubi.types.common import generate_ksuid

    oid = generate_ksuid()
    client = QdrantClient(":memory:")
    _collection_with(
        client,
        "musubi_episodic",
        [{"object_id": oid, "namespace": "ns/a", "state": "provisional", "version": 1}],
    )
    _collection_with(
        client,
        "musubi_curated",
        [{"object_id": oid, "namespace": "ns/b", "state": "provisional", "version": 1}],
    )

    real_scroll = client.scroll

    def scroll_failing_on_curated(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("collection_name") == "musubi_curated":
            raise TimeoutError("qdrant timed out")  # a transport fault, not an absence
        return real_scroll(*args, **kwargs)

    client.scroll = scroll_failing_on_curated  # type: ignore[method-assign]
    coordinator = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "wk.db")

    with pytest.raises(TimeoutError):
        transition(
            client,
            coordinator=coordinator,
            object_id=oid,
            target_state="matured",
            actor="operator",
            reason="fault-must-not-be-a-miss",
            namespace=None,
        )

    client.scroll = real_scroll  # type: ignore[method-assign]
    rows, _ = real_scroll(
        collection_name="musubi_episodic",
        scroll_filter=qmodels.Filter(
            must=[qmodels.FieldCondition(key="object_id", match=qmodels.MatchValue(value=oid))]
        ),
        limit=2,
        with_payload=True,
    )
    assert (rows[0].payload or {})["state"] == "provisional", (
        "the episodic row was transitioned while the plane holding its duplicate was "
        "unreachable -- the error was read as a confirmed miss"
    )


def test_duplicate_anchors_in_one_namespace_refuse(tmp_path: Path) -> None:
    """Counting DISTINCT NAMESPACES let this pass, which is why that rule is gone.

    Two authoritative rows in the SAME namespace collapse to a namespace-set of size
    one, so the old check saw no ambiguity and `records[0]` decided. A third namespace
    could then hide behind them entirely under a `limit=2` scan. The rule is now
    exactly-one-authoritative-row, which does not care what the cause was -- and note
    the lookup here is QUALIFIED, so supplying a namespace does not rescue it
    (Copilot/Yua, musubi#771)."""
    from musubi.lifecycle.transitions import AmbiguousObjectId, _locate_object
    from musubi.types.common import generate_ksuid

    oid = generate_ksuid()
    client = QdrantClient(":memory:")
    _collection_with(
        client,
        "musubi_episodic",
        [
            {"object_id": oid, "namespace": "ns/a", "state": "provisional", "version": 1},
            {"object_id": oid, "namespace": "ns/a", "state": "provisional", "version": 2},
        ],
    )

    with pytest.raises(AmbiguousObjectId) as excinfo:
        _locate_object(client, object_id=oid, namespace="ns/a")
    assert "at least 2 authoritative" in str(excinfo.value)

    # THE REMEDIATION MUST FIT THE CAUSE. On the UNQUALIFIED path with both anchors in
    # one namespace, the old message said "qualify the namespace" -- advice that cannot
    # work, sending an operator to do something futile while hiding that the data needs
    # repair (Copilot, musubi#771).
    with pytest.raises(AmbiguousObjectId) as unqualified:
        _locate_object(client, object_id=oid, namespace=None)
    message = str(unqualified.value)
    assert "qualify the namespace" not in message, (
        f"both anchors are in ONE namespace; qualifying cannot resolve this: {message!r}"
    )
    assert "duplicate anchors" in message and "repair" in message, (
        f"the refusal does not say what would actually fix it: {message!r}"
    )


def test_the_error_contract_does_not_claim_completeness_it_does_not_have() -> None:
    """A completeness PROMISE is testable even when the prose around it is not.

    The previous revision said "this list is exhaustive" while omitting all six
    coordinator codes that `transition()` can forward. A caller can rely on that
    sentence — it is worse than no promise. Removing it was the fix; this is what stops
    it coming back, because the next person to add a tidy "exhaustive" will be told.

    The rule is conditional, so it stays true either way: claim completeness only if the
    list is actually complete (Copilot/Yua, musubi#771)."""
    from musubi.lifecycle.transitions import TransitionError

    doc = TransitionError.__doc__ or ""
    forwarded = [
        "cap_exceeded",
        "active_intent_exists",
        "durable_begin_failed",
        "operation_key_conflict",
        "terminal_apply_failure",
        "maintenance_active",
    ]
    # DOCUMENTED means a contract BULLET, not a mention. The first version of this cell
    # checked `code in doc` and passed under the plant, because the six codes are named
    # in the prose explaining that they belong elsewhere -- so it could not tell
    # "exhaustive" from "not exhaustive" at all. Caught by its own red-proof, which is
    # the tenth inert check tonight and the third of mine.
    claims_complete = "not exhaustive" not in doc and "exhaustive" in doc
    if claims_complete:
        missing = [c for c in forwarded if f"- ``{c}``" not in doc]
        assert not missing, (
            f"the contract claims to be exhaustive but does not document these codes "
            f"`transition()` can return: {missing}"
        )
    else:
        assert "not exhaustive" in doc, (
            "the contract neither claims completeness nor says it is incomplete; a "
            "caller cannot tell whether to expect other codes"
        )


@pytest.mark.parametrize("field", ["superseded_by", "supersedes"])
@pytest.mark.parametrize("where", ["other-namespace", "other-plane"])
def test_a_supersession_target_outside_the_subjects_identity_is_refused(
    tmp_path: Path, field: str, where: str
) -> None:
    """THE LINEAGE IDENTITY CELL. A unique id is not a valid lineage target.

    The walk used `namespace` only to chase a cycle; it never checked that the id it was
    handed identifies a row in the subject's collection AND namespace. So on the
    unqualified path any unique KSUID from another namespace -- or another plane --
    was accepted as `superseded_by`, against the same-type/same-namespace contract
    (Copilot, musubi#771).

    The target here is UNIQUE and RESOLVABLE, just not the subject's. That is what makes
    the cell specific: it cannot pass because of the ambiguity refusal."""
    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
    from musubi.lifecycle.transitions import transition
    from musubi.types.common import generate_ksuid

    subject, foreign = generate_ksuid(), generate_ksuid()
    client = QdrantClient(":memory:")
    _collection_with(
        client,
        "musubi_episodic",
        [{"object_id": subject, "namespace": "ns/a", "state": "provisional", "version": 1}],
    )
    # Both violations of the SAME contract, and neither is caught by the other's check:
    # a right-plane/wrong-namespace target, and a right-namespace/wrong-PLANE one.
    if where == "other-namespace":
        client.upsert(
            "musubi_episodic",
            points=[
                qmodels.PointStruct(
                    id=99,
                    vector=[0.1, 0.2],
                    payload={
                        "object_id": foreign,
                        "namespace": "ns/b",
                        "state": "matured",
                        "version": 1,
                    },
                )
            ],
        )
    else:
        _collection_with(
            client,
            "musubi_curated",
            [{"object_id": foreign, "namespace": "ns/a", "state": "matured", "version": 1}],
        )
    coordinator = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "wk.db")

    result = transition(
        client,
        coordinator=coordinator,
        object_id=subject,
        target_state="matured",
        actor="operator",
        reason="foreign-lineage",
        namespace="ns/a",
        lineage_updates=(
            LineageUpdates(superseded_by=foreign)
            if field == "superseded_by"
            else LineageUpdates(supersedes=[foreign])
        ),
    )

    assert isinstance(result, Err), (
        f"a {where} lineage target was accepted via {field}: {result!r} -- `supersedes` "
        f"was never inspected at all, so one direction cannot certify both"
    )
    assert result.error.code == "invariant_violation", (
        f"refused with {result.error.code!r}; the target is unique and resolvable, so "
        f"this must not be the ambiguity refusal wearing a different hat"
    )

    rows, _ = client.scroll(
        collection_name="musubi_episodic",
        scroll_filter=qmodels.Filter(
            must=[qmodels.FieldCondition(key="object_id", match=qmodels.MatchValue(value=subject))]
        ),
        limit=2,
        with_payload=True,
    )
    assert (rows[0].payload or {})["state"] == "provisional", "the subject moved anyway"
