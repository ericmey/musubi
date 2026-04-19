"""Test contract for slice-lifecycle-reflection.

Implements the Test Contract bullets from
[[06-ingestion/reflection]] § Test contract. Every bullet is in one of
the three Closure states:

- a passing test whose name transcribes the bullet text verbatim, OR
- ``@pytest.mark.skip(reason=...)`` pointing at a named follow-up, OR
- declared out-of-scope in
  ``docs/architecture/_slices/slice-lifecycle-reflection.md`` ``## Work
  log`` (the two integration bullets — the in-case-form bullets they
  cover are exercised here as unit tests).

Runs against an in-memory Qdrant (``QdrantClient(":memory:")``), a
deterministic :class:`FakeEmbedder`, plus three in-process protocol
fakes (vault writer, thought emitter, reflection LLM) so the sweep
runs end-to-end without touching the filesystem, the Obsidian vault,
the thoughts plane, or a real Ollama.

Architecture notes:

- The reflection writes a curated row via the canonical
  :class:`musubi.planes.curated.CuratedPlane.create` surface (no direct
  ``set_payload``). The vault filesystem write goes through a
  :class:`VaultWriter` Protocol whose production wiring is owned by
  ``slice-vault-sync``; the loud-failure stub is shipped here per the
  ADR-punted-deps-fail-loud rule.
- Thought emission to operator presences goes through a
  :class:`ThoughtEmitter` Protocol; production wiring is the thoughts
  plane's create surface (slice-plane-thoughts, in-review).
- The patterns LLM call goes through a :class:`ReflectionLLM` Protocol
  with the same loud-failure default — a future ``slice-llm-client``
  satisfies all three lifecycle-side LLM Protocols
  (``OllamaClient`` from maturation, ``ReflectionLLM`` from here, and
  whatever synthesis defines next).
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.lifecycle import LifecycleEventSink
from musubi.lifecycle.reflection import (
    ReflectionConfig,
    ReflectionLLM,
    ReflectionResult,
    ThoughtEmitter,
    VaultWriter,
    render_frontmatter,
    render_markdown,
    run_reflection_sweep,
    validate_cited_ids,
    vault_path_for,
)
from musubi.planes.curated import CuratedPlane
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.types.curated import CuratedKnowledge
from musubi.types.episodic import EpisodicMemory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def episodic(qdrant: QdrantClient) -> EpisodicPlane:
    return EpisodicPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def curated(qdrant: QdrantClient) -> CuratedPlane:
    return CuratedPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def sink(tmp_path: Path) -> Iterator[LifecycleEventSink]:
    s = LifecycleEventSink(
        db_path=tmp_path / "events.db", flush_every_n=10, flush_every_s=1.0
    )
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 4, 17, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def reflection_namespace() -> str:
    return "eric/lifecycle-worker/curated"


@pytest.fixture
def episodic_namespace() -> str:
    return "eric/claude-code/episodic"


# ---------------------------------------------------------------------------
# Protocol fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeVaultWriter:
    """Records every ``write_reflection`` call so tests can assert on path
    + frontmatter + body without touching disk."""

    writes: list[tuple[str, str, str]] = field(default_factory=list)

    async def write_reflection(self, *, path: str, frontmatter: str, body: str) -> None:
        self.writes.append((path, frontmatter, body))


@dataclass
class FakeThoughtEmitter:
    """Records every ``emit`` call. Production wiring is the thoughts plane."""

    emissions: list[dict[str, object]] = field(default_factory=list)

    async def emit(
        self,
        *,
        namespace: str,
        channel: str,
        content: str,
        importance: int,
    ) -> None:
        self.emissions.append(
            {
                "namespace": namespace,
                "channel": channel,
                "content": content,
                "importance": importance,
            }
        )


@dataclass
class FakeReflectionLLM:
    """Returns either a canned patterns markdown or ``None`` (outage)."""

    available: bool = True
    canned_patterns: str = (
        "## CUDA host work\n"
        "Three sessions touched GPU setup. Cited: {first_id}.\n\n"
        "## Curated note edits\n"
        "Two notes refined. Cited: {second_id}.\n"
    )
    citations: list[str] = field(default_factory=list)
    calls: list[list[dict[str, object]]] = field(default_factory=list)

    async def summarize_patterns(
        self, items: list[dict[str, object]]
    ) -> str | None:
        self.calls.append(list(items))
        if not self.available:
            return None
        # Render with whatever ids the caller fed in so tests can verify
        # cited-id validation against real episodic rows.
        if self.citations and "{first_id}" in self.canned_patterns:
            return self.canned_patterns.format(
                first_id=self.citations[0],
                second_id=self.citations[1] if len(self.citations) > 1 else self.citations[0],
            )
        return self.canned_patterns


# Sanity: each fake satisfies its Protocol.
_v: VaultWriter = FakeVaultWriter()
_t: ThoughtEmitter = FakeThoughtEmitter()
_l: ReflectionLLM = FakeReflectionLLM()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_episodic_in_window(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    namespace: str,
    *,
    content: str,
    importance: int = 5,
    when: datetime,
) -> EpisodicMemory:
    """Create an episodic and force its ``created_epoch`` to land in the
    test's reflection window. The sweep itself never back-dates rows —
    this is a test fixture only."""
    saved = await plane.create(
        EpisodicMemory(namespace=namespace, content=content, importance=importance)
    )
    from qdrant_client import models as qmodels

    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={
            "created_at": when.isoformat(),
            "created_epoch": when.timestamp(),
            "updated_at": when.isoformat(),
            "updated_epoch": when.timestamp(),
        },
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=saved.object_id)
                )
            ]
        ),
    )
    refreshed = await plane.get(namespace=namespace, object_id=saved.object_id)
    assert refreshed is not None
    return refreshed


def _config(**overrides: object) -> ReflectionConfig:
    base: dict[str, object] = {
        "revisit_min_importance": 8,
        "revisit_min_age_days": 30,
        "at_risk_importance_max": 4,
        "at_risk_age_days_min": 30,
    }
    base.update(overrides)
    return ReflectionConfig(**base)  # type: ignore[arg-type]


async def _run(
    *,
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    vault: FakeVaultWriter,
    thoughts: FakeThoughtEmitter,
    llm: FakeReflectionLLM | None = None,
    namespace: str = "eric/lifecycle-worker/curated",
    now: datetime,
    config: ReflectionConfig | None = None,
) -> ReflectionResult:
    return await run_reflection_sweep(
        qdrant=qdrant,
        sink=sink,
        curated_plane=curated,
        vault=vault,
        thoughts=thoughts,
        llm=llm or FakeReflectionLLM(),
        namespace=namespace,
        now=now,
        config=config or _config(),
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


async def test_capture_summary_counts_correct(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    episodic: EpisodicPlane,
    now: datetime,
    episodic_namespace: str,
) -> None:
    """Bullet 1 — the capture-summary section reports correct counts of new
    episodic captures inside the 24h window."""
    in_window_a = now - timedelta(hours=2)
    in_window_b = now - timedelta(hours=10)
    out_of_window = now - timedelta(hours=30)

    await _seed_episodic_in_window(
        episodic, qdrant, episodic_namespace, content="recent-1", when=in_window_a
    )
    await _seed_episodic_in_window(
        episodic, qdrant, episodic_namespace, content="recent-2", when=in_window_b
    )
    await _seed_episodic_in_window(
        episodic, qdrant, episodic_namespace, content="too-old", when=out_of_window
    )

    vault = FakeVaultWriter()
    thoughts = FakeThoughtEmitter()
    result = await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=thoughts,
        now=now,
    )

    assert result.sections["capture"] == 2
    # Body actually says "2 new episodic captures".
    body = vault.writes[-1][2]
    assert "2 new episodic captures" in body or "2 episodic captures" in body


async def test_patterns_section_parses_llm_output(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    episodic: EpisodicPlane,
    now: datetime,
    episodic_namespace: str,
) -> None:
    """Bullet 2 — when the LLM returns valid markdown, it lands verbatim
    inside the rendered patterns section (after citation validation)."""
    seeded = await _seed_episodic_in_window(
        episodic,
        qdrant,
        episodic_namespace,
        content="recent-cita",
        when=now - timedelta(hours=2),
    )
    llm = FakeReflectionLLM(
        canned_patterns=(
            "## CUDA work\n"
            f"Three sessions. Cited: {seeded.object_id}.\n"
        )
    )
    vault = FakeVaultWriter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        llm=llm,
        now=now,
    )
    body = vault.writes[-1][2]
    assert "## CUDA work" in body
    assert "Three sessions" in body
    assert seeded.object_id in body


async def test_patterns_section_validates_cited_ids(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    episodic: EpisodicPlane,
    now: datetime,
    episodic_namespace: str,
) -> None:
    """Bullet 3 — IDs the LLM cites that don't exist in the episodic
    plane are stripped from the rendered output (or marked as
    unverified). Real IDs survive."""
    real = await _seed_episodic_in_window(
        episodic,
        qdrant,
        episodic_namespace,
        content="real-citation",
        when=now - timedelta(hours=3),
    )
    fake_id = "0" * 27
    llm = FakeReflectionLLM(
        canned_patterns=f"## Theme\nReal: {real.object_id}. Fake: {fake_id}.\n"
    )
    vault = FakeVaultWriter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        llm=llm,
        now=now,
    )
    body = vault.writes[-1][2]
    assert real.object_id in body
    # The fake id is either stripped entirely or surrounded with an
    # "(unverified)" marker — both are acceptable validations.
    if fake_id in body:
        assert "unverified" in body.lower()


async def test_promotion_section_lists_both_promoted_and_skipped(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    now: datetime,
) -> None:
    """Bullet 4 — the promotion section lists rows promoted in the window,
    plus rows that *passed the promotion gate but were skipped*. The
    skipped-side data needs a source the lifecycle-promotion slice will
    own; today the renderer simply lists what's queryable + a header for
    the (currently empty) skipped list — both lists are present."""
    # Seed one concept-promotion event in the window.
    from musubi.types.lifecycle_event import LifecycleEvent

    promoted_event = LifecycleEvent(
        object_id="P" * 27,
        object_type="concept",
        namespace="eric/lifecycle-worker/concept",
        from_state="matured",
        to_state="promoted",
        actor="lifecycle-promotion",
        reason="gate-passed",
        occurred_at=now - timedelta(hours=4),
    )
    sink.record(promoted_event)
    sink.flush()

    vault = FakeVaultWriter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        now=now,
    )
    body = vault.writes[-1][2]
    assert "## Promotion candidates" in body
    assert "Promoted" in body
    assert "Skipped" in body  # header is present even when list is empty
    assert promoted_event.object_id in body


async def test_demotion_section_includes_at_risk(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    episodic: EpisodicPlane,
    now: datetime,
    episodic_namespace: str,
) -> None:
    """Bullet 5 — the demotion section lists rows demoted in the window
    PLUS a heuristic "at-risk" list (low importance + long inactivity)."""
    from musubi.types.lifecycle_event import LifecycleEvent

    demoted_event = LifecycleEvent(
        object_id="D" * 27,
        object_type="episodic",
        namespace=episodic_namespace,
        from_state="matured",
        to_state="demoted",
        actor="lifecycle-worker",
        reason="maturation-demotion",
        occurred_at=now - timedelta(hours=2),
    )
    sink.record(demoted_event)
    sink.flush()

    # Seed a matured episodic with low importance and old last-touch.
    at_risk = await episodic.create(
        EpisodicMemory(
            namespace=episodic_namespace, content="risky", importance=2
        )
    )
    await episodic.transition(
        namespace=episodic_namespace,
        object_id=at_risk.object_id,
        to_state="matured",
        actor="seed",
        reason="seed",
    )
    from qdrant_client import models as qmodels

    backdate = now - timedelta(days=60)
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={
            "updated_at": backdate.isoformat(),
            "updated_epoch": backdate.timestamp(),
        },
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=at_risk.object_id)
                )
            ]
        ),
    )

    vault = FakeVaultWriter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        now=now,
    )
    body = vault.writes[-1][2]
    assert "## Demotion candidates" in body
    assert demoted_event.object_id in body
    assert "at-risk" in body.lower() or "at risk" in body.lower()
    assert at_risk.object_id in body


async def test_contradiction_section_separates_new_and_resolved(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    now: datetime,
) -> None:
    """Bullet 6 — the contradiction section has two sub-lists, ``new`` and
    ``resolved``, even when one of the sources is empty."""
    vault = FakeVaultWriter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        now=now,
    )
    body = vault.writes[-1][2]
    assert "## Contradictions" in body
    assert "New" in body
    assert "Resolved" in body


async def test_revisit_section_filters_by_importance_and_age(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    now: datetime,
    reflection_namespace: str,
) -> None:
    """Bullet 7 — the revisit section lists curated rows that meet both
    the importance threshold and the age-since-last-access threshold."""
    import hashlib as _h

    def _hash(s: str) -> str:
        return _h.sha256(s.encode()).hexdigest()

    # High-importance, long-untouched curated row → SHOULD appear.
    high = await curated.create(
        CuratedKnowledge(
            namespace=reflection_namespace,
            title="Project ship dates",
            content="Important milestones for the team.",
            importance=9,
            vault_path="curated/eric/projects/ship-dates.md",
            body_hash=_hash("Important milestones for the team."),
        )
    )
    # Low importance — should NOT appear even if old.
    low = await curated.create(
        CuratedKnowledge(
            namespace=reflection_namespace,
            title="Trivia",
            content="Some inconsequential note.",
            importance=3,
            vault_path="curated/eric/trivia.md",
            body_hash=_hash("Some inconsequential note."),
        )
    )
    # Recent access on the high-importance one would disqualify it; we
    # leave last_accessed_at as None which the sweep treats as "never
    # accessed" → eligible.

    vault = FakeVaultWriter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        now=now,
    )
    body = vault.writes[-1][2]
    assert "## Worth revisiting" in body
    assert high.object_id in body
    assert low.object_id not in body


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


async def test_file_written_at_expected_path(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    now: datetime,
) -> None:
    """Bullet 8 — the rendered file path is ``vault/reflections/YYYY-MM/YYYY-MM-DD.md``."""
    vault = FakeVaultWriter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        now=now,
    )
    assert len(vault.writes) == 1
    path, _, _ = vault.writes[0]
    assert path == "vault/reflections/2026-04/2026-04-17.md"


async def test_frontmatter_has_musubi_managed_true(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    now: datetime,
) -> None:
    """Bullet 9 — the rendered file frontmatter carries
    ``musubi-managed: true`` (Musubi authored, vault-sync writer permits
    system writes per the curated spec)."""
    vault = FakeVaultWriter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        now=now,
    )
    _, frontmatter, _ = vault.writes[0]
    assert "musubi-managed: true" in frontmatter
    assert "title:" in frontmatter
    assert "Reflection — 2026-04-17" in frontmatter


async def test_file_indexed_in_musubi_curated(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    now: datetime,
    reflection_namespace: str,
) -> None:
    """Bullet 10 — the reflection writes a row through the canonical
    ``CuratedPlane.create`` surface; it appears in the ``musubi_curated``
    collection with ``topics: [reflection]`` + ``musubi_managed: true``."""
    vault = FakeVaultWriter()
    result = await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        namespace=reflection_namespace,
        now=now,
    )
    fetched = await curated.get(
        namespace=reflection_namespace, object_id=result.object_id
    )
    assert fetched is not None
    assert "reflection" in fetched.topics
    assert fetched.musubi_managed is True
    assert fetched.title == "Reflection — 2026-04-17"


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


async def test_ollama_outage_skips_patterns_section_only(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    episodic: EpisodicPlane,
    now: datetime,
    episodic_namespace: str,
) -> None:
    """Bullet 11 — when the ReflectionLLM is unavailable, the patterns
    section is replaced with the documented skip notice and the rest of
    the file still renders (capture summary, demotion section, etc.)."""
    await _seed_episodic_in_window(
        episodic,
        qdrant,
        episodic_namespace,
        content="captured-during-outage",
        when=now - timedelta(hours=4),
    )
    vault = FakeVaultWriter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        llm=FakeReflectionLLM(available=False),
        now=now,
    )
    _, _, body = vault.writes[0]
    assert "LLM was unavailable at reflection time" in body
    # Other sections still present.
    assert "## Capture summary" in body
    assert "## Demotion candidates" in body
    assert "## Worth revisiting" in body


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_rerun_same_date_overwrites_same_file(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    now: datetime,
    reflection_namespace: str,
) -> None:
    """Bullet 12 — re-running reflection for the same date writes the
    same vault path and the same curated row (dedup-by-vault-path keeps
    a single point in Qdrant). Second run does not duplicate."""
    vault = FakeVaultWriter()
    first = await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        namespace=reflection_namespace,
        now=now,
    )
    second = await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        namespace=reflection_namespace,
        now=now,
    )
    # Same vault path written twice (the writer overwrites; the curated
    # plane dedups by (namespace, vault_path)).
    assert vault.writes[0][0] == vault.writes[1][0]
    # Curated plane returns the same object id on the second run because
    # body_hash matches → idempotent no-op (or supersession with the new
    # row; either way the file path is stable).
    count = qdrant.count(collection_name="musubi_curated", exact=True).count
    assert count == 1, f"expected 1 curated row, got {count}"
    assert first.path == second.path


# ---------------------------------------------------------------------------
# Integration bullets — declared out-of-scope in the slice work log
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="declared out-of-scope in slice work log: 100-memory integration "
    "test; the unit-form bullets it covers (capture/promotion/demotion/file) "
    "are all passing here. Deferred to a follow-up integration suite."
)
def test_integration_seed_100_memories_across_24h_run_reflection_file_exists_sections_populated_point_indexed() -> None:
    pass


@pytest.mark.skip(
    reason="declared out-of-scope in slice work log: real-Ollama integration "
    "test; the in-case-form bullet is exercised by "
    "test_ollama_outage_skips_patterns_section_only. Deferred."
)
def test_integration_llm_outage_scenario_file_generated_with_patterns_skipped_notice() -> None:
    pass


# ---------------------------------------------------------------------------
# Coverage tests — pure helpers + loud-failure stubs.
# ---------------------------------------------------------------------------


def test_vault_path_for_renders_year_month_day() -> None:
    when = datetime(2026, 1, 5, 6, 0, 0, tzinfo=UTC)
    assert vault_path_for(when) == "vault/reflections/2026-01/2026-01-05.md"


def test_render_frontmatter_contains_required_fields() -> None:
    fm = render_frontmatter(
        date=datetime(2026, 4, 17, 6, 0, 0, tzinfo=UTC),
        object_id="A" * 27,
        namespace="eric/lifecycle-worker/curated",
    )
    assert "object_id: AAAAAAAAAAAAAAAAAAAAAAAAAAA" in fm
    assert "namespace: eric/lifecycle-worker/curated" in fm
    assert "topics:" in fm
    assert "reflection" in fm
    assert "musubi-managed: true" in fm
    assert "Reflection — 2026-04-17" in fm


def test_render_markdown_assembles_sections_in_order() -> None:
    body = render_markdown(
        date=datetime(2026, 4, 17, 6, 0, 0, tzinfo=UTC),
        capture_summary={"episodic": 5, "artifact": 0, "thought": 1},
        patterns_md="## Theme\nbody",
        promotions=[],
        demotions=[],
        contradictions={"new": [], "resolved": []},
        revisit=[],
    )
    headers = [
        "# Reflection — 2026-04-17",
        "## Capture summary",
        "## Surfaced patterns",
        "## Promotion candidates",
        "## Demotion candidates",
        "## Contradictions",
        "## Worth revisiting",
    ]
    last = -1
    for h in headers:
        idx = body.find(h)
        assert idx > last, f"header {h!r} missing or out-of-order"
        last = idx


def test_validate_cited_ids_strips_unknown() -> None:
    # Two ids cited; only one exists.
    real = "R" * 27
    fake = "0" * 27
    text = f"Theme A — see {real}.\nTheme B — see {fake}.\n"
    cleaned = validate_cited_ids(text, available_ids={real})
    assert real in cleaned
    # Fake id is either gone or annotated.
    assert fake not in cleaned or "unverified" in cleaned.lower()


def test_default_vault_writer_raises_loud() -> None:
    """Production stub fails closed when an unconfigured deployment tries
    to run a reflection sweep — same ADR-punted-deps rule the maturation
    slice's _NotConfiguredOllama satisfies."""
    import asyncio

    from musubi.lifecycle.reflection import _NotConfiguredVaultWriter

    stub = _NotConfiguredVaultWriter()
    with pytest.raises(NotImplementedError, match="VaultWriter"):
        asyncio.run(stub.write_reflection(path="x", frontmatter="x", body="x"))


def test_default_thought_emitter_raises_loud() -> None:
    import asyncio

    from musubi.lifecycle.reflection import _NotConfiguredThoughtEmitter

    stub = _NotConfiguredThoughtEmitter()
    with pytest.raises(NotImplementedError, match="ThoughtEmitter"):
        asyncio.run(
            stub.emit(namespace="ns", channel="c", content="x", importance=5)
        )


def test_default_reflection_llm_raises_loud() -> None:
    import asyncio

    from musubi.lifecycle.reflection import _NotConfiguredReflectionLLM

    stub = _NotConfiguredReflectionLLM()
    with pytest.raises(NotImplementedError, match="ReflectionLLM"):
        asyncio.run(stub.summarize_patterns([]))


def test_default_factories_return_loud_stubs() -> None:
    from musubi.lifecycle.reflection import (
        _NotConfiguredReflectionLLM,
        _NotConfiguredThoughtEmitter,
        _NotConfiguredVaultWriter,
        default_reflection_llm,
        default_thought_emitter,
        default_vault_writer,
    )

    assert isinstance(default_vault_writer(), _NotConfiguredVaultWriter)
    assert isinstance(default_thought_emitter(), _NotConfiguredThoughtEmitter)
    assert isinstance(default_reflection_llm(), _NotConfiguredReflectionLLM)


async def test_run_emits_thought_to_operator_channel(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    now: datetime,
) -> None:
    """Per the spec implementation snippet, the sweep emits a thought
    pointing at the freshly-written reflection file. Verifies the thought
    emitter was invoked with a content string carrying the reflection
    file's vault path."""
    vault = FakeVaultWriter()
    thoughts = FakeThoughtEmitter()
    await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=thoughts,
        now=now,
    )
    assert thoughts.emissions, "no thought was emitted"
    last = thoughts.emissions[-1]
    assert "2026-04-17" in str(last["content"])
    assert last["channel"] == "scheduler"


async def test_capture_summary_is_zero_when_no_recent_episodics(
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    curated: CuratedPlane,
    now: datetime,
) -> None:
    vault = FakeVaultWriter()
    result = await _run(
        qdrant=qdrant,
        sink=sink,
        curated=curated,
        vault=vault,
        thoughts=FakeThoughtEmitter(),
        now=now,
    )
    assert result.sections["capture"] == 0
