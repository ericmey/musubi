"""Unit tests for :mod:`musubi.lifecycle.runner`.

Scope:

- ``_cron_matches`` — minute / hour / day / day_of_week / composite
  triggers evaluate correctly; unknown keys raise.
- ``_interval_due`` — first call fires; subsequent only after the
  interval elapses.
- ``LifecycleRunner._tick`` — dispatches matching jobs; dedupes so a
  job fires at most once per minute even if the tick ran twice in the
  same wall-minute.
- ``LifecycleRunner.run`` — ticks, dispatches, then exits on
  :meth:`request_stop` within one tick.
- ``build_lifecycle_jobs`` — real maturation jobs replace the
  placeholders for their names; other placeholders pass through.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from musubi.lifecycle.runner import (
    LifecycleRunner,
    _cron_matches,
    _interval_due,
    build_lifecycle_jobs,
)
from musubi.lifecycle.scheduler import Job, build_default_jobs

# ---------------------------------------------------------------------------
# _cron_matches
# ---------------------------------------------------------------------------


def test_cron_matches_minute_only_fires_each_hour_at_that_minute() -> None:
    kwargs = {"minute": 13}
    assert _cron_matches(kwargs, datetime(2026, 1, 1, 5, 13))
    assert _cron_matches(kwargs, datetime(2026, 1, 1, 23, 13))
    assert not _cron_matches(kwargs, datetime(2026, 1, 1, 5, 14))
    assert not _cron_matches(kwargs, datetime(2026, 1, 1, 5, 12))


def test_cron_matches_hour_and_minute_fires_once_per_day() -> None:
    kwargs = {"hour": 3, "minute": 0}
    assert _cron_matches(kwargs, datetime(2026, 1, 1, 3, 0))
    assert not _cron_matches(kwargs, datetime(2026, 1, 1, 4, 0))
    assert not _cron_matches(kwargs, datetime(2026, 1, 1, 3, 1))


def test_cron_matches_day_of_week() -> None:
    # 2026-01-04 is a Sunday.
    kwargs = {"day_of_week": "sun", "hour": 3, "minute": 45}
    assert _cron_matches(kwargs, datetime(2026, 1, 4, 3, 45))
    assert not _cron_matches(kwargs, datetime(2026, 1, 5, 3, 45))  # Mon
    assert not _cron_matches(kwargs, datetime(2026, 1, 4, 4, 45))


def test_cron_matches_unknown_field_raises() -> None:
    with pytest.raises(ValueError, match="unsupported cron field"):
        _cron_matches({"second": 10}, datetime(2026, 1, 1, 0, 0))


# ---------------------------------------------------------------------------
# _interval_due
# ---------------------------------------------------------------------------


def test_interval_due_fires_on_first_tick() -> None:
    assert _interval_due({"hours": 6}, None, datetime(2026, 1, 1, 0, 0))


def test_interval_due_waits_until_elapsed() -> None:
    last = datetime(2026, 1, 1, 0, 0)
    assert not _interval_due({"hours": 6}, last, last + timedelta(hours=5))
    assert _interval_due({"hours": 6}, last, last + timedelta(hours=6))
    assert _interval_due({"hours": 6}, last, last + timedelta(hours=7))


# ---------------------------------------------------------------------------
# Runner dispatch
# ---------------------------------------------------------------------------


def _make_counter_job(
    name: str, *, trigger_kind: str = "cron", **kwargs: Any
) -> tuple[Job, list[datetime]]:
    """Build a Job whose func appends the fire-time to a log list."""
    fires: list[datetime] = []

    def _run() -> None:
        fires.append(datetime.now(UTC).replace(tzinfo=None))

    return (
        Job(
            name=name,
            trigger_kind=trigger_kind,  # type: ignore[arg-type]
            trigger_kwargs=kwargs,
            func=_run,
            grace_time_s=600,
        ),
        fires,
    )


async def test_runner_dispatches_matching_job_once_per_minute() -> None:
    job, fires = _make_counter_job("every_minute_13", minute=13)
    runner = LifecycleRunner(jobs=[job], tick_seconds=60)

    # Two ticks at the same minute — dedupe to one dispatch.
    await runner._tick(datetime(2026, 1, 1, 0, 13))
    await runner._tick(datetime(2026, 1, 1, 0, 13))
    # Different minute same job triggers, does not fire.
    await runner._tick(datetime(2026, 1, 1, 0, 14))
    # Next hour same minute — fires again.
    await runner._tick(datetime(2026, 1, 1, 1, 13))

    await _drain_runner_tasks()
    assert len(fires) == 2


async def test_runner_skips_non_matching_job() -> None:
    job, fires = _make_counter_job("hourly_at_13", minute=13)
    runner = LifecycleRunner(jobs=[job], tick_seconds=60)

    await runner._tick(datetime(2026, 1, 1, 0, 12))
    await runner._tick(datetime(2026, 1, 1, 0, 14))
    await _drain_runner_tasks()
    assert fires == []


async def test_runner_fires_interval_on_boot_then_waits() -> None:
    job, fires = _make_counter_job("reconcile", trigger_kind="interval", hours=6)
    runner = LifecycleRunner(jobs=[job], tick_seconds=60)

    await runner._tick(datetime(2026, 1, 1, 0, 0))
    await _drain_runner_tasks()
    assert len(fires) == 1

    # 5 hours later — not yet due.
    await runner._tick(datetime(2026, 1, 1, 5, 0))
    await _drain_runner_tasks()
    assert len(fires) == 1

    # 6 hours later — due.
    await runner._tick(datetime(2026, 1, 1, 6, 0))
    await _drain_runner_tasks()
    assert len(fires) == 2


async def test_runner_isolates_job_exception() -> None:
    """A crashing job must not prevent other jobs from running."""

    def _boom() -> None:
        raise RuntimeError("crash")

    boom_job = Job(
        name="boom",
        trigger_kind="cron",
        trigger_kwargs={"minute": 13},
        func=_boom,
        grace_time_s=600,
    )
    good_job, fires = _make_counter_job("good", minute=13)
    runner = LifecycleRunner(jobs=[boom_job, good_job], tick_seconds=60)

    await runner._tick(datetime(2026, 1, 1, 0, 13))
    await _drain_runner_tasks()

    assert len(fires) == 1  # good_job ran despite boom_job crashing


async def test_runner_exits_when_request_stop_called() -> None:
    runner = LifecycleRunner(jobs=[], tick_seconds=1)
    runner.request_stop()
    # run() must return promptly (not block on tick_seconds).
    await asyncio.wait_for(runner.run(), timeout=2.0)


async def _drain_runner_tasks() -> None:
    """Let background :func:`asyncio.create_task` dispatches finish."""
    # Give every pending task a chance to complete. One round of
    # ``sleep(0)`` is usually enough but a small non-zero sleep also
    # covers the ``to_thread`` hop.
    for _ in range(20):
        await asyncio.sleep(0.01)
        remaining = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and (t.get_name() or "").startswith("lifecycle-job-")
            and not t.done()
        ]
        if not remaining:
            return


# ---------------------------------------------------------------------------
# build_lifecycle_jobs
# ---------------------------------------------------------------------------


def test_build_lifecycle_jobs_uses_maturation_builders_for_covered_names() -> None:
    stub_names = {"maturation_episodic", "provisional_ttl"}
    stubs = [
        Job(
            name=name,
            trigger_kind="cron",
            trigger_kwargs={"minute": 0},
            func=lambda: None,
        )
        for name in stub_names
    ]

    all_jobs = build_lifecycle_jobs(maturation_jobs=stubs)
    by_name = {j.name: j for j in all_jobs}

    # Our stubs replaced the placeholder entries for those names.
    for name in stub_names:
        assert by_name[name].func.__name__ in ("<lambda>", "_runner")

    # Every documented job name is present.
    documented = {j.name for j in build_default_jobs()}
    assert documented.issubset(by_name.keys())


def test_build_lifecycle_jobs_without_maturation_keeps_placeholders() -> None:
    all_jobs = build_lifecycle_jobs()
    names = {j.name for j in all_jobs}
    assert names == {j.name for j in build_default_jobs()}


def test_build_lifecycle_jobs_wires_demotion_builders() -> None:
    """demotion_episodic + demotion_concept come from the real builder
    when passed; other sweeps fall back to placeholders."""
    dem_stubs = [
        Job(
            name="demotion_episodic",
            trigger_kind="cron",
            trigger_kwargs={"day_of_week": "sun", "hour": 3, "minute": 45},
            func=lambda: None,
            grace_time_s=3600,
        ),
        Job(
            name="demotion_concept",
            trigger_kind="cron",
            trigger_kwargs={"hour": 5, "minute": 0},
            func=lambda: None,
            grace_time_s=3600,
        ),
    ]
    jobs = build_lifecycle_jobs(demotion_jobs=dem_stubs)
    by_name = {j.name: j for j in jobs}
    # The demotion slots carry the real stubs.
    assert by_name["demotion_episodic"] is dem_stubs[0]
    assert by_name["demotion_concept"] is dem_stubs[1]
    # Other names stay as the default placeholders.
    assert by_name["synthesis"].func.__name__ == "_run"
    # Full documented set is still present.
    documented = {j.name for j in build_default_jobs()}
    assert documented.issubset(by_name.keys())


def test_build_lifecycle_jobs_merges_maturation_and_demotion() -> None:
    """Both real builder groups get composed together with placeholders
    for everything else."""
    mat_stub = Job(
        name="maturation_episodic",
        trigger_kind="cron",
        trigger_kwargs={"minute": 13},
        func=lambda: None,
        grace_time_s=900,
    )
    dem_stub = Job(
        name="demotion_concept",
        trigger_kind="cron",
        trigger_kwargs={"hour": 5, "minute": 0},
        func=lambda: None,
        grace_time_s=3600,
    )
    jobs = build_lifecycle_jobs(maturation_jobs=[mat_stub], demotion_jobs=[dem_stub])
    by_name = {j.name: j for j in jobs}
    assert by_name["maturation_episodic"] is mat_stub
    assert by_name["demotion_concept"] is dem_stub
    # A name from neither real group stays placeholder.
    assert by_name["synthesis"].func.__name__ == "_run"
