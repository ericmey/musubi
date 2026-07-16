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
from musubi.store.memory_serialization import LEASE_OWNED_FIELDS
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import epoch_of, utc_now

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


def _legacy_conversion_filter(namespace: str, object_id: str, obs_version: int) -> models.Filter:
    """Fence for the IN-PLACE conversion of a v1 legacy row INTO the v2 anchor (Yua item 1): target the
    identity row (object_id+namespace, neither anchor nor content) AT the observed version, so a
    concurrent Phase-1 bump matches zero and the op-token readback shows we lost -> retry. A
    never-mutated legacy row carries NO ``version`` field (semantically 0); at ``obs_version == 0`` we
    therefore admit version-absent-or-zero and EXCLUDE any positive version (a Phase-1 bump), rather than
    an equality match that a version-less row could never satisfy."""
    must: list[models.Condition] = [
        models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
        models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
    ]
    must_not: list[models.Condition] = [
        models.FieldCondition(
            key="point_kind", match=models.MatchAny(any=[ANCHOR_KIND, CONTENT_KIND])
        )
    ]
    if obs_version == 0:
        # absent-or-0 admitted; exclude version > 0 (a concurrent Phase-1 bump on the legacy row).
        must_not.append(models.FieldCondition(key="version", range=models.Range(gt=0)))
    else:
        must.append(
            models.FieldCondition(key="version", match=models.MatchValue(value=obs_version))
        )
    return models.Filter(must=must, must_not=must_not)


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
    expose or hide a row.

    The named content point must actually HYDRATE the committed object (Yua): a dangling pointer (no
    such point / empty payload), or a corrupt/cross-object one (not ``point_kind=content``, or a
    mismatched namespace/object_id), FAILS CLOSED — we never serve an anchor-only shell or borrow a
    different row's payload under this anchor's identity."""
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
        if not pts or not pts[0].payload:
            return None  # dangling committed pointer -> fail closed (never an anchor-only shell)
        content_payload = dict(pts[0].payload)
        if (
            content_payload.get("point_kind") != CONTENT_KIND
            or content_payload.get("namespace") != anchor_payload.get("namespace")
            or content_payload.get("object_id") != anchor_payload.get("object_id")
        ):
            return (
                None  # corrupt / cross-object live_point -> fail closed (never borrow another row)
            )
        return {**content_payload, **anchor_payload}  # anchor OVER content = authoritative mutable
    legacy = _read_legacy_v1(client, collection, namespace=namespace, object_id=object_id)
    if legacy is not None and legacy.payload:
        return dict(legacy.payload)  # v1 legacy self-pointer
    return None


_EMBED_KINDS = ("episodic", "curated")


def _projection(embed_kind: str, payload: dict[str, Any]) -> str:
    """The EMBEDDING PROJECTION for a plane (Yua) — the exact text a plane feeds the embedder, derived
    from the fully rebased authoritative payload. A vector change is decided by whether THIS text
    changes (title/summary/content for curated; summary-or-content for episodic), NOT the stored body
    alone: a metadata-only mutation outside the projection stays payload-only, and a summary/title-only
    change re-embeds. Mirrors episodic ``_embed_target`` (plane.py:218) and curated ``_embed_target``
    (plane.py:91-99) exactly, so a reinforced/updated row's vector matches its fresh-insert sibling."""
    body = payload.get("summary") or payload.get("content", "")
    if embed_kind == "curated":
        return f"{payload.get('title', '')}\n\n{body}"
    return str(body)  # episodic / generic: summary-or-content


def _projection_snapshot(embed_kind: str, new_full: dict[str, Any]) -> dict[str, Any]:
    """The IMMUTABLE projection-source fields stamped on the write-once content point (Yua): episodic
    stores content+summary; curated stores title+content+summary — never a ``body`` key. The content
    point is thus a faithful, self-describing snapshot of exactly what produced its vector."""
    snap: dict[str, Any] = {
        "content": new_full.get("content", ""),
        "summary": new_full.get("summary"),
    }
    if embed_kind == "curated":
        snap["title"] = new_full.get("title", "")
    return snap


def _parse_descriptor(patch_json: str | None) -> dict[str, Any] | None:
    """The durable intent payload is an INTENDED MUTATION DESCRIPTOR (never a full snapshot). It carries
    a validated ``embed_kind`` (which projection the handler re-embeds against); a missing/invalid kind
    FAILS CLOSED (Yua) — the handler returns a terminal fence rather than embed the wrong projection."""
    if not patch_json:
        return None
    try:
        outer = json.loads(patch_json)
    except (json.JSONDecodeError, TypeError):
        return None
    desc = outer.get("descriptor") if isinstance(outer, dict) else None
    if not isinstance(desc, dict) or desc.get("op") not in ("reinforce", "set"):
        return None
    if desc.get("embed_kind") not in _EMBED_KINDS:
        return None  # missing/invalid embed_kind -> fail closed (never guess the projection).
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
        self._inject_pre_publish: Any | None = None

    def register(self, coordinator: Any) -> None:
        coordinator.register_intent_handler(INTENT_KIND, self.apply)

    def stall_after_staging_once(self) -> None:
        self._stall_after_staging = True

    def fail_cleanup_once(self) -> None:
        self._fail_cleanup = True

    def inject_pre_publish_once(self, fn: Any) -> None:
        """Test seam: run ``fn`` exactly once INSIDE apply, AFTER fresh has been read/rebased but BEFORE
        the fenced write — the exact window a concurrent Phase-1 mutation would land in. Lets a test
        prove the version fence / attributable readback without depending on real thread interleaving."""
        self._inject_pre_publish = fn

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
            patch_json=self._descriptor_json(
                {"op": "set", "set_fields": content_payload, "embed_kind": "episodic"}
            ),
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
        lease/access fields from fresh — decides vector-change by the EPISODIC projection (summary or
        content), and dual-fences on pointer_version AND version. Returns the committed authoritative
        payload; raises pending if it cannot commit inline."""
        return self._publish_descriptor(
            coordinator,
            object_id,
            namespace,
            {
                "op": "reinforce",
                "new_memory": new_memory,
                "merge_strategy": merge_strategy,
                "embed_kind": "episodic",
            },
        )

    def curated_publish(
        self, coordinator: Any, *, object_id: str, namespace: str, set_fields: dict[str, Any]
    ) -> dict[str, Any]:
        """SYNCHRONOUS curated field-set publish, rebased on fresh state + dual-fenced. Vector-change is
        decided by the CURATED projection (title + summary-or-content), so a title/summary/content change
        re-embeds while a non-projection metadata change stays payload-only."""
        return self._publish_descriptor(
            coordinator,
            object_id,
            namespace,
            {"op": "set", "set_fields": set_fields, "embed_kind": "curated"},
        )

    def publish(
        self, coordinator: Any, *, object_id: str, namespace: str, content_payload: dict[str, Any]
    ) -> dict[str, Any]:
        """SYNCHRONOUS 'set' publish (production/test, GENERIC episodic projection), rebased + fenced."""
        return self._publish_descriptor(
            coordinator,
            object_id,
            namespace,
            {"op": "set", "set_fields": content_payload, "embed_kind": "episodic"},
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
            # ``already_active``: a DIFFERENT operation holds the active intent for this object (our opk is
            # freshly random and was NOT inserted). NEVER return the current pre-mutation committed row as
            # if THIS request landed — fail loud pending so the caller sees its write did not commit (Yua
            # item 3). The other operation's durable intent will drive that object forward on its own.
            raise ImmutableVectorPublishPending(
                f"another intent is already active for {object_id!r}; this publish did not land"
            )
        # Drive OUR intent inline, retrying under contention: a dual-fence conflict returns 'retry', and
        # drive_intent bypasses the retry backoff for this explicit inline drive (Yua item 4) so the
        # coordinator re-reads fresh and re-applies immediately — no production sleep. Both changes
        # converge; a persistent conflict exhausts the bound and fails loud (durable intent remains).
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
        # Read the FRESH authoritative identity (anchor-over-content, or a v1 legacy row) ONCE — it drives
        # both idempotent-replay detection and the rebase/observed-version fence below.
        fresh = resolve_committed_content(
            self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
        )
        # Idempotent replay (crash after our commit, before FINAL) for BOTH v1 and v2, vector-change AND
        # payload-only: every committed path stamps ``committed_operation_id``, so we re-detect OUR exact
        # token on the identity row and re-run only cleanup — never a second apply (Yua item 2).
        if fresh is not None and fresh.get("committed_operation_id") == ctx.operation_key:
            return self._cleanup_and_confirm(
                ctx.object_id, ctx.namespace, keep=fresh.get("live_point")
            )

        # REBASE ON FRESH AUTHORITATIVE STATE (Yua): apply ONLY the intended generic ops to fresh, take
        # lease/access fields from FRESH (never the caller patch), and decide content/vector-change HERE.
        # A stale caller snapshot can never overwrite a concurrent unrelated mutation because we recompute
        # against fresh + fence on the observed version below.
        obs_version = int((fresh or {}).get("version", 0))
        obs_pv = anchor.pointer_version if anchor is not None else 0
        embed_kind = str(descriptor["embed_kind"])
        new_full, _, _ = _rebase(descriptor, fresh)
        # never let a caller patch carry lease-owned fields onto the anchor.
        for f in _LEASE_OWNED_ON_ANCHOR:
            new_full.pop(f, None)

        # test-only concurrency seam: land a mutation in the fresh-read -> fenced-write window.
        if self._inject_pre_publish is not None:
            fn = self._inject_pre_publish
            self._inject_pre_publish = None
            fn()

        # VECTOR-CHANGE is decided by the EMBEDDING PROJECTION, not the stored content alone (Yua): a
        # title/summary/content change re-embeds; a metadata-only mutation OUTSIDE the projection (e.g.
        # importance, tags) stays payload-only even though new_full differs from fresh.
        new_projection = _projection(embed_kind, new_full)
        vector_changed = (new_projection != _projection(embed_kind, fresh or {})) or not fresh
        if not vector_changed:
            # PAYLOAD-ONLY: narrow fenced set_payload on the identity row, fenced on the observed version.
            # No new content point. A concurrent version bump fails the fence -> retry against fresh.
            return self._publish_payload_only(ctx, anchor, new_full, obs_version)

        # VECTOR CHANGE: embed the NEW projection, stage a write-once content point carrying the immutable
        # projection-source snapshot, then a SINGLE fenced anchor publish gated on pointer_version AND
        # version.
        dense = (await self._embedder.embed_dense([new_projection]))[0]
        sparse = (await self._embedder.embed_sparse([new_projection]))[0]
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
                        # immutable projection-source snapshot (episodic: content+summary;
                        # curated: title+content+summary) — never a 'body' key (Yua).
                        **_projection_snapshot(embed_kind, new_full),
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
            legacy = _read_legacy_v1(
                self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
            )
            if legacy is not None:
                # IN-PLACE fenced conversion of the v1 row INTO the anchor — NEVER delete an unfenced
                # legacy row (Yua item 1). The version-fenced set_payload matches zero if a concurrent
                # Phase-1 bump moved the row's version, and the op-token readback below then shows we did
                # not win -> retry against fresh. One row always represents the object (no two-identity
                # window), so the leases' ``must_not content`` still targets exactly one row throughout.
                # The converted row keeps its legacy vector; anchor-aware reads exclude it by point_kind
                # (the universal anchor-never-ranks mechanism), so a real vector here cannot leak.
                base_access = int((legacy.payload or {}).get("access_count", 0))
                self._client.set_payload(
                    collection_name=self._collection,
                    payload={**publish, "access_count": base_access},
                    points=_legacy_conversion_filter(ctx.namespace, ctx.object_id, obs_version),
                )
            else:
                # Brand-new object (no legacy row): create the anchor separately with a zero vector.
                self._client.upsert(
                    collection_name=self._collection,
                    points=[
                        models.PointStruct(
                            id=anchor_point_id(ctx.namespace, ctx.object_id),
                            payload={**publish, "access_count": 0},
                            vector={
                                DENSE_VECTOR_NAME: [0.0] * len(dense),
                                SPARSE_VECTOR_NAME: models.SparseVector(indices=[], values=[]),
                            },
                        )
                    ],
                )
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
        identity row (anchor if v2, else the v1 row) gated on the observed version, stamping OUR
        ``committed_operation_id`` so success is ATTRIBUTABLE (Yua item 2): a concurrent writer that also
        bumped version to ``obs+1`` leaves ITS token, not ours, so version equality alone would falsely
        confirm. No new content point, no vector recompute. Losing the fence -> retry against fresh.

        The identity fence is chosen by LAYOUT (Yua item 7): a v2 anchor fences on its EXACT anchor
        identity (point_kind==anchor) + observed version; a v1/legacy row (``anchor is None``) reuses
        :func:`_legacy_conversion_filter`, which admits an absent-or-zero ``version`` so a version-less
        legacy row's metadata-only mutation commits once instead of an exact ``version==0`` match that a
        field-less row can never satisfy (which would retry forever). The v1 row keeps NO ``point_kind``,
        so a payload-only mutation preserves the v1 self-pointer and never fabricates a content point."""
        narrow = {
            **new_full,
            "object_id": ctx.object_id,
            "namespace": ctx.namespace,
            "version": obs_version + 1,
            "committed_operation_id": ctx.operation_key,
        }
        if anchor is None:
            fence = _legacy_conversion_filter(ctx.namespace, ctx.object_id, obs_version)
        else:
            # v2: fence on the EXACT anchor identity (point_kind==anchor), not merely must_not content —
            # a stray legacy identity row for the same object would otherwise match too (Yua tightening).
            fence = models.Filter(
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
                        key="version", match=models.MatchValue(value=obs_version)
                    ),
                ]
            )
        self._client.set_payload(collection_name=self._collection, payload=narrow, points=fence)
        fresh = resolve_committed_content(
            self._client, self._collection, namespace=ctx.namespace, object_id=ctx.object_id
        )
        # sole success signal: OUR exact token landed (a bare version==obs+1 could be a foreign writer).
        if (
            fresh is not None
            and fresh.get("committed_operation_id") == ctx.operation_key
            and int(fresh.get("version", -1)) == obs_version + 1
        ):
            return "confirmed"
        return "retry"  # a concurrent writer won the version -> recompute against fresh.

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
        """Delete EVERY content point for this object except the committed one (``keep``) in ONE
        server-side filtered delete (Yua item 5): ``must_not has_id(keep)`` excludes exactly the live
        point, so a >256 fan-out is fully collected with no client-side ``limit``/pagination that could
        silently orphan the tail past the first page."""
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
                ],
                must_not=[models.HasIdCondition(has_id=[keep])],
            ),
        )


def register_immutable_vector_dispatch(
    coordinator: Any, publishers: dict[str, ImmutableVectorPublisher]
) -> None:
    """Register ONE collection-aware handler for the shared ``immutable_vector_publish`` intent kind
    (Yua). The coordinator holds exactly ONE handler per intent_kind; episodic and curated both publish
    under this kind, so calling each publisher's :meth:`ImmutableVectorPublisher.register` in turn would
    SILENTLY OVERWRITE — only the last-registered collection could reconcile, and the other's durable
    intents would dispatch to the wrong bound apply. Instead this installs a single dispatcher that
    routes ``ctx.collection`` to that collection's bound ``publisher.apply``; an intent for an
    unregistered collection is a misconfiguration and FAILS LOUD as a terminal fence (never a silent
    wrong-collection apply, never an endless retry). Use this in every multi-collection composition;
    the per-publisher ``register`` remains for single-collection tests."""
    by_collection = dict(publishers)

    def _dispatch(ctx: Any) -> str:
        publisher = by_collection.get(ctx.collection)
        if publisher is None:
            return "fence"  # no publisher bound to this collection -> terminal, fail loud.
        return str(publisher.apply(ctx))

    coordinator.register_intent_handler(INTENT_KIND, _dispatch)


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
    "register_immutable_vector_dispatch",
    "resolve_committed_content",
]
