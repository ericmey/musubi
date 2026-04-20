"""``EpisodicPlane`` — CRUD + lifecycle for :class:`EpisodicMemory`.

Responsibilities (from [[04-data-model/episodic-memory]]):

- **Create** — always ``state = "provisional"``, ``version = 1``. Auto-embed
  dense + sparse. Dedup against existing points in the same namespace via
  dense cosine similarity; on hit, merge tags, bump ``reinforcement_count``
  and ``version``, replace content with new text.
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
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import KSUID, LifecycleState, Namespace, epoch_of, utc_now
from musubi.types.episodic import EpisodicMemory
from musubi.types.lifecycle_event import LifecycleEvent

# Namespace UUID used to derive point IDs from KSUIDs. Random but fixed
# forever — any change to this constant would orphan every stored point.
_POINT_NS = uuid.UUID("6b0d5e2e-1e8e-4e0f-8e3e-000000000001")

_DEFAULT_DEDUP_THRESHOLD = 0.92
_VISIBLE_STATES: tuple[LifecycleState, ...] = ("matured",)
_VISIBLE_STATES_WITH_DEMOTED: tuple[LifecycleState, ...] = ("matured", "demoted")


def _point_id(object_id: str) -> str:
    """Return the deterministic Qdrant point ID for a given KSUID."""
    return str(uuid.uuid5(_POINT_NS, object_id))


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

    async def create(self, memory: EpisodicMemory) -> EpisodicMemory:
        """Write ``memory`` to Qdrant, deduping against the same namespace.

        On a dedup hit, merges tags + bumps ``reinforcement_count`` and
        ``version`` on the existing row instead of inserting. Returns the
        final state of the row (original object id on dedup hit, new id on
        fresh insert).
        """
        if len(memory.content.encode("utf-8")) > 32768:
            raise ValueError("content exceeds 32KB limit, please use artifact plane instead")
        if memory.event_at > utc_now():
            raise ValueError("event_at cannot be in the future")
        import re

        if not re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", memory.namespace):
            raise ValueError("invalid namespace format")

        now = utc_now()
        text = memory.summary or memory.content
        dense, sparse = await self._embed_both(text)

        if len(dense) != 1024:
            raise ValueError(f"vector dimension mismatch: got {len(dense)}, expected 1024")

        existing = self._find_dedup_candidate(memory.namespace, dense)
        if existing is not None:
            return self._reinforce(existing=existing, new=memory, dense=dense, sparse=sparse)

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
        self._upsert(fresh, dense=dense, sparse=sparse)
        return fresh

    def _reinforce(
        self,
        *,
        existing: EpisodicMemory,
        new: EpisodicMemory,
        dense: list[float],
        sparse: dict[int, float],
    ) -> EpisodicMemory:
        """Merge ``new`` into ``existing`` and re-upsert under the same id."""
        merged_tags = sorted(set(existing.tags) | set(new.tags))
        now = utc_now()
        data = existing.model_dump()
        data.update(
            content=new.content,
            tags=merged_tags,
            reinforcement_count=existing.reinforcement_count + 1,
            version=existing.version + 1,
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        updated = EpisodicMemory.model_validate(data)
        self._upsert(updated, dense=dense, sparse=sparse)
        return updated

    def _upsert(
        self,
        memory: EpisodicMemory,
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
    # Dedup
    # ------------------------------------------------------------------

    def _find_dedup_candidate(self, namespace: str, dense: list[float]) -> EpisodicMemory | None:
        """Return the best existing point above the dedup threshold, if any."""
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
        )
        if not resp.points:
            return None
        payload = resp.points[0].payload
        if not payload:
            return None
        return _memory_from_payload(payload)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(
        self, *, namespace: Namespace, object_id: KSUID, bump_access: bool = True
    ) -> EpisodicMemory | None:
        """Fetch one object by id, scoped to ``namespace``."""
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
            access_count = payload.get("access_count", 0) + 1
            now = utc_now()
            now_str = now.isoformat().replace("+00:00", "Z")
            self._client.batch_update_points(
                collection_name=self._collection,
                update_operations=[
                    models.SetPayloadOperation(
                        set_payload=models.SetPayload(
                            payload={"access_count": access_count, "last_accessed_at": now_str},
                            points=[_point_id(object_id)],
                        )
                    )
                ],
            )
            payload["access_count"] = access_count
            payload["last_accessed_at"] = now_str

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
        updates = []
        now = utc_now()
        now_str = now.isoformat().replace("+00:00", "Z")

        for point in resp.points:
            if point.payload:
                payload = dict(point.payload)
                ac = payload.get("access_count", 0) + 1
                updates.append(
                    models.SetPayloadOperation(
                        set_payload=models.SetPayload(
                            payload={"access_count": ac, "last_accessed_at": now_str},
                            points=[point.id],
                        )
                    )
                )
                payload["access_count"] = ac
                payload["last_accessed_at"] = now_str
                out.append(_memory_from_payload(payload))

        if updates:
            self._client.batch_update_points(
                collection_name=self._collection,
                update_operations=updates,
            )
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
            payload=updated.model_dump(mode="json"),
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

        current = await self.get(namespace=namespace, object_id=object_id, bump_access=False)
        if not current:
            raise LookupError(f"episodic object {object_id!r} not found in namespace {namespace!r}")

        now = utc_now()
        from musubi.types.common import generate_ksuid

        event = LifecycleEvent.model_construct(
            event_id=generate_ksuid(),
            object_id=object_id,
            object_type="episodic",
            namespace=namespace,
            schema_version=1,
            from_state=current.state,
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
    ) -> tuple[EpisodicMemory, LifecycleEvent]:
        """Mutate ``state`` and emit a :class:`LifecycleEvent`.

        Raises :class:`LookupError` if the object doesn't exist in the given
        namespace (this enforces write-side namespace isolation). Raises
        :class:`ValueError` if the transition is illegal per the episodic
        transition table.
        """
        current = await self.get(namespace=namespace, object_id=object_id, bump_access=False)
        if current is None:
            raise LookupError(f"episodic object {object_id!r} not found in namespace {namespace!r}")
        # LifecycleEvent's own validator raises ValueError on illegal
        # transitions — that's the single source of truth for legality.
        event = LifecycleEvent(
            object_id=object_id,
            object_type="episodic",
            namespace=namespace,
            from_state=current.state,
            to_state=to_state,
            actor=actor,
            reason=reason,
        )
        now = utc_now()
        data = current.model_dump()
        data.update(
            state=to_state,
            version=current.version + 1,
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        updated = EpisodicMemory.model_validate(data)
        self._client.set_payload(
            collection_name=self._collection,
            payload=updated.model_dump(mode="json"),
            points=[_point_id(object_id)],
        )
        return updated, event

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _embed_both(self, text: str) -> tuple[list[float], dict[int, float]]:
        """Compute dense + sparse embeddings for a single text."""
        dense = (await self._embedder.embed_dense([text]))[0]
        sparse = (await self._embedder.embed_sparse([text]))[0]
        return dense, sparse


__all__ = ["EpisodicPlane"]
