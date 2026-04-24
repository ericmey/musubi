"""``ThoughtsPlane`` — CRUD + lifecycle for :class:`Thought`.

Responsibilities (from [[04-data-model/thoughts]]):
- **Send** — Creates a Thought with ``state="provisional"``, ``read=False``.
- **Get** — fetch by namespace + ``object_id``.
- **Check** — returns unread thoughts for a presence.
- **Read** — appends to ``read_by``, sets ``read=True`` if unicast.
- **History** — semantic and filtered retrieval.
- **Transition** — lifecycle state mutation.
"""

from __future__ import annotations

import uuid
from typing import Any

from ksuid import Ksuid
from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import KSUID, LifecycleState, Namespace, epoch_of, utc_now
from musubi.types.lifecycle_event import LifecycleEvent
from musubi.types.thought import Thought

# Dedicated UUID namespace for thoughts.
_POINT_NS = uuid.UUID("6b0d5e2e-1e8e-4e0f-8e3e-000000000005")

_VISIBLE_STATES: tuple[LifecycleState, ...] = ("provisional", "matured", "archived")


def _point_id(object_id: str) -> str:
    return str(uuid.uuid5(_POINT_NS, object_id))


def _sparse_to_model(sparse: dict[int, float]) -> models.SparseVector:
    return models.SparseVector(
        indices=list(sparse.keys()),
        values=list(sparse.values()),
    )


def _thought_from_payload(payload: dict[str, Any]) -> Thought:
    return Thought.model_validate(payload)


class ThoughtsPlane:
    def __init__(
        self,
        *,
        client: QdrantClient,
        embedder: Embedder,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._collection = collection_for_plane("thought")

    # ------------------------------------------------------------------
    # Create / Send
    # ------------------------------------------------------------------

    async def send(
        self, thought: Thought, *, defer_embedding: bool = False, enforce_tenant_scope: bool = False
    ) -> Thought:
        now = utc_now()

        # Spec 15: test_cross_tenant_thought_requires_multi_tenant_scope
        if enforce_tenant_scope:
            raise ValueError("Cross-tenant thoughts require explicit multi-tenant scope")

        data = thought.model_dump()
        data.update(
            state="provisional",
            version=1,
            read=False,
            read_by=[],
            created_at=now,
            created_epoch=epoch_of(now),
            updated_at=now,
            updated_epoch=epoch_of(now),
        )

        # Make sure default fields from base class are populated properly if empty
        if not data.get("schema_version"):
            data["schema_version"] = 1

        fresh = Thought.model_validate(data)

        if defer_embedding:
            dense: list[float] = []
            sparse: dict[int, float] = {}
        else:
            dense, sparse = await self._embed_both(fresh.content)

        self._upsert(fresh, dense=dense, sparse=sparse)
        return fresh

    def _upsert(
        self,
        thought: Thought,
        *,
        dense: list[float],
        sparse: dict[int, float],
    ) -> None:
        vector: dict[str, Any] = {}
        if dense:
            vector[DENSE_VECTOR_NAME] = dense
        if sparse:
            vector[SPARSE_VECTOR_NAME] = _sparse_to_model(sparse)

        point = models.PointStruct(
            id=_point_id(thought.object_id),
            payload=thought.model_dump(mode="json"),
            vector=vector or {},
        )
        self._client.upsert(collection_name=self._collection, points=[point])

    # ------------------------------------------------------------------
    # Read (Fetch)
    # ------------------------------------------------------------------

    async def get(self, *, namespace: Namespace, object_id: KSUID) -> Thought | None:
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
        return _thought_from_payload(payload)

    # ------------------------------------------------------------------
    # Check Unread
    # ------------------------------------------------------------------

    async def check(self, *, namespace: Namespace, my_presence: str) -> list[Thought]:
        """Returns unread thoughts for `my_presence`."""
        resp, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="to_presence",
                        match=models.MatchAny(any=[my_presence, "all"]),
                    ),
                ],
                must_not=[
                    models.FieldCondition(
                        key="read_by", match=models.MatchValue(value=my_presence)
                    ),
                    models.FieldCondition(
                        key="from_presence", match=models.MatchValue(value=my_presence)
                    ),
                ],
            ),
            limit=1000,
            with_payload=True,
        )
        out: list[Thought] = []
        for point in resp:
            if point.payload:
                out.append(_thought_from_payload(point.payload))
        return out

    # ------------------------------------------------------------------
    # Replay (SSE reconnect backfill)
    # ------------------------------------------------------------------

    async def replay_since(
        self,
        *,
        namespace: Namespace,
        includes: frozenset[str] | set[str],
        last_event_id: str,
        cap: int = 500,
    ) -> tuple[list[Thought], bool]:
        """Return thoughts emitted since ``last_event_id`` for SSE backfill.

        Filter shape:
        - ``namespace`` matches exactly.
        - ``to_presence`` is in ``includes`` (typically the presence plus
          the ``all`` broadcast bucket).
        - ``created_epoch >= anchor_epoch``, where ``anchor_epoch`` comes
          from decoding ``last_event_id`` as a KSUID. Narrows the scroll
          at index level so we don't scan the whole namespace.
        - ``object_id > last_event_id`` applied client-side, since two
          thoughts created in the same second have random KSUID suffixes
          and lex comparison is the tiebreaker.

        Qdrant ``scroll`` doesn't order by object_id (point ids are
        UUIDv5, uncorrelated with KSUID), so we paginate through the
        filtered slice until we collect ``cap + 1`` post-anchor
        candidates or exhaust the result set. Sorting + cap are applied
        after the paginated fetch so ``truncated`` reflects whether a
        genuine overflow occurred, not scroll-window quirks.

        Ordered ascending by ``object_id`` lexicographically. Since
        ``object_id`` is a KSUID, this preserves second-level creation
        time order with the KSUID suffix acting as the within-second
        tiebreaker — matches the canonical-api.md contract exactly.
        The returned ``truncated`` flag tells the caller whether to
        set the ``X-Musubi-Replay-Truncated`` response header so
        clients can fall back to ``/v1/thoughts/history`` for deeper
        backfill.
        """
        # Malformed anchor → return empty replay rather than 500. A
        # garbage Last-Event-ID means the client's state is corrupt;
        # live-tail from here is the best we can do. The KSUID
        # decoder can raise multiple exception types depending on
        # the flavour of garbage (empty string → IndexError from
        # baseconv, oversized timestamp → OverflowError, invalid
        # chars → ValueError, etc.), so we catch the superset.
        try:
            anchor_epoch = float(Ksuid.from_base62(last_event_id).timestamp)
        except Exception:
            return [], False

        base_filter = models.Filter(
            must=[
                models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
                models.FieldCondition(
                    key="to_presence",
                    match=models.MatchAny(any=list(includes)),
                ),
                models.FieldCondition(
                    key="created_epoch",
                    range=models.Range(gte=anchor_epoch),
                ),
            ],
        )

        candidates: list[Thought] = []
        offset: Any | None = None
        page_size = max(cap + 1, 64)

        while len(candidates) <= cap:
            resp, offset = self._client.scroll(
                collection_name=self._collection,
                scroll_filter=base_filter,
                limit=page_size,
                offset=offset,
                with_payload=True,
            )

            for point in resp:
                if not point.payload:
                    continue
                thought = _thought_from_payload(point.payload)
                if thought.object_id > last_event_id:
                    candidates.append(thought)
                    if len(candidates) > cap:
                        break

            if offset is None or len(candidates) > cap:
                break

        # Sort by object_id lex-ascending — matches the canonical-api
        # contract ("object_id > anchor (lexicographic, ascending)")
        # and the client-side dedup-set insertion rule. KSUID lex order
        # is second-level time order with random within-second
        # tiebreaks, which is fine for SSE replay.
        candidates.sort(key=lambda t: t.object_id)
        truncated = len(candidates) > cap
        return candidates[:cap], truncated

    # ------------------------------------------------------------------
    # Mark Read
    # ------------------------------------------------------------------

    async def read(self, *, namespace: Namespace, object_id: KSUID, reader: str) -> Thought:
        res = await self.read_batch(namespace=namespace, object_ids=[object_id], reader=reader)
        if not res:
            raise LookupError(f"thought {object_id!r} not found in namespace {namespace!r}")
        return res[0]

    async def read_batch(
        self, *, namespace: Namespace, object_ids: list[KSUID], reader: str
    ) -> list[Thought]:
        if not object_ids:
            return []

        records, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(key="object_id", match=models.MatchAny(any=object_ids)),
                ]
            ),
            limit=len(object_ids),
            with_payload=True,
        )

        operations = []
        updated_thoughts = []
        now = utc_now()

        for record in records:
            if not record.payload:
                continue
            t = _thought_from_payload(record.payload)
            if reader in t.read_by:
                updated_thoughts.append(t)
                continue

            new_read_by = [*list(t.read_by), reader]
            new_read = t.read or (t.to_presence == reader)

            data = t.model_dump()
            data.update(
                read_by=new_read_by,
                read=new_read,
                version=t.version + 1,
                updated_at=now,
                updated_epoch=epoch_of(now),
            )
            updated = Thought.model_validate(data)
            updated_thoughts.append(updated)

            operations.append(
                models.SetPayloadOperation(
                    set_payload=models.SetPayload(
                        payload=updated.model_dump(mode="json"), points=[_point_id(t.object_id)]
                    )
                )
            )

        if operations:
            self._client.batch_update_points(
                collection_name=self._collection, update_operations=operations
            )

        return updated_thoughts

    # ------------------------------------------------------------------
    # History (Search)
    # ------------------------------------------------------------------

    async def history(
        self,
        *,
        namespace: Namespace,
        channel: str = "default",
        presence: str | None = None,
        query: str | None = None,
        min_importance: int | None = None,
        in_reply_to: str | None = None,
        limit: int = 50,
    ) -> list[Thought]:
        must_clauses: list[models.Condition] = [
            models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
        ]

        if channel:
            must_clauses.append(
                models.FieldCondition(key="channel", match=models.MatchValue(value=channel))
            )

        if presence:
            must_clauses.append(
                models.Filter(
                    should=[
                        models.FieldCondition(
                            key="from_presence", match=models.MatchValue(value=presence)
                        ),
                        models.FieldCondition(
                            key="to_presence", match=models.MatchValue(value=presence)
                        ),
                    ]
                )
            )

        if min_importance is not None:
            must_clauses.append(
                models.FieldCondition(key="importance", range=models.Range(gte=min_importance))
            )

        if in_reply_to is not None:
            must_clauses.append(
                models.FieldCondition(key="in_reply_to", match=models.MatchValue(value=in_reply_to))
            )

        out: list[Thought] = []

        if query:
            dense = (await self._embedder.embed_dense([query]))[0]
            resp = self._client.query_points(
                collection_name=self._collection,
                query=dense,
                using=DENSE_VECTOR_NAME,
                query_filter=models.Filter(must=must_clauses),
                limit=limit,
                with_payload=True,
            )
            for pt in resp.points:
                if pt.payload:
                    out.append(_thought_from_payload(pt.payload))
        else:
            resp_scroll, _ = self._client.scroll(
                collection_name=self._collection,
                scroll_filter=models.Filter(must=must_clauses),
                limit=limit,
                with_payload=True,
                order_by=models.OrderBy(key="created_epoch", direction=models.Direction.DESC),
            )
            for rec in resp_scroll:
                if rec.payload:
                    out.append(_thought_from_payload(rec.payload))

        return out

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
    ) -> tuple[Thought, LifecycleEvent]:
        current = await self.get(namespace=namespace, object_id=object_id)
        if current is None:
            raise LookupError(f"thought {object_id!r} not found in namespace {namespace!r}")

        event = LifecycleEvent(
            object_id=object_id,
            object_type="thought",
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
        updated = Thought.model_validate(data)
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
        dense = (await self._embedder.embed_dense([text]))[0]
        sparse = (await self._embedder.embed_sparse([text]))[0]
        return dense, sparse


__all__ = ["ThoughtsPlane"]
