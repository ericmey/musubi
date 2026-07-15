import sys
from unittest.mock import MagicMock

import pytest

from musubi.lifecycle.runner import LifecycleRunner
from musubi.lifecycle.scheduler import Job, build_default_jobs
from musubi.observability.registry import default_registry, render_text_format

pytestmark = pytest.mark.anyio


def _duration_count(job_name: str) -> int:
    text = render_text_format(default_registry())
    total = 0
    for line in text.split("\n"):
        if (
            line.startswith('musubi_lifecycle_job_duration_seconds_count{job="')
            and f'job="{job_name}"' in line
        ):
            parts = line.split(" ")
            if len(parts) == 2:
                total += int(float(parts[1]))
    return total


def _error_count(job_name: str) -> int:
    text = render_text_format(default_registry())
    for line in text.split("\n"):
        if (
            line.startswith('musubi_lifecycle_job_errors_total{job="')
            and f'job="{job_name}"' in line
        ):
            parts = line.split(" ")
            if len(parts) == 2:
                return int(float(parts[1]))
    return 0


async def test_runner_dispatch_observes_job_duration_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mock opentelemetry imports since it is missing in the test environment
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry", MagicMock())

    def fast_job() -> None:
        pass

    job = Job(
        name="test_fast_job",
        trigger_kind="interval",
        trigger_kwargs={"seconds": 1},
        func=fast_job,
    )

    runner = LifecycleRunner(jobs=[job])

    before_dur = _duration_count("test_fast_job")
    before_err = _error_count("test_fast_job")

    await runner._dispatch(job)

    assert _duration_count("test_fast_job") == before_dur + 1
    assert _error_count("test_fast_job") == before_err


async def test_runner_dispatch_observes_job_duration_and_errors_on_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry", MagicMock())

    def crashing_job() -> None:
        raise RuntimeError("simulated crash")

    job = Job(
        name="test_crashing_job",
        trigger_kind="interval",
        trigger_kwargs={"seconds": 1},
        func=crashing_job,
    )

    runner = LifecycleRunner(jobs=[job])

    before_dur = _duration_count("test_crashing_job")
    before_err = _error_count("test_crashing_job")

    await runner._dispatch(job)

    assert _duration_count("test_crashing_job") == before_dur + 1
    assert _error_count("test_crashing_job") == before_err + 1


async def test_all_default_jobs_are_instrumented(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every registered job emits duration + errors with the exact canonical dispatch name."""
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry", MagicMock())

    jobs = build_default_jobs()
    runner = LifecycleRunner(jobs=jobs)

    for job in jobs:
        before_dur = _duration_count(job.name)
        before_err = _error_count(job.name)

        await runner._dispatch(job)

        # We assert that the metric incremented EXACTLY once for this canonical job name
        assert _duration_count(job.name) == before_dur + 1
        # Success path, so no errors
        assert _error_count(job.name) == before_err
