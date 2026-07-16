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
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from musubi.lifecycle.scheduler import Job
from musubi.observability.registry import default_registry


class _AlertEmitter(Protocol):
    async def emit(self, channel: str, content: str, title: str | None = None) -> None: ...


_REG = default_registry()
_DURATION = _REG.histogram(
    "musubi_lifecycle_job_duration_seconds",
    "lifecycle worker tick duration",
    buckets=(0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
    labelnames=("job",),
)
_ERRORS = _REG.counter(
    "musubi_lifecycle_job_errors_total",
    "lifecycle worker tick errors",
    labelnames=("job",),
)
_ALERT_ERRORS = _REG.counter(
    "musubi_lifecycle_job_alert_errors_total",
    "lifecycle worker tick alert emission errors",
    labelnames=("job",),
)

log = logging.getLogger(__name__)

_DAYS_OF_WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_COORDINATOR_READY = default_registry().gauge(
    "musubi_lifecycle_coordinator_ready",
    "1 when lifecycle shared storage is open and reconciliation can safely participate.",
)


class _ReconcileDriver:
    """Worker-only reconciliation, cleanup, and bounded readiness state."""

    def __init__(
        self,
        *,
        reconcile: Callable[[], object],
        ready: Callable[[], bool],
        cleanup: Callable[[float], object],
        max_failures: int,
    ) -> None:
        self._reconcile = reconcile
        self._ready = ready
        self._cleanup = cleanup
        self._max_failures = max_failures
        self._failures = 0
        _COORDINATOR_READY.set(0)

    def run(self) -> None:
        try:
            if not self._ready():
                raise RuntimeError("lifecycle coordinator is not ready to reconcile")
            self._reconcile()
            self._cleanup(datetime.now(UTC).timestamp())
        except Exception:
            self._failures += 1
            if self._failures >= self._max_failures:
                _COORDINATOR_READY.set(0)
            raise
        self._failures = 0
        _COORDINATOR_READY.set(1)


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
    thought_emitter: _AlertEmitter | None = None
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
            now = _utc_now()
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
        if job.trigger_kind == "cron":
            minute = now.replace(second=0, microsecond=0)
            if last is not None and last.replace(second=0, microsecond=0) == minute:
                return False
            return _cron_matches(job.trigger_kwargs, now)
        if job.trigger_kind == "interval":
            return _interval_due(job.trigger_kwargs, last, now)
        raise ValueError(f"unsupported trigger_kind: {job.trigger_kind!r}")

    async def _dispatch(self, job: Job) -> None:
        """Run a Job's func in a worker thread; log any crash.

        Each job execution gets its own span (`lifecycle.job.<name>`)
        so cron-driven work shows up as a top-level trace in Tempo.
        Without a parent span the per-job httpx/qdrant/embedding calls
        scatter as orphan spans; one wrapping span ties them together.
        """
        from opentelemetry.trace import Status, StatusCode

        from musubi.observability import get_tracer

        tracer = get_tracer("musubi.lifecycle.runner")
        with tracer.start_as_current_span(f"lifecycle.job.{job.name}") as span:
            span.set_attribute("lifecycle.job.name", job.name)
            span.set_attribute("lifecycle.job.trigger_kind", job.trigger_kind)
            start = time.monotonic()
            try:
                await asyncio.to_thread(job.func)
            except Exception as exc:
                _ERRORS.labels(job=job.name).inc()
                log.exception("lifecycle-job-crashed name=%s", job.name)

                if self.thought_emitter is not None:
                    try:
                        safe_content = _bounded_job_failure_alert(
                            job_name=job.name,
                            exc=exc,
                            span=span,
                        )
                        await asyncio.wait_for(
                            self.thought_emitter.emit(
                                channel="ops-alerts",
                                content=safe_content,
                                title="Lifecycle Job Failure",
                            ),
                            timeout=5.0,
                        )
                    except Exception as alert_exc:
                        _ALERT_ERRORS.labels(job=job.name).inc()
                        log.error(
                            "lifecycle-job-alert-failed name=%s error=%r", job.name, alert_exc
                        )

                if span.is_recording():
                    span.set_attribute("lifecycle.job.crashed", True)
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
            finally:
                _DURATION.labels(job=job.name).observe(time.monotonic() - start)


def _trace_id_hex(span: Any) -> str:
    """Extract a hex trace_id from an OTel span, or a stable placeholder.

    Must never raise and must never include exception / PII content — alerts
    are operator-facing Thoughts that land in the Thought plane.
    """
    try:
        ctx = span.get_span_context()
        trace_id = getattr(ctx, "trace_id", 0)
        if isinstance(trace_id, int) and trace_id != 0:
            return format(trace_id, "032x")
    except Exception:
        pass
    return "unavailable"


def _bounded_job_failure_alert(*, job_name: str, exc: BaseException, span: Any) -> str:
    """Bounded, non-secret ops-alert body for a crashed lifecycle job.

    Includes job name, exception *class* (not ``str(exc)``), a UTC timestamp,
    and the current span's ``trace_id`` so operators can distinguish repeats
    and jump from the Thought into logs/traces.
    """
    occurred = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"Job '{job_name}' crashed with {type(exc).__name__} "
        f"at {occurred} trace_id={_trace_id_hex(span)}. See logs for details."
    )


def _utc_now() -> datetime:
    """Current UTC time as naive ``datetime`` (matches APScheduler semantics)."""
    return datetime.now(UTC).replace(tzinfo=None)


def build_lifecycle_jobs(
    *,
    maturation_jobs: Iterable[Job] | None = None,
    demotion_jobs: Iterable[Job] | None = None,
    synthesis_jobs: Iterable[Job] | None = None,
    promotion_jobs: Iterable[Job] | None = None,
    reflection_jobs: Iterable[Job] | None = None,
    vault_reconcile_jobs: Iterable[Job] | None = None,
    reconcile_jobs: Iterable[Job] | None = None,
) -> list[Job]:
    """Compose the full job list the production worker drives.

    Real job builders replace the placeholder-lambdas emitted by
    :func:`build_default_jobs` for every name they cover. Any job
    name the builders haven't claimed keeps the placeholder (which
    log-skips). Post-musubi#345 every job has a real builder; the
    placeholder fallback only fires for tests that intentionally
    omit a group.

    Injection points keep unit tests deterministic — a test can
    pass a stub Job without wiring a QdrantClient / Ollama / etc.
    """
    from musubi.lifecycle.scheduler import build_default_jobs

    real_jobs: list[Job] = []
    for group in (
        maturation_jobs,
        demotion_jobs,
        synthesis_jobs,
        promotion_jobs,
        reflection_jobs,
        vault_reconcile_jobs,
        reconcile_jobs,
    ):
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


def register_boot_intent_handlers(
    coordinator: Any, *, qdrant: Any, embedder: Any, blob_root: Any
) -> tuple[Any, Any]:
    """Construct the write-plane immutable-vector publishers and register the FULL set of custom-intent
    handlers on ``coordinator`` — the ONE collection-aware dispatcher for the shared
    ``immutable_vector_publish`` kind (episodic + curated), plus the ART-001 artifact indexer.

    Extracted so the boot sequence is a testable unit (DATA-001 P2): ``_main_async`` calls this BEFORE
    the synchronous boot reconcile, so a durable PENDING intent left by a prior crash is dispatched on
    the first reconcile instead of driven with no handler. Returns ``(episodic_publisher,
    curated_publisher)`` for injection into the write planes."""
    from musubi.planes.artifact.indexer import ArtifactIndexer
    from musubi.store.immutable_vectors import (
        ImmutableVectorPublisher,
        register_immutable_vector_dispatch,
    )
    from musubi.store.names import collection_for_plane

    ep_collection = collection_for_plane("episodic")
    cur_collection = collection_for_plane("curated")
    episodic_publisher = ImmutableVectorPublisher(
        client=qdrant, embedder=embedder, collection=ep_collection
    )
    curated_publisher = ImmutableVectorPublisher(
        client=qdrant, embedder=embedder, collection=cur_collection
    )
    register_immutable_vector_dispatch(
        coordinator,
        {ep_collection: episodic_publisher, cur_collection: curated_publisher},
    )
    ArtifactIndexer(client=qdrant, embedder=embedder, blob_root=blob_root).register(coordinator)
    return episodic_publisher, curated_publisher


async def _main_async() -> None:
    """Module entrypoint — compose real deps and run until signal."""
    from pathlib import Path

    from musubi.config import get_settings
    from musubi.embedding.chunked import ChunkedEmbedder
    from musubi.embedding.tei import TEIDenseClient, TEIRerankerClient, TEISparseClient
    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
    from musubi.lifecycle.demotion import DemotionDeps, build_demotion_jobs
    from musubi.lifecycle.emitters import (
        ReflectionThoughtsEmitter,
        ReflectionVaultWriter,
        ThoughtsPlaneEmitter,
    )
    from musubi.lifecycle.events import LifecycleEventSink
    from musubi.lifecycle.maturation import (
        MaturationCursor,
        build_maturation_jobs,
        default_ollama_client,
    )
    from musubi.lifecycle.promotion import PromotionDeps, build_promotion_jobs
    from musubi.lifecycle.reflection import build_reflection_jobs
    from musubi.lifecycle.synthesis import (
        SynthesisCursor,
        SynthesisOllamaClient,
        build_synthesis_jobs,
    )
    from musubi.llm.promotion_client import HttpxPromotionClient
    from musubi.llm.reflection_client import HttpxReflectionClient
    from musubi.observability import configure_logging, init_tracing
    from musubi.planes.artifact.plane import ArtifactPlane
    from musubi.planes.concept.plane import ConceptPlane
    from musubi.planes.curated.plane import CuratedPlane
    from musubi.planes.episodic.plane import EpisodicPlane
    from musubi.planes.thoughts.plane import ThoughtsPlane
    from musubi.storage import build_qdrant_client
    from musubi.vault.reconciler import build_vault_reconcile_jobs
    from musubi.vault.writelog import WriteLog
    from musubi.vault.writer import VaultWriter as _VaultWriter

    # Same structured-JSON logging the API uses, so log shippers see one
    # format across both containers (lifecycle-worker shares the
    # musubi-core image but runs `python -m musubi.lifecycle.runner`).
    configure_logging()
    settings = get_settings()

    # Initialize OTel tracing. Mirrors api/app.py but pins
    # `service.name=lifecycle-worker` so spans land under their own
    # service in Tempo instead of being attributed to musubi-core. The
    # exporter, version, host, and environment come from the shared
    # settings (same env file as core).
    init_tracing(
        endpoint=settings.otel_exporter_otlp_endpoint or None,
        service_name="lifecycle-worker",
        service_namespace=settings.otel_service_namespace,
        host_name=settings.otel_host_name or None,
        service_version=settings.musubi_service_version or None,
        deployment_environment=settings.otel_deployment_environment,
    )

    # Start `/metrics` HTTP exposition. The four lifecycle jobs
    # (maturation, synthesis, promotion, reflection) register
    # `musubi_lifecycle_job_duration_seconds` + `_errors_total` against
    # `default_registry()` and increment per tick — without this server,
    # those increments never reach Prometheus and operators cannot answer
    # "did synthesis run? how long? did it error?" from the metrics
    # layer. See musubi#344.
    from musubi.observability.scrape_server import start_metrics_server

    start_metrics_server(settings.lifecycle_metrics_port)

    qdrant = build_qdrant_client(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key.get_secret_value(),
        https=not settings.musubi_allow_plaintext,
    )
    coordinator = LifecycleTransitionCoordinator(
        client=qdrant,
        db_path=settings.lifecycle_sqlite_path,
        pending_cap=settings.lifecycle_pending_cap,
        lease_ttl=settings.lifecycle_lease_ttl_s,
        backoff_base_s=settings.lifecycle_backoff_base_s,
        backoff_max_s=settings.lifecycle_backoff_max_s,
        busy_timeout_ms=settings.lifecycle_sqlite_busy_timeout_ms,
    )

    # Wrap the composite in ChunkedEmbedder so sparse inputs > 510 tokens
    # are sliding-window-chunked + max-pooled before they hit tei-sparse
    # (SPLADE-v3 has a hard 512-token model cap). The API server's
    # bootstrap (src/musubi/api/bootstrap.py) wraps the same way; without
    # this, vault_reconcile / synthesis / any lifecycle-side embed of a
    # long input HTTP 413s. See musubi#367.
    embedder = ChunkedEmbedder(
        _TEICompositeEmbedder(
            dense=TEIDenseClient(base_url=str(settings.tei_dense_url)),
            sparse=TEISparseClient(base_url=str(settings.tei_sparse_url)),
            reranker=TEIRerankerClient(base_url=str(settings.tei_reranker_url)),
        )
    )

    # DATA-001 P2: register ALL custom-intent handlers BEFORE the synchronous boot reconcile below,
    # else a durable PENDING immutable/artifact intent left by a prior crash is first driven with no
    # handler (kept pending, not dispatched) until the next interval tick. The publishers are injected
    # into the write planes below.
    episodic_publisher, curated_publisher = register_boot_intent_handlers(
        coordinator, qdrant=qdrant, embedder=embedder, blob_root=settings.artifact_blob_path
    )

    reconcile_driver = _ReconcileDriver(
        reconcile=coordinator.reconcile_once,
        ready=coordinator.readiness_check,
        cleanup=lambda now_epoch: coordinator.cleanup_terminal(
            cutoff_epoch=now_epoch - settings.lifecycle_cleanup_retention_s,
            batch_limit=settings.lifecycle_cleanup_batch,
        ),
        max_failures=settings.lifecycle_readiness_max_reconcile_failures,
    )
    # A synchronous boot pass proves the shared schema and reconciler before
    # readiness can rise (handlers are registered above so it can dispatch a
    # durable pending intent). The interval job below is the only ongoing reconciler.
    reconcile_driver.run()
    sink = LifecycleEventSink(
        db_path=settings.lifecycle_sqlite_path,
        busy_timeout_ms=settings.lifecycle_sqlite_busy_timeout_ms,
    )
    cursor = MaturationCursor(
        db_path=settings.lifecycle_sqlite_path,
        busy_timeout_ms=settings.lifecycle_sqlite_busy_timeout_ms,
    )
    synth_cursor = SynthesisCursor(
        db_path=settings.lifecycle_sqlite_path,
        busy_timeout_ms=settings.lifecycle_sqlite_busy_timeout_ms,
    )
    # One HttpxOllamaClient satisfies both the maturation + synthesis
    # Protocols — see src/musubi/llm/ollama.py.
    ollama = default_ollama_client()

    # DATA-001 P2: inject the coordinator + the exact-collection publisher into the WRITE planes so
    # vector-capable mutations (episodic reinforce, curated same-id body update) publish through the
    # fenced immutable-vector seam instead of the retired unfenceable update_vectors.
    episodic_plane = EpisodicPlane(
        client=qdrant,
        embedder=embedder,
        coordinator=coordinator,
        vector_publisher=episodic_publisher,
    )
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
        coordinator=coordinator,
        ollama=ollama,
        cursor=cursor,
        lock_dir=lock_dir,
        embedder=embedder,
    )
    # C4/ART-001: the committed-generation indexer's 'artifact_index' handler is now registered ABOVE,
    # before the boot reconcile (DATA-001 P2 ordering fix), alongside the immutable-vector dispatcher.
    artifact_plane = ArtifactPlane(client=qdrant, embedder=embedder)
    dem_jobs = build_demotion_jobs(
        deps=DemotionDeps(
            qdrant=qdrant,
            coordinator=coordinator,
            episodic_plane=episodic_plane,
            concept_plane=concept_plane,
            events=sink,
            thoughts=thought_emitter,
            artifact_plane=artifact_plane,
            artifact_archival_enabled=settings.musubi_artifact_archival_enabled,
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
    curated_plane = CuratedPlane(
        client=qdrant,
        embedder=embedder,
        coordinator=coordinator,
        vector_publisher=curated_publisher,
    )
    prom_jobs = build_promotion_jobs(
        deps=PromotionDeps(
            qdrant=qdrant,
            coordinator=coordinator,
            concept_plane=concept_plane,
            curated_plane=curated_plane,
            events=sink,
            llm=promotion_llm,
            vault_writer=vault_writer,
            thoughts=thought_emitter,
        ),
        lock_dir=lock_dir,
    )
    # Reflection needs its own shaped adapters (async `write_reflection`,
    # kw-only `emit`) — see `lifecycle/emitters.py`.
    reflection_vault = ReflectionVaultWriter(
        vault_root=settings.vault_path,
        write_log=vault_writer.write_log,
    )
    reflection_thoughts = ReflectionThoughtsEmitter(
        thoughts=thoughts_plane,
        from_presence="lifecycle-worker",
    )
    reflection_llm = HttpxReflectionClient(
        base_url=str(settings.ollama_url),
        model=settings.llm_model,
    )
    # Homelab default: one reflection namespace for the whole deploy.
    # A future namespace-discovery pass (like synthesis) can per-namespace
    # this when we grow multiple presences.
    reflection_namespace = "lifecycle-worker/ops/curated"
    ref_jobs = build_reflection_jobs(
        qdrant=qdrant,
        sink=sink,
        curated_plane=curated_plane,
        vault=reflection_vault,
        thoughts=reflection_thoughts,
        llm=reflection_llm,
        namespace=reflection_namespace,
        lock_dir=lock_dir,
    )
    # vault_reconcile — periodic drift catch-up between the Obsidian
    # vault filesystem and the curated plane. Real-time changes go
    # through the (still-unbuilt) musubi-vault-watcher process; this
    # 6h sweep catches anything the watcher missed (or runs in lieu of
    # the watcher entirely until it's deployed). See musubi#345.
    vault_reconcile_jobs = build_vault_reconcile_jobs(
        vault_root=Path(settings.vault_path),
        curated_plane=curated_plane,
        lock_dir=lock_dir,
        coordinator=coordinator,
    )
    jobs = build_lifecycle_jobs(
        maturation_jobs=mat_jobs,
        demotion_jobs=dem_jobs,
        synthesis_jobs=syn_jobs,
        promotion_jobs=prom_jobs,
        reflection_jobs=ref_jobs,
        vault_reconcile_jobs=vault_reconcile_jobs,
        reconcile_jobs=[
            Job(
                name="lifecycle_reconcile",
                trigger_kind="interval",
                trigger_kwargs={"seconds": settings.lifecycle_reconcile_interval_s},
                func=reconcile_driver.run,
                grace_time_s=settings.lifecycle_reconcile_interval_s,
            )
        ],
    )

    # LIFE-006: wire the same Thought emitter demotion/promotion use into the
    # runner so crashed ticks emit a durable ops-alerts Thought in production
    # (not only when tests inject a fake emitter).
    runner = LifecycleRunner(
        jobs=jobs,
        tick_seconds=min(60, settings.lifecycle_reconcile_interval_s),
        thought_emitter=thought_emitter,
    )
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
