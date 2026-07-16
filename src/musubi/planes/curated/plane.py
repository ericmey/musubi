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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator, TransitionPending
from musubi.lifecycle.transitions import TransitionError, TransitionResult, transition
from musubi.store.mutation_lease import MutationPlan, owned_update
from musubi.store.names import collection_for_plane
from musubi.store.raw_lookup import point_exists, raw_payload
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME, strip_layout_fields
from musubi.types.common import KSUID, Err, LifecycleState, Namespace, Ok, Result, epoch_of, utc_now
from musubi.types.curated import CuratedKnowledge

# Distinct from the episodic point-namespace UUID — keeps the two
# collections' point IDs in disjoint UUID spaces even when KSUIDs collide
# (they shouldn't, but defence in depth).
_POINT_NS = uuid.UUID("6b0d5e2e-1e8e-4e0f-8e3e-000000000002")

_VISIBLE_STATES: tuple[LifecycleState, ...] = ("matured",)

# DATA-001 P2: the ONLY fields a same-id body/frontmatter update may set on the durable descriptor
# (Yua ruling). Everything else — lifecycle ``state`` (transitions own it), ``namespace``, identity
# (``object_id``), creation (``created_at``/``created_epoch``), ``version``, lineage
# (``supersedes``/``superseded_by``/``promoted_from``/``promoted_at``), and access/lease/anchor
# internals — is inherited from the FRESH authoritative row inside the handler, so a concurrent
# transition/lineage/access mutation between this read and the apply is never clobbered. An ALLOWLIST
# (not a denylist) so a newly-added model field defaults to the conservative inherit-from-fresh.
# ``updated_at``/``updated_epoch`` are stamped separately with ONE request timestamp.
_CURATED_AUTHOR_FIELDS: tuple[str, ...] = (
    "schema_version",
    "title",
    "summary",
    "content",
    "topics",
    "tags",
    "importance",
    "vault_path",
    "valid_from",
    "valid_from_epoch",  # derived epoch — must move WITH valid_from or range queries filter on stale
    "valid_until",
    "valid_until_epoch",
    "musubi_managed",
    "body_hash",
    "merged_from",
    "supported_by",
    "linked_to_topics",
    "contradicts",
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


def _curated_from_payload(payload: dict[str, Any]) -> CuratedKnowledge:
    """Rehydrate a :class:`CuratedKnowledge` from a Qdrant payload dict.

    ``model_validate`` re-runs the bitemporal + monotonicity validators —
    the plane must never hand out a half-constructed object.
    """
    return CuratedKnowledge.model_validate(payload)


def _curated_safe(payload: dict[str, Any]) -> CuratedKnowledge | None:
    """Validate a resolved+stripped ranked-query candidate, or None if it will not model-validate.

    Only the RANKED QUERY skips a malformed authoritative payload — it must not 500 the whole retrieval
    over one bad row. The by-id fetch (:meth:`get`) and the reconciler scan (:meth:`scan_vault_rows`)
    do the opposite and SURFACE corruption (raise): a caller asking for one specific row, or building a
    complete trustworthy inventory, must hear that a row is broken (Yua)."""
    try:
        return _curated_from_payload(payload)
    except ValidationError:
        return None


def _curated_visible_at(row: CuratedKnowledge, at_epoch: float) -> bool:
    """POST-hydration curated visibility on the TYPED, already-validated row: state in the default view
    AND the bitemporal window ``(valid_from is null OR valid_from <= at) AND (valid_until is null OR at <
    valid_until)``. Evaluated on ``CuratedKnowledge`` (``valid_*_epoch`` are ``float | None``), NEVER on
    the raw payload — a malformed string/object epoch there would ``TypeError`` and 500 the query, so the
    row must be validated (or dropped) FIRST (Yua). Applied after anchor hydration because a v2 content
    point carries no state/validity — only the committed anchor does."""
    if str(row.state) not in {str(s) for s in _VISIBLE_STATES}:
        return False
    if row.valid_from_epoch is not None and row.valid_from_epoch > at_epoch:
        return False
    return not (row.valid_until_epoch is not None and at_epoch >= row.valid_until_epoch)


def _embed_target(memory: CuratedKnowledge) -> str:
    """The text we feed the embedder.

    Per the spec, curated rows embed ``title + summary`` when the summary
    is present and ``title + content`` otherwise. Real chunking of large
    bodies into ``ArtifactChunk`` rows is a slice-plane-artifact concern.
    """
    body = memory.summary if memory.summary else memory.content
    return f"{memory.title}\n\n{body}"


@dataclass(frozen=True)
class FindByVaultPathError:
    """Typed error from :meth:`CuratedPlane.find_by_vault_path`.

    ``code`` is one of:
      - ``not_found``        — no IDENTITY row matched the supplied ``vault_path``.
      - ``multiple_matches`` — more than one IDENTITY row matched (the
        ``(namespace, vault_path)`` uniqueness invariant was violated).
        The caller MUST treat this as a visible warning and refuse to
        take destructive action against an arbitrary match (Yua
        VAULT-003 binding: fail closed and visibly on >1 matches).
      - ``invalid_row``      — exactly one identity matched but it is
        DANGLING (a v2 anchor with no committed content) or MALFORMED
        (will not model-validate). DATA-001 P2 (Yua): a broken identity
        must NOT collapse into a clean ``not_found`` — the path IS
        occupied, so the watcher must WARN/REFUSE rather than treat it as
        a safe archive-by-path no-op. Visible fail-closed, never silent.
    """

    code: str
    detail: str
    # For ``multiple_matches`` this is the OBSERVED BOUNDED LOWER BOUND, not
    # the total cardinality: ``find_by_vault_path`` scrolls with ``limit=2``
    # (the second match is sufficient to fail closed), so ``match_count`` is
    # capped at 2 and a real duplicate set may be larger. Read it as
    # "at least this many matched", never as an exact count.
    match_count: int = 0
    # Likewise bounded: at most the first two matching object_ids observed.
    match_object_ids: tuple[str, ...] = ()


class CuratedPlane:
    """CRUD + lifecycle transitions for the curated knowledge plane."""

    def __init__(
        self,
        *,
        client: QdrantClient,
        embedder: Embedder,
        coordinator: LifecycleTransitionCoordinator | None = None,
        vector_publisher: Any | None = None,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._collection = collection_for_plane("curated")
        # DATA-001 P2: a same-id body/frontmatter update is a vector-capable mutation and goes through
        # the durable immutable-vector publisher (fenced anchor + content snapshot). Injected only in
        # PRODUCTION WRITE compositions; a read-only construction leaves them None and a same-id update
        # then FAILS CLOSED (never the old unfenceable update_vectors). Approved optional injection (Yua).
        self._coordinator = coordinator
        self._vector_publisher = vector_publisher

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

        # Same logical object (same `object_id` in vault frontmatter),
        # new body: this is an UPDATE, not a supersession. Pre-musubi#362
        # the code below went into the supersession path even for this
        # case, which built a new_row with `object_id == existing.object_id`
        # AND `supersedes=[existing.object_id]` — failing the
        # `MemoryObject` invariant "object cannot appear in its own
        # supersedes list." Surfaced loudly the first time vault_reconcile
        # actually ran (musubi#357 deploy), errored 11 of 26 reflection
        # files. Distinguishing the two cases here is the honest semantic:
        # supersession is for distinct objects sharing a vault slot;
        # same-id-different-body is just an in-place update.
        if memory.object_id == existing.object_id:
            # Same-id-different-body is an in-place UPDATE (not a supersession). DATA-001 P2: it is a
            # vector-capable mutation, so it publishes through the durable immutable-vector seam (fenced
            # anchor + write-once content), fail-closed if unwired. The durable descriptor carries ONLY
            # the author-managed frontmatter (``_CURATED_AUTHOR_FIELDS`` + one request ``updated_at``);
            # identity, creation, lineage, lifecycle ``state`` (transitions own it), ``namespace``, and
            # access/anchor internals are inherited from the FRESH authoritative row INSIDE the handler,
            # so a concurrent supersession/promotion/state/access mutation between this read and the
            # apply is never overwritten (Yua). The handler decides payload-only vs vector-change by the
            # curated projection (title + summary-or-content). Version bumps once, in the handler.
            if self._vector_publisher is None or self._coordinator is None:
                raise RuntimeError(
                    "curated same-id update reached the vector path but the immutable-vector publisher "
                    "is not wired (DATA-001 P2 fail-closed)"
                )
            now_u = utc_now()
            dump = memory.model_dump(mode="json")
            set_fields: dict[str, Any] = {
                key: dump[key] for key in _CURATED_AUTHOR_FIELDS if key in dump
            }
            set_fields["updated_at"] = now_u.isoformat()
            set_fields["updated_epoch"] = epoch_of(now_u)
            committed = self._vector_publisher.curated_publish(
                self._coordinator,
                object_id=str(existing.object_id),
                namespace=str(existing.namespace),
                set_fields=set_fields,
            )
            # resolve, then validate: strip the Phase-2 layout-only keys the extra="forbid" model rejects.
            return CuratedKnowledge.model_validate(strip_layout_fields(committed))

        # True supersession path (distinct objects sharing a vault slot).
        # Two writes: insert the new row, mark the old row superseded.
        # Both writes hit the same collection; if the second fails the
        # first is still on disk — acceptable because a stale "superseded"
        # row won't appear in default queries until the supersession
        # completes (eventual consistency handled at
        # slice-lifecycle-engine).
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

        # Mark the old row superseded through the attributable mutation lease (DATA-001 #530): a
        # NARROW change-set (state + superseded_by + updated_at) fenced on the exact version, so a
        # concurrent unrelated mutation to the old row is never overwritten. No vector change.
        def supersede_plan(current: dict[str, Any]) -> MutationPlan:
            now_s = utc_now()
            superseded = CuratedKnowledge.model_validate(
                {
                    **current,
                    "state": "superseded",
                    "superseded_by": new_row.object_id,
                    "updated_at": now_s,
                    "updated_epoch": epoch_of(now_s),
                }
            )
            dumped = superseded.model_dump(mode="json")
            changes = {
                k: dumped[k] for k in ("state", "superseded_by", "updated_at", "updated_epoch")
            }
            return MutationPlan(changes=changes)

        await owned_update(
            self._client,
            self._collection,
            namespace=str(existing.namespace),
            object_id=str(existing.object_id),
            point_id=_point_id(existing.object_id),
            plan=supersede_plan,
        )

        return new_row

    def _upsert(
        self,
        memory: CuratedKnowledge,
        *,
        dense: list[float],
        sparse: dict[int, float],
    ) -> None:
        """CREATE-only full-point upsert (fresh insert / supersession new row). Every UPDATE path
        publishes through the attributable mutation lease (:mod:`musubi.store.mutation_lease`)."""
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

        DATA-001 P2: the scroll targets the IDENTITY row (``must_not`` content) and RESOLVES it through
        its anchor before validating (``extra="forbid"`` rejects raw layout keys). A normal v2 content
        point carries no ``vault_path`` (only its projection source), so ``must_not`` content is here as
        fail-closed defense against a corrupt/future content shell that DID carry ``vault_path`` and could
        shadow the real anchor. Crucially, this DISTINGUISHES two cases the caller must not conflate
        (Yua): NO identity for the path -> ``None`` (``create`` inserts fresh); a found-but-DANGLING/
        MALFORMED identity -> RAISE (fail closed) — returning ``None`` there would let ``create``
        manufacture a DUPLICATE for a path that is already occupied by a broken row."""
        from musubi.store.immutable_vectors import not_content_condition, resolve_committed_content

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
                ],
                must_not=not_content_condition(),
            ),
            limit=1,
            with_payload=True,
        )
        if not records:
            return None  # genuinely no identity for this path -> create() may insert fresh
        payload = records[0].payload
        if not payload:
            raise ValueError(  # an identity row with no payload is BROKEN, not absent -> fail closed
                f"curated identity for vault_path={vault_path!r} has an empty payload"
            )
        resolved = resolve_committed_content(
            self._client,
            self._collection,
            namespace=str(payload.get("namespace", namespace)),
            object_id=str(payload.get("object_id", "")),
        )
        if resolved is None:
            raise ValueError(  # found-but-dangling: the path IS occupied by a broken identity
                f"curated identity for vault_path={vault_path!r} is dangling (no committed content); "
                "refusing to report absent so create() cannot manufacture a duplicate"
            )
        return _curated_from_payload(strip_layout_fields(resolved))  # raises on malformed -> fail closed

    async def find_by_vault_path(
        self, vault_path: str
    ) -> Result[CuratedKnowledge, FindByVaultPathError]:
        """VAULT-003: typed public method that resolves a curated row by its
        STORED ``vault_path``, no ``namespace`` argument.

        The watcher is the primary caller: when a file is deleted from the
        vault the frontmatter is gone, so the watcher has no namespace
        context. This method does a cross-namespace exact-match scroll
        (Qdrant ``MatchValue``, NOT ``startswith`` / ``prefix`` / regex).
        Sibling and prefix-collision paths cannot match by construction.

        Uniqueness invariant: ``(namespace, vault_path)`` is unique per
        slice-vault-sync. ``vault_path`` alone is NOT unique across
        namespaces — a duplicate ``vault_path`` in two namespaces is a
        schema bug, but the public API MUST fail closed on it: returning
        an arbitrary match would let the watcher's archive-by-path
        target the wrong row. This method:

        - Returns ``Err(FindByVaultPathError(code='not_found'))`` when
          no IDENTITY row matches (the watcher's clean observable no-op).
        - Returns ``Ok(row)`` when EXACTLY one identity matches AND
          resolves+validates.
        - Returns ``Err(FindByVaultPathError(code='multiple_matches'))``
          when more than one IDENTITY row matches — the caller MUST
          refuse to take destructive action.
        - Returns ``Err(FindByVaultPathError(code='invalid_row'))`` when
          exactly one identity matches but is DANGLING or MALFORMED — a
          broken-but-present row, NOT a clean absence (DATA-001 P2, Yua).

        The scroll excludes content shells (``must_not`` content) so
        cardinality is counted over DISTINCT IDENTITIES. A normal content
        snapshot carries no ``vault_path``, so this is the fail-closed
        defense against a corrupt/future shell that DID carry it inflating
        the count into a false ``multiple_matches`` or shadowing the real
        anchor. It is bounded to ``limit=2`` (Yua VAULT-003 review
        binding): fetching the second match is sufficient to fail
        closed, and a limit of 2 is the smallest unconstrained value
        that still surfaces the duplicate case without pulling a
        potentially unbounded row count. Zero identities -> not_found;
        one -> resolve (Ok / invalid_row); two -> multiple_matches.
        """
        from musubi.store.immutable_vectors import not_content_condition, resolve_committed_content

        if not isinstance(vault_path, str) or not vault_path:
            return Err(
                error=FindByVaultPathError(
                    code="not_found",
                    detail="empty or non-string vault_path",
                )
            )
        records, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="vault_path", match=models.MatchValue(value=vault_path)
                    ),
                ],
                must_not=not_content_condition(),  # count DISTINCT IDENTITIES, never content shells
            ),
            limit=2,
            with_payload=True,
            # with_payload=True still rehydrates the FULL CuratedKnowledge
            # payload (this is not a field-selective fetch of object_id/
            # state). The only optimization here is with_vectors=False: the
            # resolver never needs the dense+sparse embeddings, so we avoid
            # shipping them back on every vault delete.
            with_vectors=False,
        )
        if not records:
            return Err(
                error=FindByVaultPathError(
                    code="not_found",
                    detail=f"no curated row matches vault_path={vault_path!r}",
                )
            )
        if len(records) > 1:
            ids = tuple(str(rec.payload.get("object_id", "")) for rec in records if rec.payload)
            return Err(
                error=FindByVaultPathError(
                    code="multiple_matches",
                    detail=(
                        f"vault_path={vault_path!r} matched >=2 identity rows; "
                        "(namespace, vault_path) uniqueness invariant violated. "
                        "Fetches at most 2 rows because the second match is "
                        "sufficient to fail closed."
                    ),
                    match_count=len(records),
                    match_object_ids=ids,
                )
            )
        payload = records[0].payload
        if not payload:
            return Err(
                error=FindByVaultPathError(
                    code="invalid_row",  # present-but-broken, NOT a clean absence
                    detail=f"matched identity for vault_path={vault_path!r} has an empty payload",
                )
            )
        resolved = resolve_committed_content(
            self._client,
            self._collection,
            namespace=str(payload.get("namespace", "")),
            object_id=str(payload.get("object_id", "")),
        )
        if resolved is None:
            return Err(  # a dangling identity is present-but-broken — must not become clean not_found
                error=FindByVaultPathError(
                    code="invalid_row",
                    detail=(
                        f"identity for vault_path={vault_path!r} is dangling (no committed content); "
                        "the path is occupied by a broken row — the watcher must warn/refuse, not "
                        "treat it as absent"
                    ),
                    match_object_ids=(str(payload.get("object_id", "")),),
                )
            )
        row = _curated_safe(strip_layout_fields(resolved))
        if row is None:
            return Err(  # a malformed identity is present-but-broken, likewise never a clean not_found
                error=FindByVaultPathError(
                    code="invalid_row",
                    detail=(
                        f"identity for vault_path={vault_path!r} will not model-validate; "
                        "present-but-corrupt, the watcher must warn/refuse"
                    ),
                    match_object_ids=(str(payload.get("object_id", "")),),
                )
            )
        return Ok(value=row)

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

    async def get(self, *, namespace: Namespace, object_id: KSUID) -> CuratedKnowledge | None:
        """Fetch one curated row by id, scoped to ``namespace``.

        Wrong-namespace lookups return ``None`` — this is how the read
        path enforces namespace isolation.

        Raises if the stored payload does not satisfy ``CuratedKnowledge``. To ask
        only whether the row is present, call :meth:`exists` — it does not
        deserialize, so it still answers for a corrupted row.

        DATA-001 P2: resolves the AUTHORITATIVE committed payload (a v2 anchor merged over its
        ``live_point`` content, or a v1/legacy row) before validating — never a raw anchor/content
        shell — and fails closed (None) on a v2 anchor with a dangling/absent committed pointer.
        """
        from musubi.store.immutable_vectors import resolve_committed_content

        resolved = resolve_committed_content(
            self._client, self._collection, namespace=str(namespace), object_id=str(object_id)
        )
        if resolved is None:
            return None
        return _curated_from_payload(strip_layout_fields(resolved))

    async def patch_metadata(
        self, *, namespace: Namespace, object_id: KSUID, changes: dict[str, Any]
    ) -> CuratedKnowledge:
        """Apply a metadata-only PATCH (author frontmatter, NO body/vector change) to the identity row
        through the attributable Phase-1 mutation lease (DATA-001 P2, Yua): version-fenced owner-token,
        rebased on the FRESH row each round, one version bump, targets the identity row (v1 or v2 anchor
        via ``must_not content``), and composes with concurrent access/transition mutations — so a
        concurrent state/access change survives while the intended metadata lands. Returns the published
        row (stripped of layout keys + validated). Raises if the row vanished / lease contention exhausts."""
        payload_changes = {k: v for k, v in changes.items() if k != "version"}

        def plan(_current: dict[str, Any]) -> MutationPlan:
            # narrow metadata-only change-set; the mutation lease owns the version bump, and no vectors
            # change (a body/projection change goes through create() -> the immutable-vector seam).
            return MutationPlan(changes=dict(payload_changes))

        published = await owned_update(
            self._client,
            self._collection,
            namespace=str(namespace),
            object_id=str(object_id),
            point_id=_point_id(object_id),
            plan=plan,
        )
        return CuratedKnowledge.model_validate(strip_layout_fields(published))

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

        DATA-001 P2: the prefilter is IMMUTABLE-only (namespace + ``must_not`` anchor, the shared
        seam) — a v2 content point carries no ``state``/validity, so a state/bitemporal prefilter would
        drop the real vectors and surface zero-vector anchors. Each candidate is hydrated through its
        anchor (``resolve_ranked_candidate``); state AND the bitemporal window are applied POST-hydration
        on the authoritative payload; the scan is BOUNDED-overfetched to refill drops (anchors,
        superseded content, out-of-window/wrong-state rows) with a truthful underfill, never an unbounded
        retry; and a malformed authoritative payload fails closed (skips) rather than 500-ing the query.
        """
        at = valid_at if valid_at is not None else utc_now()
        at_epoch = epoch_of(at)
        dense = (await self._embedder.embed_dense([query]))[0]
        from musubi.store.immutable_vectors import (
            not_anchor_condition,
            ranked_overfetch,
            resolve_ranked_candidate,
        )

        resp = self._client.query_points(
            collection_name=self._collection,
            query=dense,
            using=DENSE_VECTOR_NAME,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                ],
                must_not=not_anchor_condition(),
            ),
            limit=ranked_overfetch(limit),
            with_payload=True,
        )
        out: list[CuratedKnowledge] = []
        for point in resp.points:  # score-descending; preserve candidate score order
            if len(out) >= limit:
                break
            resolved = resolve_ranked_candidate(
                self._client, self._collection, point_id=point.id, payload=dict(point.payload or {})
            )
            if resolved is None:
                continue  # anchor / superseded content / dangling-cross-object -> not a live candidate
            row = _curated_safe(strip_layout_fields(resolved))
            if row is None:
                continue  # malformed authoritative payload -> fail closed (skip from the ranked view)
            if not _curated_visible_at(row, at_epoch):
                continue  # POST-hydration state + bitemporal window on the TYPED row (never raw epochs)
            out.append(row)
        return out

    async def scan_vault_rows(self) -> list[CuratedKnowledge]:
        """Return a snapshot of all validated curated rows.
        Used by the vault reconciler to detect ghost rows.

        DATA-001 P2: the scroll excludes write-once CONTENT shells (``must_not`` content). Because this
        scan RESOLVES each row by ``object_id``, a v2 object's content points would each resolve back to
        its anchor and be counted again — excluding content both collapses that double-count and stops the
        1000-row page budget being burned on snapshots that are not identities. Each identity row is
        RESOLVED through its anchor before validating (``extra="forbid"`` rejects raw layout keys). This
        scan is FAIL-LOUD, the opposite of the ranked query (Yua / VaultReconciler contract): a dangling
        or malformed identity RAISES rather than being skipped — the reconciler needs a COMPLETE,
        trustworthy inventory, and a silently-dropped row would let ghost archival run against an
        incomplete picture."""
        from musubi.store.immutable_vectors import not_content_condition, resolve_committed_content

        out: list[CuratedKnowledge] = []
        offset = None
        while True:
            resp, offset = self._client.scroll(
                collection_name=self._collection,
                scroll_filter=models.Filter(must_not=not_content_condition()),
                limit=1000,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in resp:
                if point.payload is None:
                    raise ValueError("curated inventory row is missing its payload")
                resolved = resolve_committed_content(
                    self._client,
                    self._collection,
                    namespace=str(point.payload.get("namespace", "")),
                    object_id=str(point.payload.get("object_id", "")),
                )
                if resolved is None:
                    raise ValueError(  # dangling identity -> fail loud (never a silent gap in inventory)
                        "curated inventory identity "
                        f"{point.payload.get('object_id')!r} is dangling (no committed content)"
                    )
                row = _curated_from_payload(strip_layout_fields(resolved))  # raises on malformed
                if row.vault_path:
                    out.append(row)
            if offset is None:
                break
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
        coordinator: LifecycleTransitionCoordinator,
    ) -> Result[TransitionResult | TransitionPending, TransitionError]:
        """Delegate the namespace-scoped state change to the canonical coordinator."""
        current = await self.get(namespace=namespace, object_id=object_id)
        if current is None:
            return Err(
                error=TransitionError(
                    code="not_found",
                    message=(f"curated object {object_id!r} not found in namespace {namespace!r}"),
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
        dense = (await self._embedder.embed_dense([text]))[0]
        sparse = (await self._embedder.embed_sparse([text]))[0]
        return dense, sparse


__all__ = ["CuratedPlane"]
