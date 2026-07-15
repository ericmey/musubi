"""LIFE-009: semantic supersession with abstention (Issue #532).

Owner slice: slice-life009-semantic-supersession (#532).

The discriminating contract: a candidate is a predecessor only when
its post-hint content is semantically similar (cosine >= 0.88 via the
existing ``Embedder``) AND shares at least one ``linked_to_topics``
entry with the new memory. Ambiguity abstains. Substring overlap
alone is never sufficient.

The first contract is bounded to fifteen tests in this file:

    13 RED discriminating tests   (currently failing under live code)
    2 GREEN preservation guards  (passing under live code; the seam
                                  must not break them)

Test function names transcribe the slice doc's Test Contract bullets
verbatim per the AGENTS.md Test Contract Closure Rule. The tests
use a ``_ControlledEmbedder`` that maps texts to pre-computed vectors
so cosine similarity is deterministic and controllable.

    uv run pytest tests/lifecycle/test_life009_semantic_supersession.py -v
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.maturation import (
    MaturationConfig,
    MaturationCursor,
    OllamaImportance,
    OllamaTopic,
    episodic_maturation_sweep,
)
from musubi.lifecycle.maturation import (
    _find_supersession_candidate as seam_candidate,
)
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.types.episodic import EpisodicMemory


# --------------------------------------------------------------------------- #
# Controlled embedder
# --------------------------------------------------------------------------- #


def _unit_vector(angle: float) -> list[float]:
    return [math.cos(angle), math.sin(angle)]


class _ControlledEmbedder(Embedder):
    """Deterministic embedder for tests; ``_vectors[text]`` sets the
    content's vector. Texts not in the map get a default orthogonal
    vector so similarity is 0 with any explicit vector."""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self._vectors: dict[str, list[float]] = dict(vectors or {})
        # Default: a vector orthogonal to every test vector so unknown
        # texts have similarity 0 with any known vector.
        self._default: list[float] = _pad_to_1024(_unit_vector(math.pi / 2))

    def set(self, text: str, vector: list[float]) -> None:
        self._vectors[text] = _pad_to_1024(vector)

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors.get(t, self._default) for t in texts]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return [{} for _ in texts]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return [0.0 for _ in candidates]


# --------------------------------------------------------------------------- #
# Pre-computed test vectors (1024D to match DENSE_SIZE; first two dims
# encode the semantic angle, rest zero so cosine is preserved).
# --------------------------------------------------------------------------- #


def _vec_high_sim_to_meeting_2pm() -> list[float]:
    return _pad_to_1024(_unit_vector(0.0))


def _vec_meeting_3pm() -> list[float]:
    return _pad_to_1024([math.cos(0.14), math.sin(0.14)])


def _vec_meeting_4pm() -> list[float]:
    return _pad_to_1024([math.cos(0.24), math.sin(0.24)])


def _vec_meeting_5pm_tomorrow() -> list[float]:
    return _pad_to_1024([math.cos(0.30), math.sin(0.30)])


def _vec_sky_green() -> list[float]:
    return _pad_to_1024(_unit_vector(math.pi / 2))


def _vec_alice_said_meeting_2pm() -> list[float]:
    return _pad_to_1024([math.cos(0.05), math.sin(0.05)])


def _vec_bob_said_meeting_2pm() -> list[float]:
    return _pad_to_1024([math.cos(0.06), math.sin(0.06)])


def _pad_to_1024(v: list[float]) -> list[float]:
    """Pad a short vector to 1024 dimensions with zeros so the seam's
    cosine is preserved (the zero suffix contributes nothing)."""
    from musubi.store.specs import DENSE_SIZE

    if len(v) >= DENSE_SIZE:
        return v[:DENSE_SIZE]
    return v + [0.0] * (DENSE_SIZE - len(v))


# --------------------------------------------------------------------------- #
# Fake OllamaClient
# --------------------------------------------------------------------------- #


class _FakeOllama:
    def __init__(self, *, topic_map: dict[str, list[str]] | None = None) -> None:
        self._topic_map: dict[str, list[str]] = dict(topic_map or {})

    async def score_importance(self, items: list[OllamaImportance]) -> dict[str, int] | None:
        return {item.object_id: 8 for item in items}

    async def infer_topics(self, items: list[OllamaTopic]) -> dict[str, list[str]] | None:
        return {item.object_id: self._topic_map.get(item.content, []) for item in items}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


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


async def _seed_matured(
    plane: EpisodicPlane,
    ns: str,
    *,
    content: str,
    topics: list[str],
) -> EpisodicMemory:
    saved = await plane.create(EpisodicMemory(namespace=ns, content=content, topics=topics))
    plane._client.set_payload(
        collection_name="musubi_episodic",
        payload={"state": "matured", "linked_to_topics": topics},
        points=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=saved.object_id)
                )
            ]
        ),
    )
    return await plane.get(namespace=ns, object_id=saved.object_id)  # type: ignore[return-value]


async def _seed_provisional(
    plane: EpisodicPlane,
    ns: str,
    *,
    content: str,
    topics: list[str] | None = None,
) -> EpisodicMemory:
    saved = await plane.create(EpisodicMemory(namespace=ns, content=content, topics=topics or []))
    backdate = datetime.now(UTC) - timedelta(seconds=7200)
    epoch = backdate.timestamp()
    plane._client.set_payload(
        collection_name="musubi_episodic",
        payload={
            "created_at": backdate.isoformat(),
            "created_epoch": epoch,
            "updated_at": backdate.isoformat(),
            "updated_epoch": epoch,
            "linked_to_topics": topics or [],
        },
        points=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=saved.object_id)
                )
            ]
        ),
    )
    refreshed = await plane.get(namespace=ns, object_id=saved.object_id)
    assert refreshed is not None
    return refreshed


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_paraphrase_supersession(qdrant: QdrantClient, ns: str) -> None:
    """Paraphrase supersession: high semantic similarity with
    different wording. The OLD substring logic would NOT match."""
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm today": _vec_high_sim_to_meeting_2pm(),
        }
    )
    # The needle is "today's meeting starts at 2pm" (after stripping
    # "Update:"). Give it an explicit vector so the cosine with the
    # candidate's "the meeting is at 2pm today" vector is high.
    embedder.set(
        "Update: today's meeting starts at 2pm",
        [math.cos(0.10), math.sin(0.10)],
    )
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    predecessor = await _seed_matured(
        plane, ns, content="the meeting is at 2pm", topics=["calendar/meeting"]
    )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: today's meeting starts at 2pm",
        topics=["calendar/meeting"],
    )
    assert result == predecessor.object_id


async def test_correction_supersession_links_to_right_predecessor(
    qdrant: QdrantClient, ns: str
) -> None:
    """Correction supersession: 'Correction: the meeting is at 3pm'
    links to 'the meeting is at 2pm' by semantic similarity. The
    OLD substring logic would miss this."""
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm": _vec_high_sim_to_meeting_2pm(),
            "the meeting is at 3pm": _vec_meeting_3pm(),
        }
    )
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    predecessor = await _seed_matured(
        plane, ns, content="the meeting is at 2pm", topics=["calendar/meeting"]
    )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Correction: the meeting is at 3pm",
        topics=["calendar/meeting"],
    )
    assert result == predecessor.object_id


async def test_negation_supersession_links_to_right_predecessor(
    qdrant: QdrantClient, ns: str
) -> None:
    """Negation supersession: 'Replacing: the sky is not green'
    links to 'the sky is green' by semantic similarity."""
    embedder = _ControlledEmbedder(
        {
            "the sky is green": _vec_sky_green(),
            "the sky is not green": [
                math.cos(math.pi / 2 + 0.10),
                math.sin(math.pi / 2 + 0.10),
            ],
        }
    )
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    predecessor = await _seed_matured(
        plane, ns, content="the sky is green", topics=["world/color"]
    )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Replacing: the sky is not green",
        topics=["world/color"],
    )
    assert result == predecessor.object_id


async def test_participant_change_supersession(qdrant: QdrantClient, ns: str) -> None:
    """Participant change: same topic, high similarity, just the
    participant differs."""
    embedder = _ControlledEmbedder(
        {
            "alice said the meeting is at 2pm": _vec_alice_said_meeting_2pm(),
            "bob said the meeting is at 2pm": _vec_bob_said_meeting_2pm(),
        }
    )
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    predecessor = await _seed_matured(
        plane,
        ns,
        content="alice said the meeting is at 2pm",
        topics=["calendar/meeting"],
    )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: bob said the meeting is at 2pm",
        topics=["calendar/meeting"],
    )
    assert result == predecessor.object_id


async def test_time_change_supersession(qdrant: QdrantClient, ns: str) -> None:
    """Time change: same topic, high similarity, just the time differs."""
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm": _vec_high_sim_to_meeting_2pm(),
            "the meeting is at 3pm": _vec_meeting_3pm(),
        }
    )
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    predecessor = await _seed_matured(
        plane, ns, content="the meeting is at 2pm", topics=["calendar/meeting"]
    )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: the meeting is at 3pm",
        topics=["calendar/meeting"],
    )
    assert result == predecessor.object_id


async def test_unrelated_substring_overlap_does_not_supersede(
    qdrant: QdrantClient, ns: str
) -> None:
    """Unrelated substring overlap: two memories that share a
    substring but are on DIFFERENT topics do NOT supersede."""
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm": _vec_high_sim_to_meeting_2pm(),
            "the meeting is at 5pm tomorrow": _vec_meeting_5pm_tomorrow(),
        }
    )
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    await _seed_matured(
        plane,
        ns,
        content="the meeting is at 5pm tomorrow",
        topics=["calendar/availability"],
    )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: the meeting is at 2pm",
        topics=["calendar/meeting"],
    )
    assert result is None


async def test_ambiguous_candidates_abstain(qdrant: QdrantClient, ns: str) -> None:
    """Two candidates both pass the threshold. The seam must ABSTAIN."""
    # Use DIFFERENT vectors for cand_a and cand_b so the KSUIDs are
    # distinct (KSUIDs are timestamp-based and a tight loop can collide;
    # the production seam never dedupes by vector, only by id).
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm today": _vec_high_sim_to_meeting_2pm(),
            "the meeting is at 2pm tomorrow": _vec_meeting_3pm(),
        }
    )
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    cand_a = await _seed_matured(
        plane, ns, content="the meeting is at 2pm today", topics=["calendar/meeting"]
    )
    cand_b = await _seed_matured(
        plane, ns, content="the meeting is at 2pm tomorrow", topics=["calendar/meeting"]
    )
    # The needle and its update both point at meeting_2pm-ish content;
    # both candidates match by similarity. The seam abstains.
    embedder.set("the meeting is at 2pm this week", _vec_high_sim_to_meeting_2pm())
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: the meeting is at 2pm this week",
        topics=["calendar/meeting"],
        similarity_threshold=0.5,
    )
    assert result is None
    # Sanity: both candidates were seeded (KSUIDs are time-ordered
    # but should differ at the millisecond scale). The test seed
    # uses different content so the plane creates distinct rows.
    # (We don't assert object_id inequality here because KSUID
    # collisions are possible at very tight timing; the test is
    # about the seam's behavior, not the KSUID generator.)


async def test_no_candidates_abstain(qdrant: QdrantClient, ns: str) -> None:
    """No candidates that pass the threshold. The seam must ABSTAIN."""
    # Give the candidate a vector that is ORTHOGONAL to the needle so
    # the similarity is exactly 0. The default vector would match the
    # default needle vector (similarity 1.0), so we set the candidate
    # explicitly.
    embedder = _ControlledEmbedder(
        {
            "the sky is green": _vec_sky_green(),  # orthogonal to meeting_2pm
        }
    )
    embedder.set("Update: the meeting is at 2pm", _vec_high_sim_to_meeting_2pm())
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    await _seed_matured(
        plane, ns, content="the sky is green", topics=["world/color"]
    )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: the meeting is at 2pm",
        topics=["calendar/meeting"],
    )
    assert result is None


async def test_threshold_below_minimum_abstains(qdrant: QdrantClient, ns: str) -> None:
    """A candidate with similarity below the threshold does NOT match."""
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm": _vec_high_sim_to_meeting_2pm(),
            "the meeting is at 2pm ish": [math.cos(math.pi / 3), math.sin(math.pi / 3)],
        }
    )
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    await _seed_matured(
        plane, ns, content="the meeting is at 2pm", topics=["calendar/meeting"]
    )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: the meeting is at 2pm ish",
        topics=["calendar/meeting"],
    )
    assert result is None


async def test_predecessor_and_back_link_correctness_in_sweep(
    qdrant: QdrantClient, ns: str, sink: LifecycleEventSink, cursor: MaturationCursor
) -> None:
    """When the seam detects a predecessor, the maturation sweep
    sets BOTH the predecessor's superseded_by and the new
    memory's supersedes."""
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm": _vec_high_sim_to_meeting_2pm(),
            "the meeting is at 3pm": _vec_meeting_3pm(),
        }
    )
    embedder.set("Correction: the meeting is at 3pm", _vec_meeting_3pm())
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    predecessor = await _seed_matured(
        plane, ns, content="the meeting is at 2pm", topics=["calendar/meeting"]
    )
    new_row = await _seed_provisional(
        plane,
        ns,
        content="Correction: the meeting is at 3pm",
        topics=["calendar/meeting"],
    )
    ollama = _FakeOllama(
        topic_map={"Correction: the meeting is at 3pm": ["calendar/meeting"]}
    )
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=ollama,  # type: ignore[arg-type]
        cursor=cursor,
        config=MaturationConfig(
            min_age_sec=3600,
            batch_size=500,
            provisional_ttl_sec=7 * 86400,
            importance_reenrich_age_sec=7 * 86400,
            demotion_inactivity_sec=30 * 86400,
            concept_min_age_sec=24 * 3600,
            concept_reinforcement_threshold=3,
            tag_aliases={},
        ),
        embedder=embedder,  # type: ignore[arg-type]
    )
    refreshed_predecessor = await plane.get(namespace=ns, object_id=predecessor.object_id)
    refreshed_new = await plane.get(namespace=ns, object_id=new_row.object_id)
    assert refreshed_predecessor is not None
    assert refreshed_new is not None
    assert refreshed_predecessor.superseded_by == new_row.object_id
    assert list(refreshed_new.supersedes) == [predecessor.object_id]


async def test_retry_idempotency_in_sweep(
    qdrant: QdrantClient, ns: str, sink: LifecycleEventSink, cursor: MaturationCursor
) -> None:
    """Running the maturation sweep twice produces the same result."""
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm": _vec_high_sim_to_meeting_2pm(),
            "the meeting is at 3pm": _vec_meeting_3pm(),
        }
    )
    embedder.set("Correction: the meeting is at 3pm", _vec_meeting_3pm())
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    predecessor = await _seed_matured(
        plane, ns, content="the meeting is at 2pm", topics=["calendar/meeting"]
    )
    await _seed_provisional(
        plane,
        ns,
        content="Correction: the meeting is at 3pm",
        topics=["calendar/meeting"],
    )
    ollama = _FakeOllama(
        topic_map={"Correction: the meeting is at 3pm": ["calendar/meeting"]}
    )
    cfg = MaturationConfig(
        min_age_sec=3600,
        batch_size=500,
        provisional_ttl_sec=7 * 86400,
        importance_reenrich_age_sec=7 * 86400,
        demotion_inactivity_sec=30 * 86400,
        concept_min_age_sec=24 * 3600,
        concept_reinforcement_threshold=3,
        tag_aliases={},
    )
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=ollama,  # type: ignore[arg-type]
        cursor=cursor,
        config=cfg,
        embedder=embedder,  # type: ignore[arg-type]
    )
    refreshed_after_first = await plane.get(namespace=ns, object_id=predecessor.object_id)
    assert refreshed_after_first is not None
    first_superseded_by = refreshed_after_first.superseded_by
    assert first_superseded_by is not None
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=ollama,  # type: ignore[arg-type]
        cursor=cursor,
        config=cfg,
        embedder=embedder,  # type: ignore[arg-type]
    )
    refreshed_after_second = await plane.get(namespace=ns, object_id=predecessor.object_id)
    assert refreshed_after_second is not None
    assert refreshed_after_second.superseded_by == first_superseded_by


async def test_bounded_candidate_search(qdrant: QdrantClient, ns: str) -> None:
    """The seam uses a bounded candidate search (default max_candidates=20)."""
    embedder = _ControlledEmbedder(
        {f"the meeting is at {h}pm": _vec_high_sim_to_meeting_2pm() for h in range(25)}
    )
    embedder.set("Update: the meeting is at 2pm", _vec_high_sim_to_meeting_2pm())
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    for h in range(25):
        await _seed_matured(
            plane, ns, content=f"the meeting is at {h}pm", topics=["calendar/meeting"]
        )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: the meeting is at 2pm",
        topics=["calendar/meeting"],
        max_candidates=20,
    )
    assert result is None


async def test_substring_only_does_not_match(qdrant: QdrantClient, ns: str) -> None:
    """A candidate with no semantic match does not link, regardless
    of substring overlap. The OLD substring logic abstained too
    here (no overlap); the test pins the NEW semantic abstention."""
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm": _vec_high_sim_to_meeting_2pm(),
            "completely unrelated content here": _vec_sky_green(),
        }
    )
    embedder.set("Update: the meeting is at 2pm", _vec_high_sim_to_meeting_2pm())
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    await _seed_matured(
        plane,
        ns,
        content="completely unrelated content here",
        topics=["calendar/meeting"],
    )
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: the meeting is at 2pm",
        topics=["calendar/meeting"],
    )
    assert result is None


# GREEN preservation guards


async def test_existing_no_predecessor_branch_still_returns_none(
    qdrant: QdrantClient, ns: str
) -> None:
    """GREEN guard: the no-predecessor branch still returns None."""
    embedder = _ControlledEmbedder()
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    result = await seam_candidate(
        qdrant,
        embedder=embedder,  # type: ignore[arg-type]
        collection="musubi_episodic",
        namespace=ns,
        self_id="new",
        content="Update: novel-only-content",
        topics=["misc"],
    )
    assert result is None


async def test_existing_both_sides_of_link_still_set(
    qdrant: QdrantClient, ns: str, sink: LifecycleEventSink, cursor: MaturationCursor
) -> None:
    """GREEN guard: when the seam infers a predecessor, the
    maturation sweep sets BOTH sides of the lineage."""
    embedder = _ControlledEmbedder(
        {
            "the meeting is at 2pm": _vec_high_sim_to_meeting_2pm(),
            "the meeting is at 3pm": _vec_meeting_3pm(),
        }
    )
    # The needle is "the meeting is at 3pm" (after stripping "Update:").
    # Give it an explicit vector so the cosine with the candidate's
    # meeting_2pm vector is high.
    embedder.set("Update: the meeting is at 3pm", _vec_meeting_3pm())
    plane = EpisodicPlane(client=qdrant, embedder=embedder)  # type: ignore[arg-type]
    predecessor = await _seed_matured(
        plane, ns, content="the meeting is at 2pm", topics=["calendar/meeting"]
    )
    new_row = await _seed_provisional(
        plane, ns, content="Update: the meeting is at 3pm", topics=["calendar/meeting"]
    )
    ollama = _FakeOllama(
        topic_map={"Update: the meeting is at 3pm": ["calendar/meeting"]}
    )
    await episodic_maturation_sweep(
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        ollama=ollama,  # type: ignore[arg-type]
        cursor=cursor,
        config=MaturationConfig(
            min_age_sec=3600,
            batch_size=500,
            provisional_ttl_sec=7 * 86400,
            importance_reenrich_age_sec=7 * 86400,
            demotion_inactivity_sec=30 * 86400,
            concept_min_age_sec=24 * 3600,
            concept_reinforcement_threshold=3,
            tag_aliases={},
        ),
        embedder=embedder,  # type: ignore[arg-type]
    )
    refreshed_predecessor = await plane.get(namespace=ns, object_id=predecessor.object_id)
    refreshed_new = await plane.get(namespace=ns, object_id=new_row.object_id)
    assert refreshed_predecessor is not None
    assert refreshed_new is not None
    assert refreshed_predecessor.superseded_by == new_row.object_id
    assert list(refreshed_new.supersedes) == [predecessor.object_id]
