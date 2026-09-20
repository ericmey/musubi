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
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.lifecycle import LifecycleEventSink, file_lock, maturation
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.lifecycle.maturation import (
    DEFAULT_TAG_ALIASES,
    MaturationConfig,
    MaturationCursor,
    OllamaClient,
    OllamaImportance,
    OllamaTopic,
    _batched_call,
    detect_supersession_hint,
    episodic_maturation_sweep,
    normalize_tags,
    provisional_ttl_sweep,
)
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.types.common import KSUID, epoch_of, utc_now
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


def _coordinator(qdrant: QdrantClient, sink: LifecycleEventSink) -> LifecycleTransitionCoordinator:
    return LifecycleTransitionCoordinator(client=qdrant, db_path=sink._db_path)


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
        coordinator=_coordinator(qdrant, sink),
    )

    report = await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
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
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
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
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
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
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
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
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=ollama,
        cursor=cursor,
        config=_config(),
    )
    refreshed = await plane.get(namespace=ns, object_id=seeded.object_id)
    assert refreshed is not None
    assert refreshed.importance == 9
    # And the LLM was actually called.
    assert ollama.score_calls, "OllamaClient.score_importance was never invoked"
    # The score-audit stamp lands with the rescore. The field existed on
    # the model from day one but was never written; every production row
    # carried `importance_last_scored_at: None` even after a rescore.
    # Its indexed epoch twin is written in the same set_payload so the
    # `importance_last_scored_epoch` float index is queryable.
    assert refreshed.importance_last_scored_at is not None
    assert refreshed.importance_last_scored_epoch is not None


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
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(available=False),
        cursor=cursor,
        config=_config(),
    )
    refreshed = await plane.get(namespace=ns, object_id=seeded.object_id)
    assert refreshed is not None
    assert refreshed.importance == captured_importance
    # No LLM verdict ⇒ no score-audit stamp: a fallback is not a scoring
    # event, and stamping it would hide the row from a future
    # "re-score never-scored rows" pass.
    assert refreshed.importance_last_scored_at is None


async def test_unchanged_rescore_still_advances_audit_stamp(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: MaturationCursor,
) -> None:
    """`importance_last_scored_at` must mean LAST scored, not first scored.

    A row that already carries an old stamp and is rescored to the SAME
    importance (tags and topics also unchanged) must still get a fresh
    stamp — otherwise a stale-row sweep can never mark the row current
    and reselects it forever. Regression for the write-only-when-null
    gate in `_enrichment_changed`.
    """
    from qdrant_client import models as qmodels

    seeded = await _seed_provisional(plane, ns, content="already-stamped", tags=[])
    old_stamp = datetime(2026, 1, 1, tzinfo=UTC)
    plane._client.set_payload(
        collection_name="musubi_episodic",
        payload={
            "importance_last_scored_at": old_stamp.isoformat(),
            "importance_last_scored_epoch": old_stamp.timestamp(),
        },
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=seeded.object_id)
                )
            ]
        ),
    )

    # LLM returns the captured importance unchanged; no topic mapping, so
    # topics fall back unchanged too. Every legacy change-detector input
    # is identical — only the scoring event itself forces the write.
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(importance=seeded.importance),
        cursor=cursor,
        config=_config(),
    )

    refreshed = await plane.get(namespace=ns, object_id=seeded.object_id)
    assert refreshed is not None
    assert refreshed.importance == seeded.importance
    assert refreshed.importance_last_scored_at is not None
    assert refreshed.importance_last_scored_at > old_stamp
    assert refreshed.importance_last_scored_epoch is not None
    assert refreshed.importance_last_scored_epoch > old_stamp.timestamp()


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
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=ollama,
        cursor=cursor,
        config=_config(),
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
        coordinator=_coordinator(qdrant, sink),
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
    """LIFE-009 migration: the seam requires a controlled embedder
    (cosine >= 0.88 on post-hint content) + topic compatibility (at
    least one shared linked_to_topics entry). The original test
    depended on the OLD substring-based heuristic. This test now
    uses a controlled embedder stub with HIGH cosine and sets
    linked_to_topics on both rows to share the topic evidence the
    seam requires."""
    from qdrant_client import models as qmodels

    from musubi.embedding.base import Embedder
    from musubi.planes.episodic.plane import episodic_point_id
    from musubi.store.specs import DENSE_SIZE

    class _CtrlEmbedder(Embedder):
        def __init__(self, vec: list[float]) -> None:
            self._v = vec

        async def embed_dense(self, texts: list[str]) -> list[list[float]]:
            return [self._v for _ in texts]

        async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
            return [{} for _ in texts]

        async def rerank(self, query: str, candidates: list[str]) -> list[float]:
            return [0.0 for _ in candidates]

    vec = [1.0, 0.0] + [0.0] * (DENSE_SIZE - 2)
    plane._embedder = _CtrlEmbedder(vec)

    # Seed the original matured row with content the new one can hint at.
    original = await plane.create(
        EpisodicMemory(
            namespace=ns,
            content="GPU pin: nvidia driver 470",
            topics=["hardware/gpu"],
        )
    )
    await plane.transition(
        namespace=ns,
        object_id=original.object_id,
        to_state="matured",
        actor="test",
        reason="seed",
        coordinator=_coordinator(qdrant, sink),
    )
    # Set linked_to_topics on the original (the seam reads it from the payload).
    plane._client.set_payload(
        collection_name="musubi_episodic",
        payload={"linked_to_topics": ["hardware/gpu"]},
        points=qmodels.PointIdsList(points=[episodic_point_id(original.object_id)]),
    )
    # New provisional row hints at supersession.
    new_row = await _seed_provisional(
        plane,
        ns,
        content="Update: GPU pin: nvidia driver 575",
        tags=["hardware/gpu"],
    )
    # Set linked_to_topics on the new row too.
    plane._client.set_payload(
        collection_name="musubi_episodic",
        payload={"linked_to_topics": ["hardware/gpu"]},
        points=qmodels.PointIdsList(points=[episodic_point_id(new_row.object_id)]),
    )
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor,
        config=_config(),
        embedder=plane._embedder,
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
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor,
        config=_config(),
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
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor,
        config=_config(),
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
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor,
        config=_config(),
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
        coordinator=_coordinator(qdrant, sink),
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
        coordinator=_coordinator(qdrant, sink),
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
        coordinator=_coordinator(qdrant, sink),
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
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor,
        config=_config(),
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
        coordinator=_coordinator(qdrant, sink),
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
        coordinator=_coordinator(qdrant, sink),
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
        coordinator=_coordinator(qdrant, sink),
    )

    report = await episodic_demotion_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
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

    report = await episodic_demotion_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        config=_config(),
    )
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
        coordinator=_coordinator(qdrant, sink),
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
        coordinator=_coordinator(qdrant, sink),
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
        coordinator=_coordinator(qdrant, sink),
        config=_config(demotion_inactivity_sec=30 * 86400),
    )
    assert report.transitioned == 1
    refreshed = await plane.get(namespace="eric/claude-code/concept", object_id=saved.object_id)
    assert refreshed is not None and refreshed.state == "demoted"


async def test_concept_maturation_sweep_no_op_when_empty(
    qdrant: QdrantClient, sink: LifecycleEventSink
) -> None:
    from musubi.lifecycle.maturation import concept_maturation_sweep

    report = await concept_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        config=_config(),
    )
    assert report.selected == 0


async def test_concept_demotion_sweep_no_op_when_empty(
    qdrant: QdrantClient, sink: LifecycleEventSink
) -> None:
    from musubi.lifecycle.maturation import concept_demotion_sweep

    report = await concept_demotion_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        config=_config(),
    )
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
    from musubi.lifecycle.maturation import _NoopEmbedder, build_maturation_jobs

    jobs = build_maturation_jobs(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor,
        lock_dir=tmp_path / "locks",
        embedder=_NoopEmbedder(),
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
    from musubi.lifecycle.maturation import _NoopEmbedder, build_maturation_jobs

    jobs = build_maturation_jobs(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor,
        lock_dir=tmp_path / "locks",
        embedder=_NoopEmbedder(),
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
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"Update: GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor,
        config=_config(),
    )
    refreshed = await plane.get(namespace=ns, object_id=new_row.object_id)
    assert refreshed is not None
    assert refreshed.state == "matured"
    assert refreshed.supersedes == []


# ---------------------------------------------------------------------------
# Per-batch failure isolation (ADR 0043 — aligns code with the spec's
# "Partial batch failure" section; the old code nulled the whole field)
# ---------------------------------------------------------------------------


def _metric_value(kind: str) -> float:
    from musubi.observability import default_registry, render_text_format

    text = render_text_format(default_registry())
    prefix = f'musubi_lifecycle_enrichment_batch_failures_total{{kind="{kind}"}} '
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line.removeprefix(prefix))
    return 0.0


async def test_batched_call_isolates_failed_batches() -> None:
    """One failed batch must not erase the other batches' enrichment.

    This exact coupling starved synthesis: one flaky topics batch out of
    ~five nulled linked_to_topics for the entire sweep, so clustering
    fell back to capture-source tags and formed the mega-cluster. The
    counter delta proves the degradation is an operational signal.
    """
    items = _importance_items_for_batch_test(6)
    calls: list[list[str]] = []

    async def flaky(batch: list[OllamaImportance]) -> dict[str, int] | None:
        calls.append([i.object_id for i in batch])
        # Fail exactly the middle batch.
        if items[2].object_id in {i.object_id for i in batch}:
            return None
        return {i.object_id: 9 for i in batch}

    before = _metric_value("importance")
    merged: dict[KSUID, int] = await _batched_call(items, flaky, kind="importance", batch_size=2)

    assert len(calls) == 3  # all three batches attempted — no early return
    assert set(merged) == {
        items[0].object_id,
        items[1].object_id,
        items[4].object_id,
        items[5].object_id,
    }
    assert _metric_value("importance") == before + 1


async def test_batched_call_all_batches_failing_returns_empty_not_none() -> None:
    """Total outage degrades to {} — the sweep's per-id fallback covers
    every row, matching the spec's 'Ollama down' behavior."""

    async def down(_batch: list[OllamaImportance]) -> dict[str, int] | None:
        return None

    merged: dict[KSUID, int] = await _batched_call(
        _importance_items_for_batch_test(4), down, kind="importance", batch_size=2
    )
    assert merged == {}


def _importance_items_for_batch_test(n: int) -> list[OllamaImportance]:
    from musubi.types.common import generate_ksuid

    return [
        OllamaImportance(object_id=generate_ksuid(), content=f"row {i}", captured_importance=5)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Enrichment fencing (RET-012 prerequisite)
# ---------------------------------------------------------------------------
#
# An in-flight sweep must never enrich a row that left `matured` under it.
#
# `_apply_enrichment` wrote `importance`, `updated_at` and `updated_epoch` fenced on
# `object_id` ALONE — no state, no version, no lease. The sweep transitions a row to
# `matured` and enriches it as two separate writes, so anything that moves the row in
# between is overwritten by the second one.
#
# The case that made this urgent (Copilot on musubi#732, 2026-09-20): a retraction
# quarantines the row to `archived`/importance 1 in that gap. The enrichment write then
# lands anyway and the retraction finishes with a **rescored importance and a
# post-retraction `updated_at`** — breaking RET-012's terminal-state guarantee and
# IDEM-008's "the retraction timestamp is the retraction's" in one `set_payload`.
#
# The fence is `state == "matured"`: only a row still in the state this sweep just put
# it in may be enriched. That single condition also excludes v2 immutable content points
# for free, because they carry no `state` field at all — a `FieldCondition` cannot match
# a point that lacks the key. `test_v2_content_point_is_never_enriched` pins that, so if
# `state` is ever added to content payloads the exclusion stops being accidental.
#
# Version was considered and deliberately not used: the sweep does not hold the
# post-transition version without an extra read, and state is the property that actually
# matters — an archived row must never be enriched at ANY version.
#
# Shiori, 2026-09-20.


def _payload(client: QdrantClient, object_id: str) -> list[dict[str, Any]]:
    from qdrant_client import models as qmodels

    rows, _ = client.scroll(
        collection_name="musubi_episodic",
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="object_id", match=qmodels.MatchValue(value=object_id))
            ]
        ),
        limit=8,
        with_payload=True,
    )
    return [dict(r.payload or {}) for r in rows]


async def test_a_row_archived_mid_sweep_is_not_enriched(
    monkeypatch: pytest.MonkeyPatch,
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
    cursor: Any,
) -> None:
    """THE DEFECT. Archive the row in the exact gap between the sweep's transition
    and its enrichment write, which is the window a retraction quarantine occupies."""
    from qdrant_client import models as qmodels

    row = await _seed_provisional(plane, ns, content="retract me", age_seconds=7200)

    real_transition = maturation.transition  # type: ignore[attr-defined]
    archived_at: dict[str, Any] = {}

    def transition_then_archive(*args: Any, **kwargs: Any) -> Any:
        result = real_transition(*args, **kwargs)
        # The quarantine CAS lands here: after the sweep matured the row, before it
        # enriches. Written directly so the test does not depend on the retraction API.
        quarantined = utc_now()
        archived_at["updated_at"] = quarantined.isoformat()
        qdrant.set_payload(
            collection_name="musubi_episodic",
            payload={
                "state": "archived",
                "importance": 1,
                "updated_at": quarantined.isoformat(),
                "updated_epoch": epoch_of(quarantined),
            },
            points=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="object_id", match=qmodels.MatchValue(value=str(row.object_id))
                    )
                ]
            ),
            wait=True,
        )
        return result

    monkeypatch.setattr(maturation, "transition", transition_then_archive)
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={}),
        cursor=cursor,
        config=_config(min_age_sec=3600),
    )

    after = await plane.get(namespace=ns, object_id=row.object_id)
    assert after is not None
    assert after.state == "archived", "the quarantine itself did not hold"
    assert after.importance == 1, (
        f"the sweep re-scored a retracted row to importance {after.importance}; "
        f"RET-012's terminal state is not terminal"
    )
    assert after.updated_at.isoformat() == archived_at["updated_at"], (
        f"the sweep stamped a post-retraction updated_at ({after.updated_at}); "
        f"the retraction timestamp must remain the retraction's "
        f"({archived_at['updated_at']})"
    )


async def test_an_ordinary_row_is_still_enriched(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
    cursor: Any,
) -> None:
    """THE POSITIVE CONTROL. Without it, a fence that refuses everything passes the
    cell above — and the sweep would silently stop enriching anything at all."""
    row = await _seed_provisional(plane, ns, content="GPU pin: nvidia driver 575", age_seconds=7200)

    report = await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={"GPU pin: nvidia driver 575": ["hardware/gpu"]}),
        cursor=cursor,
        config=_config(min_age_sec=3600),
    )

    assert report.transitioned == 1
    assert report.enriched == 1, "the fence refused an ordinary, still-matured row"
    after = await plane.get(namespace=ns, object_id=row.object_id)
    assert after is not None and after.state == "matured"
    assert "hardware/gpu" in after.linked_to_topics


async def test_v2_content_point_is_never_enriched(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
) -> None:
    """The immutable content point must never take an enrichment write.

    The fence's `state` term is what excludes it -- content points carry no `state`
    key, and a FieldCondition cannot match a point missing the field.

    THE FIXTURE IS THE POINT. My first version gave the content sibling no `version`
    either, so it stayed excluded by the VERSION term even with the state term deleted:
    the cell passed for a reason it was not testing and could not fail for the reason it
    existed (Copilot via Aoi, musubi#771). It now carries the matching post-transition
    version, which isolates `state` as the only thing keeping it out -- remove that term
    and this cell reds."""
    from qdrant_client import models as qmodels

    from musubi.lifecycle.maturation import _apply_enrichment

    row = await plane.create(EpisodicMemory(namespace=ns, content="two-point row"))
    await plane.transition(
        namespace=ns,
        object_id=row.object_id,
        to_state="matured",
        actor="test",
        reason="seed",
        coordinator=_coordinator(qdrant, sink),
    )
    live = await plane.get(namespace=ns, object_id=row.object_id)
    assert live is not None

    qdrant.upsert(
        collection_name="musubi_episodic",
        points=[
            qmodels.PointStruct(
                id="00000000-0000-4000-8000-00000000c0de",
                vector={},
                payload={
                    "object_id": str(row.object_id),
                    "namespace": ns,
                    "point_kind": "content",
                    "importance": 8,
                    # Matching version, so the version term cannot be what excludes it.
                    "version": live.version,
                },
            )
        ],
        wait=True,
    )
    before = next(
        p for p in _payload(qdrant, str(row.object_id)) if p.get("point_kind") == "content"
    )

    applied = _apply_enrichment(
        qdrant,
        collection="musubi_episodic",
        namespace=ns,
        object_id=row.object_id,
        expected_version=live.version,
        tags=["enriched"],
        importance=3,
        topics=["hardware/gpu"],
    )

    assert applied, "the fence refused the anchor; this cell would prove nothing"
    after = next(
        p for p in _payload(qdrant, str(row.object_id)) if p.get("point_kind") == "content"
    )
    assert after == before, "enrichment wrote to the immutable content point"


async def test_a_refused_enrichment_is_not_counted_as_enriched(
    monkeypatch: pytest.MonkeyPatch,
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
    cursor: Any,
) -> None:
    """The report must not claim a write the fence refused.

    `enriched` was incremented unconditionally after the call, so once the fence
    started refusing rows the sweep reported enrichments it had not performed --
    and the sweep's own report is the record an operator reads first."""
    from qdrant_client import models as qmodels

    row = await _seed_provisional(plane, ns, content="retract me", age_seconds=7200)
    real_transition = maturation.transition  # type: ignore[attr-defined]

    def transition_then_archive(*args: Any, **kwargs: Any) -> Any:
        result = real_transition(*args, **kwargs)
        quarantined = utc_now()
        qdrant.set_payload(
            collection_name="musubi_episodic",
            payload={
                "state": "archived",
                "importance": 1,
                "updated_at": quarantined.isoformat(),
                "updated_epoch": epoch_of(quarantined),
            },
            points=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="object_id", match=qmodels.MatchValue(value=str(row.object_id))
                    )
                ]
            ),
            wait=True,
        )
        return result

    monkeypatch.setattr(maturation, "transition", transition_then_archive)
    report = await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={}),
        cursor=cursor,
        config=_config(min_age_sec=3600),
    )

    assert report.enriched == 0, (
        f"the sweep reported {report.enriched} enrichment(s) for a write the fence refused"
    )


async def test_a_row_archived_between_the_fence_and_the_write_is_not_reported(
    monkeypatch: pytest.MonkeyPatch,
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
) -> None:
    """The window a pre-write count cannot see (Yua, musubi#771).

    The first version counted under the fence BEFORE writing and returned True on a
    non-zero count. That proves the row was eligible a moment ago, not that the write
    landed: count sees `matured`, a retraction archives the row, the fenced write then
    matches zero points, and the caller is told an enrichment happened.

    Here the archive lands between the fence being built and `set_payload` running, so
    only a post-write readback can get the answer right."""
    from qdrant_client import models as qmodels

    from musubi.lifecycle.maturation import _apply_enrichment

    row = await plane.create(EpisodicMemory(namespace=ns, content="racer"))
    await plane.transition(
        namespace=ns,
        object_id=row.object_id,
        to_state="matured",
        actor="test",
        reason="seed",
        coordinator=_coordinator(qdrant, sink),
    )

    real_set_payload = qdrant.set_payload

    def archive_then_write(*args: Any, **kwargs: Any) -> Any:
        # The retraction quarantine lands in the window.
        real_set_payload(
            collection_name="musubi_episodic",
            payload={"state": "archived", "importance": 1},
            points=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="object_id", match=qmodels.MatchValue(value=str(row.object_id))
                    )
                ]
            ),
            wait=True,
        )
        monkeypatch.undo()
        return real_set_payload(*args, **kwargs)

    monkeypatch.setattr(qdrant, "set_payload", archive_then_write)

    live = await plane.get(namespace=ns, object_id=row.object_id)
    assert live is not None
    applied = _apply_enrichment(
        qdrant,
        collection="musubi_episodic",
        namespace=ns,
        object_id=row.object_id,
        expected_version=live.version,
        tags=["enriched"],
        importance=3,
        topics=["hardware/gpu"],
    )

    assert applied is False, (
        "the enrichment reported success for a write that matched zero rows; "
        "success must come from durable state, not from having issued the write"
    )
    after = await plane.get(namespace=ns, object_id=row.object_id)
    assert after is not None and after.state == "archived"
    assert after.importance == 1, "the archived row was enriched anyway"


async def test_enrichment_does_not_cross_namespaces_at_runtime(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
) -> None:
    """`object_id` is not globally unique — two namespaces may carry the same id
    (`tests/api/test_data001_episodic_patch_fence.py:113` relies on exactly that).

    Behavioural rather than structural. My first attempt at this concluded the cell was
    inert, but the plant I judged it with deleted the OTHER `key="namespace"` condition
    in the module — right check, wrong object — so the cell was never given a fair
    trial. Anchored correctly it reds."""
    from qdrant_client import models as qmodels

    from musubi.lifecycle.maturation import _apply_enrichment

    row = await plane.create(EpisodicMemory(namespace=ns, content="mine"))
    await plane.transition(
        namespace=ns,
        object_id=row.object_id,
        to_state="matured",
        actor="test",
        reason="seed",
        coordinator=_coordinator(qdrant, sink),
    )
    stranger_ns = "someone/else/episodic"
    qdrant.upsert(
        collection_name="musubi_episodic",
        points=[
            qmodels.PointStruct(
                id="00000000-0000-4000-8000-0000005747a1",
                vector={},
                payload={
                    "object_id": str(row.object_id),
                    "namespace": stranger_ns,
                    "state": "matured",
                    "importance": 9,
                    "tags": ["untouched"],
                    "updated_epoch": 1.0,
                },
            )
        ],
        wait=True,
    )
    before = next(p for p in _payload(qdrant, str(row.object_id)) if p["namespace"] == stranger_ns)

    live = await plane.get(namespace=ns, object_id=row.object_id)
    assert live is not None
    applied = _apply_enrichment(
        qdrant,
        collection="musubi_episodic",
        namespace=ns,
        object_id=row.object_id,
        expected_version=live.version,
        tags=["enriched"],
        importance=3,
        topics=["hardware/gpu"],
    )

    assert applied, "the fence refused my own matured row; this cell would prove nothing"
    after = next(p for p in _payload(qdrant, str(row.object_id)) if p["namespace"] == stranger_ns)
    assert after == before, (
        "enrichment wrote to a row in another namespace that shares this object_id"
    )


async def test_a_row_restored_to_matured_does_not_accept_stale_enrichment(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
) -> None:
    """`state == "matured"` is not a transition identity (Copilot/Yua, musubi#771).

    An archived or demoted row can be restored to `matured`. A state-only fence would
    then accept enrichment computed against the OLD snapshot -- tags, importance and
    topics derived from content the row no longer has. The version this sweep
    established is the identity; a restore bumps it, so the stale write is refused."""
    from musubi.lifecycle.maturation import _apply_enrichment

    row = await plane.create(EpisodicMemory(namespace=ns, content="round trip"))
    coordinator = _coordinator(qdrant, sink)
    await plane.transition(
        namespace=ns,
        object_id=row.object_id,
        to_state="matured",
        actor="test",
        reason="seed",
        coordinator=coordinator,
    )
    matured = await plane.get(namespace=ns, object_id=row.object_id)
    assert matured is not None
    stale_version = matured.version

    # The row leaves `matured` and comes back, exactly as a demote/restore would.
    await plane.transition(
        namespace=ns,
        object_id=row.object_id,
        to_state="demoted",
        actor="test",
        reason="round-trip",
        coordinator=coordinator,
    )
    await plane.transition(
        namespace=ns,
        object_id=row.object_id,
        to_state="matured",
        actor="test",
        reason="round-trip",
        coordinator=coordinator,
    )
    restored = await plane.get(namespace=ns, object_id=row.object_id)
    assert restored is not None and restored.state == "matured"
    assert restored.version != stale_version, "fixture drift: the restore did not bump version"

    applied = _apply_enrichment(
        qdrant,
        collection="musubi_episodic",
        namespace=ns,
        object_id=row.object_id,
        expected_version=stale_version,
        tags=["stale"],
        importance=9,
        topics=["stale/topic"],
    )

    assert applied is False, "a stale-version enrichment was applied to a restored row"
    after = await plane.get(namespace=ns, object_id=row.object_id)
    assert after is not None
    assert "stale" not in after.tags, "the restored row accepted enrichment from an old snapshot"


async def test_a_leased_row_cannot_be_transitioned_by_lifecycle(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
) -> None:
    """A row under a LIVE mutation lease belongs to another writer.

    The token must be live, not merely present. `update_lease_token` is the GENERIC
    lease every `owned_update` takes, so an EXPIRED ordinary token is takeover-eligible
    and must NOT block the lifecycle forever -- see the companion cell below.

    `_apply_conditional` fenced on namespace/object/version and ignored
    `update_lease_token`, so the lease the retraction saga fences its CAS with was
    honoured by the retraction path and ignored by the lifecycle path: a pre-RET-012
    row could be matured between the adoption read and the repair, the version-fenced
    repair would then lose, and the retracted row would stay active
    (Copilot via Aoi, musubi#732)."""
    from qdrant_client import models as qmodels

    from musubi.lifecycle.transitions import transition

    row = await plane.create(EpisodicMemory(namespace=ns, content="leased"))
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"update_lease_token": f"done:{int(utc_now().timestamp() * 1_000_000)}:live"},
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=str(row.object_id))
                )
            ]
        ),
        wait=True,
    )

    transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=row.object_id,
        target_state="matured",
        actor="test",
        reason="maturation-sweep",
        namespace=ns,
    )

    after = await plane.get(namespace=ns, object_id=row.object_id)
    assert after is not None
    assert after.state == "provisional", (
        f"a leased row was transitioned to {after.state!r}; the lifecycle CAS ignored "
        f"another writer's mutation lease"
    )


async def test_an_unleased_row_still_transitions(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
) -> None:
    """THE CONTROL. A lease condition that refused every write would satisfy the cell
    above while stopping the lifecycle entirely."""
    from musubi.lifecycle.transitions import transition

    row = await plane.create(EpisodicMemory(namespace=ns, content="ordinary"))

    transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=row.object_id,
        target_state="matured",
        actor="test",
        reason="maturation-sweep",
        namespace=ns,
    )

    after = await plane.get(namespace=ns, object_id=row.object_id)
    assert after is not None and after.state == "matured", (
        "the lease condition refused an ordinary token-empty write"
    )


async def test_a_refused_enrichment_is_recorded_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
    cursor: Any,
) -> None:
    """A refused enrichment must be visible in the report.

    The version fence can refuse after a successful transition. The row is no longer
    `provisional`, so no later sweep re-selects it -- the enrichment is LOST, not
    deferred. Counting it nowhere made that loss invisible in the only record an
    operator reads (Copilot/Yua, musubi#771)."""
    from qdrant_client import models as qmodels

    row = await _seed_provisional(plane, ns, content="racer", age_seconds=7200)
    real_transition = maturation.transition  # type: ignore[attr-defined]

    def transition_then_archive(*args: Any, **kwargs: Any) -> Any:
        result = real_transition(*args, **kwargs)
        quarantined = utc_now()
        qdrant.set_payload(
            collection_name="musubi_episodic",
            payload={
                "state": "archived",
                "importance": 1,
                "updated_at": quarantined.isoformat(),
                "updated_epoch": epoch_of(quarantined),
            },
            points=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="object_id", match=qmodels.MatchValue(value=str(row.object_id))
                    )
                ]
            ),
            wait=True,
        )
        return result

    monkeypatch.setattr(maturation, "transition", transition_then_archive)
    report = await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(topic_map={}),
        cursor=cursor,
        config=_config(min_age_sec=3600),
    )

    assert report.enriched == 0, "a refused write was counted as an enrichment"
    assert report.failed == 1, (
        "a refused enrichment was not recorded anywhere; the loss is invisible to the "
        "operator reading this report"
    )


async def test_an_expired_ordinary_lease_does_not_block_the_lifecycle_forever(
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
) -> None:
    """THE LIVENESS CONTROL, and the reason the fence is not a bare `IsEmpty`.

    `update_lease_token` is the generic mutation lease. A crashed ORDINARY patch on any
    row leaves a `done:*` token behind with no retraction saga coming to clear it. A
    fence that refused every non-empty token would block that row's lifecycle
    permanently. An expired token with no retraction evidence is takeover-eligible, so
    the coordinator clears that exact token behind its own fence and proceeds.

    Aoi raised this; I argued it away with a premise that was checkable and false, and
    she deferred to it. Her first instinct was right (musubi#771)."""
    from qdrant_client import models as qmodels

    from musubi.lifecycle.transitions import transition

    row = await plane.create(EpisodicMemory(namespace=ns, content="crashed ordinary patch"))
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"update_lease_token": "done:1:long-expired"},
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=str(row.object_id))
                )
            ]
        ),
        wait=True,
    )

    transition(
        qdrant,
        coordinator=_coordinator(qdrant, sink),
        object_id=row.object_id,
        target_state="matured",
        actor="test",
        reason="maturation-sweep",
        namespace=ns,
    )

    after = await plane.get(namespace=ns, object_id=row.object_id)
    assert after is not None
    assert after.state == "matured", (
        "an EXPIRED ordinary lease blocked the lifecycle; a crashed patch would strand "
        "this row permanently because no saga is coming to clear it"
    )


async def test_a_lease_taken_between_transition_and_enrichment_refuses(
    monkeypatch: pytest.MonkeyPatch,
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    ns: str,
    sink: Any,
    cursor: Any,
) -> None:
    """THE INTERLEAVING CELL. Tama's, and the one a pre-read cannot pass.

    The regression she stopped: a Python scroll for `update_lease_token`, then a write
    fenced on namespace/object/state/version. That CAS is real but CANNOT SEE A LEASE --
    taking one does not bump `version` -- so a writer acquiring the token between the
    read and the write sails through. Right check, wrong object, again.

    So take the lease in exactly that gap, using the same post-transition hook
    `test_a_row_archived_mid_sweep_is_not_enriched` uses, and require enrichment to
    lose. Nothing here is satisfiable by reading first; only the server-side
    `IsEmptyCondition` in the write's own `must` list can win it.

    The token is FRESH. Its expired sibling,
    `test_an_expired_ordinary_lease_does_not_block_the_lifecycle_forever`, must stay
    green: ordinary takeover happens in the coordinator, BEFORE the transition."""
    from qdrant_client import models as qmodels

    row = await _seed_provisional(plane, ns, content="enrich me", age_seconds=7200)
    # The discriminator is DERIVED from the seed, never a literal: a hard-coded 5
    # silently collided with the row's own captured importance and the cell could not
    # have failed for its own reason. Assert they differ so it can never go inert.
    seed_importance = _payload(qdrant, str(row.object_id))[0]["importance"]
    enriched_importance = seed_importance + 3
    assert enriched_importance != seed_importance
    real_transition = maturation.transition  # type: ignore[attr-defined]
    planted: dict[str, Any] = {}

    def transition_then_take_the_lease(*args: Any, **kwargs: Any) -> Any:
        result = real_transition(*args, **kwargs)
        # The window: matured, not yet enriched. A concurrent `owned_update` acquires
        # the generic mutation lease here and bumps nothing the old fence reads.
        planted["token"] = f"done:{int(utc_now().timestamp() * 1_000_000)}:racer"
        qdrant.set_payload(
            collection_name="musubi_episodic",
            payload={"update_lease_token": planted["token"]},
            points=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="object_id", match=qmodels.MatchValue(value=str(row.object_id))
                    )
                ]
            ),
            wait=True,
        )
        return result

    monkeypatch.setattr(maturation, "transition", transition_then_take_the_lease)
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=FakeOllama(importance=enriched_importance),
        cursor=cursor,
        config=_config(min_age_sec=3600),
    )

    assert planted, (
        "the hook never fired -- the sweep did not transition this row, so the cell "
        "proved nothing about the fence (inert, not passing)"
    )
    payloads = _payload(qdrant, str(row.object_id))
    assert payloads, "row vanished"
    live = [p for p in payloads if p.get("state") == "matured"]
    assert len(live) == 1, f"expected exactly one matured row, got {len(live)}"
    assert live[0].get("update_lease_token") == planted["token"], (
        "the lease was cleared by the enrichment path; enrichment must refuse a "
        "concurrent lease, never take it over -- takeover belongs in the coordinator"
    )
    assert live[0].get("importance") == seed_importance, (
        f"enrichment wrote importance {live[0].get('importance')} onto a row whose "
        f"lease was taken after the transition (seed was {seed_importance}); the "
        f"pre-read saw an empty token and the write never re-checked it"
    )
