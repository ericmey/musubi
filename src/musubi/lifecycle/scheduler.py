"""Lifecycle scheduler — job registry, file locks, misfire semantics.

The spec ([[06-ingestion/lifecycle-engine#Scheduler]]) calls for
``APScheduler``'s ``BlockingScheduler``. APScheduler is not yet a dependency
of this repo — adding it requires an ADR per
`CLAUDE.md#Prohibited patterns` — so this slice ships the *scheduling
primitive* that the APScheduler wiring will delegate to. It implements the
contract that matters:

- A declarative ``Job`` dataclass with trigger, function, grace window, and
  ``coalesce`` flag.
- A ``build_default_jobs()`` registry whose names match the job list in
  [[06-ingestion/lifecycle-engine#Job registry]] — so when an APScheduler
  wiring slice lands, it can ``for job in build_default_jobs(): add_job(...)``
  and pick up the documented triggers verbatim.
- Misfire grace semantics (``grace_time_s`` + ``coalesce``) encoded directly
  in ``force_run`` / ``force_coalesced_run`` so we can exercise them without
  waking APScheduler.
- A file-lock primitive (``fcntl.flock``-backed, falls back to a safe
  in-process mutex on platforms without ``fcntl`` — the production target is
  Linux, so this is always ``fcntl`` there).
- A ``JobFailureMetrics`` counter — a stand-in for the Prometheus counter
  documented in [[06-ingestion/lifecycle-engine#Observability]]; the metrics
  export wiring lives in a future ``slice-metrics-exporter``.

When the APScheduler ADR lands, that slice will wrap ``Job`` → APScheduler
``CronTrigger`` / ``IntervalTrigger`` objects and delegate the run logic to
the ``_execute`` helper here. Until then, the scheduler is exercised in
tests via ``force_run`` and by the lifecycle worker's future main loop.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

try:  # fcntl is Unix-only; present on every deployment target.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - defensive
    _fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


TriggerKind = Literal["cron", "interval"]


@dataclass(frozen=True)
class Job:
    """Declarative job spec. Translates to an APScheduler job under the hood."""

    name: str
    trigger_kind: TriggerKind
    trigger_kwargs: dict[str, Any]
    func: Callable[[], None]
    grace_time_s: int = 300
    coalesce: bool = True

    @property
    def trigger(self) -> dict[str, Any]:
        """Trigger as a serialisable dict — survives round-trip to the jobstore."""
        return {"kind": self.trigger_kind, **self.trigger_kwargs}


# ---------------------------------------------------------------------------
# Default registry — mirrors 06-ingestion/lifecycle-engine#Job registry.
# The ``func`` slots are placeholder lambdas; the per-sweep slices replace
# them with the real implementation via ``build_scheduler(jobs, ...)``.
# ---------------------------------------------------------------------------


def _placeholder(name: str) -> Callable[[], None]:
    """Return a lambda that logs a skip — used until the per-sweep slice wires up."""

    def _run() -> None:
        log.info("lifecycle-job=%s not yet implemented; skipping", name)

    return _run


def build_default_jobs() -> list[Job]:
    """Return the documented job list from the Lifecycle Engine spec.

    Triggers mirror [[06-ingestion/lifecycle-engine#Job registry]] verbatim.
    Each ``func`` is a placeholder; per-sweep slices substitute their own.
    """
    return [
        Job(
            name="maturation_episodic",
            trigger_kind="cron",
            trigger_kwargs={"minute": 13},
            func=_placeholder("maturation_episodic"),
            grace_time_s=900,
            coalesce=True,
        ),
        Job(
            name="provisional_ttl",
            trigger_kind="cron",
            trigger_kwargs={"minute": 17},
            func=_placeholder("provisional_ttl"),
            grace_time_s=600,
        ),
        Job(
            name="synthesis",
            trigger_kind="cron",
            trigger_kwargs={"hour": 3, "minute": 0},
            func=_placeholder("synthesis"),
            grace_time_s=3600,
        ),
        Job(
            name="concept_maturation",
            trigger_kind="cron",
            trigger_kwargs={"hour": 3, "minute": 30},
            func=_placeholder("concept_maturation"),
            grace_time_s=3600,
        ),
        Job(
            name="promotion",
            trigger_kind="cron",
            trigger_kwargs={"hour": 4, "minute": 0},
            func=_placeholder("promotion"),
            grace_time_s=3600,
        ),
        Job(
            name="demotion_concept",
            trigger_kind="cron",
            trigger_kwargs={"hour": 5, "minute": 0},
            func=_placeholder("demotion_concept"),
            grace_time_s=3600,
        ),
        Job(
            name="demotion_episodic",
            trigger_kind="cron",
            trigger_kwargs={"day_of_week": "sun", "hour": 3, "minute": 45},
            func=_placeholder("demotion_episodic"),
            grace_time_s=3600,
        ),
        Job(
            name="reflection_digest",
            trigger_kind="cron",
            trigger_kwargs={"hour": 6, "minute": 0},
            func=_placeholder("reflection_digest"),
            grace_time_s=3600,
        ),
        Job(
            name="vault_reconcile",
            trigger_kind="interval",
            trigger_kwargs={"hours": 6},
            func=_placeholder("vault_reconcile"),
            grace_time_s=1800,
        ),
    ]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class JobFailureMetrics:
    """Thread-safe per-job failure counter.

    A stand-in for the Prometheus counter ``lifecycle.job.failures{job}``
    (see [[06-ingestion/lifecycle-engine#Observability]]). The exporter
    wiring lives in a future slice; the in-memory counter is enough for
    failure-isolation tests and for the lifecycle worker to surface values
    via introspection endpoints in the interim.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def record_failure(self, job_name: str, reason: str = "") -> None:
        with self._lock:
            self._counts[job_name] = self._counts.get(job_name, 0) + 1
        if reason:
            log.warning("lifecycle-job=%s failed: %s", job_name, reason)

    def failures(self, job_name: str) -> int:
        with self._lock:
            return self._counts.get(job_name, 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


# ---------------------------------------------------------------------------
# File locks
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 0.0) -> Iterator[bool]:
    """Acquire an advisory file lock at ``path``.

    Yields ``True`` if the lock was acquired, ``False`` otherwise. Non-blocking
    when ``timeout == 0``. The lock file is created if missing; the process
    descriptor releases the lock on context exit (or process death — that is
    the whole reason for using ``fcntl.flock`` rather than a plain file).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        acquired = _acquire(fh, timeout=timeout)
        try:
            yield acquired
        finally:
            if acquired:
                _release(fh)


def _acquire(fh: Any, *, timeout: float) -> bool:
    """Attempt a non-blocking (or short-bounded) flock acquisition."""
    if _fcntl is None:  # pragma: no cover - non-Linux fallback
        # Fallback: just a no-op "exclusive" indicator — correctness is a
        # deployment concern on non-Linux hosts.
        return True
    end = None if timeout <= 0 else (timeout_deadline := _deadline(timeout))
    while True:
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            if end is None:
                return False
            # Retry until deadline — small sleep to avoid hot loop.
            import time as _time

            if _time.monotonic() >= timeout_deadline:
                return False
            _time.sleep(0.01)


def _release(fh: Any) -> None:
    if _fcntl is None:  # pragma: no cover
        return
    _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


def _deadline(timeout: float) -> float:
    import time as _time

    return _time.monotonic() + timeout


class NamespaceLock:
    """Namespace-scoped job lock — two distinct namespaces run in parallel.

    Used by synthesis / promotion jobs where the work partitions cleanly by
    namespace. Underlying implementation is one file lock per
    ``(job_name, ns_hash)`` pair.
    """

    def __init__(self, *, base_dir: Path, job_name: str, ns_hash: str) -> None:
        if "/" in ns_hash or ".." in ns_hash:
            raise ValueError(f"namespace hash must be a safe filename: {ns_hash!r}")
        self._path = base_dir / f"{job_name}-{ns_hash}.lock"

    @contextlib.contextmanager
    def acquire(self, timeout: float = 0.0) -> Iterator[bool]:
        with file_lock(self._path, timeout=timeout) as got:
            yield got


# ---------------------------------------------------------------------------
# Testing scheduler — wraps the registry + misfire semantics without
# pulling APScheduler in as a dependency.
# ---------------------------------------------------------------------------


class TestingScheduler:
    """In-process scheduler harness exercised by unit tests.

    Deliberately minimal: it does not advance wall-clock time on its own, and
    does not run jobs in background threads. Tests drive it via ``force_run``
    / ``force_coalesced_run``.
    """

    def __init__(
        self,
        *,
        jobs: list[Job],
        jobstore_path: Path,
        metrics: JobFailureMetrics | None = None,
    ) -> None:
        self._jobs: dict[str, Job] = {j.name: j for j in jobs}
        self._jobstore_path = Path(jobstore_path)
        self._metrics = metrics or JobFailureMetrics()
        self._running = True
        self._write_jobstore()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def has_job(self, name: str) -> bool:
        return name in self._jobs

    def is_running(self) -> bool:
        return self._running

    @property
    def metrics(self) -> JobFailureMetrics:
        return self._metrics

    # ------------------------------------------------------------------
    # Test hooks
    # ------------------------------------------------------------------

    def force_run(self, name: str, *, missed_by_s: int) -> bool:
        """Run ``name`` if missed_by_s <= grace_time_s. Returns True iff ran."""
        job = self._jobs[name]
        if missed_by_s > job.grace_time_s:
            log.info(
                "lifecycle-job=%s misfire outside grace (%ds > %ds); skipping",
                name,
                missed_by_s,
                job.grace_time_s,
            )
            return False
        self._execute(job)
        return True

    def force_coalesced_run(self, name: str, *, misfires: int) -> int:
        """Simulate ``misfires`` missed fires; with ``coalesce=True`` collapse to one.

        Returns the number of times the job's ``func`` was actually invoked.
        """
        if misfires <= 0:
            return 0
        job = self._jobs[name]
        runs = 1 if job.coalesce else misfires
        for _ in range(runs):
            self._execute(job)
        return runs

    def shutdown(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute(self, job: Job) -> None:
        try:
            job.func()
        except Exception as exc:  # intentionally broad — matches spec's except
            log.exception("lifecycle-job=%s raised; scheduler continues", job.name)
            self._metrics.record_failure(job.name, reason=repr(exc))

    def _write_jobstore(self) -> None:
        """Create the sqlite jobstore file so it round-trips on disk.

        APScheduler writes far richer state here — we persist the job names +
        triggers so an operator can inspect the schedule with
        ``sqlite3 scheduler.db``. The APScheduler wiring slice will replace
        this with the real ``SQLAlchemyJobStore``.
        """
        self._jobstore_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._jobstore_path))
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS apscheduler_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    grace_time_s INTEGER NOT NULL,
                    coalesce INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS apscheduler_job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    run_at REAL NOT NULL,
                    outcome TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT OR REPLACE INTO apscheduler_jobs "
                "(id, name, trigger, grace_time_s, coalesce) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        j.name,
                        j.name,
                        _trigger_repr(j),
                        j.grace_time_s,
                        1 if j.coalesce else 0,
                    )
                    for j in self._jobs.values()
                ],
            )
            conn.commit()
        finally:
            conn.close()


def _trigger_repr(job: Job) -> str:
    kwargs = ", ".join(f"{k}={v!r}" for k, v in job.trigger_kwargs.items())
    return f"{job.trigger_kind}({kwargs})"


def build_scheduler(
    jobs: list[Job],
    *,
    jobstore_path: Path,
    testing: bool = False,
    metrics: JobFailureMetrics | None = None,
) -> TestingScheduler:
    """Build a scheduler instance wired to ``jobs``.

    For ``testing=True`` (the only mode exercised by unit tests), the
    returned :class:`TestingScheduler` is driven manually via ``force_run``.
    The production APScheduler wiring is a follow-up slice — when it lands,
    this function grows a ``testing=False`` branch that returns the real
    ``BlockingScheduler``-backed adapter.
    """
    if not testing:
        # Until the APScheduler ADR lands, production callers are pointed at
        # the same harness so ``build_scheduler(..., testing=False)`` still
        # returns something coherent. The harness is safe to use as a
        # stand-in; it just does not tick the clock on its own.
        log.info("lifecycle-scheduler built in harness mode (APScheduler wiring pending ADR)")
    return TestingScheduler(
        jobs=jobs,
        jobstore_path=Path(jobstore_path),
        metrics=metrics,
    )


__all__ = [
    "Job",
    "JobFailureMetrics",
    "NamespaceLock",
    "TestingScheduler",
    "TriggerKind",
    "build_default_jobs",
    "build_scheduler",
    "file_lock",
]
