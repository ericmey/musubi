"""Production lifecycle worker entrypoint.

Fills the gap called out in
:func:`musubi.lifecycle.scheduler.build_scheduler` — "the production
APScheduler wiring is a follow-up slice." Rather than take the
APScheduler dependency (which requires an ADR), this runner implements
the subset of APScheduler semantics the lifecycle engine actually needs:

- Tick-driven cron evaluation at minute resolution (most sweeps fire on
  the documented "every hour at :13" / "daily at 03:00" cadences).
- ``interval`` triggers with simple last-fired tracking (vault
  reconcile, every 6 hours).
- Misfire grace behaviour is delegated to :class:`Job.grace_time_s` —
  when the runner wakes more than ``grace_time_s`` after a missed
  trigger, it skips that occurrence. This matches the spec's
  coalesce-by-default contract.
- Sync :attr:`Job.func` callables run via :func:`asyncio.to_thread`, so
  each job gets its own ``asyncio.run()`` call tree inside a worker
  thread. That lets the maturation wrappers from
  :func:`build_maturation_jobs` work unchanged — they internally call
  ``asyncio.run(sweep())`` and need a fresh loop each tick.

When the APScheduler ADR eventually lands, this module is the single
swap point: replace :meth:`LifecycleRunner.run` with the real
``BlockingScheduler`` wiring and delete the tick loop.

Module entrypoint: ``python -m musubi.lifecycle.runner`` boots with the
full default job registry — maturation jobs are real, everything else
is the placeholder from :func:`build_default_jobs` until follow-up
slices land real ``build_xxx_jobs`` helpers.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from musubi.lifecycle.scheduler import Job

log = logging.getLogger(__name__)

_DAYS_OF_WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _cron_matches(trigger_kwargs: Mapping[str, Any], now: datetime) -> bool:
    """Return ``True`` iff ``now`` matches every key in ``trigger_kwargs``.

    Only the kwargs emitted by :func:`build_default_jobs` are handled:
    ``minute``, ``hour``, ``day``, ``month``, ``day_of_week``. Unknown
    keys raise ``ValueError`` — silently ignoring them would let a typo
    in the job registry turn a scheduled sweep into a no-op.
    """
    for key, expected in trigger_kwargs.items():
        if key == "minute":
            if now.minute != expected:
                return False
        elif key == "hour":
            if now.hour != expected:
                return False
        elif key == "day":
            if now.day != expected:
                return False
        elif key == "month":
            if now.month != expected:
                return False
        elif key == "day_of_week":
            if _DAYS_OF_WEEK[now.weekday()] != expected:
                return False
        else:
            raise ValueError(f"unsupported cron field: {key!r}")
    return True


def _interval_due(
    trigger_kwargs: Mapping[str, Any],
    last_fired: datetime | None,
    now: datetime,
) -> bool:
    """Return ``True`` iff an interval-triggered job is due.

    ``last_fired`` of ``None`` means the runner just booted — every
    interval job runs immediately on boot so operators see progress
    in the log without waiting a full cadence.
    """
    if last_fired is None:
        return True
    td = timedelta(**{k: float(v) for k, v in trigger_kwargs.items()})
    return now - last_fired >= td


@dataclass
class LifecycleRunner:
    """Tick-driven scheduler for :class:`Job` instances.

    Attributes
    ----------
    jobs:
        The job registry to drive. Composed at build time — see
        :func:`build_lifecycle_jobs`.
    tick_seconds:
        Seconds between ticks. Defaults to 60 so cron fields at
        minute-resolution can fire at their named minute. Tests lower
        this to drive the loop deterministically.
    """

    jobs: list[Job]
    tick_seconds: int = 60
    _stopping: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _last_fired: dict[str, datetime] = field(default_factory=dict, init=False)
    _in_flight: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    def request_stop(self) -> None:
        """Signal the run loop to exit after the current tick completes."""
        self._stopping.set()

    def install_signal_handlers(self) -> None:
        """Wire SIGTERM / SIGINT to :meth:`request_stop`.

        Kept separate from :meth:`run` so unit tests can skip it — some
        test frameworks swap the signal table and misbehave when a
        handler they didn't install gets called.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, ValueError):
                # add_signal_handler is Unix-only and rejects re-installs
                # inside certain test harnesses — not fatal.
                log.debug("signal-handler-install-skipped signal=%s", sig)

    async def run(self) -> None:
        """Drive the scheduling loop until :meth:`request_stop` is called.

        One tick:

        1. Evaluate every job against ``now`` (minute-truncated so cron
           fields match the calendar).
        2. For each job that fires, dispatch it via
           :func:`asyncio.to_thread`. We do NOT await completion — a
           long-running sweep must not block the next tick.
        3. Sleep until the next tick boundary (or until ``_stopping``
           is set).

        Per-job exceptions are caught and recorded to
        :class:`JobFailureMetrics` via the Job's own wrapper — at this
        layer we only guard against bugs in the dispatch path itself.
        """
        log.info(
            "lifecycle-runner-starting jobs=%s tick_seconds=%d",
            [j.name for j in self.jobs],
            self.tick_seconds,
        )
        while not self._stopping.is_set():
            now = _utc_now().replace(second=0, microsecond=0)
            await self._tick(now)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                continue
        log.info("lifecycle-runner-stopped")

    async def _tick(self, now: datetime) -> None:
        for job in self.jobs:
            try:
                if not self._should_fire(job, now):
                    continue
            except Exception:
                log.exception("lifecycle-runner-trigger-eval-failed job=%s", job.name)
                continue
            self._last_fired[job.name] = now
            log.info("lifecycle-job-dispatch name=%s at=%s", job.name, now.isoformat())
            task = asyncio.create_task(self._dispatch(job), name=f"lifecycle-job-{job.name}")
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

    def _should_fire(self, job: Job, now: datetime) -> bool:
        """True if ``job`` should fire at ``now`` and hasn't already fired this minute."""
        last = self._last_fired.get(job.name)
        if last == now:
            return False  # dedupe: already fired at this minute
        if job.trigger_kind == "cron":
            return _cron_matches(job.trigger_kwargs, now)
        if job.trigger_kind == "interval":
            return _interval_due(job.trigger_kwargs, last, now)
        raise ValueError(f"unsupported trigger_kind: {job.trigger_kind!r}")

    async def _dispatch(self, job: Job) -> None:
        """Run a Job's func in a worker thread; log any crash."""
        try:
            await asyncio.to_thread(job.func)
        except Exception:
            log.exception("lifecycle-job-crashed name=%s", job.name)


def _utc_now() -> datetime:
    """Current UTC time as naive ``datetime`` (matches APScheduler semantics)."""
    return datetime.now(UTC).replace(tzinfo=None)


def build_lifecycle_jobs(
    *,
    maturation_jobs: Iterable[Job] | None = None,
    demotion_jobs: Iterable[Job] | None = None,
    synthesis_jobs: Iterable[Job] | None = None,
    promotion_jobs: Iterable[Job] | None = None,
) -> list[Job]:
    """Compose the full job list the production worker drives.

    Real job builders replace the placeholder-lambdas emitted by
    :func:`build_default_jobs` for every name they cover. Any job
    name the builders haven't claimed keeps the placeholder (which
    log-skips). Reflection / vault_reconcile still use placeholders
    until their builder slices land.

    Injection points keep unit tests deterministic — a test can
    pass a stub Job without wiring a QdrantClient / Ollama / etc.
    """
    from musubi.lifecycle.scheduler import build_default_jobs

    real_jobs: list[Job] = []
    for group in (maturation_jobs, demotion_jobs, synthesis_jobs, promotion_jobs):
        if group is not None:
            real_jobs.extend(group)

    real_names = {j.name for j in real_jobs}
    placeholders = [j for j in build_default_jobs() if j.name not in real_names]
    return real_jobs + placeholders


class _TEICompositeEmbedder:
    """Embedder Protocol impl backed by three TEI clients.

    Duplicated from :mod:`musubi.api.bootstrap` deliberately — the
    lifecycle worker boots without touching the API dependency tree,
    and pulling `api/` into this module would break the
    "lifecycle doesn't import api" layer rule. The class itself is
    20 lines of glue; promote to a shared home only when a third
    caller needs it.
    """

    def __init__(self, *, dense: Any, sparse: Any, reranker: Any) -> None:
        self._dense = dense
        self._sparse = sparse
        self._reranker = reranker

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return await self._dense.embed_dense(texts)  # type: ignore[no-any-return]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return await self._sparse.embed_sparse(texts)  # type: ignore[no-any-return]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return await self._reranker.rerank(query, candidates)  # type: ignore[no-any-return]


async def _main_async() -> None:
    """Module entrypoint — compose real deps and run until signal."""
    from pathlib import Path

    from qdrant_client import QdrantClient

    from musubi.config import get_settings
    from musubi.embedding.tei import TEIDenseClient, TEIRerankerClient, TEISparseClient
    from musubi.lifecycle.demotion import DemotionDeps, build_demotion_jobs
    from musubi.lifecycle.emitters import ThoughtsPlaneEmitter
    from musubi.lifecycle.events import LifecycleEventSink
    from musubi.lifecycle.maturation import (
        MaturationCursor,
        build_maturation_jobs,
        default_ollama_client,
    )
    from musubi.lifecycle.promotion import PromotionDeps, build_promotion_jobs
    from musubi.lifecycle.synthesis import (
        SynthesisCursor,
        SynthesisOllamaClient,
        build_synthesis_jobs,
    )
    from musubi.llm.promotion_client import HttpxPromotionClient
    from musubi.planes.concept.plane import ConceptPlane
    from musubi.planes.curated.plane import CuratedPlane
    from musubi.planes.episodic.plane import EpisodicPlane
    from musubi.planes.thoughts.plane import ThoughtsPlane
    from musubi.vault.writelog import WriteLog
    from musubi.vault.writer import VaultWriter as _VaultWriter

    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":%(message)r}',
    )
    settings = get_settings()

    qdrant = QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key.get_secret_value(),
        https=not settings.musubi_allow_plaintext,
    )
    sink = LifecycleEventSink(db_path=settings.lifecycle_sqlite_path)
    cursor = MaturationCursor(db_path=settings.lifecycle_sqlite_path)
    synth_cursor = SynthesisCursor(db_path=settings.lifecycle_sqlite_path)
    # One HttpxOllamaClient satisfies both the maturation + synthesis
    # Protocols — see src/musubi/llm/ollama.py.
    ollama = default_ollama_client()

    embedder = _TEICompositeEmbedder(
        dense=TEIDenseClient(base_url=str(settings.tei_dense_url)),
        sparse=TEISparseClient(base_url=str(settings.tei_sparse_url)),
        reranker=TEIRerankerClient(base_url=str(settings.tei_reranker_url)),
    )

    episodic_plane = EpisodicPlane(client=qdrant, embedder=embedder)
    concept_plane = ConceptPlane(client=qdrant, embedder=embedder)
    thoughts_plane = ThoughtsPlane(client=qdrant, embedder=embedder)
    thought_emitter = ThoughtsPlaneEmitter(
        thoughts=thoughts_plane,
        from_presence="lifecycle-worker",
    )

    lock_dir = Path(settings.lifecycle_sqlite_path).parent / "locks"

    mat_jobs = build_maturation_jobs(
        client=qdrant,
        sink=sink,
        ollama=ollama,
        cursor=cursor,
        lock_dir=lock_dir,
    )
    dem_jobs = build_demotion_jobs(
        deps=DemotionDeps(
            qdrant=qdrant,
            episodic_plane=episodic_plane,
            concept_plane=concept_plane,
            events=sink,
            thoughts=thought_emitter,
        ),
        lock_dir=lock_dir,
    )
    # HttpxOllamaClient satisfies both OllamaClient (maturation) and
    # SynthesisOllamaClient structurally — cast for the stricter Protocol.
    syn_jobs = build_synthesis_jobs(
        client=qdrant,
        sink=sink,
        ollama=cast(SynthesisOllamaClient, ollama),
        embedder=embedder,
        cursor=synth_cursor,
        lock_dir=lock_dir,
    )

    # Promotion needs the real VaultWriter + a first-party HttpxPromotionClient.
    # We reuse the lifecycle sqlite dir as the write-log home so the vault
    # watcher (if running in the same deploy) shares the echo-filter state.
    write_log_db = Path(settings.lifecycle_sqlite_path).parent / "vault-writelog.db"
    vault_writer = _VaultWriter(
        vault_root=settings.vault_path,
        write_log=WriteLog(db_path=write_log_db),
    )
    promotion_llm = HttpxPromotionClient(
        base_url=str(settings.ollama_url),
        model=settings.llm_model,
    )
    curated_plane = CuratedPlane(client=qdrant, embedder=embedder)
    prom_jobs = build_promotion_jobs(
        deps=PromotionDeps(
            qdrant=qdrant,
            concept_plane=concept_plane,
            curated_plane=curated_plane,
            events=sink,
            llm=promotion_llm,
            vault_writer=vault_writer,
            thoughts=thought_emitter,
        ),
        lock_dir=lock_dir,
    )
    jobs = build_lifecycle_jobs(
        maturation_jobs=mat_jobs,
        demotion_jobs=dem_jobs,
        synthesis_jobs=syn_jobs,
        promotion_jobs=prom_jobs,
    )

    runner = LifecycleRunner(jobs=jobs)
    runner.install_signal_handlers()
    await runner.run()


def main() -> None:
    """Synchronous entrypoint. ``python -m musubi.lifecycle.runner``."""
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()


__all__ = [
    "LifecycleRunner",
    "build_lifecycle_jobs",
    "main",
]
