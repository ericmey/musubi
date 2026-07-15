import asyncio

import pytest

from musubi.lifecycle.runner import LifecycleRunner
from musubi.lifecycle.scheduler import Job


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


@pytest.mark.asyncio
async def test_job_success_emits_no_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from unittest.mock import MagicMock

    monkeypatch.setitem(sys.modules, "opentelemetry.trace", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry", MagicMock())

    def ok_job() -> None:
        pass

    job = Job(name="test_ok", trigger_kind="interval", trigger_kwargs={"seconds": 1}, func=ok_job)
    emitter = FakeAlertEmitter()
    runner = LifecycleRunner(jobs=[job], thought_emitter=emitter)

    await runner._dispatch(job)

    assert len(emitter.emitted) == 0


@pytest.mark.asyncio
async def test_job_failure_emits_exactly_one_durable_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from unittest.mock import MagicMock

    monkeypatch.setitem(sys.modules, "opentelemetry.trace", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry", MagicMock())

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


@pytest.mark.asyncio
async def test_alert_emission_failure_remains_visible_and_does_not_crash_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from unittest.mock import MagicMock

    monkeypatch.setitem(sys.modules, "opentelemetry.trace", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry", MagicMock())

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

    # We need to capture the metric
    from musubi.observability.registry import default_registry, render_text_format

    def _alert_err_count(job_name: str) -> int:
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

    before_alert_errs = _alert_err_count("test_alert_fail")

    await runner._dispatch(job)

    # Dispatch must finish cleanly
    assert _alert_err_count("test_alert_fail") == before_alert_errs + 1


@pytest.mark.asyncio
async def test_alert_emission_timeout_is_bounded_and_does_not_crash_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from unittest.mock import MagicMock

    monkeypatch.setitem(sys.modules, "opentelemetry.trace", MagicMock())
    monkeypatch.setitem(sys.modules, "opentelemetry", MagicMock())

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

    from musubi.observability.registry import default_registry, render_text_format

    def _alert_err_count(job_name: str) -> int:
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

    before_alert_errs = _alert_err_count("test_alert_timeout")

    # Run the dispatch with asyncio.wait_for to ensure the timeout inside _dispatch works
    # We will lower the timeout in the runner or mock it so the test doesn't actually take 5s.
    # Actually wait_for takes 5s, let's patch the timeout in the runner or just wait 5s (it's async so fine, but slow).
    # Since we can monkeypatch `asyncio.wait_for`, let's just let it run or patch `asyncio.wait_for`

    # Better: patch asyncio.wait_for locally
    original_wait_for = asyncio.wait_for

    from collections.abc import Awaitable

    async def fast_wait_for(coro: Awaitable[object], timeout: float | None) -> object:
        return await original_wait_for(coro, 0.01)

    monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

    await runner._dispatch(job)

    # The timeout raises TimeoutError, which is an Exception, so it gets caught
    assert _alert_err_count("test_alert_timeout") == before_alert_errs + 1
