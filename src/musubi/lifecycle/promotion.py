"""Promotion sweeps — concept → curated knowledge + vault write.

Scheduled job that identifies eligible synthesized concepts and promotes
them to curated knowledge, rendering a human-readable markdown file via an LLM
and writing it to the Obsidian vault.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, model_validator
from qdrant_client import QdrantClient, models

from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.scheduler import Job, file_lock
from musubi.observability import default_registry
from musubi.planes.concept.plane import ConceptPlane
from musubi.planes.curated.plane import CuratedPlane
from musubi.store.names import collection_for_plane
from musubi.types.common import epoch_of, generate_ksuid, utc_now
from musubi.types.concept import SynthesizedConcept
from musubi.types.curated import CuratedKnowledge
from musubi.vault.frontmatter import CuratedFrontmatter, parse_frontmatter

log = logging.getLogger(__name__)

_LIFECYCLE_ACTOR = "lifecycle-worker"
PROMOTION_REINFORCEMENT_THRESHOLD = 3
PROMOTION_IMPORTANCE_THRESHOLD = 6
PROMOTION_MAX_ATTEMPTS = 3

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


def _instrument_promotion_job[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    @wraps(func)
    async def _wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.monotonic()
        try:
            return await func(*args, **kwargs)
        except Exception:
            _ERRORS.labels(job="promotion").inc()
            raise
        finally:
            _DURATION.labels(job="promotion").observe(time.monotonic() - start)

    return _wrapped


class PromotionRender(BaseModel):
    body: str = Field(min_length=100, max_length=20000)
    wikilinks: list[str]
    sections: list[str]

    @model_validator(mode="after")
    def _validate_content(self) -> PromotionRender:
        if "##" not in self.body and "## " not in self.body:
            raise ValueError("Must contain at least one H2")
        disclaimers = ["as an ai model", "as a language model"]
        lower_body = self.body.lower()
        if any(d in lower_body for d in disclaimers):
            raise ValueError("Found AI disclaimer in body")
        return self


class PromotionLLM(Protocol):
    async def render_curated_markdown(
        self,
        title: str,
        content: str,
        rationale: str,
        top_memories: list[str],
    ) -> PromotionRender: ...


class VaultWriter(Protocol):
    @property
    def vault_root(self) -> Path: ...

    def write_curated(
        self,
        vault_relative_path: str,
        frontmatter: CuratedFrontmatter,
        body: str,
    ) -> Path: ...


class ThoughtEmitter(Protocol):
    async def emit(self, channel: str, content: str, title: str | None = None) -> None: ...


class _NotConfiguredPromotionLLM:
    async def render_curated_markdown(
        self,
        title: str,
        content: str,
        rationale: str,
        top_memories: list[str],
    ) -> PromotionRender:
        raise NotImplementedError("PromotionLLM not configured (ADR punted dep)")


class _NotConfiguredVaultWriter:
    @property
    def vault_root(self) -> Path:
        raise NotImplementedError("VaultWriter not configured")

    def write_curated(
        self,
        vault_relative_path: str,
        frontmatter: CuratedFrontmatter,
        body: str,
    ) -> Path:
        raise NotImplementedError("VaultWriter not configured (ADR punted dep)")


class _NotConfiguredThoughtEmitter:
    async def emit(self, channel: str, content: str, title: str | None = None) -> None:
        raise NotImplementedError("ThoughtEmitter not configured (ADR punted dep)")


@dataclass
class PromotionDeps:
    qdrant: QdrantClient
    concept_plane: ConceptPlane
    curated_plane: CuratedPlane
    events: LifecycleEventSink
    llm: PromotionLLM = field(default_factory=_NotConfiguredPromotionLLM)
    vault_writer: VaultWriter = field(default_factory=_NotConfiguredVaultWriter)
    thoughts: ThoughtEmitter = field(default_factory=_NotConfiguredThoughtEmitter)


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def namespace_to_dir(namespace: str) -> str:
    parts = namespace.split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return namespace.replace("/", "_")


def compute_path(concept: SynthesizedConcept) -> str:
    # Topic-hint unification: prefer `concept.topics` (populated by the
    # synthesis job from the cluster); fall back to `linked_to_topics`
    # (inherited from the originating memories) when topics is empty.
    # `linked_to_topics` exists on every MemoryObject, so older concepts
    # synthesized before `topics` was populated reliably still get a
    # meaningful primary_topic from their source memories. Concepts with
    # neither file under `_misc` — deliberately not auto-synthesized
    # because that'd hide a real synthesis gap.
    topics = concept.topics or concept.linked_to_topics
    # Slugify topic-derived segments — topics can carry anything an LLM
    # hallucinates (path separators, `..`, spaces). `slugify` reduces
    # to `[a-z0-9-]`, making path traversal impossible at this layer.
    # `VaultWriter.write_curated` also rejects paths that escape
    # vault_root as defense-in-depth. When no topic is available, fall
    # back to the `_misc` sentinel literal — preserves the leading
    # underscore so synthesis gaps are visible on disk.
    primary_topic = (slugify(topics[0]) or "_misc") if topics else "_misc"
    slug = slugify(concept.title)
    return f"curated/{namespace_to_dir(concept.namespace)}/{primary_topic}/{slug}.md"


def _is_eligible(concept: SynthesizedConcept, now_epoch: float) -> bool:
    if concept.state != "matured":
        return False
    if concept.reinforcement_count < PROMOTION_REINFORCEMENT_THRESHOLD:
        return False
    if concept.importance < PROMOTION_IMPORTANCE_THRESHOLD:
        return False

    created_epoch = concept.created_epoch or epoch_of(concept.created_at)
    if now_epoch - created_epoch < 48 * 3600:
        return False

    # Three-strikes gate. Every `ConceptPlane.record_promotion_rejection`
    # bumps `promotion_attempts`, so after three failed renders a concept
    # is locked out of further sweeps until an operator reinstates it.
    if concept.promotion_attempts >= PROMOTION_MAX_ATTEMPTS:
        return False

    if concept.contradicts:
        return False
    return concept.promoted_to is None


@_instrument_promotion_job
async def run_promotion_sweep(
    deps: PromotionDeps,
    batch_size: int = 1,
) -> int:
    """Sweep for mature concepts that meet the promotion gate."""
    promoted_count = 0
    now_epoch = epoch_of(utc_now())

    coll_name = collection_for_plane("concept")

    must_conditions: list[Any] = [
        models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
        models.FieldCondition(
            key="reinforcement_count", range=models.Range(gte=PROMOTION_REINFORCEMENT_THRESHOLD)
        ),
        models.FieldCondition(
            key="importance", range=models.Range(gte=PROMOTION_IMPORTANCE_THRESHOLD)
        ),
        # Three-strikes filter. Skips the payload-parse + `_is_eligible`
        # call on concepts already locked out by prior rejections.
        models.FieldCondition(
            key="promotion_attempts", range=models.Range(lt=PROMOTION_MAX_ATTEMPTS)
        ),
    ]

    offset = None
    while True:
        resp = deps.qdrant.scroll(
            collection_name=coll_name,
            scroll_filter=models.Filter(must=must_conditions),
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, offset = resp[0], resp[1]

        for point in points:
            if not point.payload:
                continue

            try:
                concept = SynthesizedConcept.model_validate(point.payload)
            except Exception as e:
                log.error("Failed to parse concept %s: %s", point.id, e)
                continue

            if not _is_eligible(concept, now_epoch):
                continue

            try:
                if await _promote_concept(deps, concept):
                    promoted_count += 1
            except Exception as e:
                log.error("Failed to promote concept %s: %s", concept.object_id, e, exc_info=True)

            if promoted_count >= batch_size:
                return promoted_count

        if not offset:
            break

    return promoted_count


async def _promote_concept(deps: PromotionDeps, concept: SynthesizedConcept) -> bool:
    """Attempt to promote one concept. Returns True iff it actually shipped
    a curated row + vault file.

    Any failure — render, vault write, curated-plane create, concept
    transition, thought emit — records a rejection via
    :meth:`ConceptPlane.record_promotion_rejection`, which bumps
    `promotion_attempts`. The three-strikes gate then locks the concept
    out after three consecutive failures. Transient infra issues (e.g.
    Qdrant briefly down) do burn an attempt — accepted cost for making
    a genuinely broken concept stop poisoning every sweep."""
    now = utc_now()

    # Render markdown via LLM
    try:
        render = await deps.llm.render_curated_markdown(
            title=concept.title,
            content=concept.content,
            rationale=concept.synthesis_rationale,
            top_memories=[],
        )
    except Exception as e:
        log.warning("Rendering failed for concept %s: %s", concept.object_id, e)
        await _record_rejection(deps, concept, reason=f"Rendering failed: {e}")
        return False

    # Post-render path. Any exception below bumps promotion_attempts via
    # `record_promotion_rejection` so a broken concept can't poison every
    # sweep indefinitely; the three-strikes gate will lock it out for
    # operator attention after three consecutive failures.
    try:
        rel_path = compute_path(concept)

        # Check conflict
        full_path = deps.vault_writer.vault_root / rel_path
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8")
                data, _ = parse_frontmatter(content)
                fm = CuratedFrontmatter.model_validate(data)

                if fm.musubi_managed and fm.promoted_from == concept.object_id:
                    # Idempotent rewrite allowed
                    pass
                elif fm.musubi_managed and fm.promoted_from != concept.object_id:
                    # Sibling
                    slug = slugify(concept.title)
                    rel_path = rel_path.replace(f"{slug}.md", f"{slug}-v2.md")
                    await deps.thoughts.emit(
                        "ops-alerts",
                        f"Path conflict handled with sibling: {rel_path}",
                        "Path Conflict",
                    )
                else:
                    # Human authored
                    slug = slugify(concept.title)
                    short_id = str(concept.object_id)[:8]
                    rel_path = rel_path.replace(f"{slug}.md", f"{slug}-promoted-{short_id}.md")
                    await deps.thoughts.emit(
                        "ops-alerts",
                        f"Path conflict with human file handled with sibling: {rel_path}",
                        "Path Conflict",
                    )

            except Exception as e:
                log.warning("Path conflict resolution failed for %s: %s", rel_path, e)

        curated_id = generate_ksuid()

        fm_obj = CuratedFrontmatter(  # type: ignore
            object_id=curated_id,
            namespace=concept.namespace,
            title=concept.title,
            topics=concept.topics or concept.linked_to_topics,
            tags=concept.tags,
            importance=concept.importance,
            state="matured",
            musubi_managed=True,
            created=now,
            updated=now,
            promoted_from=concept.object_id,
            promoted_at=now,
        )

        # Write to vault
        deps.vault_writer.write_curated(rel_path, fm_obj, render.body)

        # Create Qdrant point
        body_hash = hashlib.sha256(render.body.encode("utf-8")).hexdigest()
        memory = CuratedKnowledge(
            object_id=curated_id,
            namespace=concept.namespace,
            vault_path=rel_path,
            body_hash=body_hash,
            title=concept.title,
            content=render.body,
            summary=concept.summary,
            state="matured",
            importance=concept.importance,
            topics=concept.topics or concept.linked_to_topics,
            tags=concept.tags,
            promoted_from=concept.object_id,
            promoted_at=now,
        )

        await deps.curated_plane.create(memory)

        # Transition concept
        await deps.concept_plane.transition(
            namespace=concept.namespace,
            object_id=concept.object_id,
            to_state="promoted",
            actor=_LIFECYCLE_ACTOR,
            reason="lifecycle-promotion-sweep",
            promoted_to=curated_id,
            promoted_at=now,
        )

        # Notification Thought
        await deps.thoughts.emit(
            "ops-alerts",
            f"Promoted concept '{concept.title}' to {rel_path}. Please review.",
            "Concept Promoted",
        )
        return True
    except Exception as e:
        log.warning("Post-render promotion failed for concept %s: %s", concept.object_id, e)
        await _record_rejection(deps, concept, reason=f"Post-render failed: {e}")
        return False


async def _record_rejection(
    deps: PromotionDeps, concept: SynthesizedConcept, *, reason: str
) -> None:
    """Record a promotion rejection (bumps attempts); swallow if the
    rejection-write itself fails so the sweep keeps going.

    `record_promotion_rejection` touches Qdrant too — if the
    underlying failure was Qdrant being unreachable, this write will
    fail as well. We log and move on rather than propagate, so a single
    infra blip doesn't crash the whole sweep.
    """
    try:
        await deps.concept_plane.record_promotion_rejection(
            namespace=concept.namespace,
            object_id=concept.object_id,
            reason=reason,
        )
    except Exception as e:
        log.error(
            "record_promotion_rejection also failed for %s: %s",
            concept.object_id,
            e,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


def build_promotion_jobs(
    *,
    deps: PromotionDeps,
    lock_dir: Path,
    batch_size: int = 1,
) -> list[Job]:
    """Return the one-element Job list matching
    :func:`musubi.lifecycle.scheduler.build_default_jobs`'s ``promotion``
    entry (daily at 04:00 UTC).

    ``lock_dir/promotion.lock`` serialises the sweep against any other
    worker attempting the same promotion pass. ``batch_size`` defaults
    to 1 — we'd rather promote slowly and give the human reviewer time
    to notice than fire-hose a queue on first deploy.
    """
    import asyncio as _asyncio

    lock_path = lock_dir / "promotion.lock"

    async def _run_all() -> None:
        try:
            count = await run_promotion_sweep(deps, batch_size=batch_size)
            log.info("promotion-done promoted=%d", count)
        except Exception:
            log.exception("promotion-failed")

    def _runner() -> None:
        with file_lock(lock_path) as acquired:
            if not acquired:
                log.info("lifecycle-job=promotion lock-held; skipping run")
                return
            _asyncio.run(_run_all())

    return [
        Job(
            name="promotion",
            trigger_kind="cron",
            trigger_kwargs={"hour": 4, "minute": 0},
            func=_runner,
            grace_time_s=3600,
        ),
    ]


__all__ = [
    "PromotionDeps",
    "PromotionLLM",
    "PromotionRender",
    "ThoughtEmitter",
    "VaultWriter",
    "build_promotion_jobs",
    "compute_path",
    "run_promotion_sweep",
    "slugify",
]
