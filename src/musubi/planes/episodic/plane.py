"""``EpisodicPlane`` — CRUD + lifecycle for :class:`EpisodicMemory`.

Responsibilities (from [[04-data-model/episodic-memory]]):

- **Create** — always ``state = "provisional"``, ``version = 1``. Auto-embed
  dense + sparse. Dedup against existing points in the same namespace via
  dense cosine similarity; on hit, merges ONLY when factual compatibility passes
  (rejecting negations, corrections, specific participant or time changes).
  On compatible hit, merges tags, bumps ``reinforcement_count`` and ``version``,
  and decides content retention based on ``merge_strategy``.
- **Get** — fetch by namespace + ``object_id``. Namespace scoping is
  enforced so a caller asking for the wrong namespace sees ``None``.
- **Query** — dense retrieval filtered to the caller's namespace. Default
  state filter excludes ``provisional`` and ``archived``; ``include_demoted``
  opts into demoted rows.
- **Transition** — the *only* code path allowed to mutate ``state``. Emits a
  :class:`LifecycleEvent` that self-validates against the transition table
  in :mod:`musubi.types.lifecycle_event`, so illegal transitions raise.

Design notes:

- Qdrant requires integer or UUID point IDs, but Musubi's object IDs are
  KSUIDs. We derive a deterministic ``uuid5(_POINT_NS, ksuid)`` for each
  point so the same KSUID always maps to the same Qdrant ID. The KSUID
  itself stays in the payload as ``object_id``.
- ``model_copy(update=...)`` doesn't re-run validators in pydantic v2, so
  state-mutating paths round-trip through ``model_dump`` +
  ``model_validate`` to keep the monotonicity invariant honest.
- This first cut does dense-only retrieval. Hybrid dense+sparse fusion is
  a slice-retrieval-fast concern.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, cast, get_args

from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator, TransitionPending
from musubi.lifecycle.transitions import TransitionError, TransitionResult, transition
from musubi.store.access_lease import lease_increment_access
from musubi.store.memory_serialization import memory_update_payload, preserve_lease_fields
from musubi.store.names import collection_for_plane
from musubi.store.raw_lookup import point_exists, raw_payload, retrieve_by_point_id
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import (
    KSUID,
    Err,
    LifecycleState,
    Namespace,
    Result,
    epoch_of,
    utc_now,
    validate_namespace,
)
from musubi.types.episodic import EpisodicMemory
from musubi.types.lifecycle_event import LifecycleEvent

# Namespace UUID used to derive point IDs from KSUIDs. Random but fixed
# forever — any change to this constant would orphan every stored point.
_POINT_NS = uuid.UUID("6b0d5e2e-1e8e-4e0f-8e3e-000000000001")

_DEFAULT_DEDUP_THRESHOLD = 0.92
_VISIBLE_STATES: tuple[LifecycleState, ...] = ("matured",)
_VISIBLE_STATES_WITH_DEMOTED: tuple[LifecycleState, ...] = ("matured", "demoted")

# Content-merge strategy on dedup hit.
#
#   ``longer-wins`` — keep whichever of existing / new content is
#   strictly longer. Matches the spec ([[06-ingestion/capture]]
#   § Step 4): a short follow-up shouldn't silently overwrite a
#   detailed earlier capture.
#
#   ``replace`` — always take the new content. Pre-spec behaviour;
#   preserved for explicit callers (migration / replay paths that
#   genuinely want the newest text).
MergeStrategy = Literal["replace", "longer-wins"]


def episodic_point_id(object_id: str) -> str:
    """Return the deterministic Qdrant point ID for an episodic KSUID.

    Public because cross-module callers (e.g. the lifecycle synthesis
    candidate-fetch path) need to translate object_ids → point_ids to
    `client.retrieve()` episodic points. Keeping this private would
    force those callers into reach-into-private-helper patterns that
    silently break on refactor.
    """
    return str(uuid.uuid5(_POINT_NS, object_id))


# Backwards-compatible alias for in-module callers; removable once all
# internal references are migrated to the public name.
_point_id = episodic_point_id


def _sparse_to_model(sparse: dict[int, float]) -> models.SparseVector:
    """Convert a sparse ``{index: value}`` dict to Qdrant's ``SparseVector``."""
    return models.SparseVector(
        indices=list(sparse.keys()),
        values=list(sparse.values()),
    )


def _memory_from_payload(payload: dict[str, Any]) -> EpisodicMemory:
    """Rehydrate an :class:`EpisodicMemory` from a Qdrant payload dict.

    Uses ``model_validate`` so the monotonicity + consistency validators run
    — the plane must never hand out a half-constructed object.
    """
    return EpisodicMemory.model_validate(payload)


def _is_factually_compatible(existing: EpisodicMemory, new: EpisodicMemory) -> bool:
    """Return whether a cosine hit is safe to merge.

    Compatibility is fail-closed: normalized content and the complete
    participants set must both match before dedup may mutate an existing row.
    """
    import re
    import unicodedata

    def _normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text)
        text = text.casefold()
        text = re.sub(r"\s+", " ", text)
        text = text.strip()
        text = re.sub(r"[.!?]+$", "", text)
        return text.rstrip()

    if _normalize(existing.content) != _normalize(new.content):
        return False

    return sorted(existing.participants) == sorted(new.participants)


class EpisodicPlane:
    """CRUD + lifecycle transitions for the episodic plane."""

    def __init__(
        self,
        *,
        client: QdrantClient,
        embedder: Embedder,
        dedup_threshold: float = _DEFAULT_DEDUP_THRESHOLD,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._collection = collection_for_plane("episodic")
        self._dedup_threshold = dedup_threshold

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        memory: EpisodicMemory,
        *,
        merge_strategy: MergeStrategy = "longer-wins",
        preserve_created_at: bool = False,
    ) -> EpisodicMemory:
        """Write ``memory`` to Qdrant, deduping against the same namespace.

        Dedup evaluates dense cosine similarity, but merges ONLY if the
        candidate passes strict factual compatibility (rejecting semantic
        near-matches like negations or participant alterations). If incompatible,
        inserts a fresh distinct row.

        On a compatible dedup hit, merges tags + bumps ``reinforcement_count`` and
        ``version`` on the existing row instead of inserting. ``content``
        is kept vs replaced based on ``merge_strategy`` ONLY after compatibility
        authorizes the merge:

        - ``longer-wins`` (default, matches spec §06-ingestion/capture):
          keep whichever of existing / new content is strictly longer.
        - ``replace``: always take the new content. Explicit opt-in for
          migration / replay paths that genuinely want the newest text.

        Returns the final state of the row (original object id on dedup
        hit, new id on fresh insert).

        ``preserve_created_at`` controls whether the incoming
        ``memory.created_at`` is used verbatim (migration / replay path)
        or replaced with ``utc_now()``. Default False keeps the historical
        behaviour: every fresh insert gets a server-assigned ingest
        timestamp. The migration path (API #140, SDK capture with
        operator scope + explicit created_at) flips this on so source
        timestamps round-trip through ingest.
        """
        if len(memory.content.encode("utf-8")) > 32768:
            raise ValueError("content exceeds 32KB limit, please use artifact plane instead")
        if memory.event_at > utc_now():
            raise ValueError("event_at cannot be in the future")
        import re

        if not re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", memory.namespace):
            raise ValueError("invalid namespace format")

        now = utc_now()
        # Reject a future `created_at` on the preserve path up front. Without
        # this, `EpisodicMemory.model_validate(...)` below would raise an opaque
        # "updated_at precedes created_at" when `updated_at=now` lands, which
        # surfaces as a 500. A direct guard gives the caller a clear message.
        if preserve_created_at and memory.created_at > now:
            raise ValueError("created_at cannot be in the future")
        created_at = memory.created_at if preserve_created_at else now
        text = memory.summary or memory.content
        dense, sparse = await self._embed_both(text)

        if len(dense) != 1024:
            raise ValueError(f"vector dimension mismatch: got {len(dense)}, expected 1024")

        found = self._find_dedup_candidate(memory.namespace, dense)
        if found is not None:
            existing, existing_dense, existing_sparse = found
            if _is_factually_compatible(existing, memory):
                return self._reinforce(
                    existing=existing,
                    existing_dense=existing_dense,
                    existing_sparse=existing_sparse,
                    new=memory,
                    dense=dense,
                    sparse=sparse,
                    merge_strategy=merge_strategy,
                )

        data = memory.model_dump()
        data.update(
            state="provisional",
            version=1,
            reinforcement_count=0,
            created_at=created_at,
            created_epoch=epoch_of(created_at),
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        fresh = EpisodicMemory.model_validate(data)
        self._upsert(fresh, dense=dense, sparse=sparse)
        return fresh

    async def batch_create(
        self,
        memories: list[EpisodicMemory],
        *,
        merge_strategy: MergeStrategy = "longer-wins",
    ) -> list[EpisodicMemory]:
        """Write N memories in one TEI embed call + one Qdrant upsert.

        The per-row ``create`` method does one TEI call + one Qdrant
        upsert each. Spec ``[[06-ingestion/capture]] § Batched capture``
        requires the batch path to fold those into a single TEI batch
        and a single Qdrant upsert. That's the difference this method
        makes: one round-trip to TEI for all N rows' dense vectors,
        one for sparse, then one atomic Qdrant upsert at the end.

        Dedup probes are per-row because Qdrant doesn't have a
        "batch query_points" shape we can use today. Each dedup probe must pass
        factual compatibility to authorize the hit; incompatible candidates
        resolve to fresh inserts. Every compatible dedup hit and fresh insert
        write via the same single terminal upsert.

        Returns the final row for each input position in input order,
        same as ``create`` (existing row on compatible dedup hit, fresh row
        otherwise).
        """
        if not memories:
            return []

        # Per-row validation — same as create(). Fail fast on the whole
        # batch if any row is malformed; partial success would be harder
        # to reason about for callers.
        import re

        ns_pattern = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
        for memory in memories:
            if len(memory.content.encode("utf-8")) > 32768:
                raise ValueError(
                    "content exceeds 32KB limit on batch row "
                    f"{memory.object_id}; use artifact plane instead"
                )
            if memory.event_at > utc_now():
                raise ValueError(
                    f"event_at cannot be in the future on batch row {memory.object_id}"
                )
            if not ns_pattern.match(memory.namespace):
                raise ValueError(f"invalid namespace format on batch row {memory.object_id}")

        # Single TEI dense + single TEI sparse over the whole batch.
        texts = [m.summary or m.content for m in memories]
        dense_batch = await self._embedder.embed_dense(texts)
        sparse_batch = await self._embedder.embed_sparse(texts)

        if any(len(v) != 1024 for v in dense_batch):
            dims = [len(v) for v in dense_batch]
            raise ValueError(f"dense vector dimension mismatch across batch: {dims}")

        now = utc_now()

        # Walk the batch, decide fresh-insert vs reinforce, collect all
        # final rows + their vectors, then do a SINGLE terminal upsert.
        points: list[models.PointStruct] = []
        finalised: list[EpisodicMemory] = []
        for memory, dense, sparse in zip(memories, dense_batch, sparse_batch, strict=True):
            found = self._find_dedup_candidate(memory.namespace, dense)

            compatible = False
            if found is not None:
                existing, existing_dense, existing_sparse = found
                compatible = _is_factually_compatible(existing, memory)

            if found is not None and compatible:
                existing, existing_dense, existing_sparse = found
                updated, existing_content_won = self._merge_row(
                    existing=existing,
                    new=memory,
                    merge_strategy=merge_strategy,
                    now=now,
                )
                # UPDATE of an existing row via a full-point upsert — read the stored lease-owned
                # fields and carry them forward so a concurrent leased increment is never reset
                # (RET-008 #502). Same bypass as the single-row create() dedup-merge path.
                stored_lease = raw_payload(
                    self._client,
                    self._collection,
                    namespace=str(updated.namespace),
                    object_id=str(updated.object_id),
                )
                # Preserve existing vectors when existing content won
                # (otherwise we'd overwrite them with the new text's
                # embeddings — a payload/vector drift bug).
                if (
                    existing_content_won
                    and existing_dense is not None
                    and existing_sparse is not None
                ):
                    points.append(
                        self._make_point(
                            updated,
                            dense=existing_dense,
                            sparse=existing_sparse,
                            stored_lease=stored_lease,
                        )
                    )
                else:
                    points.append(
                        self._make_point(
                            updated, dense=dense, sparse=sparse, stored_lease=stored_lease
                        )
                    )
                finalised.append(updated)
            else:
                data = memory.model_dump()
                data.update(
                    state="provisional",
                    version=1,
                    reinforcement_count=0,
                    created_at=now,
                    created_epoch=epoch_of(now),
                    updated_at=now,
                    updated_epoch=epoch_of(now),
                )
                fresh = EpisodicMemory.model_validate(data)
                points.append(self._make_point(fresh, dense=dense, sparse=sparse))
                finalised.append(fresh)

        # Single Qdrant upsert for the whole batch.
        self._client.upsert(collection_name=self._collection, points=points)
        return finalised

    def _merge_row(
        self,
        *,
        existing: EpisodicMemory,
        new: EpisodicMemory,
        merge_strategy: MergeStrategy,
        now: Any,
    ) -> tuple[EpisodicMemory, bool]:
        """Compute the merged row without writing it.

        Returns ``(updated, existing_content_won)``. The boolean tells
        the caller which text's embeddings should accompany the upsert:
        if ``existing_content_won`` is True we must preserve the
        existing point's vectors, otherwise we write with the new
        text's freshly-computed vectors. Without this bookkeeping the
        payload and vectors drift out of sync (the bug Copilot caught
        on the batch path)."""
        merged_tags = sorted(set(existing.tags) | set(new.tags))
        existing_content_won = False
        if merge_strategy == "longer-wins":
            if len(existing.content) > len(new.content):
                kept_content = existing.content
                existing_content_won = True
            else:
                kept_content = new.content
        else:
            kept_content = new.content
        data = existing.model_dump()
        data.update(
            content=kept_content,
            tags=merged_tags,
            reinforcement_count=existing.reinforcement_count + 1,
            version=existing.version + 1,
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        return EpisodicMemory.model_validate(data), existing_content_won

    def _make_point(
        self,
        memory: EpisodicMemory,
        *,
        dense: list[float],
        sparse: dict[int, float],
        stored_lease: dict[str, Any] | None = None,
    ) -> models.PointStruct:
        """Build a Qdrant PointStruct for ``memory``. Kept out of
        ``_upsert`` so ``batch_create`` can collect points without
        calling upsert per row.

        ``stored_lease`` is the row's currently-stored payload on an UPDATE
        (dedup-merge reinforce). A full-point upsert would otherwise write the
        in-memory model's STALE ``access_count`` / ``last_accessed_at`` /
        ``access_lease_token`` and reset a concurrent leased increment (RET-008
        #502). When given, :func:`preserve_lease_fields` carries the stored
        lease-owned values forward. ``None`` on a CREATE keeps the model's
        ``access_count = 0`` (lifecycle demotion keys on a fresh row having
        ``access_count == 0``)."""
        payload = memory.model_dump(mode="json")
        if stored_lease is not None:
            payload = preserve_lease_fields(payload, stored_lease)
        return models.PointStruct(
            id=_point_id(memory.object_id),
            payload=payload,
            vector={
                DENSE_VECTOR_NAME: dense,
                SPARSE_VECTOR_NAME: _sparse_to_model(sparse),
            },
        )

    def _reinforce(
        self,
        *,
        existing: EpisodicMemory,
        existing_dense: list[float] | None,
        existing_sparse: dict[int, float] | None,
        new: EpisodicMemory,
        dense: list[float],
        sparse: dict[int, float],
        merge_strategy: MergeStrategy = "longer-wins",
    ) -> EpisodicMemory:
        """Merge ``new`` into ``existing`` and re-upsert under the same id.

        ``existing_dense`` / ``existing_sparse`` are the vectors Qdrant
        already has for the matched point (fetched by the dedup probe).
        When ``merge_strategy="longer-wins"`` keeps the existing content,
        we rewrite the payload but retain those existing vectors so the
        embeddings stay aligned with what the row actually says. When
        new content wins we swap in the new vectors."""
        now = utc_now()
        updated, existing_content_won = self._merge_row(
            existing=existing, new=new, merge_strategy=merge_strategy, now=now
        )
        # UPDATE of an existing row via a full-point upsert — preserve the lease-owned fields so a
        # concurrent retrieval-delivery increment is never reset (RET-008 #502).
        if existing_content_won and existing_dense is not None and existing_sparse is not None:
            self._upsert(updated, dense=existing_dense, sparse=existing_sparse, preserve_lease=True)
        else:
            self._upsert(updated, dense=dense, sparse=sparse, preserve_lease=True)
        return updated

    def _upsert(
        self,
        memory: EpisodicMemory,
        *,
        dense: list[float],
        sparse: dict[int, float],
        preserve_lease: bool = False,
    ) -> None:
        stored_lease: dict[str, Any] | None = None
        if preserve_lease:
            # UPDATE path: read the lease-owned fields FRESH and carry them forward so the
            # full-point upsert never resets a concurrent leased access_count. This narrows the
            # read->upsert window to one round-trip; a full-point upsert cannot be server-fenced
            # the way a filtered set_payload can, so a residual window remains (RET-008 PR notes).
            stored_lease = raw_payload(
                self._client,
                self._collection,
                namespace=str(memory.namespace),
                object_id=str(memory.object_id),
            )
        point = self._make_point(memory, dense=dense, sparse=sparse, stored_lease=stored_lease)
        self._client.upsert(collection_name=self._collection, points=[point])

    # ------------------------------------------------------------------
    # Dedup
    # ------------------------------------------------------------------

    def _find_dedup_candidate(
        self, namespace: str, dense: list[float]
    ) -> tuple[EpisodicMemory, list[float] | None, dict[int, float] | None] | None:
        """Return the best existing point above the dedup threshold, if any.

        Returns the rehydrated :class:`EpisodicMemory` plus the point's
        stored dense and sparse vectors. The vectors let the
        reinforce path preserve the existing embeddings when
        ``longer-wins`` keeps the existing content — otherwise the
        payload and vectors would drift apart on every dedup hit where
        new text was shorter."""
        resp = self._client.query_points(
            collection_name=self._collection,
            query=dense,
            using=DENSE_VECTOR_NAME,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace))
                ]
            ),
            limit=1,
            score_threshold=self._dedup_threshold,
            with_payload=True,
            with_vectors=True,
        )
        if not resp.points:
            return None
        point = resp.points[0]
        payload = point.payload
        if not payload:
            return None
        memory = _memory_from_payload(payload)
        existing_dense: list[float] | None = None
        existing_sparse: dict[int, float] | None = None
        vectors = point.vector
        if isinstance(vectors, dict):
            raw_dense = vectors.get(DENSE_VECTOR_NAME)
            if isinstance(raw_dense, list) and raw_dense and isinstance(raw_dense[0], float):
                existing_dense = raw_dense  # type: ignore[assignment]
            raw_sparse = vectors.get(SPARSE_VECTOR_NAME)
            if isinstance(raw_sparse, models.SparseVector):
                existing_sparse = dict(zip(raw_sparse.indices, raw_sparse.values, strict=True))
        return memory, existing_dense, existing_sparse

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

    async def get(
        self, *, namespace: Namespace, object_id: KSUID, bump_access: bool = True
    ) -> EpisodicMemory | None:
        """Fetch one object by id, scoped to ``namespace``.

        Raises if the stored payload does not satisfy the ``EpisodicMemory`` model.
        If you only need to know whether the object is *there*, call
        :meth:`exists` — it does not deserialize, so it still answers for a
        corrupted row.
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

        if bump_access:
            # RET-008 (#502): route the direct-fetch bump through the SHARED fenced lease so it
            # never races a concurrent retrieval-delivery increment (or another get) on the same
            # row under multi-worker/cross-process parallelism. Re-read to return the post-bump row.
            await lease_increment_access(
                self._client, self._collection, {(str(namespace), str(object_id))}
            )
            refreshed, _ = self._client.scroll(
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
            if refreshed and refreshed[0].payload:
                payload = refreshed[0].payload

        return _memory_from_payload(payload)

    async def query(
        self,
        *,
        namespace: Namespace,
        query: str,
        limit: int = 10,
        include_demoted: bool = False,
    ) -> list[EpisodicMemory]:
        """Dense retrieval filtered to ``namespace`` and visible states.

        Default visible states are ``{matured}``. Setting ``include_demoted``
        expands that to ``{matured, demoted}``. ``provisional`` and
        ``archived`` are never in the default view — they require explicit
        ``get`` by id.
        """
        visible = _VISIBLE_STATES_WITH_DEMOTED if include_demoted else _VISIBLE_STATES
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
        out: list[EpisodicMemory] = []
        pairs: set[tuple[str, str]] = set()
        for point in resp.points:
            if point.payload:
                payload = dict(point.payload)
                out.append(_memory_from_payload(payload))
                pairs.add((str(payload.get("namespace")), str(payload.get("object_id"))))

        # RET-008 (#502): route the batched access bump through the shared fenced lease (never a
        # bare RMW that would race/lose a concurrent leased increment).
        if pairs:
            await lease_increment_access(self._client, self._collection, pairs)
        return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def patch(
        self,
        *,
        namespace: Namespace,
        object_id: KSUID,
        tags: list[str] | None = None,
        importance: int | None = None,
        content: str | None = None,
        actor: str,
        reason: str,
    ) -> tuple[EpisodicMemory, LifecycleEvent]:
        if content is not None:
            raise ValueError("mutating content directly is forbidden")

        current = await self.get(namespace=namespace, object_id=object_id, bump_access=False)
        if not current:
            raise LookupError(f"episodic object {object_id!r} not found in namespace {namespace!r}")

        now = utc_now()
        data = current.model_dump()

        if tags is not None:
            merged = sorted(set(current.tags) | set(tags))
            data["tags"] = merged

        if importance is not None:
            data["importance"] = importance

        data.update(
            version=current.version + 1,
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        updated = EpisodicMemory.model_validate(data)

        from musubi.types.common import generate_ksuid

        event = LifecycleEvent.model_construct(
            event_id=generate_ksuid(),
            object_id=object_id,
            object_type="episodic",
            namespace=namespace,
            schema_version=1,
            from_state=current.state,
            to_state=current.state,
            actor=actor,
            reason=reason,
            occurred_at=now,
            occurred_epoch=epoch_of(now),
            lineage_changes={},
            correlation_id="",
        )

        self._client.set_payload(
            collection_name=self._collection,
            payload=memory_update_payload(updated),
            points=[_point_id(object_id)],
        )
        return updated, event

    async def delete(
        self,
        *,
        namespace: Namespace,
        object_id: KSUID,
        actor: str,
        reason: str,
        is_operator: bool = False,
    ) -> LifecycleEvent:
        if not is_operator:
            raise PermissionError("operator scope required")

        # Read RAW, not typed. This path used to call `self.get()`, which
        # model-validates — so a row carrying an unmodeled payload key raised here,
        # and the delete never ran. That made a corrupted row undeletable through the
        # SDK exactly as it was through the API, and the router-level fix does not
        # protect direct callers (Yua, PR #398 review, 2026-07-11).
        #
        # We still need the prior state for the lifecycle event's `from_state`, so we
        # cannot skip the read — but we must not let the MODEL decide whether a delete
        # is allowed to proceed. Deleting a memory must never depend on that memory
        # being valid; that is the whole defect.
        # Address the point DIRECTLY, not through a payload filter. `raw_payload()` finds a
        # row by its `namespace`/`object_id` PAYLOAD fields — so a row that has lost or
        # malformed those very keys is invisible to it, and would once again be
        # undeletable-because-broken. The point ID is derived deterministically from the
        # object_id, so it addresses the row no matter what the payload says.
        # (Yua, rev2 review of PR #398.)
        payload = retrieve_by_point_id(
            self._client, self._collection, point_id=_point_id(object_id)
        )
        if payload is None:
            raise LookupError(f"episodic object {object_id!r} not found in namespace {namespace!r}")

        # Namespace isolation still has to hold — but ONLY when the stored value is a
        # namespace AT ALL, judged by the CANONICAL contract, not by a local approximation.
        #
        # This took three attempts, and the failures are worth naming because they are the
        # same failure:
        #
        #   1. `stored_ns is not None and stored_ns != namespace`
        #      Handled a MISSING namespace. A namespace corrupted to a list/int/dict is
        #      not-None and unequal → LookupError → undeletable because corrupted.
        #
        #   2. `isinstance(stored_ns, str) and stored_ns != namespace`
        #      Fixed exactly the examples the reviewer had listed (list/int/dict) and left
        #      the CLASS open. A namespace corrupted to `""`, `"garbage"`, a missing plane
        #      component, or bad casing is a *string* → still unequal → still undeletable.
        #      I implemented the examples instead of the class: a denylist of remembered
        #      mistakes, which is the exact unsound pattern this whole PR exists to remove.
        #
        #   3. This. `validate_namespace` is the canonical contract
        #      (`tenant/presence/plane`, lowercase). A stored value that does not satisfy it
        #      is not a namespace — it is damage.
        #
        # The rule, stated once: **isolation is enforced against a namespace that is
        # canonically VALID and different. Anything else — missing, non-string, or invalid
        # under the canonical contract — is corruption, and corruption must be removable.**
        # Operator scope already gates this path.
        # (Copilot found the class; Yua found that I had fixed only its examples.)
        stored_ns = payload.get("namespace")
        stored_ns_is_canonical = False
        if isinstance(stored_ns, str):
            try:
                validate_namespace(stored_ns)
                stored_ns_is_canonical = True
            except ValueError:
                stored_ns_is_canonical = False  # a string, but not a namespace: damage
        if stored_ns_is_canonical and stored_ns != namespace:
            raise LookupError(f"episodic object {object_id!r} not found in namespace {namespace!r}")

        # Normalize the prior state DELIBERATELY. A corrupted row may carry a `state` that
        # is not a LifecycleState at all, and `model_construct` skips validation — so
        # writing it through raw would emit an audit record that violates the very contract
        # LifecycleEvent declares. We record the weakest honest claim ("provisional") and
        # preserve the truth in `reason`, rather than fabricating a state that looks valid.
        raw_state = payload.get("state")
        from_state: LifecycleState = (
            cast(LifecycleState, raw_state)
            if raw_state in get_args(LifecycleState)
            else "provisional"
        )
        if from_state != raw_state:
            reason = f"{reason} [prior state unreadable ({raw_state!r}); normalized for audit]"

        now = utc_now()
        from musubi.types.common import generate_ksuid

        event = LifecycleEvent.model_construct(
            event_id=generate_ksuid(),
            object_id=object_id,
            object_type="episodic",
            namespace=namespace,
            schema_version=1,
            from_state=from_state,
            to_state="archived",
            actor=actor,
            reason=reason,
            occurred_at=now,
            occurred_epoch=epoch_of(now),
            lineage_changes={},
            correlation_id="",
        )
        self._client.delete(
            collection_name=self._collection,
            points_selector=models.PointIdsList(points=[_point_id(object_id)]),
        )
        return event

    async def transition(
        self,
        *,
        namespace: Namespace,
        object_id: KSUID,
        to_state: LifecycleState,
        actor: str,
        reason: str,
        coordinator: LifecycleTransitionCoordinator,
    ) -> Result[TransitionResult | TransitionPending, TransitionError]:
        """Delegate the namespace-scoped state change to the canonical coordinator."""
        current = await self.get(namespace=namespace, object_id=object_id, bump_access=False)
        if current is None:
            return Err(
                error=TransitionError(
                    code="not_found",
                    message=(f"episodic object {object_id!r} not found in namespace {namespace!r}"),
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
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _embed_both(self, text: str) -> tuple[list[float], dict[int, float]]:
        """Compute dense + sparse embeddings for a single text."""
        dense = (await self._embedder.embed_dense([text]))[0]
        sparse = (await self._embedder.embed_sparse([text]))[0]
        return dense, sparse


__all__ = ["EpisodicPlane"]
