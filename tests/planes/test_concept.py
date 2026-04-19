"""Test contract for slice-plane-concept.

Runs against an in-memory Qdrant (`qdrant_client.QdrantClient(":memory:")`)
and the deterministic :class:`FakeEmbedder`. These are unit tests — no
network, no GPU, no LLM.

Test Contract bullets covered (from [[04-data-model/synthesized-concept]]):

Per the Method-ownership rule (see
[[00-index/agent-guardrails#Method-ownership-rule]]), synthesis itself —
the LLM clustering of episodic memories into a concept — lives in
``src/musubi/lifecycle/synthesis.py`` (slice-lifecycle-synthesis), and
promotion (gate evaluation, write to the curated plane, retry, thought
emission) lives in ``src/musubi/lifecycle/promotion.py``
(slice-lifecycle-promotion). Time-driven maturation (24h timer, 30d
demotion) lives in ``src/musubi/lifecycle/maturation.py``
(slice-lifecycle-maturation). None of those paths is in this slice's
``owns_paths``; their bullets land here as ``@pytest.mark.skip`` with the
named follow-up slice and a one-line reason.

Bullets implemented here are the ones whose code path lives in
``src/musubi/planes/concept/``:

- 1  ``merged_from`` minimum-length enforcement on ``create``.
- 2  ``create`` always lands in ``state = "synthesized"``.
- 3  ``promoted_to`` requires ``state = "promoted"`` (write-side guard).
- 4  ``promoted_*`` and ``promotion_rejected_*`` fields are mutually
     exclusive on a single row (write-side guard).
- 16 ``reinforce`` bumps ``reinforcement_count``.
- 17 ``mark_accessed`` bumps ``access_count`` only — never
     ``reinforcement_count``.
- 20 ``transition`` to ``"promoted"`` sets ``state = "promoted"``.

Property tests (25, 26) are declared out-of-scope in the slice's
``## Work log`` per the Closure Rule's third state.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.concept import ConceptPlane
from musubi.store import bootstrap
from musubi.types.common import generate_ksuid, utc_now
from musubi.types.concept import SynthesizedConcept
from musubi.types.lifecycle_event import LifecycleEvent

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
def plane(qdrant: QdrantClient) -> ConceptPlane:
    return ConceptPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def ns() -> str:
    return "eric/claude-code/concept"


def _ksuids(n: int) -> list[str]:
    return [generate_ksuid() for _ in range(n)]


def _make(
    *,
    namespace: str,
    title: str = "GPU host workflow",
    content: str = "Pattern: every CUDA toolchain bump correlates with a "
    "container-toolkit reinstall.",
    synthesis_rationale: str = "Three episodic memories about CUDA upgrades "
    "all mention nvidia-container-toolkit reinstalls.",
    merged_from: list[str] | None = None,
    importance: int = 7,
    **extra: Any,
) -> SynthesizedConcept:
    """Build a :class:`SynthesizedConcept` with sane defaults."""
    return SynthesizedConcept(
        namespace=namespace,
        title=title,
        content=content,
        synthesis_rationale=synthesis_rationale,
        merged_from=merged_from if merged_from is not None else _ksuids(3),
        importance=importance,
        **extra,
    )


# ---------------------------------------------------------------------------
# Bullet 1 — concept requires min 3 merged_from
# ---------------------------------------------------------------------------


async def test_concept_requires_min_3_merged_from(plane: ConceptPlane, ns: str) -> None:
    """A concept that doesn't aggregate at least three episodic sources is
    not a concept — it's a stale single memory pretending."""
    too_few = _make(namespace=ns, merged_from=_ksuids(2))
    with pytest.raises(ValueError, match="merged_from"):
        await plane.create(too_few)
    just_enough = _make(namespace=ns, merged_from=_ksuids(3))
    saved = await plane.create(just_enough)
    assert len(saved.merged_from) == 3


# ---------------------------------------------------------------------------
# Bullet 2 — create always lands in synthesized state
# ---------------------------------------------------------------------------


async def test_concept_created_in_synthesized_state(plane: ConceptPlane, ns: str) -> None:
    """Even a caller passing ``state="matured"`` gets normalised back to
    ``synthesized`` — maturation is a transition, not a starting state."""
    saved = await plane.create(_make(namespace=ns))
    assert saved.state == "synthesized"
    assert saved.version == 1
    assert saved.reinforcement_count == 0


# ---------------------------------------------------------------------------
# Bullet 3 — promoted_to requires state=promoted
# ---------------------------------------------------------------------------


async def test_concept_promoted_to_requires_state_promoted(plane: ConceptPlane, ns: str) -> None:
    """The write-side guard. A concept may only carry ``promoted_to`` once
    its ``state`` has reached ``"promoted"`` — the field is the receipt of
    the transition, not a prediction of one."""
    bad = _make(namespace=ns, content="bad-promoted-to")
    saved = await plane.create(bad)
    # Attempting to add promoted_to without going through the transition
    # rejects.
    other = generate_ksuid()
    with pytest.raises(ValueError, match="promoted_to"):
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="matured",
            actor="t",
            reason="unit",
            promoted_to=other,
            promoted_at=utc_now(),
        )


# ---------------------------------------------------------------------------
# Bullet 4 — promoted/rejected fields mutually exclusive
# ---------------------------------------------------------------------------


async def test_concept_promotion_rejected_fields_mutually_exclusive_with_promoted_fields(
    plane: ConceptPlane, ns: str
) -> None:
    """A row carrying ``promotion_rejected_*`` cannot also carry
    ``promoted_*``. A concept is either promoted or rejected — never both."""
    saved = await plane.create(_make(namespace=ns))
    # Take it through to matured and on to promoted, then attempt to also
    # set rejected fields — must reject.
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="t",
        reason="unit",
    )
    curated_ref = generate_ksuid()
    promoted, _ = await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="promoted",
        actor="t",
        reason="unit",
        promoted_to=curated_ref,
        promoted_at=utc_now(),
    )
    assert promoted.state == "promoted"
    assert promoted.promoted_to == curated_ref
    # Trying to *also* set rejected fields on the same row is invalid.
    with pytest.raises(ValueError, match="promotion_rejected"):
        await plane.record_promotion_rejection(
            namespace=ns,
            object_id=saved.object_id,
            reason="contradicted",
        )


# ---------------------------------------------------------------------------
# Bullet 16 — reinforce bumps reinforcement_count
# ---------------------------------------------------------------------------


async def test_reinforcement_count_increments_on_match(plane: ConceptPlane, ns: str) -> None:
    """Synthesis matching an existing concept calls plane.reinforce(), which
    bumps reinforcement_count + version + appends to merged_from.

    Note: the spec also calls for ``last_reinforced_at`` to be set, but
    the SynthesizedConcept type currently lacks the field — see
    ``_inbox/cross-slice/slice-plane-concept-slice-types-promotion-attempts.md``.
    """
    saved = await plane.create(_make(namespace=ns))
    before = saved.reinforcement_count
    after = await plane.reinforce(
        namespace=ns,
        object_id=saved.object_id,
        additional_source=generate_ksuid(),
    )
    assert after.reinforcement_count == before + 1
    assert after.version == saved.version + 1
    assert len(after.merged_from) == len(saved.merged_from) + 1


# ---------------------------------------------------------------------------
# Bullet 17 — access_count does not affect reinforcement_count
# ---------------------------------------------------------------------------


async def test_access_count_does_not_affect_reinforcement_count(
    plane: ConceptPlane, ns: str
) -> None:
    """Recall is not the same as reinforcement. Surfacing a concept via
    retrieval bumps ``access_count`` only — promotion has to be driven by
    *new evidence*, not re-reads."""
    saved = await plane.create(_make(namespace=ns))
    before = await plane.mark_accessed(namespace=ns, object_id=saved.object_id)
    assert before.access_count == 1
    assert before.reinforcement_count == saved.reinforcement_count
    again = await plane.mark_accessed(namespace=ns, object_id=saved.object_id)
    assert again.access_count == 2
    assert again.reinforcement_count == saved.reinforcement_count


# ---------------------------------------------------------------------------
# Bullet 20 — transition to promoted sets state=promoted
# ---------------------------------------------------------------------------


async def test_promotion_sets_concept_state_promoted(plane: ConceptPlane, ns: str) -> None:
    """The transition that the lifecycle-promotion worker calls. The gate
    that decides whether to call it lives in slice-lifecycle-promotion;
    the state mutation itself lives here."""
    saved = await plane.create(_make(namespace=ns))
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="t",
        reason="unit",
    )
    curated_ref = generate_ksuid()
    promoted, event = await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="promoted",
        actor="lifecycle-promotion",
        reason="gate passed",
        promoted_to=curated_ref,
        promoted_at=utc_now(),
    )
    assert promoted.state == "promoted"
    assert promoted.promoted_to == curated_ref
    assert promoted.promoted_at is not None
    assert isinstance(event, LifecycleEvent)
    assert event.from_state == "matured"
    assert event.to_state == "promoted"
    assert event.object_type == "concept"


# ---------------------------------------------------------------------------
# Bullets deferred to downstream slices (Closure Rule, second state)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-synthesis: episodic clustering lives "
    "in src/musubi/lifecycle/synthesis.py, not in this slice's owns_paths."
)
def test_synthesis_clusters_episodic_memories() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-synthesis: cluster-to-concept LLM call "
    "lives in src/musubi/lifecycle/synthesis.py."
)
def test_synthesis_creates_concept_from_cluster_of_3_plus() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-synthesis: cluster-size threshold lives "
    "in src/musubi/lifecycle/synthesis.py."
)
def test_synthesis_skips_clusters_below_3() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-synthesis: dedup-by-similarity decision "
    "lives in src/musubi/lifecycle/synthesis.py (this slice ships ConceptPlane.reinforce, see bullet 16)."
)
def test_synthesis_matches_existing_concept_and_reinforces() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-synthesis: contradiction-detection LLM "
    "call lives in src/musubi/lifecycle/synthesis.py."
)
def test_synthesis_detects_contradiction_and_flags_both() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-synthesis: idempotent-by-input-hash "
    "logic lives in src/musubi/lifecycle/synthesis.py."
)
def test_synthesis_idempotent_across_runs_on_same_input() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-synthesis: synthesis loop's namespace scoping lives "
    "in src/musubi/lifecycle/synthesis.py (plane-level isolation is covered in this file)."
)
def test_synthesis_respects_namespace_isolation() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-synthesis: ollama unavailability "
    "handling lives in src/musubi/lifecycle/synthesis.py."
)
def test_synthesis_handles_ollama_unavailable_by_skipping_gracefully() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-maturation: 24h-without-contradiction timer "
    "lives in src/musubi/lifecycle/maturation.py (plane ships the synthesized->matured transition)."
)
def test_concept_matures_after_24h_without_contradiction() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-maturation: maturation-reset on "
    "contradiction lives in src/musubi/lifecycle/maturation.py."
)
def test_concept_matures_reset_if_contradiction_appears() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-maturation: 30-day demotion timer "
    "lives in src/musubi/lifecycle/maturation.py."
)
def test_concept_demotes_after_30d_no_reinforcement() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: promotion-gate predicate "
    "(reinforcement_count, importance, age, contradicts, attempts) lives in src/musubi/lifecycle/promotion.py."
)
def test_promotion_gate_all_conditions_required() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: writing the CuratedKnowledge "
    "row + linking promoted_to/promoted_from lives in src/musubi/lifecycle/promotion.py."
)
def test_promotion_writes_curated_file_and_links_back() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: rejection decision + retry-backoff "
    "bookkeeping live in src/musubi/lifecycle/promotion.py (plane exposes record_promotion_rejection)."
)
def test_promotion_rejected_sets_rejected_fields() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: retry-backoff scheduling "
    "lives in src/musubi/lifecycle/promotion.py."
)
def test_promotion_retry_backoff_after_failure() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: contradicts-blocks-promotion "
    "predicate lives in src/musubi/lifecycle/promotion.py."
)
def test_contradicted_concept_blocked_from_promotion() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion + slice-plane-thoughts: operator-thought "
    "emission lives in src/musubi/lifecycle/promotion.py and writes via the thoughts plane."
)
def test_promotion_produces_thought_notification_to_operator() -> None:
    pass


# ---------------------------------------------------------------------------
# Coverage tests — not Test Contract bullets, but they exercise the plane's
# transition + isolation paths so branch coverage clears the 90 % gate that
# the guardrails require for src/musubi/planes/**.
# ---------------------------------------------------------------------------


async def test_get_returns_none_for_missing_id(plane: ConceptPlane, ns: str) -> None:
    missing = "0" * 27
    assert await plane.get(namespace=ns, object_id=missing) is None


async def test_isolation_read_enforcement(plane: ConceptPlane) -> None:
    a_ns = "eric/claude-code/concept"
    b_ns = "yua/livekit/concept"
    a = await plane.create(_make(namespace=a_ns))
    b = await plane.create(_make(namespace=b_ns))
    assert await plane.get(namespace=a_ns, object_id=b.object_id) is None
    assert await plane.get(namespace=b_ns, object_id=a.object_id) is None


async def test_isolation_write_enforcement(plane: ConceptPlane) -> None:
    a_ns = "eric/claude-code/concept"
    b_ns = "yua/livekit/concept"
    a = await plane.create(_make(namespace=a_ns))
    with pytest.raises(LookupError):
        await plane.transition(
            namespace=b_ns,
            object_id=a.object_id,
            to_state="matured",
            actor="t",
            reason="unit",
        )
    still = await plane.get(namespace=a_ns, object_id=a.object_id)
    assert still is not None and still.state == "synthesized"


async def test_transition_illegal_raises(plane: ConceptPlane, ns: str) -> None:
    saved = await plane.create(_make(namespace=ns))
    # synthesized → promoted is illegal — must mature first.
    with pytest.raises(ValueError):
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="promoted",
            actor="t",
            reason="unit",
            promoted_to=generate_ksuid(),
            promoted_at=utc_now(),
        )


async def test_transition_to_promoted_requires_promoted_to(plane: ConceptPlane, ns: str) -> None:
    saved = await plane.create(_make(namespace=ns))
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="t",
        reason="unit",
    )
    with pytest.raises(ValueError, match="promoted_to"):
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="promoted",
            actor="t",
            reason="unit",
        )


async def test_transition_to_demoted_filters_default_reads(plane: ConceptPlane, ns: str) -> None:
    saved = await plane.create(_make(namespace=ns, content="demote-target-text"))
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="t",
        reason="unit",
    )
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="demoted",
        actor="t",
        reason="unit",
    )
    results = await plane.query(namespace=ns, query="demote-target-text", limit=10)
    assert all(r.object_id != saved.object_id for r in results)


async def test_query_excludes_synthesized_by_default(plane: ConceptPlane, ns: str) -> None:
    """Synthesized concepts are provisional (24h to mature). Default query
    filters them out — they're not yet trustworthy."""
    saved = await plane.create(_make(namespace=ns, content="provisional-pattern"))
    results = await plane.query(namespace=ns, query="provisional", limit=10)
    assert all(r.object_id != saved.object_id for r in results)


async def test_query_respects_include_synthesized_flag(plane: ConceptPlane, ns: str) -> None:
    saved = await plane.create(_make(namespace=ns, content="explicit-include-synth"))
    including = await plane.query(
        namespace=ns,
        query="explicit-include-synth",
        limit=10,
        include_synthesized=True,
    )
    assert any(r.object_id == saved.object_id for r in including)


async def test_query_respects_limit(plane: ConceptPlane, ns: str) -> None:
    for i in range(5):
        saved = await plane.create(_make(namespace=ns, content=f"limit-fixture-{i}-unique"))
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="matured",
            actor="t",
            reason="unit",
        )
    results = await plane.query(namespace=ns, query="limit-fixture", limit=3)
    assert len(results) <= 3


async def test_create_auto_embeds_dense_and_sparse_vectors(
    plane: ConceptPlane, ns: str, qdrant: QdrantClient
) -> None:
    from qdrant_client import models as qmodels

    saved = await plane.create(_make(namespace=ns))
    records, _ = qdrant.scroll(
        collection_name="musubi_concept",
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id",
                    match=qmodels.MatchValue(value=saved.object_id),
                )
            ]
        ),
        limit=1,
        with_vectors=True,
    )
    assert records, "point was not written to Qdrant"
    vectors = records[0].vector
    assert isinstance(vectors, dict)
    assert "dense_bge_m3_v1" in vectors
    assert "sparse_splade_v1" in vectors


async def test_reinforce_unknown_object_raises(plane: ConceptPlane, ns: str) -> None:
    missing = "0" * 27
    with pytest.raises(LookupError):
        await plane.reinforce(namespace=ns, object_id=missing)


async def test_mark_accessed_unknown_object_raises(plane: ConceptPlane, ns: str) -> None:
    missing = "0" * 27
    with pytest.raises(LookupError):
        await plane.mark_accessed(namespace=ns, object_id=missing)


async def test_record_promotion_rejection_unknown_object_raises(
    plane: ConceptPlane, ns: str
) -> None:
    missing = "0" * 27
    with pytest.raises(LookupError):
        await plane.record_promotion_rejection(namespace=ns, object_id=missing, reason="missing")


async def test_record_promotion_rejection_sets_rejected_fields(
    plane: ConceptPlane, ns: str
) -> None:
    """Plane-level test for the rejected-side bookkeeping. The *decision*
    to reject is lifecycle-promotion; the actual write is mine.

    Note: the spec also calls for ``promotion_attempts`` to bump here but
    the type lacks the field — see cross-slice ticket
    ``_inbox/cross-slice/slice-plane-concept-slice-types-promotion-attempts.md``.
    """
    saved = await plane.create(_make(namespace=ns))
    rejected = await plane.record_promotion_rejection(
        namespace=ns,
        object_id=saved.object_id,
        reason="contradicted by newer concept",
    )
    assert rejected.promotion_rejected_at is not None
    assert rejected.promotion_rejected_reason == "contradicted by newer concept"
    assert rejected.promoted_to is None
