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
import time
import uuid
from dataclasses import dataclass
from typing import Any

from qdrant_client import models

from musubi.embedding.base import Embedder
from musubi.planes.episodic.plane import _sparse_to_model
from musubi.store.memory_serialization import LEASE_OWNED_FIELDS
from musubi.store.specs import (
    DENSE_VECTOR_NAME,
    POINT_KIND_CONTENT,
    POINT_KIND_FIELD,
    SPARSE_VECTOR_NAME,
)
from musubi.types.common import epoch_of, utc_now

# must_not condition targeting the AUTHORITATIVE identity row (never a content snapshot).
_EXCLUDE_CONTENT_COND: list[models.Condition] = [
    models.FieldCondition(key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT))
]
_SYNC_DRIVE_ATTEMPTS = 8  # bounded inline re-drives to commit synchronously under contention

# Fields the anchor NEVER stamps on a publish — they are owned by the RET-008 access lease and the
# Phase-1 mutation lease, which write them on the anchor directly. A partial set_payload that omits
# them preserves the lease-written values (Yua: anchor owns the authoritative mutable payload, but the
# LEASES write these fields, not the vector publish).
_LEASE_OWNED_ON_ANCHOR = LEASE_OWNED_FIELDS | {"update_lease_token"}

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
    """Return the authoritative committed payload for ``object_id``: the content snapshot named by the
    anchor's ``live_point`` MERGED WITH the anchor payload OVER it (the anchor is authoritative for
    every mutable field — Yua), or a v1 legacy self-pointer. A v2 anchor with an absent ``live_point``
    FAILS CLOSED (returns None). Because the anchor wins, a stale content-snapshot field can never
    expose or hide a row."""
    recs, _ = client.scroll(
        collection_name=collection,
        scroll_filter=_anchor_filter(namespace, object_id),
        limit=1,
        with_payload=True,
    )
    if recs and recs[0].payload:
        anchor_payload = dict(recs[0].payload)
        live_point = anchor_payload.get("live_point")
        if not live_point:
            return None  # v2 anchor, no committed pointer -> fail closed (never treated as legacy)
        pts = client.retrieve(collection_name=collection, ids=[live_point], with_payload=True)
        content_payload = dict(pts[0].payload) if pts and pts[0].payload else {}
        return {**content_payload, **anchor_payload}  # anchor OVER content = authoritative mutable
    legacy = _read_legacy_v1(client, collection, namespace=namespace, object_id=object_id)
    if legacy is not None and legacy.payload:
        return dict(legacy.payload)  # v1 legacy self-pointer
    return None


def _parse_descriptor(patch_json: str | None) -> dict[str, Any] | None:
    """The durable intent payload is an INTENDED MUTATION DESCRIPTOR (never a full snapshot)."""
    if not patch_json:
        return None
    try:
        outer = json.loads(patch_json)
    except (json.JSONDecodeError, TypeError):
        return None
    desc = outer.get("descriptor") if isinstance(outer, dict) else None
    if not isinstance(desc, dict) or desc.get("op") not in ("reinforce", "set"):
        return None
    return desc


def _pick_content(existing: str, incoming: str, strategy: str) -> str:
    """Content winner for a reinforce. ``longer-wins``: the longer content wins, ties keep existing."""
    if strategy == "longer-wins":
        return incoming if len(incoming) > len(existing) else existing
    return incoming  # default: incoming wins


def _rebase(
    descriptor: dict[str, Any], fresh: dict[str, Any] | None
) -> tuple[dict[str, Any], str, bool]:
    """Apply the intended generic ops to the FRESH authoritative payload. Returns
    ``(new_full_payload, winning_content, content_changed)``. Fields not touched by the op are
    inherited from ``fresh`` unchanged — so a concurrent unrelated mutation already in ``fresh``
    survives. Lease/access fields are inherited from fresh and stripped by the caller before write."""
    base = dict(fresh or {})
    fresh_content = str(base.get("content", ""))
    if descriptor["op"] == "reinforce":
        new_mem = dict(descriptor["new_memory"])
        if not fresh:
            base = dict(new_mem)  # brand-new object -> bootstrap on the incoming memory.
        incoming = str(new_mem.get("content", ""))
        winner = _pick_content(
            fresh_content, incoming, descriptor.get("merge_strategy", "longer-wins")
        )
        tags = sorted(set(base.get("tags", []) or []) | set(new_mem.get("tags", []) or []))
        rc = int(base.get("reinforcement_count", 0)) + 1
        now = utc_now()
        new_full = {
            **base,
            "content": winner,
            "tags": tags,
            "reinforcement_count": rc,
            "updated_at": now.isoformat(),
            "updated_epoch": epoch_of(now),
        }
        return new_full, winner, (winner != fresh_content) or not fresh
    # op == "set": explicit field set (curated update + generic replace).
    set_fields = dict(descriptor["set_fields"])
    new_full = {**base, **set_fields}
    winner = str(new_full.get("content", fresh_content))
    changed = (("content" in set_fields) and set_fields["content"] != fresh_content) or not fresh
    return new_full, winner, changed


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

    def _descriptor_json(self, descriptor: dict[str, Any]) -> str:
        return json.dumps({"descriptor": descriptor}, sort_keys=True, separators=(",", ":"))

    def admit_publish(
        self, coordinator: Any, *, object_id: str, namespace: str, content_payload: dict[str, Any]
    ) -> str:
        """Durably admit ONE 'set' descriptor intent (WORKER-driven; used by the store-level tests). The
        handler rebases the field-set on fresh state. Returns the admission status."""
        status: str = coordinator.enqueue_custom_intent(
            kind=INTENT_KIND,
            object_id=object_id,
            namespace=namespace,
            collection=self._collection,
            patch_json=self._descriptor_json({"op": "set", "set_fields": content_payload}),
        )
        return status

    async def reinforce_publish(
        self,
        coordinator: Any,
        *,
        object_id: str,
        namespace: str,
        new_memory: dict[str, Any],
        merge_strategy: str,
    ) -> dict[str, Any]:
        """SYNCHRONOUS episodic reinforce publish (production write path). The handler REBASES on fresh
        state — longer-wins content choice vs ``new_memory``, tag UNION, reinforcement_count INCREMENT,
        lease/access fields from fresh — decides content/vector-change there, and dual-fences on
        pointer_version AND version. Returns the committed authoritative payload; raises pending if it
        cannot commit inline."""
        return self._publish_descriptor(
            coordinator,
            object_id,
            namespace,
            {"op": "reinforce", "new_memory": new_memory, "merge_strategy": merge_strategy},
        )

    def curated_publish(
        self, coordinator: Any, *, object_id: str, namespace: str, set_fields: dict[str, Any]
    ) -> dict[str, Any]:
        """SYNCHRONOUS curated field-set publish, rebased on fresh state + dual-fenced (as above)."""
        return self._publish_descriptor(
            coordinator, object_id, namespace, {"op": "set", "set_fields": set_fields}
        )

    def publish(
        self, coordinator: Any, *, object_id: str, namespace: str, content_payload: dict[str, Any]
    ) -> dict[str, Any]:
        """SYNCHRONOUS 'set' publish (production/test), rebased on fresh state + dual-fenced."""
        return self._publish_descriptor(
            coordinator, object_id, namespace, {"op": "set", "set_fields": content_payload}
        )

    def _publish_descriptor(
        self, coordinator: Any, object_id: str, namespace: str, descriptor: dict[str, Any]
    ) -> dict[str, Any]:
        opk = f"{INTENT_KIND}:{object_id}:{secrets.token_hex(8)}"
        status: str = coordinator.enqueue_custom_intent(
            kind=INTENT_KIND,
            object_id=object_id,
            namespace=namespace,
            collection=self._collection,
            patch_json=self._descriptor_json(descriptor),
            operation_key=opk,
        )
        if status == "at_capacity":
            raise ImmutableVectorPublishPending(f"outbox at capacity for {object_id!r}")
        if status != "admitted":
            # another writer holds an active intent for this object -> read back or fail loud.
            committed = resolve_committed_content(
                self._client, self._collection, namespace=namespace, object_id=object_id
            )
            if committed is not None:
                return committed
            raise ImmutableVectorPublishPending(f"an intent is already active for {object_id!r}")
        # Drive OUR intent inline, retrying under contention: a dual-fence conflict returns 'retry',
        # the coordinator re-reads fresh each re-drive, so both changes converge (never uncommitted).
        for _ in range(_SYNC_DRIVE_ATTEMPTS):
            report = coordinator.drive_intent(opk)
            if report.finalized:
                committed = resolve_committed_content(
                    self._client, self._collection, namespace=namespace, object_id=object_id
                )
                if committed is not None:
                    return committed
            if report.abandoned:
                break  # terminal fence -> will never commit inline.
            time.sleep(0.01)  # 'retry' outcome / backoff -> re-drive against the newer fresh state.
        raise ImmutableVectorPublishPending(
            f"vector publish for {object_id!r} not committed inline; durable intent remains for worker"
        )

    # -- the registered apply handler ------------------------------------------------------------ #

    def apply(self, ctx: Any) -> str:
        return str(_run_coro(self._apply_async(ctx)))

    async def _apply_async(self, ctx: Any) -> str:
        descriptor = _parse_descriptor(ctx.patch_json)
        if descriptor is None:
            return "fence"  # no replayable descriptor -> terminal.

        anchor = read_anchor(
            self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
        )
        # Idempotent replay: this operation already committed its pointer. Re-run cleanup only.
        if anchor is not None and anchor.committed_operation_id == ctx.operation_key:
            return self._cleanup_and_confirm(ctx.object_id, ctx.namespace, keep=anchor.live_point)

        # REBASE ON FRESH AUTHORITATIVE STATE (Yua): read the current anchor-over-content (or v1), apply
        # ONLY the intended generic ops, take lease/access fields from FRESH (never the caller patch),
        # and decide content/vector-change HERE. A stale caller snapshot can never overwrite a
        # concurrent unrelated mutation because we recompute against fresh + fence on the observed
        # version below.
        fresh = resolve_committed_content(
            self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
        )
        obs_version = int((fresh or {}).get("version", 0))
        obs_pv = anchor.pointer_version if anchor is not None else 0
        new_full, winning_content, content_changed = _rebase(descriptor, fresh)
        # never let a caller patch carry lease-owned fields onto the anchor.
        for f in _LEASE_OWNED_ON_ANCHOR:
            new_full.pop(f, None)

        if not content_changed:
            # PAYLOAD-ONLY (existing content won): narrow fenced set_payload on the identity row,
            # fenced on the observed version. No new content point. A concurrent version bump fails
            # the fence -> retry -> recompute against the newer fresh state.
            return self._publish_payload_only(ctx, anchor, new_full, obs_version)

        # VECTOR CHANGE: recompute the vector from the winning content, stage a write-once content
        # point, then a SINGLE fenced anchor publish gated on BOTH pointer_version AND version.
        dense = (await self._embedder.embed_dense([winning_content]))[0]
        sparse = (await self._embedder.embed_sparse([winning_content]))[0]
        generation = ctx.operation_key
        content_id = content_point_id_for(ctx.operation_key, 0)
        self._client.upsert(
            collection_name=self._collection,
            points=[
                models.PointStruct(
                    id=content_id,
                    payload={
                        "object_id": ctx.object_id,
                        "namespace": ctx.namespace,
                        "point_kind": CONTENT_KIND,
                        "generation": generation,
                        "owner_token": ctx.owner_token,
                        "content": winning_content,
                    },
                    vector={DENSE_VECTOR_NAME: dense, SPARSE_VECTOR_NAME: _sparse_to_model(sparse)},
                )
            ],
        )
        if self._stall_after_staging:
            self._stall_after_staging = False
            return "retry"

        publish = {
            **new_full,
            "object_id": ctx.object_id,
            "namespace": ctx.namespace,
            "point_kind": ANCHOR_KIND,
            "vector_layout_version": VECTOR_LAYOUT_V2,
            "live_point": content_id,
            "pointer_version": obs_pv + 1,
            "committed_operation_id": ctx.operation_key,
            "version": obs_version + 1,
        }
        if anchor is None:
            # First vector change / v1 bootstrap: fence the create on the legacy row's version by
            # conditionally deleting it; a concurrent Phase-1 bump on the v1 row leaves it, and our
            # readback then shows we did not win -> retry.
            legacy = _read_legacy_v1(
                self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
            )
            base_access = int((legacy.payload or {}).get("access_count", 0)) if legacy else 0
            self._client.upsert(
                collection_name=self._collection,
                points=[
                    models.PointStruct(
                        id=anchor_point_id(ctx.namespace, ctx.object_id),
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
            # Fenced pointer swap on BOTH observed pointer_version AND version (Yua dual fence): a
            # concurrent vector publish (pointer_version) OR Phase-1 payload mutation (version) matches
            # zero, so we lose and retry against fresh.
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
                            key="pointer_version", match=models.MatchValue(value=obs_pv)
                        ),
                        models.FieldCondition(
                            key="version", match=models.MatchValue(value=obs_version)
                        ),
                    ]
                ),
            )

        published = read_anchor(
            self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
        )
        if (
            published is None
            or published.committed_operation_id != ctx.operation_key
            or published.live_point != content_id
        ):
            self._delete_content_generation(ctx.object_id, ctx.namespace, generation)
            return (
                "retry"  # lost the dual fence -> reconcile re-drives against the newer fresh state.
            )
        return self._cleanup_and_confirm(ctx.object_id, ctx.namespace, keep=content_id)

    def _publish_payload_only(
        self, ctx: Any, anchor: AnchorView | None, new_full: dict[str, Any], obs_version: int
    ) -> str:
        """Existing content won -> only narrow payload fields changed. Fenced set_payload on the
        identity row (anchor if v2, else the v1 row) gated on the observed version. No new content
        point, no vector recompute. A concurrent version bump fails the fence -> retry."""
        narrow = {
            **new_full,
            "object_id": ctx.object_id,
            "namespace": ctx.namespace,
            "version": obs_version + 1,
        }
        must: list[models.Condition] = [
            models.FieldCondition(key="object_id", match=models.MatchValue(value=ctx.object_id)),
            models.FieldCondition(key="namespace", match=models.MatchValue(value=ctx.namespace)),
            models.FieldCondition(key="version", match=models.MatchValue(value=obs_version)),
        ]
        self._client.set_payload(
            collection_name=self._collection,
            payload=narrow,
            points=models.Filter(must=must, must_not=_EXCLUDE_CONTENT_COND),  # identity row only
        )
        fresh = resolve_committed_content(
            self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
        )
        if fresh is not None and int(fresh.get("version", -1)) == obs_version + 1:
            return "confirmed"
        return "retry"  # a concurrent bump won the version -> recompute against fresh.

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
