"""``ConceptPlane`` — Qdrant CRUD + lifecycle for :class:`SynthesizedConcept`.

Responsibilities (see [[04-data-model/synthesized-concept]] §Allowed
lifecycle states + §Reinforcement):

- **Create** — write a concept at ``state = "synthesized"``, ``version = 1``,
  ``reinforcement_count = 0``. Two write-side invariants the type model
  doesn't enforce on its own land here, because the synthesis worker is
  the *only* expected caller and we'd rather a programming bug surface as
  ``ValueError`` than a malformed Qdrant row:

  - ``len(merged_from) >= 3`` — a concept that doesn't aggregate at least
    three episodic sources isn't a concept, it's a mislabelled single
    memory (Test Contract bullet 1).
  - ``promoted_*`` and ``promotion_rejected_*`` are mutually exclusive on
    a single row. A concept is either promoted (success) or rejected
    (failure) — never both (bullet 4).

- **Get** — fetch one concept by namespace + ``object_id``.
  Wrong-namespace lookups return ``None`` (read-side namespace isolation).

- **Query** — dense retrieval scoped to a namespace. Default visible
  states are ``{matured, promoted}``: ``synthesized`` is provisional
  (sits 24h before the maturation worker promotes it), ``demoted``,
  ``superseded`` and ``archived`` are not in the default view.
  ``include_synthesized=True`` opts the provisional set in.

- **Reinforce** — bumps ``reinforcement_count``, ``last_reinforced_at``,
  and (optionally) appends a new source ``KSUID`` to ``merged_from``.
  This is the entrypoint the synthesis worker calls when a freshly
  matured episodic memory dense-matches an existing concept (Test
  Contract bullet 16). The synthesis-side decision logic (which concepts
  to match, similarity threshold) lives in
  ``src/musubi/lifecycle/synthesis.py``.

- **Mark accessed** — bumps ``access_count`` + ``last_accessed_at`` only.
  Never touches ``reinforcement_count``: per the spec, recall is not
  reinforcement (bullet 17). Promotion has to be driven by *new evidence*.

- **Transition** — the only path allowed to mutate ``state``. Emits a
  :class:`LifecycleEvent` whose validator enforces the concept transition
  table (``synthesized → matured → {promoted, demoted, superseded}``,
  ``* → archived``). Transitions to ``"promoted"`` require the caller to
  pass ``promoted_to`` + ``promoted_at`` — the type model also validates
  this on the resulting row, but we surface it eagerly with a clearer
  error message (bullet 3). Promotion to a CuratedKnowledge row + the
  gate that decides whether to promote both live in
  ``src/musubi/lifecycle/promotion.py``; the state mutation lives here.

- **Record promotion rejection** — sets ``promotion_rejected_at``,
  ``promotion_rejected_reason``, and bumps ``promotion_attempts``. Mirror
  of the promoted-side bookkeeping for the failure path; the *decision*
  to reject (which retry backoff, which contradiction wins, etc.) lives
  in ``src/musubi/lifecycle/promotion.py``.

Design notes mirror the curated plane:

- Qdrant point IDs derive from a slice-specific ``uuid5`` namespace —
  distinct from episodic's ``...01`` and curated's ``...02`` so the three
  collections' point ID spaces never overlap even if KSUIDs ever do.
- Every state-mutating path round-trips through ``model_dump`` +
  ``model_validate`` — pydantic v2's ``model_copy(update=...)`` skips
  validators, which would let invariants like
  ``state="promoted" requires promoted_to`` silently break.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator, TransitionPending
from musubi.lifecycle.transitions import (
    LineageUpdates,
    TransitionError,
    TransitionResult,
    transition,
)
from musubi.store.access_lease import lease_increment_access
from musubi.store.mutation_lease import MutationPlan, owned_update
from musubi.store.names import collection_for_plane
from musubi.store.raw_lookup import point_exists, raw_payload
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import KSUID, Err, LifecycleState, Namespace, Result, epoch_of, utc_now
from musubi.types.concept import SynthesizedConcept

# Distinct from episodic's ...01 and curated's ...02 — three collections,
# three disjoint point-ID namespaces.
_POINT_NS = uuid.UUID("6b0d5e2e-1e8e-4e0f-8e3e-000000000003")

_MIN_MERGED_FROM = 3
"""A concept must aggregate at least three episodic sources — otherwise
it's a single memory pretending to be a pattern. The type model's
``MemoryObject.merged_from`` field doesn't constrain length (it's shared
across plane types that have looser rules), so the plane enforces it."""

_VISIBLE_STATES: tuple[LifecycleState, ...] = ("matured", "promoted")
_VISIBLE_STATES_WITH_SYNTHESIZED: tuple[LifecycleState, ...] = (
    "synthesized",
    "matured",
    "promoted",
)


def _point_id(object_id: str) -> str:
    """Return the deterministic Qdrant point ID for a given KSUID."""
    return str(uuid.uuid5(_POINT_NS, object_id))


def _sparse_to_model(sparse: dict[int, float]) -> models.SparseVector:
    """Convert a sparse ``{index: value}`` dict to Qdrant's ``SparseVector``."""
    return models.SparseVector(
        indices=list(sparse.keys()),
        values=list(sparse.values()),
    )


def _concept_from_payload(payload: dict[str, Any]) -> SynthesizedConcept:
    """Rehydrate a :class:`SynthesizedConcept` from a Qdrant payload dict.

    ``model_validate`` re-runs the promoted_* + monotonicity validators
    so the plane never hands out a half-constructed object.
    """
    return SynthesizedConcept.model_validate(payload)


def _embed_target(memory: SynthesizedConcept) -> str:
    """Text fed to the embedder.

    Title + the synthesis_rationale captures the *what* and *why* of the
    concept in two short fields — better signal than the (often
    LLM-generated, often padded) full content.
    """
    return f"{memory.title}\n\n{memory.synthesis_rationale}"


def _has_promoted_fields(memory: SynthesizedConcept) -> bool:
    return memory.promoted_to is not None or memory.promoted_at is not None


def _has_rejected_fields(memory: SynthesizedConcept) -> bool:
    return memory.promotion_rejected_at is not None or memory.promotion_rejected_reason is not None


class ConceptPlane:
    """CRUD + lifecycle transitions for the synthesized-concept plane."""

    def __init__(self, *, client: QdrantClient, embedder: Embedder) -> None:
        self._client = client
        self._embedder = embedder
        self._collection = collection_for_plane("concept")

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, memory: SynthesizedConcept) -> SynthesizedConcept:
        """Insert ``memory`` at ``state = "synthesized"``.

        Caller-supplied ``state``, ``version``, ``reinforcement_count`` are
        normalised so the synthesis worker can't accidentally short-circuit
        the maturation step. The two write-side invariants the model
        doesn't enforce land here.
        """
        if len(memory.merged_from) < _MIN_MERGED_FROM:
            raise ValueError(
                f"merged_from must list at least {_MIN_MERGED_FROM} source ids; "
                f"got {len(memory.merged_from)}"
            )
        if _has_promoted_fields(memory) and _has_rejected_fields(memory):
            raise ValueError(
                "promoted_* and promotion_rejected_* fields are mutually "
                "exclusive on a single concept row"
            )

        now = utc_now()
        data = memory.model_dump()
        data.update(
            state="synthesized",
            version=1,
            reinforcement_count=0,
            access_count=0,
            promoted_to=None,
            promoted_at=None,
            promotion_rejected_at=None,
            promotion_rejected_reason=None,
            created_at=now,
            created_epoch=epoch_of(now),
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        fresh = SynthesizedConcept.model_validate(data)
        dense, sparse = await self._embed_both(_embed_target(fresh))
        self._upsert(fresh, dense=dense, sparse=sparse)
        return fresh

    def _upsert(
        self,
        memory: SynthesizedConcept,
        *,
        dense: list[float],
        sparse: dict[int, float],
    ) -> None:
        point = models.PointStruct(
            id=_point_id(memory.object_id),
            payload=memory.model_dump(mode="json"),
            vector={
                DENSE_VECTOR_NAME: dense,
                SPARSE_VECTOR_NAME: _sparse_to_model(sparse),
            },
        )
        self._client.upsert(collection_name=self._collection, points=[point])

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def exists(self, *, namespace: Namespace, object_id: KSUID) -> bool:
        """Is this row present? Answered WITHOUT deserializing it.

        ``get()`` model-validates, so it raises on a corrupted row — which meant any
        caller using it merely to ask "is it there?" inherited a hard failure on
        exactly the rows that are broken, and a corrupted row could not be deleted or
        archived. The removability of a memory must never depend on that memory being
        valid. See :mod:`musubi.store.raw_lookup`.
        """
        return point_exists(
            self._client, self._collection, namespace=namespace, object_id=object_id
        )

    async def raw_payload(self, *, namespace: Namespace, object_id: KSUID) -> dict[str, Any] | None:
        """The stored payload exactly as persisted — never model-validated.

        The inspection/repair door for a row the model refuses to open. Treat every key
        as untrusted: ``.get()`` with a default, never index.
        """
        return raw_payload(self._client, self._collection, namespace=namespace, object_id=object_id)

    async def get(self, *, namespace: Namespace, object_id: KSUID) -> SynthesizedConcept | None:
        """Fetch one concept by id, scoped to ``namespace``.

        Wrong-namespace lookups return ``None`` — this is how the read
        path enforces namespace isolation.
        """
        records, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=object_id)
                    ),
                ]
            ),
            limit=1,
            with_payload=True,
        )
        if not records:
            return None
        payload = records[0].payload
        if not payload:
            return None
        return _concept_from_payload(payload)

    async def query(
        self,
        *,
        namespace: Namespace,
        query: str,
        limit: int = 10,
        include_synthesized: bool = False,
    ) -> list[SynthesizedConcept]:
        """Dense retrieval over the concept plane, namespace-scoped.

        Default visible states are ``{matured, promoted}`` — synthesized
        concepts are still provisional (24h before the maturation worker
        promotes them) and shouldn't surface to ordinary callers.
        ``include_synthesized=True`` opts them in for the synthesis worker
        and for introspection.
        """
        visible = _VISIBLE_STATES_WITH_SYNTHESIZED if include_synthesized else _VISIBLE_STATES
        dense = (await self._embedder.embed_dense([query]))[0]
        resp = self._client.query_points(
            collection_name=self._collection,
            query=dense,
            using=DENSE_VECTOR_NAME,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="state",
                        match=models.MatchAny(any=[str(s) for s in visible]),
                    ),
                ]
            ),
            limit=limit,
            with_payload=True,
        )
        out: list[SynthesizedConcept] = []
        for point in resp.points:
            if point.payload:
                out.append(_concept_from_payload(point.payload))
        return out

    # ------------------------------------------------------------------
    # Reinforce + access
    # ------------------------------------------------------------------

    async def reinforce(
        self,
        *,
        namespace: Namespace,
        object_id: KSUID,
        additional_source: KSUID | None = None,
    ) -> SynthesizedConcept:
        """Bump ``reinforcement_count``, ``last_reinforced_at``, ``version``.

        Optionally append ``additional_source`` to ``merged_from`` — the
        synthesis worker passes the id of the freshly-matured episodic
        memory that triggered the match. Duplicate sources are silently
        deduplicated; ``merged_from`` is a set semantically even though
        the field type is ``list``.
        """
        current = await self.get(namespace=namespace, object_id=object_id)
        if current is None:
            raise LookupError(f"concept {object_id!r} not found in namespace {namespace!r}")

        # Publish the reinforcement through the attributable mutation lease (DATA-001 #530): a NARROW
        # change-set recomputed against the FRESH row each round (merged_from deduped against current),
        # fenced on version — a concurrent unrelated mutation is never overwritten.
        def plan(cur: dict[str, Any]) -> MutationPlan:
            now2 = utc_now()
            merged = list(cur.get("merged_from", []))
            if additional_source is not None and additional_source not in merged:
                merged.append(additional_source)
            data = {
                **cur,
                "reinforcement_count": int(cur.get("reinforcement_count", 0)) + 1,
                "last_reinforced_at": now2,
                "last_reinforced_epoch": epoch_of(now2),
                "merged_from": merged,
                "updated_at": now2,
                "updated_epoch": epoch_of(now2),
            }
            dumped = SynthesizedConcept.model_validate(data).model_dump(mode="json")
            keys = (
                "reinforcement_count",
                "last_reinforced_at",
                "last_reinforced_epoch",
                "merged_from",
                "updated_at",
                "updated_epoch",
            )
            return MutationPlan(changes={k: dumped[k] for k in keys})

        published = owned_update(
            self._client,
            self._collection,
            namespace=str(namespace),
            object_id=str(object_id),
            point_id=_point_id(object_id),
            plan=plan,
        )
        return SynthesizedConcept.model_validate(published)

    async def mark_accessed(self, *, namespace: Namespace, object_id: KSUID) -> SynthesizedConcept:
        """Bump ``access_count`` + ``last_accessed_at`` only.

        Never touches ``reinforcement_count``: recall ≠ reinforcement.
        Promotion is driven by *new evidence*, so a thousand re-reads
        still leaves a concept stuck below the promotion gate's
        reinforcement threshold.
        """
        current = await self.get(namespace=namespace, object_id=object_id)
        if current is None:
            raise LookupError(f"concept {object_id!r} not found in namespace {namespace!r}")
        # RET-008 (#502): route the access_count bump through the shared fenced lease so it never
        # races a concurrent leased increment (or resets one). The lease owns access_count +
        # last_accessed_at; re-read to return the post-bump row.
        await lease_increment_access(
            self._client, self._collection, {(str(namespace), str(object_id))}
        )
        refreshed = await self.get(namespace=namespace, object_id=object_id)
        return refreshed if refreshed is not None else current

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def transition(
        self,
        *,
        namespace: Namespace,
        object_id: KSUID,
        to_state: LifecycleState,
        actor: str,
        reason: str,
        promoted_to: KSUID | None = None,
        promoted_at: datetime | None = None,
        coordinator: LifecycleTransitionCoordinator,
    ) -> Result[TransitionResult | TransitionPending, TransitionError]:
        """Delegate the namespace-scoped transition and atomic promotion receipt.

        - Transitions to ``"promoted"`` require both ``promoted_to`` and
          ``promoted_at`` (the type model also enforces this on the
          resulting row; the plane checks first for a clearer error).
        - Transitions to anywhere else MUST NOT carry ``promoted_to`` —
          ``promoted_to`` is the receipt of a successful promotion, not a
          predicate of one (Test Contract bullet 3).
        """
        if to_state == "promoted":
            if promoted_to is None or promoted_at is None:
                return Err(
                    error=TransitionError(
                        code="invariant_violation",
                        message="transition to 'promoted' requires promoted_to and promoted_at",
                        to_state=to_state,
                    )
                )
        elif promoted_to is not None or promoted_at is not None:
            return Err(
                error=TransitionError(
                    code="invariant_violation",
                    message=(
                        "promoted_to/promoted_at may only be set when transitioning to "
                        f"'promoted'; got to_state={to_state!r}"
                    ),
                    to_state=to_state,
                )
            )

        current = await self.get(namespace=namespace, object_id=object_id)
        if current is None:
            return Err(
                error=TransitionError(
                    code="not_found",
                    message=f"concept {object_id!r} not found in namespace {namespace!r}",
                    to_state=to_state,
                )
            )
        return transition(
            self._client,
            coordinator=coordinator,
            object_id=object_id,
            target_state=to_state,
            actor=actor,
            reason=reason,
            expected_version=current.version,
            lineage_updates=LineageUpdates(
                promoted_to=promoted_to,
                promoted_at=promoted_at,
            ),
        )

    async def record_promotion_rejection(
        self,
        *,
        namespace: Namespace,
        object_id: KSUID,
        reason: str,
    ) -> SynthesizedConcept:
        """Set ``promotion_rejected_at`` + ``promotion_rejected_reason`` and
        bump ``promotion_attempts``.

        The mirror of the promoted-side bookkeeping for the failure path.
        Refuses to write rejection on a row that is already ``promoted``
        (the two outcomes are mutually exclusive — see Test Contract
        bullet 4). The *decision* to reject — retry backoff, contradiction
        priority, etc. — lives in ``src/musubi/lifecycle/promotion.py``.
        """
        if not reason:
            raise ValueError("promotion_rejected_reason must be a non-empty string")
        current = await self.get(namespace=namespace, object_id=object_id)
        if current is None:
            raise LookupError(f"concept {object_id!r} not found in namespace {namespace!r}")
        if _has_promoted_fields(current):
            raise ValueError(
                "promotion_rejected_* cannot be set on a row that already "
                "carries promoted_to/promoted_at — the two outcomes are "
                "mutually exclusive"
            )

        # Publish through the attributable mutation lease (DATA-001 #530): NARROW change-set fenced on
        # version, promotion_attempts incremented against the FRESH row. The mutual-exclusivity guard
        # is re-checked on the current row so a concurrent promotion cannot slip a rejection past it.
        def plan(cur: dict[str, Any]) -> MutationPlan:
            fresh = SynthesizedConcept.model_validate(cur)
            if _has_promoted_fields(fresh):
                raise ValueError(
                    "promotion_rejected_* cannot be set on a row that already "
                    "carries promoted_to/promoted_at — the two outcomes are "
                    "mutually exclusive"
                )
            now2 = utc_now()
            data = {
                **cur,
                "promotion_rejected_at": now2,
                "promotion_rejected_reason": reason,
                "promotion_attempts": int(cur.get("promotion_attempts", 0)) + 1,
                "updated_at": now2,
                "updated_epoch": epoch_of(now2),
            }
            dumped = SynthesizedConcept.model_validate(data).model_dump(mode="json")
            keys = (
                "promotion_rejected_at",
                "promotion_rejected_reason",
                "promotion_attempts",
                "updated_at",
                "updated_epoch",
            )
            return MutationPlan(changes={k: dumped[k] for k in keys})

        published = owned_update(
            self._client,
            self._collection,
            namespace=str(namespace),
            object_id=str(object_id),
            point_id=_point_id(object_id),
            plan=plan,
        )
        return SynthesizedConcept.model_validate(published)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _embed_both(self, text: str) -> tuple[list[float], dict[int, float]]:
        dense = (await self._embedder.embed_dense([text]))[0]
        sparse = (await self._embedder.embed_sparse([text]))[0]
        return dense, sparse


__all__ = ["ConceptPlane"]
