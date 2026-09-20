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
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from qdrant_client import QdrantClient, models

from musubi.config import get_settings
from musubi.embedding.base import Embedder
from musubi.lifecycle import store
from musubi.lifecycle.coordinator import (
    LifecycleTransitionCoordinator,
    TransitionPending,
    is_transition_pending,
)
from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.scheduler import Job, file_lock
from musubi.lifecycle.transitions import (
    LineageUpdates,
    TransitionError,
    TransitionResult,
    transition,
)
from musubi.types.common import KSUID, Ok, epoch_of, generate_ksuid, utc_now

log = logging.getLogger(__name__)


@cache
def _get_enrichment_failure_counter() -> Any:
    """Family-bounded counter for enrichment batches that returned None.

    Lazy import: observability must not become an import cycle for the
    many tests that import this module without the registry.
    """
    from musubi.observability.registry import default_registry

    return default_registry().counter(
        "musubi_lifecycle_enrichment_batch_failures_total",
        "Maturation LLM enrichment batches that failed and were isolated "
        "(their items fall back; the rest of the sweep proceeds).",
        labelnames=("kind",),
    )


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
    """Return the production lifecycle LLM client.

    Backend selection is settings-driven (ADR 0043): with the defaults
    (``lifecycle_llm_api="ollama"`` and the lifecycle overrides unset)
    this is byte-for-byte the historical behavior — ``ollama_url`` +
    ``llm_model`` over Ollama's native API. Setting
    ``lifecycle_llm_api="openai"`` plus the ``lifecycle_llm_*``
    overrides points every lifecycle LLM task (importance, topics,
    synthesis, contradiction) at an OpenAI-compatible endpoint —
    LiteLLM ``house/backup`` in this deployment, chosen because the
    lifecycle workload is serial nightly batch (its concurrency
    penalty never applies) and the synthesis prompt needs more context
    and capability than the co-located 4B provides.

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

    api = getattr(settings, "lifecycle_llm_api", "ollama")
    base_url = getattr(settings, "lifecycle_llm_base_url", None) or settings.ollama_url
    model = getattr(settings, "lifecycle_llm_model", None) or settings.llm_model
    key = getattr(settings, "lifecycle_llm_api_key", None)
    return HttpxOllamaClient(
        base_url=str(base_url).rstrip("/"),
        model=model,
        debug_dir=debug_dir,
        api=api,
        api_key=key.get_secret_value() if key is not None else None,
    )


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


class MaturationCursor:
    """Per-sweep cursor persisted to sqlite.

    Records the ``updated_epoch`` of the last processed row so the next
    sweep run resumes after the last committed item. The store is
    intentionally append-by-name: each sweep keeps its own row keyed by
    a sweep identifier (``"episodic_maturation"``, etc.). Operators can
    reset a cursor by deleting the row from sqlite.
    """

    def __init__(
        self, *, db_path: Path, busy_timeout_ms: int = store.DEFAULT_BUSY_TIMEOUT_MS
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        with self._connect() as conn:
            store.ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        return store.connect(self._db_path, busy_timeout_ms=self._busy_timeout_ms)

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
    deferred: list[TransitionPending] = field(default_factory=list)


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


async def episodic_maturation_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    coordinator: LifecycleTransitionCoordinator,
    ollama: OllamaClient,
    cursor: MaturationCursor,
    config: MaturationConfig | None = None,
    now: datetime | None = None,
    embedder: Embedder | None = None,
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
    deferred: list[TransitionPending] = []
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
        # Step 5 — supersession inference (LIFE-009: semantic + topic
        # compatibility + abstention on ambiguity; bounded candidate
        # search). The seam requires the embedder; if the runner did
        # not pass one (legacy direct caller, or a test that bypasses
        # the embedder), fall back to a NoopEmbedder. NoopEmbedder
        # returns 1024D zero vectors so cosine is 0 with everything
        # and the seam abstains on every candidate — conservative,
        # not the OLD substring heuristic. The production-wired
        # ``build_maturation_jobs`` path passes the real
        # ``_TEICompositeEmbedder`` (LIFE-009 production wiring).
        # ------------------------------------------------------------------
        lineage_updates: LineageUpdates | None = None
        superseded_target_id: KSUID | None = None

        _hint = detect_supersession_hint(row.get("content", ""))
        if _hint:
            seam_embedder: Embedder = embedder if embedder is not None else _NoopEmbedder()
            superseded_target_id = await _find_supersession_candidate(
                client,
                collection=_EPISODIC_COLLECTION,
                namespace=row["namespace"],
                self_id=object_id,
                content=row.get("content", ""),
                embedder=seam_embedder,
                topics=new_topics,
            )
            if superseded_target_id is not None:
                lineage_updates = LineageUpdates(supersedes=[superseded_target_id])

        # ------------------------------------------------------------------
        # Step 6 — canonical state transition.
        # ------------------------------------------------------------------
        result = transition(
            client,
            coordinator=coordinator,
            object_id=object_id,
            target_state="matured",
            actor=_LIFECYCLE_ACTOR,
            reason="maturation-sweep",
            lineage_updates=lineage_updates,
            sink=sink,
            # Qualify the canonical lookup: `object_id` is not globally unique, and an
            # unqualified transition could mature a stranger's row and count it here.
            namespace=row["namespace"],
            # The candidate payload is the snapshot every enrichment value above was
            # computed from. Refuse if another writer moved it after selection instead
            # of maturing a newer row with stale derived data (Copilot, musubi#771).
            expected_version=int(row["version"]),
        )
        if not isinstance(result, Ok):
            failed += 1
            log.warning(
                "maturation-transition-failed object_id=%s err=%r",
                object_id,
                result.error,
            )
            continue
        if is_transition_pending(result.value):
            deferred.append(result.value)
            continue
        # The version this sweep just established. Captured here, where the outcome
        # is known final, so the enrichment fence below can name the exact row this
        # transition produced rather than any row that happens to read `matured`.
        assert isinstance(result.value, TransitionResult)  # narrowed by the check above
        matured_version = result.value.version

        # If we marked an old row as the predecessor, flip it to
        # "superseded" with the back-pointer. Bullet 13 covers both sides.
        if superseded_target_id is not None:
            back_result = transition(
                client,
                coordinator=coordinator,
                object_id=superseded_target_id,
                # The predecessor is, by construction, in the same namespace:
                # `_find_supersession_candidate` returns "the unique matured row in the
                # SAME namespace". This back-link was one of four identical unqualified
                # sites, one per plane (Aoi's enumeration, musubi#771).
                namespace=row["namespace"],
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
            elif is_transition_pending(back_result.value):
                deferred.append(back_result.value)

        # ------------------------------------------------------------------
        # Enrichment write — non-state fields, applied via set_payload on
        # the same point id. Not a state change → no separate ledger entry.
        # ------------------------------------------------------------------
        importance_scored = importance_by_id is not None and object_id in importance_by_id
        if _enrichment_changed(
            row, normalized, new_importance, new_topics, importance_scored=importance_scored
        ):
            applied = _apply_enrichment(
                client,
                collection=_EPISODIC_COLLECTION,
                namespace=row["namespace"],
                object_id=object_id,
                expected_version=matured_version,
                tags=normalized,
                importance=new_importance,
                topics=new_topics,
                importance_scored=importance_scored,
            )
            if applied:
                enriched += 1
            else:
                # The version fence refused: something moved the row between this
                # sweep's transition and its enrichment write. The transition itself
                # stands, so the row is no longer `provisional` and will NOT be
                # re-selected by a later sweep -- this enrichment is lost, not deferred.
                #
                # Recorded rather than swallowed. `enriched` must not count it (the
                # write did not happen) and silence would make the loss invisible in
                # the one report an operator reads (Copilot/Yua, musubi#771).
                failed += 1
                log.warning(
                    # The fence has FOUR conditions and this path cannot tell which one
                    # refused -- `state`/`version` movement and a concurrent mutation
                    # lease produce the identical zero-match result. Naming one of them
                    # sends an operator looking for a retraction when the real cause was
                    # a lease, so the message names the OBSERVATION and lists the
                    # possible causes (Copilot, musubi#771).
                    "maturation-enrichment-refused object_id=%s namespace=%s version=%s "
                    "(fence matched no row: the row moved state/version after the "
                    "transition, or a concurrent mutation lease was held; enrichment "
                    "not applied and not retried)",
                    object_id,
                    row["namespace"],
                    matured_version,
                )

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
        deferred=deferred,
    )


# ---------------------------------------------------------------------------
# Provisional TTL sweep
# ---------------------------------------------------------------------------


async def provisional_ttl_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    coordinator: LifecycleTransitionCoordinator,
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
    deferred: list[TransitionPending] = []
    for row in candidates:
        object_id: KSUID = row["object_id"]
        result = transition(
            client,
            coordinator=coordinator,
            object_id=object_id,
            namespace=row["namespace"],
            target_state="archived",
            actor=_LIFECYCLE_ACTOR,
            reason="provisional-ttl",
            sink=sink,
        )
        if isinstance(result, Ok) and is_transition_pending(result.value):
            deferred.append(result.value)
        elif isinstance(result, Ok):
            transitioned += 1
        else:
            failed += 1
            log.warning("provisional-ttl-failed object_id=%s err=%r", object_id, result.error)
    return SweepReport(
        selected=len(candidates),
        transitioned=transitioned,
        failed=failed,
        deferred=deferred,
    )


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


async def episodic_demotion_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    coordinator: LifecycleTransitionCoordinator,
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
    deferred: list[TransitionPending] = []
    for row in candidates:
        result = transition(
            client,
            coordinator=coordinator,
            object_id=row["object_id"],
            namespace=row["namespace"],
            target_state="demoted",
            actor=_LIFECYCLE_ACTOR,
            reason="maturation-demotion",
            sink=sink,
        )
        if isinstance(result, Ok) and is_transition_pending(result.value):
            deferred.append(result.value)
        elif isinstance(result, Ok):
            transitioned += 1
        else:
            failed += 1
    return SweepReport(
        selected=len(candidates),
        transitioned=transitioned,
        failed=failed,
        deferred=deferred,
    )


async def concept_maturation_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    coordinator: LifecycleTransitionCoordinator,
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
    deferred: list[TransitionPending] = []
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
            coordinator=coordinator,
            object_id=row["object_id"],
            namespace=row["namespace"],
            target_state="matured",
            actor=_LIFECYCLE_ACTOR,
            reason="concept-maturation",
            sink=sink,
        )
        if isinstance(result, Ok) and is_transition_pending(result.value):
            deferred.append(result.value)
        elif isinstance(result, Ok):
            transitioned += 1
        else:
            failed += 1
    return SweepReport(
        selected=len(candidates),
        transitioned=transitioned,
        failed=failed,
        deferred=deferred,
    )


async def concept_demotion_sweep(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    coordinator: LifecycleTransitionCoordinator,
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
    deferred: list[TransitionPending] = []
    for row in candidates:
        result = transition(
            client,
            coordinator=coordinator,
            object_id=row["object_id"],
            namespace=row["namespace"],
            target_state="demoted",
            actor=_LIFECYCLE_ACTOR,
            reason="concept-demotion",
            sink=sink,
        )
        if isinstance(result, Ok) and is_transition_pending(result.value):
            deferred.append(result.value)
        elif isinstance(result, Ok):
            transitioned += 1
        else:
            failed += 1
    return SweepReport(
        selected=len(candidates),
        transitioned=transitioned,
        failed=failed,
        deferred=deferred,
    )


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


async def _find_supersession_candidate(
    client: QdrantClient,
    *,
    collection: str,
    namespace: str,
    self_id: KSUID,
    content: str,
    embedder: Embedder,
    topics: list[str],
    similarity_threshold: float = 0.88,
    max_candidates: int = 20,
) -> KSUID | None:
    """Return the unique matured row in the same namespace that passes
    BOTH the semantic similarity AND the topic-compatibility checks
    (Issue #532 / LIFE-009).

    Discriminating contract:

      - The candidate's post-hint content is semantically similar to
        the new memory's post-hint content (cosine ≥ ``similarity_threshold``).
      - The candidate shares at least one ``linked_to_topics`` entry
        with the new memory's ``topics``.
      - If zero or two-or-more candidates pass, the seam abstains
        (returns ``None``). Only when EXACTLY ONE candidate passes
        is the supersession inferred.

    Substring overlap alone is NEVER sufficient (the OLD substring
    heuristic would link unrelated rows that share a substring).

    The function is bounded: at most ``max_candidates`` rows are
    scored. The needle and candidate content have any leading
    supersession hint (``update:``, ``correction:``, ``replacing:``)
    stripped before embedding/scoring.
    """
    head = content.lstrip().lower()
    needle = head
    for hint in _SUPERSESSION_HINTS:
        if needle.startswith(hint):
            needle = needle[len(hint) :].lstrip()
            break
    if not needle:
        return None
    # Topic compatibility is a hard requirement (the seam's
    # discriminating contract): a candidate MUST share at least one
    # `linked_to_topics` entry with the needle. An empty `topics`
    # list means "no topic is compatible with the needle" — abstain
    # early without paying for the Qdrant scroll.
    if not topics:
        return None
    records, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
                models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
            ]
        ),
        limit=max_candidates,
        with_payload=True,
    )

    # Topic-filter candidates first (no embedding needed; the topic
    # check is cheap). Surviving topic-compatible candidates are the
    # only rows whose dense vectors we will ever need.
    candidate_pairs: list[tuple[KSUID, str]] = []
    for rec in records:
        if not rec.payload:
            continue
        candidate_id = rec.payload.get("object_id")
        if not isinstance(candidate_id, str) or candidate_id == self_id:
            continue
        candidate_content = (rec.payload.get("content") or "").strip().lower()
        if not candidate_content:
            continue
        for hint in _SUPERSESSION_HINTS:
            if candidate_content.startswith(hint):
                candidate_content = candidate_content[len(hint) :].lstrip()
                break
        if not candidate_content:
            continue
        candidate_topics = rec.payload.get("linked_to_topics") or []
        if not any(t in topics for t in candidate_topics):
            continue
        candidate_pairs.append((candidate_id, candidate_content))

    # ONE batched embed_dense call (discriminator: at most one network
    # roundtrip per seam invocation). The needle rides in the SAME
    # batch as the topic-surviving candidates (`[needle] + contents`).
    # This is the single-call contract: zero per-candidate network
    # roundtrips, regardless of how many topic-compatible candidates
    # the namespace has. The Embedder Protocol contract is
    # `len(vectors) == len(batch)`; we defensively check it below.
    if not candidate_pairs:
        return None
    batch = [needle] + [c for _, c in candidate_pairs]
    try:
        vectors = await embedder.embed_dense(batch)
    except Exception as exc:
        # Supersession detection is optional: an embedder outage (TEI
        # unreachable, model OOM, etc.) must not crash the maturation
        # sweep. Abstain (return None) and log a warning so the
        # operator can see the failure mode without losing the row.
        log.warning(
            "supersession-seam-abstain-embed-failed batch_size=%d exc_type=%s",
            len(batch),
            type(exc).__name__,
        )
        return None
    if len(vectors) != len(batch):
        # Defensive: the Embedder Protocol promises len(vectors) == len(batch).
        # Treat a malformed response as no candidate (abstain).
        return None
    needle_vec = vectors[0]
    needle_norm = math.sqrt(sum(x * x for x in needle_vec)) or 1.0

    candidates: list[KSUID] = []
    for (cid, _), candidate_vec in zip(candidate_pairs, vectors[1:], strict=True):
        # The Embedder Protocol doesn't guarantee fixed-length vectors.
        # A misbehaving embedder returning a different-length candidate
        # vector would let the strict-zip below raise and abort the
        # whole sweep; we abstain on the malformed pair instead.
        if len(candidate_vec) != len(needle_vec):
            log.warning(
                "supersession-seam-abstain-skipped-candidate candidate_id=%s "
                "len(needle_vec)=%d len(candidate_vec)=%d",
                cid,
                len(needle_vec),
                len(candidate_vec),
            )
            continue
        candidate_norm = math.sqrt(sum(x * x for x in candidate_vec)) or 1.0
        dot = sum(x * y for x, y in zip(needle_vec, candidate_vec, strict=True))
        similarity = dot / (needle_norm * candidate_norm)
        if similarity < similarity_threshold:
            continue
        candidates.append(cid)

    # Abstain on zero or two-or-more matches (ambiguous). The seam
    # never returns a list — a single confident predecessor is the
    # only verdict.
    if len(candidates) != 1:
        return None
    return candidates[0]


def _enrichment_changed(
    row: dict[str, Any],
    normalized_tags: list[str],
    new_importance: int,
    new_topics: list[str],
    *,
    importance_scored: bool = False,
) -> bool:
    """Avoid an unnecessary write when nothing about the enrichment fields
    changed — keeps idempotent sweeps from churning Qdrant on re-run.

    ``importance_scored`` forces a write whenever the LLM scored this row —
    unconditionally, not merely when the stamp is null. A verdict that
    happens to equal the current value is still a scoring event, and
    ``importance_last_scored_at`` must mean *last scored*: a stale-row
    sweep that rescores an already-stamped row to the same value must
    advance the stamp, or the row reads as perpetually stale and is
    selected again on every pass.
    """
    return (
        importance_scored
        or list(row.get("tags", [])) != normalized_tags
        or int(row.get("importance", 5)) != new_importance
        or list(row.get("linked_to_topics", [])) != new_topics
    )


def _apply_enrichment(
    client: QdrantClient,
    *,
    collection: str,
    namespace: str,
    object_id: KSUID,
    expected_version: int,
    tags: list[str],
    importance: int,
    topics: list[str],
    importance_scored: bool = False,
) -> bool:
    """Apply non-state enrichment fields to one row.

    ``importance_scored`` records the LLM score-audit timestamp. The field
    existed on the model from day one but was never written, so every row
    carried ``importance_last_scored_at: None`` even after a rescore. Both
    the datetime and its indexed epoch twin are written together, matching
    the ``updated_at`` / ``updated_epoch`` convention (the epoch key backs
    the ``importance_last_scored_epoch`` float index in store/specs.py).
    """
    now = utc_now()
    # An UNGUESSABLE per-attempt marker, written inside the same fenced `set_payload`
    # and read back to attribute the write. `updated_epoch` could not do this job: the
    # transition immediately before also writes it, and two `utc_now()` calls can land
    # in the same microsecond -- so the readback could match the TRANSITION's write and
    # report an enrichment that never landed (Copilot, musubi#771). A timestamp is a
    # measurement of when, never proof of who. This marker is unique by construction.
    attempt = generate_ksuid()
    payload: dict[str, Any] = {
        "tags": tags,
        "importance": importance,
        "linked_to_topics": topics,
        "updated_at": now.isoformat(),
        "updated_epoch": epoch_of(now),
        "enrichment_attempt": attempt,
    }
    if importance_scored:
        payload["importance_last_scored_at"] = now.isoformat()
        payload["importance_last_scored_epoch"] = epoch_of(now)
    # FENCED on the state this sweep just established. The transition and this
    # enrichment are two writes, so anything that moves the row in between --
    # a retraction quarantining it to `archived`/importance 1 is the live case --
    # was previously overwritten here, giving a retracted row a rescored
    # importance and a post-retraction `updated_at` (musubi#732, 2026-09-20).
    #
    # This also excludes v2 immutable content points, which carry no `state` key
    # at all: a FieldCondition cannot match a point that lacks the field. That
    # exclusion is load-bearing rather than incidental, so
    # `test_v2_content_point_is_never_enriched` pins it.
    conditions: list[models.Condition] = [
        # `object_id` is NOT globally unique -- the same id can exist under a
        # different namespace, and an unqualified filter would enrich a
        # stranger's row (Copilot, musubi#771).
        models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
        models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
        models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
        # VERSION is the transition identity; `state` alone is not. An archived or
        # demoted row can be restored to `matured`, and a stale snapshot would then
        # satisfy a state-only fence and write enrichment computed for a row that
        # has since moved (Copilot/Yua, musubi#771). A restore bumps the version, so
        # this condition refuses anything that is not the exact row this sweep
        # transitioned.
        models.FieldCondition(key="version", match=models.MatchValue(value=expected_version)),
    ]

    # THE LEASE CHECK IS THE WRITE'S OWN CONDITION, not a preceding read.
    #
    # This was a python pre-read -- scroll, inspect `update_lease_token`, then
    # `set_payload` on a fence that did not mention the lease. That is a TOCTOU: a
    # writer acquiring the lease between the scroll and the write sails straight
    # through, because the thing that was checked is not the thing that gated the
    # write. Same defect class as enriching on `state` while `version` identifies the
    # row -- right check, wrong object (Tama, musubi#771).
    #
    # Refusal here is STRICT: any token present, fresh or expired, refuses. That is
    # sound only because expired-ordinary-token TAKEOVER happens in the coordinator
    # BEFORE the transition, so by the time enrichment runs the row is known to have
    # been lease-free at transition time. A token observed now was therefore acquired
    # AFTER the transition -- genuinely concurrent, never a crashed-patch fossil -- and
    # the liveness argument for tolerating expired tokens (Aoi's, and correct) does not
    # reach this call site.
    write_fence = models.Filter(
        must=[
            *conditions,
            models.IsEmptyCondition(is_empty=models.PayloadField(key="update_lease_token")),
        ]
    )
    client.set_payload(collection_name=collection, payload=payload, points=write_fence)
    # Success is read back from DURABLE STATE, never inferred from having issued the
    # write. A pre-write count cannot prove a post-write outcome: count sees `matured`,
    # a retraction archives the row, the fenced write then matches zero rows, and the
    # caller is told an enrichment happened (Yua, musubi#771). `set_payload` reports
    # operation status, not how many points it matched, so the only honest signal is
    # to look afterwards.
    #
    # The discriminator is the per-attempt marker, which only THIS call could have
    # written. It rides in the same fenced payload, so a row carrying it is a row this
    # write landed on -- no clock, no coincidence.
    applied = models.Filter(
        must=[
            *conditions,
            models.FieldCondition(key="enrichment_attempt", match=models.MatchValue(value=attempt)),
        ]
    )
    landed = client.count(collection_name=collection, count_filter=applied, exact=True).count == 1

    # Remove the marker behind its own exact value, so the payload does not accumulate
    # a field that means nothing after the answer is read. Fenced on the marker rather
    # than on identity: if the row moved on between the readback and here, the delete
    # must not touch it.
    #
    # CRASH RESIDUE IS SAFE BY DESIGN, and that is a claim with a cell. A process that
    # dies between the write and this cleanup leaves a stale `enrichment_attempt`
    # behind. Nothing reads the field except a count fenced on a freshly generated
    # value, so a residual marker can never be mistaken for a later attempt's -- and
    # the next enrichment overwrites it in the same `set_payload`.
    # BEST-EFFORT, and skipped entirely when nothing landed. The authoritative answer is
    # already determined above; cleanup is hygiene. Letting it raise would turn a
    # DURABLE SUCCESS into a reported failure -- the caller would count `failed += 1`
    # and log a refusal for enrichment that is sitting in the collection (Yua,
    # musubi#771).
    #
    # Yes, this is the broad catch I just deleted from `_scroll_by_object_id`. The
    # difference is what the result is used for, and it is worth stating rather than
    # relying on: there, an exception was converted into an ANSWER ("no rows"), so
    # failing open changed a decision. Here the decision is already made and recorded;
    # suppressing this one cannot make `landed` wrong. It is logged rather than
    # silenced, because repeated failures mean something systemic even though each one
    # is harmless.
    if landed:
        try:
            client.delete_payload(
                collection_name=collection,
                keys=["enrichment_attempt"],
                points=models.Filter(
                    must=[
                        *conditions,
                        models.FieldCondition(
                            key="enrichment_attempt", match=models.MatchValue(value=attempt)
                        ),
                    ]
                ),
            )
        except Exception:
            log.warning(
                "maturation-enrichment-marker-cleanup-failed object_id=%s namespace=%s "
                "attempt=%s (enrichment DID apply; a residual marker is inert because "
                "every readback is fenced on a freshly generated value)",
                object_id,
                namespace,
                attempt,
                exc_info=True,
            )
    return landed


# ---------------------------------------------------------------------------
# Internal — LLM batching
# ---------------------------------------------------------------------------


async def _ollama_score_in_batches(
    ollama: OllamaClient,
    items: list[OllamaImportance],
) -> dict[KSUID, int]:
    """Call ``score_importance`` in batches; failed batches are isolated."""
    return await _batched_call(items, ollama.score_importance, kind="importance")


async def _ollama_topics_in_batches(
    ollama: OllamaClient,
    items: list[OllamaTopic],
) -> dict[KSUID, list[str]]:
    """Call ``infer_topics`` in batches; failed batches are isolated."""
    return await _batched_call(items, ollama.infer_topics, kind="topics")


async def _batched_call[T, R](
    items: list[T],
    call: Any,
    *,
    kind: str,
    batch_size: int = _DEFAULT_LLM_BATCH,
) -> dict[KSUID, R]:
    """Drive ``call`` in batches and merge results, isolating failures
    PER BATCH.

    Contract change (ADR 0043, supersedes the original all-or-nothing
    handling): a batch returning ``None`` no longer nulls the whole
    sweep's enrichment for that field. Its items simply stay absent from
    the merged map — the sweep's per-row fallback covers them — while
    every other batch's results land. Measured motivation: one flaky
    structured-output batch out of ~five was erasing topics for entire
    sweeps, which starved synthesis clustering down to capture-source
    tags. Failed batches are counted on
    ``musubi_lifecycle_enrichment_batch_failures_total{kind}`` so the
    degradation is an operational signal, not a log line (the #684
    lesson). Per-item parse failures (different concern) remain the
    LLM client's responsibility.
    """
    if not items:
        return {}
    merged: dict[KSUID, R] = {}
    failed = 0
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        result = await call(batch)
        if result is None:
            failed += 1
            _get_enrichment_failure_counter().labels(kind=kind).inc()
            continue
        merged.update(result)
    if failed:
        log.warning(
            "maturation-enrichment-degraded kind=%s failed_batches=%d/%d items_recovered=%d",
            kind,
            failed,
            math.ceil(len(items) / batch_size),
            len(merged),
        )
    return merged


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


def build_maturation_jobs(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    coordinator: LifecycleTransitionCoordinator,
    ollama: OllamaClient,
    cursor: MaturationCursor,
    lock_dir: Path,
    embedder: Embedder,
    config: MaturationConfig | None = None,
) -> list[Job]:
    """Return :class:`Job` objects matching the lifecycle-scheduler default
    job names that maturation *owns*: ``maturation_episodic``,
    ``provisional_ttl``, and ``concept_maturation``.

    The ``embedder`` parameter is REQUIRED (production-wiring
    discriminator). A caller that omits ``embedder=`` fails at the
    Python call site (``TypeError: missing 1 required keyword-only
    argument``) — there is no silent fallback to :class:`_NoopEmbedder`
    at the scheduler boundary. Direct test callers that want the
    conservative seam (abstain on every candidate) must explicitly
    pass ``_NoopEmbedder()``. Production's :func:`musubi.lifecycle.runner._main_async`
    passes the real ``ChunkedEmbedder`` wrapping ``_TEICompositeEmbedder``.

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
                client=client,
                sink=sink,
                coordinator=coordinator,
                ollama=ollama,
                cursor=cursor,
                config=cfg,
                embedder=embedder,
            ),
        ),
        _wrap(
            "provisional_ttl",
            lambda: provisional_ttl_sweep(
                client=client, sink=sink, coordinator=coordinator, config=cfg
            ),
        ),
        _wrap(
            "concept_maturation",
            lambda: concept_maturation_sweep(
                client=client, sink=sink, coordinator=coordinator, config=cfg
            ),
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


class _NoopEmbedder:
    """Stub embedder used when the runner did not pass one. Returns
    1024D zero vectors so the seam abstains (the zero vector has
    cosine 0 with everything)."""

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        from musubi.store.specs import DENSE_SIZE

        return [[0.0] * DENSE_SIZE for _ in texts]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return [{} for _ in texts]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return [0.0 for _ in candidates]
