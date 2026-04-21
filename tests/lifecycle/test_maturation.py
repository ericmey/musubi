"""Test contract for slice-lifecycle-maturation.

Implements the Test Contract bullets from
[[06-ingestion/maturation]] § Test contract. Every bullet is one of:

- a passing test whose name transcribes the bullet text verbatim, OR
- ``@pytest.mark.skip(reason=...)`` pointing at the named follow-up slice, OR
- declared out-of-scope in
  ``docs/Musubi/_slices/slice-lifecycle-maturation.md`` ``## Work log``
  (for the two hypothesis bullets and two integration bullets).

Runs against an in-memory Qdrant (``QdrantClient(":memory:")``), a deterministic
:class:`FakeEmbedder`, an in-process :class:`FakeOllama`, and on-disk sqlite
under ``tmp_path``. No network, no real LLM calls.

Architecture notes:

- Every state mutation routes through
  :func:`musubi.lifecycle.transitions.transition` — not direct
  ``client.set_payload``. We assert that by reading back the
  :class:`LifecycleEventSink`: every ``maturation-sweep`` /
  ``provisional-ttl`` / ``maturation-demotion`` reason is paired with a
  ledger entry.
- Enrichment fields (``importance``, ``tags``, ``linked_to_topics``) are
  applied via Qdrant ``set_payload`` with the same point id *after* the
  state transition succeeds. They are not state mutations, so they don't
  warrant a separate ledger entry — but they do bundle into the same
  per-object sweep step, so a partial failure (Ollama down) is observable
  by the post-sweep payload.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.lifecycle import LifecycleEventSink, file_lock
from musubi.lifecycle.maturation import (
    DEFAULT_TAG_ALIASES,
    MaturationConfig,
    MaturationCursor,
    OllamaClient,
    OllamaImportance,
    OllamaTopic,
    detect_supersession_hint,
    episodic_maturation_sweep,
    normalize_tags,
    provisional_ttl_sweep,
)
from musubi.observability import default_registry, render_text_format
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
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
def plane(qdrant: QdrantClient) -> EpisodicPlane:
    return EpisodicPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def ns() -> str:
    return "eric/claude-code/episodic"


@pytest.fixture
def sink(tmp_path: Path) -> Iterator[LifecycleEventSink]:
    s = LifecycleEventSink(db_path=tmp_path / "events.db", flush_every_n=10, flush_every_s=1.0)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def cursor(tmp_path: Path) -> MaturationCursor:
    return MaturationCursor(db_path=tmp_path / "cursor.db")


# ---------------------------------------------------------------------------
# Fake OllamaClient — deterministic, no network
# ---------------------------------------------------------------------------


class FakeOllama:
    """Deterministic in-process ``OllamaClient`` stand-in.

    - ``score_importance`` returns a constant ``importance`` per call (set in
      the constructor) for every item, unless ``available=False`` in which
      case it returns ``None`` (the spec's outage signal).
    - ``infer_topics`` returns the configured ``topics_for(content)`` map, or
      ``[]`` for any content the test didn't explicitly map.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        importance: int = 8,
        topic_map: dict[str, list[str]] | None = None,
    ) -> None:
        self.available = available
        self.importance = importance
        self.topic_map = topic_map or {}
        self.score_calls: list[list[OllamaImportance]] = []
        self.topic_calls: list[list[OllamaTopic]] = []

    async def score_importance(self, items: list[OllamaImportance]) -> dict[str, int] | None:
        self.score_calls.append(list(items))
        if not self.available:
            return None
        return {item.object_id: self.importance for item in items}

    async def infer_topics(self, items: list[OllamaTopic]) -> dict[str, list[str]] | None:
        self.topic_calls.append(list(items))
        if not self.available:
            return None
        return {item.object_id: self.topic_map.get(item.content, []) for item in items}


# Sanity: FakeOllama satisfies the OllamaClient Protocol.
_: OllamaClient = FakeOllama()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_provisional(
    plane: EpisodicPlane,
    ns: str,
    *,
    content: str,
    age_seconds: int = 7200,
    tags: list[str] | None = None,
) -> EpisodicMemory:
    """Create a provisional row, then back-date its ``created_epoch`` so the
    selection cutoff (`now - min_age_sec`) sees it.

    The plane's ``create()`` always stamps ``now`` for the timestamps, so
    the only way to simulate age in a unit test is to overwrite the
    payload after creation. This is a *test-fixture* concern only — the
    sweep itself never touches Qdrant directly outside the canonical
    transition primitive.
    """
    saved = await plane.create(EpisodicMemory(namespace=ns, content=content, tags=tags or []))
    backdate = datetime.now(UTC) - timedelta(seconds=age_seconds)
    epoch = backdate.timestamp()
    from qdrant_client import models as qmodels

    plane._client.set_payload(
        collection_name="musubi_episodic",
        payload={
            "created_at": backdate.isoformat(),
            "created_epoch": epoch,
            "updated_at": backdate.isoformat(),
            "updated_epoch": epoch,
        },
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=saved.object_id)
                )
            ]
        ),
    )
    refreshed = await plane.get(namespace=ns, object_id=saved.object_id)
    assert refreshed is not None
    return refreshed


def _config(**overrides: object) -> MaturationConfig:
    base = {
        "min_age_sec": 3600,
        "batch_size": 500,
        "provisional_ttl_sec": 7 * 86400,
        "importance_reenrich_age_sec": 7 * 86400,
        "demotion_inactivity_sec": 30 * 86400,
        "concept_min_age_sec": 24 * 3600,
        "concept_reinforcement_threshold": 3,
        "tag_aliases": dict(DEFAULT_TAG_ALIASES),
    }
    base.update(overrides)
    return MaturationConfig(**base)  # type: ignore[arg-type]


def _duration_count(job: str) -> int:
    text = render_text_format(default_registry())
    prefix = f'musubi_lifecycle_job_duration_seconds_count{{job="{job}"}} '
    for line in text.splitlines():
        if line.startswith(prefix):
            return int(line.removeprefix(prefix))
    return 0


def _read_state(plane: EpisodicPlane, ns: str, object_id: str) -> str | None:
    mem = asyncio.get_event_loop().run_until_complete(plane.get(namespace=ns, object_id=object_id))
    return mem.state if mem else None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


async def test_selects_only_provisional_older_than_min_age(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 1 — only ``provisional`` rows older than ``min_age_sec`` are
    eligible. Younger rows and non-provisional rows are skipped."""
    too_young = await plane.create(EpisodicMemory(namespace=ns, content="too-young"))
    eligible = await _seed_provisional(plane, ns, content="ready-to-mature", age_seconds=7200)
    # A pre-matured row must not be re-touched.
    pre_matured = await plane.create(EpisodicMemory(namespace=ns, content="already-matured"))
    await plane.transition(
        namespace=ns,
        object_id=pre_matured.object_id,
        to_state="matured",
        actor="test",
        reason="seed",
    )

    report = await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(),
        cursor=cursor,
        config=_config(min_age_sec=3600),
    )

    assert report.transitioned == 1
    assert (await plane.get(namespace=ns, object_id=eligible.object_id)).state == "matured"  # type: ignore[union-attr]
    assert (await plane.get(namespace=ns, object_id=too_young.object_id)).state == "provisional"  # type: ignore[union-attr]
    assert (await plane.get(namespace=ns, object_id=pre_matured.object_id)).state == "matured"  # type: ignore[union-attr]


async def test_batch_size_limits_selection(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 2 — at most ``batch_size`` rows are processed per sweep."""
    for i in range(5):
        await _seed_provisional(plane, ns, content=f"batch-fixture-{i}")
    report = await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(),
        cursor=cursor,
        config=_config(batch_size=3),
    )
    assert report.selected <= 3
    assert report.transitioned <= 3


async def test_cursor_resumes_across_runs(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 3 — cursor is persisted to sqlite and the next run resumes."""
    for i in range(4):
        await _seed_provisional(plane, ns, content=f"cursor-fixture-{i}")

    first = await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(),
        cursor=cursor,
        config=_config(batch_size=2),
    )
    assert first.transitioned == 2
    assert first.cursor_advanced_to is not None

    # Second run: a fresh cursor object reading the same db must pick up
    # where the first one left off.
    cursor2 = MaturationCursor(db_path=cursor._db_path)
    second = await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(),
        cursor=cursor2,
        config=_config(batch_size=10),
    )
    # The first batch shouldn't be re-processed; total transitioned across
    # both runs equals the seed count.
    assert first.transitioned + second.transitioned == 4


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


async def test_importance_rescored_via_llm(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 4 — the LLM is asked for an importance score and the payload
    reflects the new value."""
    seeded = await _seed_provisional(plane, ns, content="rescore-me")
    ollama = FakeOllama(importance=9)
    await episodic_maturation_sweep(
        client=qdrant, sink=sink, ollama=ollama, cursor=cursor, config=_config()
    )
    refreshed = await plane.get(namespace=ns, object_id=seeded.object_id)
    assert refreshed is not None
    assert refreshed.importance == 9
    # And the LLM was actually called.
    assert ollama.score_calls, "OllamaClient.score_importance was never invoked"


async def test_importance_fallback_on_ollama_unavailable(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 5 — Ollama down ⇒ keep the captured importance unchanged."""
    seeded = await _seed_provisional(plane, ns, content="ollama-down", tags=[])
    captured_importance = seeded.importance
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(available=False),
        cursor=cursor,
        config=_config(),
    )
    refreshed = await plane.get(namespace=ns, object_id=seeded.object_id)
    assert refreshed is not None
    assert refreshed.importance == captured_importance


def test_tags_normalized_lowercase_and_hyphenated() -> None:
    """Bullet 6 — tag normalization lowercases + converts spaces → hyphens."""
    out = normalize_tags(["GPU Setup", "  CUDA  ", "NVIDIA"], aliases={})
    assert "gpu-setup" in out
    assert "cuda" in out
    assert "nvidia" in out
    assert all(t == t.lower() for t in out)
    assert all(" " not in t for t in out)


def test_tag_aliases_applied() -> None:
    """Bullet 7 — alias dictionary canonicalises known synonyms."""
    aliases = {"nvidia-gpu": "nvidia", "gpu-setup": "gpu"}
    out = normalize_tags(["NVIDIA-GPU", "GPU Setup", "freeform"], aliases=aliases)
    assert "nvidia" in out
    assert "gpu" in out
    assert "freeform" in out
    assert "nvidia-gpu" not in out
    assert "gpu-setup" not in out


def test_tags_deduped() -> None:
    """Bullet 8 — duplicates collapse after normalization + alias rewrite."""
    out = normalize_tags(["nvidia", "NVIDIA", "Nvidia "], aliases={})
    assert out.count("nvidia") == 1


async def test_topics_inferred_from_llm(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 9 — the LLM is asked for topics; the payload reflects them.

    Topics are written to ``linked_to_topics`` (the field that exists on
    ``EpisodicMemory`` via the inherited ``MemoryObject``); see the work
    log entry on the spec/type drift for ``topics`` vs ``linked_to_topics``.
    """
    seeded = await _seed_provisional(plane, ns, content="gpu-setup-content")
    ollama = FakeOllama(topic_map={"gpu-setup-content": ["infrastructure/gpu", "projects/musubi"]})
    await episodic_maturation_sweep(
        client=qdrant, sink=sink, ollama=ollama, cursor=cursor, config=_config()
    )
    refreshed = await plane.get(namespace=ns, object_id=seeded.object_id)
    assert refreshed is not None
    assert "infrastructure/gpu" in refreshed.linked_to_topics
    assert "projects/musubi" in refreshed.linked_to_topics
    assert ollama.topic_calls, "OllamaClient.infer_topics was never invoked"


async def test_topics_empty_on_unknown(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 10 — if no confident topic matches, the field stays empty."""
    seeded = await _seed_provisional(plane, ns, content="content-with-no-known-topic")
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(topic_map={}),
        cursor=cursor,
        config=_config(),
    )
    refreshed = await plane.get(namespace=ns, object_id=seeded.object_id)
    assert refreshed is not None
    assert refreshed.linked_to_topics == []


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


def test_supersession_inferred_from_hint_keyword() -> None:
    """Bullet 11 — content starting with 'Update:' / 'Correction:' /
    'Replacing:' triggers supersession detection."""
    assert detect_supersession_hint("Update: GPU pin moved to driver 575") is True
    assert detect_supersession_hint("Correction: was driver 470 not 460") is True
    assert detect_supersession_hint("Replacing: previous deployment notes") is True
    assert detect_supersession_hint("update: lowercase still triggers") is True


def test_supersession_not_inferred_without_hint() -> None:
    """Bullet 12 — plain content does not trigger supersession detection."""
    assert detect_supersession_hint("Just a regular memory.") is False
    assert detect_supersession_hint("This contains the word update somewhere.") is False
    assert detect_supersession_hint("") is False


async def test_supersession_sets_both_sides_of_link(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 13 — when supersession is inferred, the new row's
    ``supersedes`` and the old row's ``superseded_by`` both get set."""
    # Seed the original "matured" row with content the new one can hint at.
    original = await plane.create(
        EpisodicMemory(namespace=ns, content="GPU pin: nvidia driver 470")
    )
    await plane.transition(
        namespace=ns,
        object_id=original.object_id,
        to_state="matured",
        actor="test",
        reason="seed",
    )
    # New provisional row hints at supersession.
    new_row = await _seed_provisional(
        plane,
        ns,
        content="Update: GPU pin: nvidia driver 470",
    )
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(),
        cursor=cursor,
        config=_config(),
    )
    new_after = await plane.get(namespace=ns, object_id=new_row.object_id)
    old_after = await plane.get(namespace=ns, object_id=original.object_id)
    assert new_after is not None and old_after is not None
    assert original.object_id in new_after.supersedes
    assert old_after.superseded_by == new_row.object_id
    assert old_after.state == "superseded"


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


async def test_state_transitions_to_matured(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 14 — eligible provisional rows reach ``state = "matured"``."""
    seeded = await _seed_provisional(plane, ns, content="state-check")
    await episodic_maturation_sweep(
        client=qdrant, sink=sink, ollama=FakeOllama(), cursor=cursor, config=_config()
    )
    refreshed = await plane.get(namespace=ns, object_id=seeded.object_id)
    assert refreshed is not None
    assert refreshed.state == "matured"


async def test_transition_uses_typed_function(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 15 — the canonical ``transition()`` is the path of record. We
    verify by reading the sink: every state-changed row has a paired event
    with the ``maturation-sweep`` reason and the ``lifecycle-worker`` actor.
    Direct ``client.set_payload`` would not produce a sink entry."""
    seeded = await _seed_provisional(plane, ns, content="typed-transition")
    await episodic_maturation_sweep(
        client=qdrant, sink=sink, ollama=FakeOllama(), cursor=cursor, config=_config()
    )
    sink.flush()
    events = sink.read_all()
    matured_events = [
        e for e in events if e.object_id == seeded.object_id and e.to_state == "matured"
    ]
    assert len(matured_events) == 1
    assert matured_events[0].reason == "maturation-sweep"
    assert matured_events[0].actor == "lifecycle-worker"


async def test_lifecycle_event_emitted(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 16 — at least one ``LifecycleEvent`` lands in the sink per
    transitioned row."""
    seeded = await _seed_provisional(plane, ns, content="ledger-check")
    await episodic_maturation_sweep(
        client=qdrant, sink=sink, ollama=FakeOllama(), cursor=cursor, config=_config()
    )
    sink.flush()
    events = sink.read_all()
    assert any(
        e.object_id == seeded.object_id
        and e.from_state == "provisional"
        and e.to_state == "matured"
        for e in events
    )


async def test_ollama_outage_still_matures_without_enrichment(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Bullet 17 — Ollama down does not block the state transition. Per the
    spec failure mode: an unenriched ``matured`` row beats a stuck
    ``provisional`` one (the next sweep re-enriches)."""
    seeded = await _seed_provisional(plane, ns, content="enrichment-skipped")
    captured_importance = seeded.importance
    captured_topics = list(seeded.linked_to_topics)
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(available=False),
        cursor=cursor,
        config=_config(),
    )
    refreshed = await plane.get(namespace=ns, object_id=seeded.object_id)
    assert refreshed is not None
    assert refreshed.state == "matured"
    # Enrichment fields untouched.
    assert refreshed.importance == captured_importance
    assert refreshed.linked_to_topics == captured_topics


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


async def test_provisional_older_than_7d_archived(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 18 — provisional rows older than the configured TTL are
    archived (not deleted) by the TTL sweep."""
    aged = await _seed_provisional(plane, ns, content="ttl-target", age_seconds=8 * 86400)
    young = await _seed_provisional(plane, ns, content="ttl-young", age_seconds=3600)
    report = await provisional_ttl_sweep(
        client=qdrant,
        sink=sink,
        config=_config(provisional_ttl_sec=7 * 86400),
    )
    assert report.transitioned == 1
    aged_after = await plane.get(namespace=ns, object_id=aged.object_id)
    young_after = await plane.get(namespace=ns, object_id=young.object_id)
    assert aged_after is not None and aged_after.state == "archived"
    assert young_after is not None and young_after.state == "provisional"


async def test_archival_emits_lifecycle_event(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 19 — TTL-driven archival emits a ``LifecycleEvent`` with the
    ``provisional-ttl`` reason."""
    aged = await _seed_provisional(plane, ns, content="ttl-event", age_seconds=8 * 86400)
    await provisional_ttl_sweep(
        client=qdrant,
        sink=sink,
        config=_config(provisional_ttl_sec=7 * 86400),
    )
    sink.flush()
    events = sink.read_all()
    archived = [e for e in events if e.object_id == aged.object_id and e.to_state == "archived"]
    assert len(archived) == 1
    assert archived[0].reason == "provisional-ttl"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_file_lock_prevents_double_execution(tmp_path: Path) -> None:
    """Bullet 20 — concurrent sweep attempts coordinate via a file lock; the
    second attempt observes the lock as taken and skips. Builds on the
    primitive shipped by slice-lifecycle-engine."""
    lock_path = tmp_path / "locks" / "maturation.lock"
    with file_lock(lock_path) as first_acquired:
        assert first_acquired is True
        with file_lock(lock_path, timeout=0.0) as second_acquired:
            assert second_acquired is False
    # After the outer context exits, the lock is releasable again.
    with file_lock(lock_path) as third_acquired:
        assert third_acquired is True


# ---------------------------------------------------------------------------
# Property + integration bullets — declared out-of-scope in the slice
# work log per the Closure Rule's third state.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="declared out-of-scope in slice work log: hypothesis property "
    "(no matured row has created_epoch in the future) requires a property "
    "harness — deferred to a follow-up `test-property-lifecycle` slice."
)
def test_hypothesis_no_matured_memory_has_created_epoch_in_the_future() -> None:
    """Bullet 21 placeholder."""


@pytest.mark.skip(
    reason="declared out-of-scope in slice work log: hypothesis property "
    "(provisional > 7d always archived after one sweep) is exercised by "
    "test_provisional_older_than_7d_archived; full hypothesis run deferred."
)
def test_hypothesis_provisional_memories_older_than_7d_are_always_archived_after_one_sweep() -> (
    None
):
    """Bullet 22 placeholder."""


@pytest.mark.skip(
    reason="declared out-of-scope in slice work log: integration test needs a "
    "real Ollama endpoint — deferred to a follow-up integration suite."
)
def test_integration_real_ollama_50_synthetic_provisional_memories_mature_in_one_sweep() -> None:
    """Bullet 23 placeholder."""


@pytest.mark.skip(
    reason="declared out-of-scope in slice work log: integration test needs a "
    "real Ollama endpoint to exercise the offline path end-to-end — deferred."
)
def test_integration_ollama_offline_scenario_maturation_completes_without_enrichment() -> None:
    """Bullet 24 placeholder."""


# ---------------------------------------------------------------------------
# Coverage tests — not Test Contract bullets, but they exercise the
# OllamaClient stub's loud-failure path + a few edge cases.
# ---------------------------------------------------------------------------


def test_default_ollama_client_raises_loud_when_invoked() -> None:
    """Production stub for ``OllamaClient`` must raise ``NotImplementedError``
    so an unconfigured deployment fails closed (per the ADR-punted-deps
    rule in CLAUDE.md / agent-handoff)."""
    from musubi.lifecycle.maturation import _NotConfiguredOllama

    stub = _NotConfiguredOllama()
    with pytest.raises(NotImplementedError, match="OllamaClient"):
        asyncio.run(stub.score_importance([]))
    with pytest.raises(NotImplementedError, match="OllamaClient"):
        asyncio.run(stub.infer_topics([]))


async def test_sweep_is_no_op_when_no_eligible_rows(
    qdrant: QdrantClient, sink: LifecycleEventSink, cursor: MaturationCursor
) -> None:
    """An empty plane is a clean no-op — no ledger entries, no failures."""
    report = await episodic_maturation_sweep(
        client=qdrant, sink=sink, ollama=FakeOllama(), cursor=cursor, config=_config()
    )
    assert report.selected == 0
    assert report.transitioned == 0


async def test_ttl_sweep_is_no_op_when_nothing_aged(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    await _seed_provisional(plane, ns, content="too-young-for-ttl", age_seconds=3600)
    report = await provisional_ttl_sweep(
        client=qdrant,
        sink=sink,
        config=_config(provisional_ttl_sec=7 * 86400),
    )
    assert report.transitioned == 0


# ---------------------------------------------------------------------------
# Coverage for the scope-extension sweeps + scheduler wiring — not Test
# Contract bullets, but they ship in this slice so coverage clears the
# 85 % gate for src/musubi/lifecycle/**.
# ---------------------------------------------------------------------------


async def test_episodic_demotion_sweep_demotes_inactive_matured_rows(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
) -> None:
    """Matured row whose ``updated_epoch`` is older than the inactivity
    cutoff is demoted; recently-active matured row is left alone."""
    from qdrant_client import models as qmodels

    from musubi.lifecycle.maturation import episodic_demotion_sweep

    inactive = await _seed_provisional(plane, ns, content="demote-target", age_seconds=40 * 86400)
    await plane.transition(
        namespace=ns,
        object_id=inactive.object_id,
        to_state="matured",
        actor="seed",
        reason="seed",
    )
    backdate = datetime.now(UTC) - timedelta(days=40)
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={
            "updated_at": backdate.isoformat(),
            "updated_epoch": backdate.timestamp(),
        },
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=inactive.object_id)
                )
            ]
        ),
    )
    active = await plane.create(EpisodicMemory(namespace=ns, content="still-active"))
    await plane.transition(
        namespace=ns,
        object_id=active.object_id,
        to_state="matured",
        actor="seed",
        reason="seed",
    )

    report = await episodic_demotion_sweep(
        client=qdrant,
        sink=sink,
        config=_config(demotion_inactivity_sec=30 * 86400),
    )
    assert report.transitioned == 1
    refreshed_inactive = await plane.get(namespace=ns, object_id=inactive.object_id)
    refreshed_active = await plane.get(namespace=ns, object_id=active.object_id)
    assert refreshed_inactive is not None and refreshed_inactive.state == "demoted"
    assert refreshed_active is not None and refreshed_active.state == "matured"


async def test_episodic_demotion_sweep_no_op_when_empty(
    qdrant: QdrantClient, sink: LifecycleEventSink
) -> None:
    from musubi.lifecycle.maturation import episodic_demotion_sweep

    report = await episodic_demotion_sweep(client=qdrant, sink=sink, config=_config())
    assert report.selected == 0


async def test_concept_maturation_sweep_promotes_eligible(
    qdrant: QdrantClient, sink: LifecycleEventSink
) -> None:
    """A synthesized concept past the quiet window with sufficient
    reinforcement matures; one below the threshold is left alone."""
    from qdrant_client import models as qmodels

    from musubi.lifecycle.maturation import concept_maturation_sweep
    from musubi.planes.concept import ConceptPlane
    from musubi.types.common import generate_ksuid
    from musubi.types.concept import SynthesizedConcept

    plane = ConceptPlane(client=qdrant, embedder=FakeEmbedder())
    eligible = await plane.create(
        SynthesizedConcept(
            namespace="eric/claude-code/concept",
            title="GPU host pattern",
            content="Pattern across CUDA bumps.",
            synthesis_rationale="Three episodic memories about CUDA upgrades cluster.",
            merged_from=[generate_ksuid() for _ in range(3)],
        )
    )
    not_yet = await plane.create(
        SynthesizedConcept(
            namespace="eric/claude-code/concept",
            title="Networking pattern",
            content="Recent observation; not yet reinforced.",
            synthesis_rationale="Only one source so far.",
            merged_from=[generate_ksuid() for _ in range(3)],
        )
    )
    for _ in range(3):
        await plane.reinforce(namespace="eric/claude-code/concept", object_id=eligible.object_id)
    backdate = datetime.now(UTC) - timedelta(days=2)
    for cid in (eligible.object_id, not_yet.object_id):
        qdrant.set_payload(
            collection_name="musubi_concept",
            payload={
                "created_at": backdate.isoformat(),
                "created_epoch": backdate.timestamp(),
            },
            points=qmodels.Filter(
                must=[qmodels.FieldCondition(key="object_id", match=qmodels.MatchValue(value=cid))]
            ),
        )

    report = await concept_maturation_sweep(
        client=qdrant,
        sink=sink,
        config=_config(concept_min_age_sec=24 * 3600, concept_reinforcement_threshold=3),
    )
    assert report.transitioned == 1
    refreshed_eligible = await plane.get(
        namespace="eric/claude-code/concept", object_id=eligible.object_id
    )
    refreshed_not_yet = await plane.get(
        namespace="eric/claude-code/concept", object_id=not_yet.object_id
    )
    assert refreshed_eligible is not None and refreshed_eligible.state == "matured"
    assert refreshed_not_yet is not None and refreshed_not_yet.state == "synthesized"


async def test_concept_demotion_sweep_demotes_inactive(
    qdrant: QdrantClient, sink: LifecycleEventSink
) -> None:
    from qdrant_client import models as qmodels

    from musubi.lifecycle.maturation import concept_demotion_sweep
    from musubi.planes.concept import ConceptPlane
    from musubi.types.common import generate_ksuid
    from musubi.types.concept import SynthesizedConcept

    plane = ConceptPlane(client=qdrant, embedder=FakeEmbedder())
    saved = await plane.create(
        SynthesizedConcept(
            namespace="eric/claude-code/concept",
            title="Inactive pattern",
            content="Hasn't been reinforced in a while.",
            synthesis_rationale="Initial cluster of three.",
            merged_from=[generate_ksuid() for _ in range(3)],
        )
    )
    await plane.transition(
        namespace="eric/claude-code/concept",
        object_id=saved.object_id,
        to_state="matured",
        actor="seed",
        reason="seed",
    )
    backdate = datetime.now(UTC) - timedelta(days=40)
    qdrant.set_payload(
        collection_name="musubi_concept",
        payload={
            "updated_at": backdate.isoformat(),
            "updated_epoch": backdate.timestamp(),
        },
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=saved.object_id)
                )
            ]
        ),
    )
    report = await concept_demotion_sweep(
        client=qdrant,
        sink=sink,
        config=_config(demotion_inactivity_sec=30 * 86400),
    )
    assert report.transitioned == 1
    refreshed = await plane.get(namespace="eric/claude-code/concept", object_id=saved.object_id)
    assert refreshed is not None and refreshed.state == "demoted"


async def test_concept_maturation_sweep_no_op_when_empty(
    qdrant: QdrantClient, sink: LifecycleEventSink
) -> None:
    from musubi.lifecycle.maturation import concept_maturation_sweep

    report = await concept_maturation_sweep(client=qdrant, sink=sink, config=_config())
    assert report.selected == 0


async def test_maturation_worker_observes_lifecycle_job_duration(
    qdrant: QdrantClient, sink: LifecycleEventSink
) -> None:
    from musubi.lifecycle.maturation import concept_maturation_sweep

    before = _duration_count("maturation")
    await concept_maturation_sweep(client=qdrant, sink=sink, config=_config())
    assert _duration_count("maturation") == before + 1


async def test_concept_demotion_sweep_no_op_when_empty(
    qdrant: QdrantClient, sink: LifecycleEventSink
) -> None:
    from musubi.lifecycle.maturation import concept_demotion_sweep

    report = await concept_demotion_sweep(client=qdrant, sink=sink, config=_config())
    assert report.selected == 0


def test_default_ollama_client_returns_loud_stub() -> None:
    from musubi.lifecycle.maturation import _NotConfiguredOllama, default_ollama_client

    client = default_ollama_client()
    assert isinstance(client, _NotConfiguredOllama)


def test_build_maturation_jobs_registers_documented_names(
    tmp_path: Path,
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """The Job names this module owns line up with the lifecycle
    scheduler's default-job registry. Demotion moved to the dedicated
    `demotion.py` module after slice-lifecycle-demotion-builder landed;
    maturation retains the three sweeps that still live here."""
    from musubi.lifecycle.maturation import build_maturation_jobs

    jobs = build_maturation_jobs(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(),
        cursor=cursor,
        lock_dir=tmp_path / "locks",
        config=_config(),
    )
    names = {j.name for j in jobs}
    assert names == {
        "maturation_episodic",
        "provisional_ttl",
        "concept_maturation",
    }
    for job in jobs:
        assert callable(job.func)
        assert isinstance(job.trigger_kwargs, dict)


def test_build_maturation_jobs_runner_skips_when_lock_held(
    tmp_path: Path,
    qdrant: QdrantClient,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Wrapped jobs acquire the per-job file lock before running. We
    verify by holding the lock externally and confirming the job's
    runner returns cleanly without doing work."""
    from musubi.lifecycle.maturation import build_maturation_jobs

    jobs = build_maturation_jobs(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(),
        cursor=cursor,
        lock_dir=tmp_path / "locks",
        config=_config(),
    )
    by_name = {j.name: j for j in jobs}
    target = by_name["maturation_episodic"]
    lock_path = tmp_path / "locks" / "maturation_episodic.lock"
    with file_lock(lock_path) as got:
        assert got is True
        target.func()  # observes lock, returns no-op
    # Lock free again — runs cleanly against an empty plane.
    target.func()


def test_normalize_tags_drops_empty_strings() -> None:
    out = normalize_tags(["", "   ", "real-tag"], aliases={})
    assert out == ["real-tag"]


async def test_supersession_no_predecessor_match(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """Supersession hint with no plausible predecessor leaves the new row
    matured cleanly with empty ``supersedes`` — covers the
    ``_find_supersession_candidate`` returns-None branch."""
    new_row = await _seed_provisional(plane, ns, content="Update: novel-only-content")
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        ollama=FakeOllama(),
        cursor=cursor,
        config=_config(),
    )
    refreshed = await plane.get(namespace=ns, object_id=new_row.object_id)
    assert refreshed is not None
    assert refreshed.state == "matured"
    assert refreshed.supersedes == []
