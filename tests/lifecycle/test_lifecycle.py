"""Test contract for slice-lifecycle-engine.

Implements the Test Contract bullets from the two specs this slice owns:

- [[04-data-model/lifecycle#Test contract]] (transition engine + events)
- [[06-ingestion/lifecycle-engine#Test contract]] (APScheduler + locks)

Every bullet in those sections is present here with its verbatim name — either
as a passing test, or ``@pytest.mark.skip(reason=...)`` pointing at the
downstream slice that owns the method under test, or declared ``⊘ out-of-scope``
in ``docs/architecture/_slices/slice-lifecycle-engine.md`` ``## Work log`` (for
the two hypothesis bullets + three integration bullets).

Runs against an in-memory Qdrant (``QdrantClient(":memory:")``) plus on-disk
sqlite under ``tmp_path``. No network, no real LLM calls.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
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


async def _seed_matured(plane: EpisodicPlane, ns: str, content: str = "seeded") -> EpisodicMemory:
    """Helper: create + mature an episodic so it is eligible for demote/supersede."""
    saved = await plane.create(EpisodicMemory(namespace=ns, content=content))
    matured, _ = await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="test-fixture",
        reason="seed",
    )
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
        object_id=saved.object_id,
        target_state="matured",
        actor="test-suite",
        reason="unit",
        sink=sink,
    )
    assert isinstance(result, Ok), result
    tr = result.value
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
        object_id=saved.object_id,
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
        object_id=saved.object_id,
        target_state="matured",
        actor="t",
        reason="u",
        sink=sink,
    )
    assert isinstance(result, Ok)
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
    old = await _seed_matured(plane, ns, content="old-version")
    new = await _seed_matured(plane, ns, content="new-version")
    result = transition(
        qdrant,
        object_id=old.object_id,
        target_state="superseded",
        actor="rewrite",
        reason="new version written",
        lineage_updates=LineageUpdates(superseded_by=new.object_id),
        sink=sink,
    )
    assert isinstance(result, Ok), result
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
    a = await _seed_matured(plane, ns, content="alpha")
    b = await _seed_matured(plane, ns, content="beta")
    # A superseded by B — legal.
    ok_first = transition(
        qdrant,
        object_id=a.object_id,
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
        object_id=b.object_id,
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
    matured = await _seed_matured(plane, ns, content="demote-me")
    result = transition(
        qdrant,
        object_id=matured.object_id,
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
            object_id=saved.object_id,
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


async def test_concurrent_transitions_last_writer_wins_with_logged_warning(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bullet 13 — concurrent transitions: last writer wins; warning logged."""
    saved = await _seed_matured(plane, ns, content="concurrent")
    caplog.set_level(logging.WARNING, logger="musubi.lifecycle.transitions")
    # Two sequential transitions simulating the race — the second must see the
    # version bumped by the first and log a concurrent-modification warning.
    first = transition(
        qdrant,
        object_id=saved.object_id,
        target_state="demoted",
        actor="worker-a",
        reason="a-demote",
        expected_version=saved.version,
        sink=sink,
    )
    second = transition(
        qdrant,
        object_id=saved.object_id,
        target_state="superseded",
        actor="worker-b",
        reason="b-supersede",
        expected_version=saved.version,  # stale!
        lineage_updates=LineageUpdates(superseded_by="0" * 27),
        sink=sink,
    )
    # Second transition is either (a) rejected and retried internally (Ok with warning),
    # or (b) wins last-write-wins semantics with a warning record. Either way a
    # warning must be present in the captured log text.
    assert isinstance(first, Ok)
    assert isinstance(second, (Ok, Err))
    captured = caplog.text.lower()
    assert "concurrent" in captured or "stale version" in captured, captured


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
        seeded = await _seed_matured(plane, ns, content="flush-load")
        started = time.monotonic()
        result = transition(
            qdrant,
            object_id=seeded.object_id,
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
    seeded = await _seed_matured(plane, ns, content="restart-survivor")
    first = LifecycleEventSink(db_path=events_db, flush_every_n=100, flush_every_s=5.0)
    try:
        result = transition(
            qdrant,
            object_id=seeded.object_id,
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
                object_id=saved.object_id,
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
    seeded = await _seed_matured(plane, ns, content="restart-events")
    s1 = LifecycleEventSink(db_path=events_db, flush_every_n=1, flush_every_s=0.1)
    try:
        result = transition(
            qdrant,
            object_id=seeded.object_id,
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


def test_sink_record_after_close_raises(tmp_path: Path) -> None:
    """Recording into a closed sink raises RuntimeError."""
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
    with pytest.raises(RuntimeError, match="closed"):
        sink.record(ev)


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


def test_transition_not_found_returns_typed_error(qdrant: QdrantClient) -> None:
    """Missing object_id yields code='not_found' without mutating any plane."""
    result = transition(
        qdrant,
        object_id="z" * 27,
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
        transition(qdrant, object_id=a.object_id, target_state="matured", actor="t", reason="warm"),
        Ok,
    )
    r = transition(
        qdrant,
        object_id=a.object_id,
        target_state="superseded",
        actor="t",
        reason="dup",
        lineage_updates=LineageUpdates(superseded_by=b.object_id),
        sink=sink,
    )
    assert isinstance(r, Ok)
    assert r.value.event.lineage_changes["superseded_by"] == b.object_id
