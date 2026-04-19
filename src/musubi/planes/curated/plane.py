"""``CuratedPlane`` — Qdrant CRUD + lifecycle for :class:`CuratedKnowledge`.

Responsibilities (see [[04-data-model/curated-knowledge]] §Storage semantics):

- **Create** — write a curated row at ``state = "matured"``. Dedup is by
  ``(namespace, vault_path)`` (not similarity, unlike episodic): a second
  write for the same path is either a no-op (same ``body_hash``) or a
  supersession (different ``body_hash`` — old row → ``superseded`` with
  ``superseded_by``; new row gets ``supersedes = [old_id]``).
- **Get** — fetch one row by namespace + ``object_id``. Wrong-namespace
  reads return ``None`` (read-side namespace isolation).
- **Query** — dense retrieval scoped to a namespace, with two default
  predicates the curated plane adds on top of namespace + state: the
  bitemporal validity window ``valid_from <= valid_at < valid_until`` and
  ``state == "matured"`` (so a superseded or archived row never appears in
  a default query). Callers pass ``valid_at`` to time-travel.
- **Transition** — the only path allowed to mutate ``state``. Emits a
  :class:`LifecycleEvent` whose validator enforces the curated transition
  table (``matured ↔ superseded / archived``).

Vault filesystem behaviour — watcher debounce, write-log echo detection,
file-move / file-delete event handlers, frontmatter parsing,
``musubi-managed`` write authorization — is **not** owned here; it lives
in ``src/musubi/vault_sync/`` per slice-vault-sync.

Design notes:

- Qdrant point IDs are derived deterministically from a slice-specific
  ``uuid5`` namespace so the same KSUID always maps to the same point ID.
  The KSUID itself stays in the payload as ``object_id``.
- ``model_copy(update=...)`` skips validators in pydantic v2, so every
  state-mutating path round-trips through ``model_dump`` +
  ``model_validate`` — that's the only way to keep the monotonicity +
  lineage invariants honest.
- Bitemporal queries express "(valid_from is null OR valid_from <= t) AND
  (valid_until is null OR valid_until > t)" as two nested ``Filter``\\ s
  in ``must``, each with a ``should`` of the two branches. Qdrant supports
  this pattern natively — there is no need to filter rows in Python.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import KSUID, LifecycleState, Namespace, epoch_of, utc_now
from musubi.types.curated import CuratedKnowledge
from musubi.types.lifecycle_event import LifecycleEvent

# Distinct from the episodic point-namespace UUID — keeps the two
# collections' point IDs in disjoint UUID spaces even when KSUIDs collide
# (they shouldn't, but defence in depth).
_POINT_NS = uuid.UUID("6b0d5e2e-1e8e-4e0f-8e3e-000000000002")

_VISIBLE_STATES: tuple[LifecycleState, ...] = ("matured",)


def _point_id(object_id: str) -> str:
    """Return the deterministic Qdrant point ID for a given KSUID."""
    return str(uuid.uuid5(_POINT_NS, object_id))


def _sparse_to_model(sparse: dict[int, float]) -> models.SparseVector:
    """Convert a sparse ``{index: value}`` dict to Qdrant's ``SparseVector``."""
    return models.SparseVector(
        indices=list(sparse.keys()),
        values=list(sparse.values()),
    )


def _curated_from_payload(payload: dict[str, Any]) -> CuratedKnowledge:
    """Rehydrate a :class:`CuratedKnowledge` from a Qdrant payload dict.

    ``model_validate`` re-runs the bitemporal + monotonicity validators —
    the plane must never hand out a half-constructed object.
    """
    return CuratedKnowledge.model_validate(payload)


def _embed_target(memory: CuratedKnowledge) -> str:
    """The text we feed the embedder.

    Per the spec, curated rows embed ``title + summary`` when the summary
    is present and ``title + content`` otherwise. Real chunking of large
    bodies into ``ArtifactChunk`` rows is a slice-plane-artifact concern.
    """
    body = memory.summary if memory.summary else memory.content
    return f"{memory.title}\n\n{body}"


class CuratedPlane:
    """CRUD + lifecycle transitions for the curated knowledge plane."""

    def __init__(self, *, client: QdrantClient, embedder: Embedder) -> None:
        self._client = client
        self._embedder = embedder
        self._collection = collection_for_plane("curated")

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, memory: CuratedKnowledge) -> CuratedKnowledge:
        """Write ``memory`` to Qdrant with vault-path-keyed dedup.

        Three outcomes:

        1. **Fresh** — no existing row for ``(namespace, vault_path)``.
           Insert the row at ``state = "matured"``.
        2. **Idempotent** — existing row has the same ``body_hash``. No
           write; return the existing row unchanged.
        3. **Supersession** — existing row has a different ``body_hash``.
           Mark it ``superseded`` with ``superseded_by = new.object_id``;
           insert the new row with ``supersedes = [old.object_id]``.
        """
        existing = self._find_by_vault_path(
            namespace=memory.namespace, vault_path=memory.vault_path
        )

        if existing is not None and existing.body_hash == memory.body_hash:
            # Idempotent re-save of the same body. The watcher would
            # normally short-circuit before calling us, but defending here
            # too lets the plane be safely called from any caller without
            # double-counting writes.
            return existing

        now = utc_now()

        if existing is None:
            data = memory.model_dump()
            data.update(
                state="matured",
                version=1,
                created_at=now,
                created_epoch=epoch_of(now),
                updated_at=now,
                updated_epoch=epoch_of(now),
            )
            fresh = CuratedKnowledge.model_validate(data)
            dense, sparse = await self._embed_both(_embed_target(fresh))
            self._upsert(fresh, dense=dense, sparse=sparse)
            return fresh

        # Supersession path. Two writes: re-embed the new row, mark the
        # old row superseded. Both writes hit the same collection; if the
        # second one fails the first one is still on disk — acceptable
        # because a stale "superseded" row simply won't appear in default
        # queries until the supersession completes (eventual consistency
        # handled at slice-lifecycle-engine).
        data = memory.model_dump()
        data.update(
            state="matured",
            version=1,
            supersedes=[existing.object_id],
            created_at=now,
            created_epoch=epoch_of(now),
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        new_row = CuratedKnowledge.model_validate(data)
        dense, sparse = await self._embed_both(_embed_target(new_row))
        self._upsert(new_row, dense=dense, sparse=sparse)

        old_data = existing.model_dump()
        old_data.update(
            state="superseded",
            superseded_by=new_row.object_id,
            version=existing.version + 1,
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        superseded = CuratedKnowledge.model_validate(old_data)
        # Payload-only update keeps the existing vectors; cheaper than a
        # full upsert and the body didn't change for the superseded row.
        self._client.set_payload(
            collection_name=self._collection,
            payload=superseded.model_dump(mode="json"),
            points=[_point_id(existing.object_id)],
        )

        return new_row

    def _upsert(
        self,
        memory: CuratedKnowledge,
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

    def _find_by_vault_path(self, *, namespace: str, vault_path: str) -> CuratedKnowledge | None:
        """Look up the (single) curated row mirroring ``vault_path``.

        ``(namespace, vault_path)`` is meant to be unique — the watcher
        enforces uniqueness on the way in. We scroll defensively with
        ``limit=1`` so a duplicated vault_path (a Musubi bug) surfaces as
        whichever row Qdrant returns first rather than a hard crash; the
        rebuild integration test (deferred to slice-vault-sync) catches
        the duplicate.
        """
        records, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="vault_path", match=models.MatchValue(value=vault_path)
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
        return _curated_from_payload(payload)

    async def get(self, *, namespace: Namespace, object_id: KSUID) -> CuratedKnowledge | None:
        """Fetch one curated row by id, scoped to ``namespace``.

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
        return _curated_from_payload(payload)

    async def query(
        self,
        *,
        namespace: Namespace,
        query: str,
        limit: int = 10,
        valid_at: datetime | None = None,
    ) -> list[CuratedKnowledge]:
        """Dense retrieval over the curated plane, namespace-scoped.

        Default predicates layered on top of namespace + state-in-{matured}:

        - ``valid_from`` is null OR ``valid_from <= valid_at`` (defaults
          to "now").
        - ``valid_until`` is null OR ``valid_until > valid_at``.

        Pass ``valid_at=...`` to time-travel — useful for "what did we
        believe on 2025-12-01?" introspection. Superseded and archived
        rows are not in the default view; reach for them by id via
        :meth:`get`.
        """
        at = valid_at if valid_at is not None else utc_now()
        at_epoch = epoch_of(at)
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
                        match=models.MatchAny(any=[str(s) for s in _VISIBLE_STATES]),
                    ),
                    models.Filter(
                        should=[
                            models.IsNullCondition(
                                is_null=models.PayloadField(key="valid_from_epoch")
                            ),
                            models.FieldCondition(
                                key="valid_from_epoch",
                                range=models.Range(lte=at_epoch),
                            ),
                        ]
                    ),
                    models.Filter(
                        should=[
                            models.IsNullCondition(
                                is_null=models.PayloadField(key="valid_until_epoch")
                            ),
                            models.FieldCondition(
                                key="valid_until_epoch",
                                range=models.Range(gt=at_epoch),
                            ),
                        ]
                    ),
                ]
            ),
            limit=limit,
            with_payload=True,
        )
        out: list[CuratedKnowledge] = []
        for point in resp.points:
            if point.payload:
                out.append(_curated_from_payload(point.payload))
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
    ) -> tuple[CuratedKnowledge, LifecycleEvent]:
        """Mutate ``state`` and emit a :class:`LifecycleEvent`.

        Raises :class:`LookupError` when the object doesn't exist in the
        given namespace (write-side namespace isolation). The
        :class:`LifecycleEvent` validator raises :class:`ValueError` for
        transitions outside the curated table.
        """
        current = await self.get(namespace=namespace, object_id=object_id)
        if current is None:
            raise LookupError(f"curated object {object_id!r} not found in namespace {namespace!r}")
        event = LifecycleEvent(
            object_id=object_id,
            object_type="curated",
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
        updated = CuratedKnowledge.model_validate(data)
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


__all__ = ["CuratedPlane"]
