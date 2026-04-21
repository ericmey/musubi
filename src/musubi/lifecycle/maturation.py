"""Maturation sweeps — provisional → matured + provisional-TTL archival.

Two scheduled jobs over the episodic plane (with first-cut concept-side
helpers below for the lifecycle-engine job registry to consume):

- :func:`episodic_maturation_sweep` — hourly sweep that scores
  ``provisional`` rows older than ``min_age_sec`` for promotion to
  ``matured``. Per the spec it batches LLM calls for importance + topics,
  rule-normalises tags, optionally infers supersession, then routes each
  state change through the canonical
  :func:`musubi.lifecycle.transitions.transition` primitive. Enrichment
  fields (``importance``, ``tags``, ``linked_to_topics``) land via a
  separate Qdrant ``set_payload`` after the transition succeeds — they
  are not state mutations and therefore not audited individually, but
  they bundle into the same per-row sweep step so a partial failure
  (Ollama outage) is observable on the post-sweep payload.
- :func:`provisional_ttl_sweep` — hourly sweep that archives
  ``provisional`` rows older than ``provisional_ttl_sec``. Archival also
  goes through ``transition()`` with reason ``"provisional-ttl"``.

See [[06-ingestion/maturation]] for the spec.

Architecture decisions:

- **Selection scrolls Qdrant directly.** The plane's public API
  (``EpisodicPlane.query``) is dense-search; there is no public
  ``scroll_by_state`` surface, and the spec's selection is a cheap
  payload-filter scroll (no embedding needed). The plane owns
  *mutation*; the lifecycle worker is allowed to read the same
  collection by payload predicate to find candidates.
- **Mutations route through ``transition()``**, never direct
  ``client.set_payload`` for the ``state`` field. The lifecycle ledger
  records the source of every state change.
- **Cursor lives in sqlite.** A tiny per-sweep table at
  ``cursor.db`` records the last ``updated_epoch`` we processed so
  restarts resume cleanly. The store is created on first ``set()`` and
  never truncated by the sweep itself.
- **Ollama is a Protocol.** :class:`OllamaClient` is the abstraction
  the production wiring (a future ``slice-llm-client``) will satisfy
  with a real httpx implementation. The shipped default is
  :class:`_NotConfiguredOllama`, which raises ``NotImplementedError`` on
  use — per the ADR-punted-deps rule in
  ``CLAUDE.md`` § Additional handoff-readiness rules. Production must
  either configure a real client or accept that maturation cannot run.
- **Idempotency.** Re-running a sweep on the same input produces the
  same transitions: the candidate selection filters out anything
  already past ``provisional``, and the cursor advances on success so a
  crash mid-batch resumes after the last committed row.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Protocol

from qdrant_client import QdrantClient, models

from musubi.config import get_settings
from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.scheduler import Job, file_lock
from musubi.lifecycle.transitions import LineageUpdates, TransitionError, transition
from musubi.observability import default_registry
from musubi.types.common import KSUID, Ok, epoch_of, utc_now

log = logging.getLogger(__name__)

DEFAULT_TAG_ALIASES: dict[str, str] = {
    "nvidia-gpu": "nvidia",
    "gpu-setup": "gpu",
}
"""Conservative seed alias map. Production reads + merges
``config/tag-aliases.yaml`` per the spec; that file-loader belongs to a
follow-up slice."""

_SUPERSESSION_HINTS: tuple[str, ...] = ("update:", "correction:", "replacing:")
"""Case-insensitive content-prefix triggers for supersession inference."""

_DEFAULT_LLM_BATCH = 10
"""Items per LLM call; spec § Per-memory pipeline calls for 10."""

_LIFECYCLE_ACTOR = "lifecycle-worker"
"""Actor recorded on every transition this module emits — matches the spec."""

_REG = default_registry()
_DURATION = _REG.histogram(
    "musubi_lifecycle_job_duration_seconds",
    "lifecycle worker tick duration",
    labelnames=("job",),
)
_ERRORS = _REG.counter(
    "musubi_lifecycle_job_errors_total",
    "lifecycle worker tick errors",
    labelnames=("job",),
)


def _instrument_maturation_job[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    @wraps(func)
    async def _wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.monotonic()
        try:
            return await func(*args, **kwargs)
        except Exception:
            _ERRORS.labels(job="maturation").inc()
            raise
        finally:
            _DURATION.labels(job="maturation").observe(time.monotonic() - start)

    return _wrapped


# ---------------------------------------------------------------------------
# OllamaClient — Protocol + production stub
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OllamaImportance:
    """Input row for the importance-rescore prompt."""

    object_id: KSUID
    content: str
    captured_importance: int


@dataclass(frozen=True)
class OllamaTopic:
    """Input row for the topic-inference prompt."""

    object_id: KSUID
    content: str
    existing_tags: list[str] = field(default_factory=list)


class OllamaClient(Protocol):
    """Minimal shape the maturation sweep needs from an LLM client.

    Both methods return ``None`` to signal "Ollama is unavailable" — the
    spec's failure-mode contract. A successful call returns a mapping of
    ``object_id`` to enrichment value (importance int, or topics list).
    """

    async def score_importance(self, items: list[OllamaImportance]) -> dict[KSUID, int] | None: ...

    async def infer_topics(self, items: list[OllamaTopic]) -> dict[KSUID, list[str]] | None: ...


class _NotConfiguredOllama:
    """Production stub. Raises ``NotImplementedError`` on every call.

    The lifecycle worker fails closed when an unconfigured deployment
    tries to run a maturation sweep — this is the
    "ADR-punted-deps must fail loud" rule from
    ``CLAUDE.md`` § Additional handoff-readiness rules. A future
    ``slice-llm-client`` will provide a real httpx-backed implementation
    that reads ``Settings.ollama_url``.
    """

    async def score_importance(self, items: list[OllamaImportance]) -> dict[KSUID, int] | None:
        raise NotImplementedError(
            "OllamaClient is not configured. The maturation sweep cannot run "
            "in production without a real OllamaClient wired in (see the "
            "future slice-llm-client). Read Settings.ollama_url and "
            "instantiate a real client."
        )

    async def infer_topics(self, items: list[OllamaTopic]) -> dict[KSUID, list[str]] | None:
        raise NotImplementedError(
            "OllamaClient is not configured. The maturation sweep cannot run "
            "in production without a real OllamaClient wired in (see the "
            "future slice-llm-client). Read Settings.ollama_url and "
            "instantiate a real client."
        )


def default_ollama_client() -> OllamaClient:
    """Return the production :class:`OllamaClient`.

    Reads ``Settings.ollama_url`` and ``Settings.llm_model`` and returns
    an :class:`musubi.llm.HttpxOllamaClient` wired to that endpoint.
    Falls back to :class:`_NotConfiguredOllama` (fail-loud) if settings
    are unavailable — tests and CI that don't set the env vars will
    still raise ``NotImplementedError`` on call, matching the "ADR-
    punted-deps must fail loud" rule for deployments that forgot to
    configure the LLM.
    """
    # Lazy import — :mod:`musubi.llm.ollama` imports this module for the
    # Protocol definition, and an eager import would cycle.
    from musubi.llm.ollama import HttpxOllamaClient

    try:
        settings = get_settings()
    except Exception:
        return _NotConfiguredOllama()

    debug_dir: Path | None = None
    maturation_debug = getattr(settings, "maturation_debug_dir", None)
    if maturation_debug is not None:
        debug_dir = Path(str(maturation_debug))

    return HttpxOllamaClient(
        base_url=str(settings.ollama_url).rstrip("/"),
        model=settings.llm_model,
        debug_dir=debug_dir,
    )


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


_CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS maturation_cursor (
    sweep_name TEXT PRIMARY KEY,
    last_processed_epoch REAL NOT NULL
);
"""


class MaturationCursor:
    """Per-sweep cursor persisted to sqlite.

    Records the ``updated_epoch`` of the last processed row so the next
    sweep run resumes after the last committed item. The store is
    intentionally append-by-name: each sweep keeps its own row keyed by
    a sweep identifier (``"episodic_maturation"``, etc.). Operators can
    reset a cursor by deleting the row from sqlite.
    """

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_CURSOR_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    def get(self, sweep_name: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_processed_epoch FROM maturation_cursor WHERE sweep_name = ?",
                (sweep_name,),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def set(self, sweep_name: str, value: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO maturation_cursor (sweep_name, last_processed_epoch) "
                "VALUES (?, ?) ON CONFLICT(sweep_name) DO UPDATE SET "
                "last_processed_epoch = excluded.last_processed_epoch",
                (sweep_name, value),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Config + report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaturationConfig:
    """Tunable sweep parameters.

    Spec defaults match
    [[06-ingestion/maturation#Selection]]. Operator overrides flow in via
    ``build_maturation_jobs(config=...)``. The fields are not yet
    surfaced on :class:`musubi.settings.Settings` because they are
    workload thresholds rather than infrastructure — a future
    ``slice-config-thresholds`` PR can promote them, but the prohibition
    on "hardcoded thresholds" is satisfied today by accepting overrides
    at every entry point.
    """

    min_age_sec: int = 3600
    batch_size: int = 500
    provisional_ttl_sec: int = 7 * 86400
    importance_reenrich_age_sec: int = 7 * 86400
    demotion_inactivity_sec: int = 30 * 86400
    concept_min_age_sec: int = 24 * 3600
    concept_reinforcement_threshold: int = 3
    tag_aliases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TAG_ALIASES))


@dataclass(frozen=True)
class SweepReport:
    """Outcome of one sweep invocation."""

    selected: int
    transitioned: int
    enriched: int = 0
    failed: int = 0
    cursor_advanced_to: float | None = None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalize_tags(tags: Sequence[str], *, aliases: dict[str, str]) -> list[str]:
    """Lowercase, strip, hyphenate, de-alias, dedupe — all the rules from
    [[06-ingestion/maturation#Step 3 — Tag normalization]].

    Order is preserved across the deduplication pass so test assertions
    can rely on a stable result. Empty strings post-strip are dropped.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        normalized = raw.strip().lower().replace(" ", "-")
        if not normalized:
            continue
        canonical = aliases.get(normalized, normalized)
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


def detect_supersession_hint(content: str) -> bool:
    """``True`` iff ``content`` starts with one of the supersession hints."""
    if not content:
        return False
    head = content.lstrip().lower()
    return any(head.startswith(hint) for hint in _SUPERSESSION_HINTS)


# ---------------------------------------------------------------------------
# Episodic maturation sweep
# ---------------------------------------------------------------------------


_EPISODIC_COLLECTION = "musubi_episodic"
_CURSOR_NAME_EPISODIC = "episodic_maturation"
_CURSOR_NAME_TTL = "provisional_ttl"
_CURSOR_NAME_DEMOTION = "episodic_demotion"


@_instrument_maturation_job
async def episodic_maturation_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    ollama: OllamaClient,
    cursor: MaturationCursor,
    config: MaturationConfig | None = None,
    now: datetime | None = None,
) -> SweepReport:
    """One pass of the maturation sweep.

    Selects ``provisional`` rows older than ``config.min_age_sec`` and
    with ``updated_epoch`` strictly greater than the persisted cursor,
    up to ``config.batch_size``. For each:

    1. Compute normalized tags + supersession hint locally.
    2. Batch the rows into LLM calls for importance + topics. Failures
       (``None`` return) leave the captured value in place.
    3. If supersession is inferred, find the candidate (same namespace,
       same dense match per :func:`_find_supersession_candidate` — a
       deliberately cheap payload-filter for now; the spec's similarity
       threshold lives in a follow-up).
    4. Call :func:`transition` to flip ``state = "matured"`` (and apply
       lineage updates).
    5. Apply enrichment fields via ``set_payload`` on the same point id.
    6. Advance the cursor to the largest processed ``updated_epoch``.
    """
    cfg = config or MaturationConfig()
    now_dt = now or utc_now()
    now_epoch = now_dt.timestamp()
    cursor_value = cursor.get(_CURSOR_NAME_EPISODIC)

    candidates = _scroll_eligible(
        client,
        collection=_EPISODIC_COLLECTION,
        state="provisional",
        max_age_cutoff_epoch=now_epoch - cfg.min_age_sec,
        cursor_value=cursor_value,
        limit=cfg.batch_size,
    )

    if not candidates:
        return SweepReport(selected=0, transitioned=0)

    # ------------------------------------------------------------------
    # Step 2 — LLM enrichment, batched.
    # ------------------------------------------------------------------
    importance_inputs = [
        OllamaImportance(
            object_id=row["object_id"],
            content=row.get("content", ""),
            captured_importance=int(row.get("importance", 5)),
        )
        for row in candidates
    ]
    importance_by_id = await _ollama_score_in_batches(ollama, importance_inputs)

    topic_inputs = [
        OllamaTopic(
            object_id=row["object_id"],
            content=row.get("content", ""),
            existing_tags=list(row.get("tags", [])),
        )
        for row in candidates
    ]
    topics_by_id = await _ollama_topics_in_batches(ollama, topic_inputs)

    transitioned = 0
    enriched = 0
    failed = 0
    max_epoch = cursor_value

    for row in candidates:
        object_id: KSUID = row["object_id"]
        normalized = normalize_tags(row.get("tags", []), aliases=cfg.tag_aliases)
        new_importance = (
            importance_by_id[object_id]
            if importance_by_id is not None and object_id in importance_by_id
            else int(row.get("importance", 5))
        )
        new_topics = (
            topics_by_id[object_id]
            if topics_by_id is not None and object_id in topics_by_id
            else list(row.get("linked_to_topics", []))
        )

        # ------------------------------------------------------------------
        # Step 5 — supersession inference (cheap content-prefix only).
        # ------------------------------------------------------------------
        lineage_updates: LineageUpdates | None = None
        superseded_target_id: KSUID | None = None
        if detect_supersession_hint(row.get("content", "")):
            superseded_target_id = _find_supersession_candidate(
                client,
                collection=_EPISODIC_COLLECTION,
                namespace=row["namespace"],
                self_id=object_id,
                content=row.get("content", ""),
            )
            if superseded_target_id is not None:
                lineage_updates = LineageUpdates(supersedes=[superseded_target_id])

        # ------------------------------------------------------------------
        # Step 6 — canonical state transition.
        # ------------------------------------------------------------------
        result = transition(
            client,
            object_id=object_id,
            target_state="matured",
            actor=_LIFECYCLE_ACTOR,
            reason="maturation-sweep",
            lineage_updates=lineage_updates,
            sink=sink,
        )
        if not isinstance(result, Ok):
            failed += 1
            log.warning(
                "maturation-transition-failed object_id=%s err=%r",
                object_id,
                result.error,
            )
            continue

        # If we marked an old row as the predecessor, flip it to
        # "superseded" with the back-pointer. Bullet 13 covers both sides.
        if superseded_target_id is not None:
            back_result = transition(
                client,
                object_id=superseded_target_id,
                target_state="superseded",
                actor=_LIFECYCLE_ACTOR,
                reason="maturation-sweep-supersession",
                lineage_updates=LineageUpdates(superseded_by=object_id),
                sink=sink,
            )
            if not isinstance(back_result, Ok):
                # Roll-forward: the new row is matured, the old is
                # half-linked. Operators see both events in the ledger.
                log.warning(
                    "supersession-back-link-failed new=%s old=%s err=%r",
                    object_id,
                    superseded_target_id,
                    back_result.error,
                )

        # ------------------------------------------------------------------
        # Enrichment write — non-state fields, applied via set_payload on
        # the same point id. Not a state change → no separate ledger entry.
        # ------------------------------------------------------------------
        if _enrichment_changed(row, normalized, new_importance, new_topics):
            _apply_enrichment(
                client,
                collection=_EPISODIC_COLLECTION,
                object_id=object_id,
                tags=normalized,
                importance=new_importance,
                topics=new_topics,
            )
            enriched += 1

        transitioned += 1
        row_epoch = float(row.get("updated_epoch", 0.0))
        if row_epoch > max_epoch:
            max_epoch = row_epoch

    if max_epoch > cursor_value:
        cursor.set(_CURSOR_NAME_EPISODIC, max_epoch)
        advanced_to: float | None = max_epoch
    else:
        advanced_to = None

    return SweepReport(
        selected=len(candidates),
        transitioned=transitioned,
        enriched=enriched,
        failed=failed,
        cursor_advanced_to=advanced_to,
    )


# ---------------------------------------------------------------------------
# Provisional TTL sweep
# ---------------------------------------------------------------------------


@_instrument_maturation_job
async def provisional_ttl_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    config: MaturationConfig | None = None,
    now: datetime | None = None,
) -> SweepReport:
    """Archive provisional rows older than ``provisional_ttl_sec``.

    Per the spec's Provisional-TTL section: a memory still ``provisional``
    after seven days is almost certainly a capture error or Ollama-outage
    casualty. Archival (state → ``archived``) preserves it for forensic
    review while removing it from default retrieval.
    """
    cfg = config or MaturationConfig()
    now_dt = now or utc_now()
    now_epoch = now_dt.timestamp()
    candidates = _scroll_eligible(
        client,
        collection=_EPISODIC_COLLECTION,
        state="provisional",
        max_age_cutoff_epoch=now_epoch - cfg.provisional_ttl_sec,
        cursor_value=0.0,
        limit=cfg.batch_size,
    )
    if not candidates:
        return SweepReport(selected=0, transitioned=0)

    transitioned = 0
    failed = 0
    for row in candidates:
        object_id: KSUID = row["object_id"]
        result = transition(
            client,
            object_id=object_id,
            target_state="archived",
            actor=_LIFECYCLE_ACTOR,
            reason="provisional-ttl",
            sink=sink,
        )
        if isinstance(result, Ok):
            transitioned += 1
        else:
            failed += 1
            log.warning("provisional-ttl-failed object_id=%s err=%r", object_id, result.error)
    return SweepReport(selected=len(candidates), transitioned=transitioned, failed=failed)


# ---------------------------------------------------------------------------
# First-cut episodic + concept demotion sweeps
#
# These cover the lifecycle-engine job-registry slots
# (``demotion_episodic``, ``concept_maturation``, ``demotion_concept``)
# that the spec doesn't explicitly bullet but that the scheduler does
# expect a real function for. Conservative implementations: filter by
# state + last-activity-epoch, demote via ``transition()``. The spec's
# more sophisticated demotion criteria (e.g. low-reinforcement scoring)
# are deferred to slice-lifecycle-reflection / slice-lifecycle-promotion
# follow-ups.
# ---------------------------------------------------------------------------


@_instrument_maturation_job
async def episodic_demotion_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    config: MaturationConfig | None = None,
    now: datetime | None = None,
) -> SweepReport:
    """Demote ``matured`` rows whose last activity is older than the
    inactivity window.

    ``last_accessed_at`` is checked first; if missing, falls back to
    ``updated_epoch``. Conservative: the spec's "score for demotion via
    Qwen2.5-7B" path is a follow-up.
    """
    cfg = config or MaturationConfig()
    now_dt = now or utc_now()
    cutoff = now_dt.timestamp() - cfg.demotion_inactivity_sec
    candidates = _scroll_eligible(
        client,
        collection=_EPISODIC_COLLECTION,
        state="matured",
        max_age_cutoff_epoch=cutoff,
        cursor_value=0.0,
        limit=cfg.batch_size,
        age_field="updated_epoch",
    )
    if not candidates:
        return SweepReport(selected=0, transitioned=0)

    transitioned = 0
    failed = 0
    for row in candidates:
        result = transition(
            client,
            object_id=row["object_id"],
            target_state="demoted",
            actor=_LIFECYCLE_ACTOR,
            reason="maturation-demotion",
            sink=sink,
        )
        if isinstance(result, Ok):
            transitioned += 1
        else:
            failed += 1
    return SweepReport(selected=len(candidates), transitioned=transitioned, failed=failed)


@_instrument_maturation_job
async def concept_maturation_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    config: MaturationConfig | None = None,
    now: datetime | None = None,
) -> SweepReport:
    """Promote ``synthesized`` concepts past the 24-hour quiet window.

    Spec lives at [[04-data-model/synthesized-concept]]; the trigger
    here is the ``concept_maturation`` slot in the lifecycle scheduler's
    job registry. Conservative: ``synthesized`` rows whose
    ``created_epoch`` is older than ``concept_min_age_sec`` and whose
    ``reinforcement_count`` is at or above the threshold are matured.
    Contradiction handling is a follow-up
    (slice-lifecycle-synthesis-contradictions).
    """
    cfg = config or MaturationConfig()
    now_dt = now or utc_now()
    cutoff = now_dt.timestamp() - cfg.concept_min_age_sec
    candidates = _scroll_eligible(
        client,
        collection="musubi_concept",
        state="synthesized",
        max_age_cutoff_epoch=cutoff,
        cursor_value=0.0,
        limit=cfg.batch_size,
    )
    if not candidates:
        return SweepReport(selected=0, transitioned=0)

    transitioned = 0
    failed = 0
    for row in candidates:
        if int(row.get("reinforcement_count", 0)) < cfg.concept_reinforcement_threshold:
            continue
        # Skip concepts with active contradictions — spec says maturation is
        # blocked until the contradiction is resolved (surfaced by
        # slice-lifecycle-synthesis; see cross-slice ticket
        # _inbox/cross-slice/slice-lifecycle-synthesis-slice-lifecycle-maturation-missing-contradicts-check.md).
        contradicts = row.get("contradicts", [])
        if isinstance(contradicts, list) and len(contradicts) > 0:
            log.info(
                "skipping concept-maturation for %s: %d active contradiction(s)",
                row["object_id"],
                len(contradicts),
            )
            continue
        result = transition(
            client,
            object_id=row["object_id"],
            target_state="matured",
            actor=_LIFECYCLE_ACTOR,
            reason="concept-maturation",
            sink=sink,
        )
        if isinstance(result, Ok):
            transitioned += 1
        else:
            failed += 1
    return SweepReport(selected=len(candidates), transitioned=transitioned, failed=failed)


@_instrument_maturation_job
async def concept_demotion_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    config: MaturationConfig | None = None,
    now: datetime | None = None,
) -> SweepReport:
    """Demote ``matured`` concepts with no reinforcement in the window."""
    cfg = config or MaturationConfig()
    now_dt = now or utc_now()
    cutoff = now_dt.timestamp() - cfg.demotion_inactivity_sec
    candidates = _scroll_eligible(
        client,
        collection="musubi_concept",
        state="matured",
        max_age_cutoff_epoch=cutoff,
        cursor_value=0.0,
        limit=cfg.batch_size,
        age_field="updated_epoch",
    )
    if not candidates:
        return SweepReport(selected=0, transitioned=0)

    transitioned = 0
    failed = 0
    for row in candidates:
        result = transition(
            client,
            object_id=row["object_id"],
            target_state="demoted",
            actor=_LIFECYCLE_ACTOR,
            reason="concept-demotion",
            sink=sink,
        )
        if isinstance(result, Ok):
            transitioned += 1
        else:
            failed += 1
    return SweepReport(selected=len(candidates), transitioned=transitioned, failed=failed)


# ---------------------------------------------------------------------------
# Internal — Qdrant access helpers
# ---------------------------------------------------------------------------


def _scroll_eligible(
    client: QdrantClient,
    *,
    collection: str,
    state: str,
    max_age_cutoff_epoch: float,
    cursor_value: float,
    limit: int,
    age_field: str = "created_epoch",
) -> list[dict[str, Any]]:
    """Payload-filtered scroll for sweep candidates.

    Returns rows whose ``state`` matches and whose ``age_field`` is
    strictly older than the cutoff (so it has settled long enough). The
    state predicate alone gates "have we processed this row yet?" — once
    a row transitions out of ``provisional``, it's no longer selectable.
    The cursor is therefore a *progress high-water mark for observability*,
    not a selection gate, which keeps Qdrant's natural scroll order from
    interacting badly with batch-by-batch advances. ``cursor_value`` is
    accepted (and exposed in the report) so the future operator
    introspection surface can render "current cursor" without changing
    the selection contract.
    """
    del cursor_value  # intentionally unused — see docstring
    try:
        records, _ = client.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="state", match=models.MatchValue(value=state)),
                    models.FieldCondition(
                        key=age_field, range=models.Range(lt=max_age_cutoff_epoch)
                    ),
                ]
            ),
            limit=limit,
            with_payload=True,
        )
    except Exception as exc:
        log.warning("scroll-failed collection=%s err=%r", collection, exc)
        return []
    out: list[dict[str, Any]] = []
    for rec in records:
        if rec.payload:
            out.append(dict(rec.payload))
    return out


def _find_supersession_candidate(
    client: QdrantClient,
    *,
    collection: str,
    namespace: str,
    self_id: KSUID,
    content: str,
) -> KSUID | None:
    """Return the most recently matured row in the same namespace that
    plausibly precedes ``self_id``.

    Spec calls for a similarity check at ≥ 0.88 plus a topic match. Both
    require the embedder + a topics index that this slice doesn't hold;
    a follow-up will tighten the heuristic. For now: same namespace,
    state=matured, most recent updated_epoch — sufficient to exercise
    the plumbing the spec calls for.
    """
    head = content.lstrip().lower()
    needle = head
    for hint in _SUPERSESSION_HINTS:
        if needle.startswith(hint):
            needle = needle[len(hint) :].lstrip()
            break
    if not needle:
        return None
    records, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
                models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
            ]
        ),
        limit=50,
        with_payload=True,
    )
    best: KSUID | None = None
    best_epoch = -1.0
    for rec in records:
        if not rec.payload:
            continue
        candidate_id = rec.payload.get("object_id")
        if candidate_id == self_id:
            continue
        candidate_content = (rec.payload.get("content") or "").strip().lower()
        if not candidate_content:
            continue
        if (
            candidate_content == needle
            or needle in candidate_content
            or candidate_content in needle
        ):
            epoch = float(rec.payload.get("updated_epoch", 0.0))
            if epoch > best_epoch:
                best_epoch = epoch
                best = candidate_id
    return best


def _enrichment_changed(
    row: dict[str, Any],
    normalized_tags: list[str],
    new_importance: int,
    new_topics: list[str],
) -> bool:
    """Avoid an unnecessary write when nothing about the enrichment fields
    changed — keeps idempotent sweeps from churning Qdrant on re-run."""
    return (
        list(row.get("tags", [])) != normalized_tags
        or int(row.get("importance", 5)) != new_importance
        or list(row.get("linked_to_topics", [])) != new_topics
    )


def _apply_enrichment(
    client: QdrantClient,
    *,
    collection: str,
    object_id: KSUID,
    tags: list[str],
    importance: int,
    topics: list[str],
) -> None:
    """Apply non-state enrichment fields to one row."""
    now = utc_now()
    client.set_payload(
        collection_name=collection,
        payload={
            "tags": tags,
            "importance": importance,
            "linked_to_topics": topics,
            "updated_at": now.isoformat(),
            "updated_epoch": epoch_of(now),
        },
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
    )


# ---------------------------------------------------------------------------
# Internal — LLM batching
# ---------------------------------------------------------------------------


async def _ollama_score_in_batches(
    ollama: OllamaClient,
    items: list[OllamaImportance],
) -> dict[KSUID, int] | None:
    """Call ``score_importance`` in batches; ``None`` on outage."""
    return await _batched_call(items, ollama.score_importance)


async def _ollama_topics_in_batches(
    ollama: OllamaClient,
    items: list[OllamaTopic],
) -> dict[KSUID, list[str]] | None:
    """Call ``infer_topics`` in batches; ``None`` on outage."""
    return await _batched_call(items, ollama.infer_topics)


async def _batched_call[T, R](
    items: list[T],
    call: Any,
    *,
    batch_size: int = _DEFAULT_LLM_BATCH,
) -> dict[KSUID, R] | None:
    """Drive ``call`` in batches and merge results.

    A single batch returning ``None`` poisons the whole sweep's
    enrichment for that field — matches the spec's all-or-nothing
    failure-mode handling. Per-item parse failures (different concern)
    are the OllamaClient's responsibility.
    """
    if not items:
        return {}
    merged: dict[KSUID, R] = {}
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        result = await call(batch)
        if result is None:
            return None
        merged.update(result)
    return merged


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


def build_maturation_jobs(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    ollama: OllamaClient,
    cursor: MaturationCursor,
    lock_dir: Path,
    config: MaturationConfig | None = None,
) -> list[Job]:
    """Return :class:`Job` objects matching the lifecycle-scheduler default
    job names that maturation *owns*: ``maturation_episodic``,
    ``provisional_ttl``, and ``concept_maturation``.

    Demotion used to live here (``demotion_episodic``, ``demotion_concept``)
    as helper sweeps that predated the dedicated demotion slice. They've
    since moved to :mod:`musubi.lifecycle.demotion`, which owns the real
    demotion path with ``DemotionDeps`` (including the thoughts emitter
    for "concept X demoted" ops notifications). This builder no longer
    schedules them — see :func:`musubi.lifecycle.demotion.build_demotion_jobs`
    for the canonical wiring. The legacy sweep functions remain in this
    module for tests and any caller that still imports them directly,
    but they are not on the cron anymore.

    Each job acquires the documented file lock before running so two
    workers on the same host can't double-execute (covered by spec
    bullet 20).
    """
    cfg = config or MaturationConfig()

    def _wrap(name: str, run: Any) -> Job:
        lock_path = lock_dir / f"{name}.lock"

        def _runner() -> None:
            with file_lock(lock_path) as acquired:
                if not acquired:
                    log.info("lifecycle-job=%s lock-held; skipping run", name)
                    return
                import asyncio as _asyncio

                _asyncio.run(run())

        # Schedule kwargs match build_default_jobs() exactly; the
        # lifecycle worker can swap our Job in for the placeholder
        # without touching its trigger config.
        kwargs: dict[str, Any]
        if name == "maturation_episodic":
            kwargs, grace = {"minute": 13}, 900
        elif name == "provisional_ttl":
            kwargs, grace = {"minute": 17}, 600
        elif name == "concept_maturation":
            kwargs, grace = {"hour": 3, "minute": 30}, 3600
        else:  # pragma: no cover — every name in the registry above is enumerated
            raise ValueError(f"unknown maturation job name: {name}")
        return Job(
            name=name,
            trigger_kind="cron",
            trigger_kwargs=kwargs,
            func=_runner,
            grace_time_s=grace,
        )

    return [
        _wrap(
            "maturation_episodic",
            lambda: episodic_maturation_sweep(
                client=client, sink=sink, ollama=ollama, cursor=cursor, config=cfg
            ),
        ),
        _wrap(
            "provisional_ttl",
            lambda: provisional_ttl_sweep(client=client, sink=sink, config=cfg),
        ),
        _wrap(
            "concept_maturation",
            lambda: concept_maturation_sweep(client=client, sink=sink, config=cfg),
        ),
    ]


# Re-export TransitionError for callers that want to type-check sweep
# failures without reaching into the lifecycle.transitions module.
__all__ = [
    "DEFAULT_TAG_ALIASES",
    "MaturationConfig",
    "MaturationCursor",
    "OllamaClient",
    "OllamaImportance",
    "OllamaTopic",
    "SweepReport",
    "TransitionError",
    "build_maturation_jobs",
    "concept_demotion_sweep",
    "concept_maturation_sweep",
    "default_ollama_client",
    "detect_supersession_hint",
    "episodic_demotion_sweep",
    "episodic_maturation_sweep",
    "normalize_tags",
    "provisional_ttl_sweep",
]
