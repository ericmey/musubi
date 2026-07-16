"""LIFE-006 — durable ops-alerts Thought on lifecycle job crash.

Test Contract (slice-issue528-life006-job-failure-alerts):
- test_job_success_emits_no_alert
- test_job_failure_emits_exactly_one_durable_alert
- test_alert_emission_failure_remains_visible_and_does_not_crash_runner
- test_alert_emission_timeout_is_bounded_and_does_not_crash_runner
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable

import pytest

from musubi.lifecycle.runner import LifecycleRunner
from musubi.lifecycle.scheduler import Job


@pytest.fixture(autouse=True)
def mock_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate OTel imports so other suites are not polluted by leftover mocks."""
    import sys
    from unittest.mock import MagicMock

    monkeypatch.setitem(sys.modules, "opentelemetry", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", MagicMock())


class FakeAlertEmitter:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, str, str | None]] = []
        self.should_fail = False
        self.should_timeout = False

    async def emit(self, channel: str, content: str, title: str | None = None) -> None:
        if self.should_fail:
            raise RuntimeError("Fake alert emission failed")
        if self.should_timeout:
            await asyncio.sleep(10.0)  # > 5.0s wait_for

        self.emitted.append((channel, content, title))


def _alert_err_count(job_name: str) -> int:
    from musubi.observability.registry import default_registry, render_text_format

    text = render_text_format(default_registry())
    for line in text.split("\n"):
        if (
            line.startswith('musubi_lifecycle_job_alert_errors_total{job="')
            and f'job="{job_name}"' in line
        ):
            parts = line.split(" ")
            if len(parts) == 2:
                return int(float(parts[1]))
    return 0


@pytest.mark.asyncio
async def test_job_success_emits_no_alert() -> None:
    def ok_job() -> None:
        pass

    job = Job(name="test_ok", trigger_kind="interval", trigger_kwargs={"seconds": 1}, func=ok_job)
    emitter = FakeAlertEmitter()
    runner = LifecycleRunner(jobs=[job], thought_emitter=emitter)

    await runner._dispatch(job)

    assert len(emitter.emitted) == 0


@pytest.mark.asyncio
async def test_job_failure_emits_exactly_one_durable_alert() -> None:
    def crashing_job() -> None:
        raise ValueError("Oops I failed")

    job = Job(
        name="test_crash", trigger_kind="interval", trigger_kwargs={"seconds": 1}, func=crashing_job
    )
    emitter = FakeAlertEmitter()
    runner = LifecycleRunner(jobs=[job], thought_emitter=emitter)

    await runner._dispatch(job)

    assert len(emitter.emitted) == 1
    channel, content, title = emitter.emitted[0]

    assert channel == "ops-alerts"
    assert title == "Lifecycle Job Failure"
    # Content must be bounded and safe: job name + exception class + generic failure
    assert "test_crash" in content
    assert "ValueError" in content
    # Should not blindly dump str(exc) secrets
    assert "Oops I failed" not in content
    # LIFE-006 Copilot: bounded UTC timestamp + correlation/trace id
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", content)
    assert "trace_id=" in content


@pytest.mark.asyncio
async def test_alert_emission_failure_remains_visible_and_does_not_crash_runner() -> None:
    def crashing_job() -> None:
        raise ValueError("I failed")

    job = Job(
        name="test_alert_fail",
        trigger_kind="interval",
        trigger_kwargs={"seconds": 1},
        func=crashing_job,
    )
    emitter = FakeAlertEmitter()
    emitter.should_fail = True
    runner = LifecycleRunner(jobs=[job], thought_emitter=emitter)

    before_alert_errs = _alert_err_count("test_alert_fail")

    await runner._dispatch(job)

    # Dispatch must finish cleanly
    assert _alert_err_count("test_alert_fail") == before_alert_errs + 1


@pytest.mark.asyncio
async def test_alert_emission_timeout_is_bounded_and_does_not_crash_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crashing_job() -> None:
        raise ValueError("I failed")

    job = Job(
        name="test_alert_timeout",
        trigger_kind="interval",
        trigger_kwargs={"seconds": 1},
        func=crashing_job,
    )
    emitter = FakeAlertEmitter()
    emitter.should_timeout = True
    runner = LifecycleRunner(jobs=[job], thought_emitter=emitter)

    before_alert_errs = _alert_err_count("test_alert_timeout")

    original_wait_for = asyncio.wait_for

    async def fast_wait_for(coro: Awaitable[object], timeout: float | None) -> object:
        return await original_wait_for(coro, 0.01)

    monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

    await runner._dispatch(job)

    # The timeout raises TimeoutError, which is an Exception, so it gets caught
    assert _alert_err_count("test_alert_timeout") == before_alert_errs + 1


def test_main_async_wires_thought_emitter_into_runner() -> None:
    """Production boot must pass thought_emitter — otherwise alerts never fire."""
    import inspect

    from musubi.lifecycle import runner as runner_mod

    source = inspect.getsource(runner_mod._main_async)
    assert "thought_emitter=thought_emitter" in source
    assert "LifecycleRunner(" in source
