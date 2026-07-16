"""DATA-001 Phase 2 — immutable vectors + fenced committed pointer (#530).

A vector/content change writes a NEW write-once content point and commits by a SINGLE fenced
``set_payload`` on a stable, object_id-keyed ANCHOR that swaps ``live_point`` + ``pointer_version`` +
``committed_operation_id`` + ``version``. Reads resolve the anchor and expose only the content point
named by the committed ``live_point``. Reconciliation rides the lifecycle coordinator's custom-intent
seam (``immutable_vector_publish``): the handler recomputes its effect from the durable ``patch_json``
alone, so a crash replays from disk with no caller memory. Mirrors the proven ART-001
head/generation/fenced-publish pattern.

Design: docs/Musubi/13-decisions/data001-phase2-immutable-vectors.md.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from qdrant_client import models

from musubi.embedding.base import Embedder
from musubi.planes.episodic.plane import _sparse_to_model
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME

ANCHOR_KIND = "anchor"
CONTENT_KIND = "content"
VECTOR_LAYOUT_V2 = 2
INTENT_KIND = "immutable_vector_publish"

_ID_NS = uuid.UUID("d0e5c1a0-0000-4000-8000-000000000001")  # stable namespace for deterministic ids


class ImmutableVectorPublishPending(RuntimeError):
    """A synchronous :meth:`ImmutableVectorPublisher.publish` did NOT commit the vector inline (a
    retry/fence/worker-held outcome). The durable intent remains for the worker to finish; the caller
    fails loud rather than returning an uncommitted object (DATA-001 P2, Yua ruling)."""


def content_point_id_for(operation_key: str, generation: int = 0) -> str:
    """Deterministic content-point id from the STABLE operation_key (+ generation) — a reconcile
    re-drive of the same operation reuses the SAME id (never the per-claim owner_token)."""
    return str(uuid.uuid5(_ID_NS, f"content:{operation_key}:{generation}"))


def anchor_point_id(namespace: str, object_id: str) -> str:
    """Deterministic anchor id, stable per public (namespace, object_id)."""
    return str(uuid.uuid5(_ID_NS, f"anchor:{namespace}:{object_id}"))


def _run_coro(coro: Any) -> Any:
    """Run an async coroutine from a SYNC caller (the reconcile worker), nesting-safe."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


@dataclass(frozen=True)
class AnchorView:
    object_id: str
    namespace: str
    live_point: str | None
    version: int
    vector_layout_version: int
    access_count: int
    pointer_version: int
    committed_operation_id: str | None


def _anchor_filter(namespace: str, object_id: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
            models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
            models.FieldCondition(key="point_kind", match=models.MatchValue(value=ANCHOR_KIND)),
        ]
    )


def read_anchor(
    client: Any, collection: str, *, namespace: str, object_id: str
) -> AnchorView | None:
    recs, _ = client.scroll(
        collection_name=collection,
        scroll_filter=_anchor_filter(namespace, object_id),
        limit=1,
        with_payload=True,
    )
    if not recs or not recs[0].payload:
        return None
    p = recs[0].payload
    return AnchorView(
        object_id=str(p.get("object_id")),
        namespace=str(p.get("namespace")),
        live_point=p.get("live_point"),
        version=int(p.get("version", 0)),
        vector_layout_version=int(p.get("vector_layout_version", VECTOR_LAYOUT_V2)),
        access_count=int(p.get("access_count", 0)),
        pointer_version=int(p.get("pointer_version", 0)),
        committed_operation_id=p.get("committed_operation_id"),
    )


def _read_legacy_v1(client: Any, collection: str, *, namespace: str, object_id: str) -> Any:
    """A pre-Phase-2 row: object_id present, NO point_kind (neither anchor nor content)."""
    recs, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
                models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
            ],
            must_not=[
                models.FieldCondition(
                    key="point_kind",
                    match=models.MatchAny(any=[ANCHOR_KIND, CONTENT_KIND]),
                )
            ],
        ),
        limit=1,
        with_payload=True,
    )
    if not recs or not recs[0].payload:
        return None
    return recs[0]


def resolve_committed_content(
    client: Any, collection: str, *, namespace: str, object_id: str
) -> dict[str, Any] | None:
    """Return the committed content payload, following ONLY the anchor's ``live_point`` (v2) or a v1
    legacy self-pointer. A v2 anchor with an absent ``live_point`` FAILS CLOSED (returns None)."""
    anchor = read_anchor(client, collection, namespace=namespace, object_id=object_id)
    if anchor is not None:
        if not anchor.live_point:
            return (
                None  # v2 anchor with no committed pointer -> fail closed (never treated as legacy)
            )
        pts = client.retrieve(
            collection_name=collection, ids=[anchor.live_point], with_payload=True
        )
        if not pts or not pts[0].payload:
            return None
        committed: dict[str, Any] = dict(pts[0].payload)
        return committed
    legacy = _read_legacy_v1(client, collection, namespace=namespace, object_id=object_id)
    if legacy is not None and legacy.payload:
        return dict(legacy.payload)  # v1 legacy self-pointer
    return None


class ImmutableVectorPublisher:
    """Registers the ``immutable_vector_publish`` handler and admits durable publish intents."""

    def __init__(self, *, client: Any, embedder: Embedder, collection: str) -> None:
        self._client = client
        self._embedder = embedder
        self._collection = collection
        self._stall_after_staging = False  # fault-injection seams (tests only)
        self._fail_cleanup = False

    def register(self, coordinator: Any) -> None:
        coordinator.register_intent_handler(INTENT_KIND, self.apply)

    def stall_after_staging_once(self) -> None:
        self._stall_after_staging = True

    def fail_cleanup_once(self) -> None:
        self._fail_cleanup = True

    def admit_publish(
        self, coordinator: Any, *, object_id: str, namespace: str, content_payload: dict[str, Any]
    ) -> str:
        """Durably admit ONE vector-publish intent. The COMPLETE mutation (canonical content + narrow
        fields + a recompute fingerprint — never a raw vector blob) is persisted in the outbox
        ``patch_json``; the handler recomputes the vector from it. Returns the coordinator admission
        status (``'admitted'`` / ``'already_active'`` / ``'at_capacity'``)."""
        status: str = coordinator.enqueue_custom_intent(
            kind=INTENT_KIND,
            object_id=object_id,
            namespace=namespace,
            collection=self._collection,
            patch_json=self._patch_json(content_payload),
        )
        return status

    def _patch_json(self, content_payload: dict[str, Any]) -> str:
        content = str(content_payload.get("content", ""))
        patch = {
            "content_payload": content_payload,
            "embed_fingerprint": {
                "embedder": type(self._embedder).__name__,
                "content_len": len(content),
            },
        }
        return json.dumps(patch, sort_keys=True, separators=(",", ":"))

    def publish(
        self, coordinator: Any, *, object_id: str, namespace: str, content_payload: dict[str, Any]
    ) -> dict[str, Any]:
        """SYNCHRONOUS durable publish (the production write path). Admits the durable intent with an
        explicit operation_key, drives ONLY that operation inline via ``coordinator.drive_intent``, then
        returns the content read back through the committed ``anchor.live_point``. NEVER returns an
        uncommitted object: any non-committed outcome (at-capacity / already-active / retry / fence /
        worker-held) raises :class:`ImmutableVectorPublishPending` while the durable intent stays for
        the worker to finish."""
        opk = f"{INTENT_KIND}:{object_id}:{secrets.token_hex(8)}"
        status: str = coordinator.enqueue_custom_intent(
            kind=INTENT_KIND,
            object_id=object_id,
            namespace=namespace,
            collection=self._collection,
            patch_json=self._patch_json(content_payload),
            operation_key=opk,
        )
        if status == "admitted":
            coordinator.drive_intent(opk)
        # 'already_active'/'at_capacity' -> we did not admit THIS opk; fall through to the readback,
        # which fails loud unless another writer already committed exactly our intended content point.
        anchor = read_anchor(
            self._client, self._collection, namespace=namespace, object_id=object_id
        )
        committed = resolve_committed_content(
            self._client, self._collection, namespace=namespace, object_id=object_id
        )
        if (
            anchor is not None
            and anchor.live_point == content_point_id_for(opk, 0)
            and committed is not None
        ):
            return committed
        raise ImmutableVectorPublishPending(
            f"vector publish for {object_id!r} not committed inline (status={status!r}); "
            "durable intent remains for worker retry"
        )

    # -- the registered apply handler ------------------------------------------------------------ #

    def apply(self, ctx: Any) -> str:
        return str(_run_coro(self._apply_async(ctx)))

    async def _apply_async(self, ctx: Any) -> str:
        if not ctx.patch_json:
            return "fence"  # no durable payload -> nothing replayable; terminal.
        try:
            patch = json.loads(ctx.patch_json)
            content_payload = dict(patch["content_payload"])
            content = str(content_payload["content"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return "fence"

        anchor = read_anchor(
            self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
        )
        # Idempotent replay: this operation already committed its pointer. Do NOT re-publish, but
        # STILL (re)run cleanup — a prior attempt may have published then failed cleanup, and cleanup
        # is terminal correctness (a confirmed is only truthful once losers are gone).
        if anchor is not None and anchor.committed_operation_id == ctx.operation_key:
            return self._cleanup_and_confirm(ctx.object_id, ctx.namespace, keep=anchor.live_point)

        # Recompute the vector deterministically from the persisted content (NOT a stored blob).
        dense = (await self._embedder.embed_dense([content]))[0]
        sparse = (await self._embedder.embed_sparse([content]))[0]
        generation = ctx.operation_key  # stable per operation
        content_id = content_point_id_for(ctx.operation_key, 0)

        # Stage the write-once content point INVISIBLY (not yet named by live_point).
        self._client.upsert(
            collection_name=self._collection,
            points=[
                models.PointStruct(
                    id=content_id,
                    payload={
                        **content_payload,
                        "object_id": ctx.object_id,
                        "namespace": ctx.namespace,
                        "point_kind": CONTENT_KIND,
                        "generation": generation,
                        "owner_token": ctx.owner_token,
                        "state": "matured",
                    },
                    vector={
                        DENSE_VECTOR_NAME: dense,
                        SPARSE_VECTOR_NAME: _sparse_to_model(sparse),
                    },
                )
            ],
        )
        if self._stall_after_staging:
            self._stall_after_staging = False
            return "retry"  # simulate crash after staging, before publish; reconcile re-drives.

        expected_pv = anchor.pointer_version if anchor else 0
        new_version = (anchor.version if anchor else 0) + 1
        anchor_id = anchor_point_id(ctx.namespace, ctx.object_id)
        publish = {
            "object_id": ctx.object_id,
            "namespace": ctx.namespace,
            "point_kind": ANCHOR_KIND,
            "vector_layout_version": VECTOR_LAYOUT_V2,
            "live_point": content_id,
            "pointer_version": expected_pv + 1,
            "committed_operation_id": ctx.operation_key,
            "version": new_version,
        }
        if anchor is None:
            # First publish or v1 bootstrap: create the anchor, preserving a legacy row's access_count.
            legacy = _read_legacy_v1(
                self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
            )
            base_access = int((legacy.payload or {}).get("access_count", 0)) if legacy else 0
            self._client.upsert(
                collection_name=self._collection,
                points=[
                    models.PointStruct(
                        id=anchor_id,
                        payload={**publish, "access_count": base_access},
                        vector={
                            DENSE_VECTOR_NAME: [0.0] * len(dense),
                            SPARSE_VECTOR_NAME: models.SparseVector(indices=[], values=[]),
                        },
                    )
                ],
            )
            if legacy is not None:
                self._client.delete(collection_name=self._collection, points_selector=[legacy.id])
        else:
            # Fenced pointer swap: partial set_payload (preserves access fields), filtered on the
            # exact observed pointer_version — a stale/lost publisher matches zero.
            self._client.set_payload(
                collection_name=self._collection,
                payload=publish,
                points=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="object_id", match=models.MatchValue(value=ctx.object_id)
                        ),
                        models.FieldCondition(
                            key="namespace", match=models.MatchValue(value=ctx.namespace)
                        ),
                        models.FieldCondition(
                            key="point_kind", match=models.MatchValue(value=ANCHOR_KIND)
                        ),
                        models.FieldCondition(
                            key="pointer_version", match=models.MatchValue(value=expected_pv)
                        ),
                    ]
                ),
            )

        # Exact readback is the ONLY success signal.
        published = read_anchor(
            self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
        )
        if (
            published is None
            or published.committed_operation_id != ctx.operation_key
            or published.live_point != content_id
        ):
            self._delete_content_generation(ctx.object_id, ctx.namespace, generation)
            return "fence"  # lost/fenced — remove only our own staged point.

        return self._cleanup_and_confirm(ctx.object_id, ctx.namespace, keep=content_id)

    def _cleanup_and_confirm(self, object_id: str, namespace: str, keep: str | None) -> str:
        """Cleanup is terminal correctness: confirm ONLY after superseded/loser content is gone. On
        failure return ``retry`` — the published pointer stays attributable and reconcile retries."""
        try:
            if self._fail_cleanup:
                self._fail_cleanup = False
                raise RuntimeError("injected cleanup failure")
            if keep is not None:
                self._delete_superseded_content(object_id, namespace, keep=keep)
        except Exception:
            return "retry"
        return "confirmed"

    def _delete_content_generation(self, object_id: str, namespace: str, generation: str) -> None:
        self._client.delete(
            collection_name=self._collection,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=object_id)
                    ),
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="point_kind", match=models.MatchValue(value=CONTENT_KIND)
                    ),
                    models.FieldCondition(
                        key="generation", match=models.MatchValue(value=generation)
                    ),
                ]
            ),
        )

    def _delete_superseded_content(self, object_id: str, namespace: str, keep: str) -> None:
        """Delete every content point for this object EXCEPT the committed one (``keep``)."""
        recs, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=object_id)
                    ),
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="point_kind", match=models.MatchValue(value=CONTENT_KIND)
                    ),
                ]
            ),
            limit=256,
            with_payload=False,
        )
        losers = [r.id for r in recs if str(r.id) != keep]
        if losers:
            self._client.delete(collection_name=self._collection, points_selector=losers)


__all__ = [
    "ANCHOR_KIND",
    "CONTENT_KIND",
    "INTENT_KIND",
    "VECTOR_LAYOUT_V2",
    "AnchorView",
    "ImmutableVectorPublishPending",
    "ImmutableVectorPublisher",
    "anchor_point_id",
    "content_point_id_for",
    "read_anchor",
    "resolve_committed_content",
]
