"""Reflection sweep — daily narrative digest written to the vault.

Per [[06-ingestion/reflection]], one daily background pass that:

1. Counts captures in the last 24h (episodic + artifact + thought).
2. Asks an LLM to surface 3-5 themes across those captures.
3. Lists rows promoted (and rows whose gate passed but were skipped).
4. Lists rows demoted in the window plus a heuristic "at-risk" list.
5. Lists active contradictions surfaced or resolved since yesterday.
6. Lists curated rows worth revisiting (high importance, long
   inactivity).

The rendered markdown is written to the Obsidian vault at
``vault/reflections/YYYY-MM/YYYY-MM-DD.md`` AND indexed in
``musubi_curated`` with ``topics: [reflection]``. A thought is emitted
to the operator's scheduler channel pointing at the new file.

Architecture decisions:

- **Three Protocols + loud-failure stubs.** ``VaultWriter`` (filesystem
  write owned by ``slice-vault-sync``), ``ThoughtEmitter`` (the
  thoughts-plane create surface — ``slice-plane-thoughts``), and
  ``ReflectionLLM`` (Ollama summarisation) are all abstracted so this
  module ships without forcing those upstream slices to land first.
  The default implementations all raise ``NotImplementedError`` per the
  ADR-punted-deps-fail-loud rule
  (``CLAUDE.md`` § Additional handoff-readiness rules). A future
  ``slice-llm-client`` satisfies the LLM Protocol; ``slice-vault-sync``
  satisfies the writer; the future API/wiring slice adapts the thoughts
  plane's ``create()`` into ``ThoughtEmitter``.
- **State mutations route through ``CuratedPlane.create``.** The reflection
  output is a curated row; the canonical create surface owns
  vault-path-keyed dedup so re-running for the same date keeps a single
  row in Qdrant (idempotency, bullet 12).
- **Read-only across other planes.** Capture summary / promotion /
  demotion / contradiction / revisit sections all scroll Qdrant by
  payload predicate; we never mutate episodic / concept / thought rows.
- **Cited-id validation.** The LLM may hallucinate object_ids in the
  patterns section; ``validate_cited_ids`` checks each candidate against
  the actual episodic ids in the window and either strips or annotates
  the unknowns.
- **Degradation.** ``ReflectionLLM`` returning ``None`` means Ollama is
  down; the patterns section is replaced with the documented skip notice
  and every other section still renders. The file is still written, the
  curated row is still indexed.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, Protocol

from qdrant_client import QdrantClient, models

from musubi.config import get_settings
from musubi.lifecycle.events import LifecycleEventSink
from musubi.observability import default_registry
from musubi.planes.curated import CuratedPlane
from musubi.types.common import KSUID, Namespace, generate_ksuid, utc_now
from musubi.types.curated import CuratedKnowledge

log = logging.getLogger(__name__)

_REFLECTION_TOPIC = "reflection"
_DEFAULT_IMPORTANCE = 6
_LLM_OUTAGE_NOTICE = "> LLM was unavailable at reflection time; patterns section skipped."

_KSUID_RE = re.compile(r"\b[0-9A-Za-z]{27}\b")

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


def _instrument_reflection_job[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    @wraps(func)
    async def _wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.monotonic()
        try:
            return await func(*args, **kwargs)
        except Exception:
            _ERRORS.labels(job="reflection").inc()
            raise
        finally:
            _DURATION.labels(job="reflection").observe(time.monotonic() - start)

    return _wrapped


# ---------------------------------------------------------------------------
# Protocols + production stubs
# ---------------------------------------------------------------------------


class VaultWriter(Protocol):
    """Filesystem write surface for vault-managed files.

    Production wiring lives in ``slice-vault-sync``. The reflection
    sweep does not write to ``vault/`` directly — every byte that lands
    in the Obsidian vault flows through this Protocol so the watcher's
    write-log echo filter can attribute the write to Musubi.
    """

    async def write_reflection(self, *, path: str, frontmatter: str, body: str) -> None: ...


class ThoughtEmitter(Protocol):
    """Channel for emitting thoughts to operator presences.

    Production wiring adapts ``ThoughtsPlane.create()`` (slice-plane-thoughts)
    into this shape. The reflection sweep emits one thought per run
    pointing the operator at the new file.
    """

    async def emit(
        self,
        *,
        namespace: str,
        channel: str,
        content: str,
        importance: int,
    ) -> None: ...


class ReflectionLLM(Protocol):
    """LLM call surface for the patterns section.

    A successful call returns a markdown blob already shaped as
    ``## Theme\\nbody...`` per the prompt. ``None`` signals an Ollama
    outage — the renderer substitutes the documented skip notice.
    """

    async def summarize_patterns(self, items: list[dict[str, object]]) -> str | None: ...


class _NotConfiguredVaultWriter:
    """Production stub. Raises ``NotImplementedError`` on every call.

    Reflection cannot run in a deployment that hasn't wired a real
    vault writer (slice-vault-sync). Failing closed beats silently
    dropping the daily digest.
    """

    async def write_reflection(self, *, path: str, frontmatter: str, body: str) -> None:
        raise NotImplementedError(
            "VaultWriter is not configured. The reflection sweep cannot "
            "run in production without a real VaultWriter wired in (see "
            "slice-vault-sync). The default stub fails closed so an "
            "unconfigured deployment doesn't silently drop the daily "
            "digest."
        )


class _NotConfiguredThoughtEmitter:
    """Production stub. Raises ``NotImplementedError`` on every call."""

    async def emit(
        self,
        *,
        namespace: str,
        channel: str,
        content: str,
        importance: int,
    ) -> None:
        raise NotImplementedError(
            "ThoughtEmitter is not configured. The reflection sweep cannot "
            "run in production without a real ThoughtEmitter wired in "
            "(adapter over slice-plane-thoughts). The default stub fails "
            "closed."
        )


class _NotConfiguredReflectionLLM:
    """Production stub. Raises ``NotImplementedError`` on every call.

    Note that the *spec's* failure-mode contract is that an Ollama
    outage produces a ``None`` return + a skip notice in the rendered
    file — it does NOT raise. The loud-failure default below catches a
    different failure: the production wiring forgot to swap in a real
    LLM client. ``None`` is a legitimate runtime state; raising is the
    misconfiguration signal.
    """

    async def summarize_patterns(self, items: list[dict[str, object]]) -> str | None:
        raise NotImplementedError(
            "ReflectionLLM is not configured. The reflection sweep cannot "
            "run in production without a real ReflectionLLM wired in "
            "(see future slice-llm-client; will read Settings.ollama_url + "
            "Settings.llm_model). The default stub fails closed."
        )


def default_vault_writer() -> VaultWriter:
    """Production-default writer (the loud stub)."""
    _ = get_settings  # keep the integration import live
    return _NotConfiguredVaultWriter()


def default_thought_emitter() -> ThoughtEmitter:
    """Production-default emitter (the loud stub)."""
    _ = get_settings
    return _NotConfiguredThoughtEmitter()


def default_reflection_llm() -> ReflectionLLM:
    """Production-default LLM client (the loud stub)."""
    _ = get_settings
    return _NotConfiguredReflectionLLM()


# ---------------------------------------------------------------------------
# Config + result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReflectionConfig:
    """Tunable thresholds. Defaults match the spec; callers can override."""

    revisit_min_importance: int = 8
    revisit_min_age_days: int = 30
    at_risk_importance_max: int = 4
    at_risk_age_days_min: int = 30
    digest_importance: int = _DEFAULT_IMPORTANCE


@dataclass(frozen=True)
class ReflectionResult:
    """Outcome of one reflection-sweep invocation."""

    path: str
    object_id: KSUID
    sections: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def vault_path_for(date: datetime) -> str:
    """``vault/reflections/YYYY-MM/YYYY-MM-DD.md`` for a given date."""
    return (
        f"vault/reflections/{date.year:04d}-{date.month:02d}/"
        f"{date.year:04d}-{date.month:02d}-{date.day:02d}.md"
    )


def render_frontmatter(
    *,
    date: datetime,
    object_id: KSUID,
    namespace: str,
) -> str:
    """Render the YAML frontmatter for the reflection file."""
    iso = date.isoformat()
    title = f"Reflection — {date.year:04d}-{date.month:02d}-{date.day:02d}"
    return (
        "---\n"
        f"object_id: {object_id}\n"
        f"namespace: {namespace}\n"
        "schema_version: 1\n"
        f'title: "{title}"\n'
        "topics:\n"
        f"  - {_REFLECTION_TOPIC}\n"
        f"tags: [{_REFLECTION_TOPIC}, daily]\n"
        f"importance: {_DEFAULT_IMPORTANCE}\n"
        "state: matured\n"
        "version: 1\n"
        "musubi-managed: true\n"
        f"created: {iso}\n"
        f"updated: {iso}\n"
        "---\n"
    )


def validate_cited_ids(text: str, *, available_ids: set[str]) -> str:
    """Strip or annotate KSUID-shaped tokens in ``text`` that aren't in
    ``available_ids``. Real ids pass through unchanged."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in available_ids:
            return token
        return f"{token} (unverified)"

    return _KSUID_RE.sub(_replace, text)


# ---------------------------------------------------------------------------
# Section gatherers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PromotionRow:
    object_id: KSUID
    namespace: str
    occurred_at: datetime


@dataclass(frozen=True)
class _DemotionRow:
    object_id: KSUID
    namespace: str
    occurred_at: datetime
    reason: str


@dataclass(frozen=True)
class _AtRiskRow:
    object_id: KSUID
    namespace: str
    importance: int
    last_seen_epoch: float


@dataclass(frozen=True)
class _RevisitRow:
    object_id: KSUID
    namespace: str
    title: str
    importance: int
    days_since_access: int


def _gather_capture_summary(
    client: QdrantClient, *, window_start_epoch: float, window_end_epoch: float
) -> dict[str, int]:
    """Count rows in each plane created in the window."""
    counts: dict[str, int] = {}
    for collection, key in (
        ("musubi_episodic", "episodic"),
        ("musubi_artifact", "artifact"),
        ("musubi_thought", "thought"),
    ):
        try:
            count = _count_in_window(
                client,
                collection=collection,
                window_start_epoch=window_start_epoch,
                window_end_epoch=window_end_epoch,
            )
        except Exception as exc:
            log.warning("reflection-capture-count-failed collection=%s err=%r", collection, exc)
            count = 0
        counts[key] = count
    return counts


def _count_in_window(
    client: QdrantClient,
    *,
    collection: str,
    window_start_epoch: float,
    window_end_epoch: float,
) -> int:
    """Count rows whose ``created_epoch`` falls in ``[start, end)``."""
    response = client.count(
        collection_name=collection,
        count_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="created_epoch",
                    range=models.Range(gte=window_start_epoch, lt=window_end_epoch),
                )
            ]
        ),
        exact=True,
    )
    return int(response.count)


def _gather_promotions(
    sink: LifecycleEventSink,
    *,
    window_start_epoch: float,
    window_end_epoch: float,
) -> list[_PromotionRow]:
    """Read ``LifecycleEvent`` rows for promoted concepts in the window.

    The "skipped" sub-list (gate passed but promotion job declined)
    needs a data source that ``slice-lifecycle-promotion`` will own; the
    renderer carries the header for it but the list is empty until that
    slice lands.
    """
    out: list[_PromotionRow] = []
    for ev in sink.read_all():
        if ev.to_state != "promoted":
            continue
        if not (window_start_epoch <= (ev.occurred_epoch or 0.0) < window_end_epoch):
            continue
        out.append(
            _PromotionRow(
                object_id=ev.object_id,
                namespace=ev.namespace,
                occurred_at=ev.occurred_at,
            )
        )
    return out


def _gather_demotions(
    sink: LifecycleEventSink,
    client: QdrantClient,
    *,
    window_start_epoch: float,
    window_end_epoch: float,
    config: ReflectionConfig,
    now: datetime,
) -> tuple[list[_DemotionRow], list[_AtRiskRow]]:
    """Demoted in the window + an at-risk heuristic list."""
    demoted: list[_DemotionRow] = []
    for ev in sink.read_all():
        if ev.to_state != "demoted":
            continue
        if not (window_start_epoch <= (ev.occurred_epoch or 0.0) < window_end_epoch):
            continue
        demoted.append(
            _DemotionRow(
                object_id=ev.object_id,
                namespace=ev.namespace,
                occurred_at=ev.occurred_at,
                reason=ev.reason,
            )
        )

    cutoff = (now - timedelta(days=config.at_risk_age_days_min)).timestamp()
    at_risk: list[_AtRiskRow] = []
    try:
        records, _ = client.scroll(
            collection_name="musubi_episodic",
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
                    models.FieldCondition(key="updated_epoch", range=models.Range(lt=cutoff)),
                    models.FieldCondition(
                        key="importance",
                        range=models.Range(lte=config.at_risk_importance_max),
                    ),
                ]
            ),
            limit=200,
            with_payload=True,
        )
    except Exception as exc:
        log.warning("reflection-at-risk-scan-failed err=%r", exc)
        records = []
    for rec in records:
        if not rec.payload:
            continue
        at_risk.append(
            _AtRiskRow(
                object_id=rec.payload["object_id"],
                namespace=rec.payload["namespace"],
                importance=int(rec.payload.get("importance", 0)),
                last_seen_epoch=float(rec.payload.get("updated_epoch", 0.0)),
            )
        )
    return demoted, at_risk


def _gather_contradictions(
    client: QdrantClient,
    *,
    window_start_epoch: float,
    window_end_epoch: float,
) -> dict[str, list[KSUID]]:
    """Concepts whose ``contradicts`` field is non-empty + that were
    updated in the window land under ``new``. Resolution events would
    need a "contradiction-resolved" lifecycle source that doesn't exist
    yet; the ``resolved`` list is empty until ``slice-lifecycle-promotion``
    or a dedicated reconciler ships."""
    new_pairs: list[KSUID] = []
    try:
        records, _ = client.scroll(
            collection_name="musubi_concept",
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="updated_epoch",
                        range=models.Range(gte=window_start_epoch, lt=window_end_epoch),
                    )
                ]
            ),
            limit=200,
            with_payload=True,
        )
    except Exception as exc:
        log.warning("reflection-contradiction-scan-failed err=%r", exc)
        records = []
    for rec in records:
        if not rec.payload:
            continue
        contradicts = rec.payload.get("contradicts") or []
        if contradicts:
            new_pairs.append(rec.payload["object_id"])
    return {"new": new_pairs, "resolved": []}


def _gather_revisit(
    client: QdrantClient,
    *,
    config: ReflectionConfig,
    now: datetime,
) -> list[_RevisitRow]:
    """High-importance curated rows that haven't been accessed in a long
    time. ``last_accessed_at IS NULL`` is treated as eligible (never
    accessed)."""
    cutoff = (now - timedelta(days=config.revisit_min_age_days)).timestamp()
    out: list[_RevisitRow] = []
    try:
        records, _ = client.scroll(
            collection_name="musubi_curated",
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
                    models.FieldCondition(
                        key="importance",
                        range=models.Range(gte=config.revisit_min_importance),
                    ),
                ]
            ),
            limit=200,
            with_payload=True,
        )
    except Exception as exc:
        log.warning("reflection-revisit-scan-failed err=%r", exc)
        records = []
    for rec in records:
        if not rec.payload:
            continue
        last_accessed = rec.payload.get("last_accessed_at")
        last_accessed_epoch: float | None = None
        if last_accessed:
            try:
                parsed = datetime.fromisoformat(str(last_accessed).replace("Z", "+00:00"))
                last_accessed_epoch = parsed.timestamp()
            except ValueError:
                last_accessed_epoch = None
        if last_accessed_epoch is not None and last_accessed_epoch > cutoff:
            continue
        if last_accessed_epoch is None:
            days = config.revisit_min_age_days
        else:
            days = max(0, int((now.timestamp() - last_accessed_epoch) / 86400))
        out.append(
            _RevisitRow(
                object_id=rec.payload["object_id"],
                namespace=rec.payload["namespace"],
                title=str(rec.payload.get("title", "")),
                importance=int(rec.payload.get("importance", 0)),
                days_since_access=days,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(
    *,
    date: datetime,
    capture_summary: dict[str, int],
    patterns_md: str,
    promotions: list[_PromotionRow],
    demotions: list[_DemotionRow],
    at_risk: list[_AtRiskRow] | None = None,
    contradictions: dict[str, list[KSUID]],
    revisit: list[_RevisitRow],
) -> str:
    """Assemble all sections into the final markdown body."""
    at_risk = at_risk or []
    title = f"# Reflection — {date.year:04d}-{date.month:02d}-{date.day:02d}"
    sections: list[str] = [title, ""]

    sections.append("## Capture summary")
    sections.append(f"- {capture_summary.get('episodic', 0)} new episodic captures")
    sections.append(f"- {capture_summary.get('artifact', 0)} new artifacts indexed")
    sections.append(f"- {capture_summary.get('thought', 0)} new thoughts sent")
    sections.append("")

    sections.append("## Surfaced patterns")
    sections.append(patterns_md or _LLM_OUTAGE_NOTICE)
    sections.append("")

    sections.append("## Promotion candidates")
    sections.append("### Promoted")
    if promotions:
        for row in promotions:
            sections.append(f"- {row.object_id} ({row.namespace}) — promoted")
    else:
        sections.append("- _(none in window)_")
    sections.append("### Skipped (gate passed, not promoted)")
    sections.append(
        "- _(no source available yet — populated once "
        "slice-lifecycle-promotion records gate-pass events)_"
    )
    sections.append("")

    sections.append("## Demotion candidates")
    if demotions:
        for d in demotions:
            sections.append(f"- {d.object_id} ({d.namespace}) — demoted ({d.reason})")
    else:
        sections.append("- _(none demoted in window)_")
    sections.append("### at-risk")
    if at_risk:
        for ar in at_risk:
            sections.append(
                f"- {ar.object_id} ({ar.namespace}) — importance {ar.importance}, untouched"
            )
    else:
        sections.append("- _(none at-risk in current scan)_")
    sections.append("")

    sections.append("## Contradictions")
    sections.append("### New")
    if contradictions.get("new"):
        for cid in contradictions["new"]:
            sections.append(f"- {cid}")
    else:
        sections.append("- _(none surfaced in window)_")
    sections.append("### Resolved")
    if contradictions.get("resolved"):
        for cid in contradictions["resolved"]:
            sections.append(f"- {cid}")
    else:
        sections.append(
            "- _(no source available yet — populated once a "
            "contradiction-resolved event source ships)_"
        )
    sections.append("")

    sections.append("## Worth revisiting")
    if revisit:
        for r in revisit:
            sections.append(
                f'- {r.object_id} ({r.namespace}) — "{r.title}", '
                f"importance {r.importance}, {r.days_since_access}d since access"
            )
    else:
        sections.append("- _(no curated rows meet the threshold today)_")
    sections.append("")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@_instrument_reflection_job
async def run_reflection_sweep(
    *,
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated_plane: CuratedPlane,
    vault: VaultWriter,
    thoughts: ThoughtEmitter,
    llm: ReflectionLLM,
    namespace: Namespace,
    now: datetime | None = None,
    config: ReflectionConfig | None = None,
) -> ReflectionResult:
    """One reflection sweep — render + write + index + emit thought.

    All file writes go through ``vault``; every plane mutation goes
    through ``curated_plane.create``. Read-only Qdrant scrolls power
    the data sections. ``llm.summarize_patterns`` returning ``None``
    triggers the documented skip notice rather than failing the whole
    sweep.
    """
    cfg = config or ReflectionConfig()
    when = now or utc_now()
    end_epoch = when.timestamp()
    start_epoch = (when - timedelta(days=1)).timestamp()

    capture = _gather_capture_summary(
        qdrant, window_start_epoch=start_epoch, window_end_epoch=end_epoch
    )
    promotions = _gather_promotions(
        sink, window_start_epoch=start_epoch, window_end_epoch=end_epoch
    )
    demotions, at_risk = _gather_demotions(
        sink,
        qdrant,
        window_start_epoch=start_epoch,
        window_end_epoch=end_epoch,
        config=cfg,
        now=when,
    )
    contradictions = _gather_contradictions(
        qdrant, window_start_epoch=start_epoch, window_end_epoch=end_epoch
    )
    revisit = _gather_revisit(qdrant, config=cfg, now=when)

    # Pull the episodic-id set in the window so the LLM's cited ids can
    # be validated against ground truth.
    available_ids = _episodic_ids_in_window(
        qdrant, window_start_epoch=start_epoch, window_end_epoch=end_epoch
    )

    patterns_input = _llm_input_from_episodics(qdrant, available_ids)
    raw_patterns: str | None
    try:
        raw_patterns = await llm.summarize_patterns(patterns_input)
    except NotImplementedError:
        # Loud failure for an unconfigured deployment; let it propagate.
        raise
    except Exception as exc:
        log.warning("reflection-llm-call-failed err=%r", exc)
        raw_patterns = None

    if raw_patterns is None:
        patterns_md = _LLM_OUTAGE_NOTICE
    else:
        patterns_md = validate_cited_ids(raw_patterns, available_ids=available_ids)

    object_id = generate_ksuid()
    body = render_markdown(
        date=when,
        capture_summary=capture,
        patterns_md=patterns_md,
        promotions=promotions,
        demotions=demotions,
        at_risk=at_risk,
        contradictions=contradictions,
        revisit=revisit,
    )
    frontmatter = render_frontmatter(date=when, object_id=object_id, namespace=namespace)
    path = vault_path_for(when)

    # Write the file via the vault writer (slice-vault-sync surface).
    await vault.write_reflection(path=path, frontmatter=frontmatter, body=body)

    # Index the rendered content in musubi_curated. The CuratedPlane
    # handles vault-path-keyed dedup so re-runs for the same date yield
    # a single row (idempotency, bullet 12).
    body_hash = _sha256(body)
    title = f"Reflection — {when.year:04d}-{when.month:02d}-{when.day:02d}"
    indexed = await curated_plane.create(
        CuratedKnowledge(
            object_id=object_id,
            namespace=namespace,
            title=title,
            content=body,
            topics=[_REFLECTION_TOPIC],
            tags=[_REFLECTION_TOPIC, "daily"],
            importance=cfg.digest_importance,
            vault_path=path,
            body_hash=body_hash,
            musubi_managed=True,
        )
    )

    # Emit a thought pointing the operator at the new file.
    await thoughts.emit(
        namespace=namespace,
        channel="scheduler",
        content=f"Daily reflection ready: [[{path}]]",
        importance=5,
    )

    sections = {
        "capture": capture.get("episodic", 0),
        "promotions": len(promotions),
        "demotions": len(demotions),
        "at_risk": len(at_risk),
        "revisit": len(revisit),
    }
    return ReflectionResult(path=path, object_id=indexed.object_id, sections=sections)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _episodic_ids_in_window(
    client: QdrantClient,
    *,
    window_start_epoch: float,
    window_end_epoch: float,
) -> set[str]:
    """Return every episodic ``object_id`` whose ``created_epoch`` is in
    the window. Used for cited-id validation."""
    try:
        records, _ = client.scroll(
            collection_name="musubi_episodic",
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="created_epoch",
                        range=models.Range(gte=window_start_epoch, lt=window_end_epoch),
                    )
                ]
            ),
            limit=500,
            with_payload=True,
        )
    except Exception:
        return set()
    return {r.payload["object_id"] for r in records if r.payload}


def _llm_input_from_episodics(
    client: QdrantClient, available_ids: Iterable[str]
) -> list[dict[str, object]]:
    """Hydrate (id, title/content, importance, topics) for the LLM prompt."""
    ids = list(available_ids)
    if not ids:
        return []
    try:
        records, _ = client.scroll(
            collection_name="musubi_episodic",
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="object_id", match=models.MatchAny(any=ids))]
            ),
            limit=len(ids),
            with_payload=True,
        )
    except Exception:
        return []
    out: list[dict[str, object]] = []
    for rec in records:
        if not rec.payload:
            continue
        out.append(
            {
                "id": rec.payload["object_id"],
                "content": rec.payload.get("content", ""),
                "importance": int(rec.payload.get("importance", 5)),
                "topics": list(rec.payload.get("linked_to_topics", [])),
            }
        )
    return out


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Quiet the "Iterable is unused at runtime" hint — it's used in the
# parameter annotation above.
_ = (UTC, Any)


__all__ = [
    "ReflectionConfig",
    "ReflectionLLM",
    "ReflectionResult",
    "ThoughtEmitter",
    "VaultWriter",
    "default_reflection_llm",
    "default_thought_emitter",
    "default_vault_writer",
    "render_frontmatter",
    "render_markdown",
    "run_reflection_sweep",
    "validate_cited_ids",
    "vault_path_for",
]
