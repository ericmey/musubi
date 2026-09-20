"""Canonical ``transition()`` — the only code path that mutates ``state``.

Behaviour (from [[04-data-model/lifecycle#Transition function]]):

1. Fetch the current object. We scan each plane collection until we find the
   point; this is intentionally simple — the lifecycle worker deals in
   thousands, not millions, of objects per tick.
2. Validate ``(current_state, target_state)`` against the legal-transition
   table in :mod:`musubi.types.lifecycle_event`.
3. Apply the transition: update ``state``, bump ``updated_at`` /
   ``updated_epoch``, increment ``version``.
4. Apply lineage updates (supersession, merge-in, etc.) and reject cycles.
5. Construct a :class:`LifecycleEvent`, hand it to the
   :class:`LifecycleEventSink` for durable persistence.
6. Return ``Ok(TransitionResult)`` or ``Err(TransitionError)``.

Invalid transitions never mutate the Qdrant payload — the error is surfaced
before any ``set_payload`` call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient, models

from musubi.lifecycle.coordinator import (
    LifecycleTransitionCoordinator,
    TransitionFinal,
    TransitionIntent,
    TransitionPending,
)
from musubi.lifecycle.events import LifecycleEventSink
from musubi.store.names import COLLECTION_NAMES
from musubi.store.specs import POINT_KIND_CONTENT, POINT_KIND_FIELD
from musubi.types.common import (
    KSUID,
    Err,
    LifecycleState,
    Ok,
    Result,
    epoch_of,
    utc_now,
)
from musubi.types.lifecycle_event import (
    LifecycleEvent,
    ObjectType,
    is_legal_transition,
    legal_next_states,
)

# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class LineageUpdates(BaseModel):
    """Optional lineage-field changes to apply during a transition.

    Only the fields provided here are mutated. None of these are validated
    against the target object's schema at construction time — the transition
    function re-validates through the pydantic model of the target type.
    """

    model_config = ConfigDict(extra="forbid")

    superseded_by: KSUID | None = None
    supersedes: list[KSUID] = Field(default_factory=list)
    merged_from: list[KSUID] = Field(default_factory=list)
    contradicts: list[KSUID] = Field(default_factory=list)
    promoted_to: KSUID | None = None
    promoted_at: datetime | None = None

    def to_payload_patch(self) -> dict[str, Any]:
        """Serialise as a dict suitable for ``set_payload``; empty keys omitted."""
        patch: dict[str, Any] = {}
        if self.superseded_by is not None:
            patch["superseded_by"] = self.superseded_by
        if self.supersedes:
            patch["supersedes"] = list(self.supersedes)
        if self.merged_from:
            patch["merged_from"] = list(self.merged_from)
        if self.contradicts:
            patch["contradicts"] = list(self.contradicts)
        if self.promoted_to is not None:
            patch["promoted_to"] = self.promoted_to
        if self.promoted_at is not None:
            patch["promoted_at"] = self.promoted_at.isoformat()
        return patch

    def to_event_changes(self) -> dict[str, Any]:
        """Serialise as the ``lineage_changes`` field on :class:`LifecycleEvent`."""
        return self.to_payload_patch()


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a successful :func:`transition`."""

    object_id: KSUID
    object_type: ObjectType
    from_state: LifecycleState
    to_state: LifecycleState
    version: int
    event: LifecycleEvent


@dataclass(frozen=True)
class TransitionError:
    """Typed error from a failed :func:`transition` call.

    ``code`` is one of:

    - ``not_found``            — no object with that id across any plane.
    - ``illegal_transition``   — ``(from_state, to_state)`` not in the table.
    - ``missing_reason``       — the ``reason`` argument was empty.
    - ``circular_supersession`` — supersession would create A → B → A.
    - ``invariant_violation``  — model validation failed on the updated payload.
    - ``lifecycle_event_write_failed`` — mutation committed, audit persistence refused.
    - ``version_fence_violation``     — expected_version did not match current_version.
    - ``ambiguous_object_id``  — the lookup could not identify ONE row, **even after any
      namespace the caller supplied**. That covers an unqualified id spanning
      namespaces, duplicate anchors within a single namespace, and the same id present
      in more than one plane — qualifying does not resolve the last two. Distinct from
      ``not_found``: the object EXISTS, and refusing is the safe answer because picking
      one would transition a stranger's row. Callers map this to a 4xx.

    **This list is not exhaustive.** ``transition()`` delegates to the coordinator,
    which owns its own admission codes — ``cap_exceeded``, ``active_intent_exists``,
    ``durable_begin_failed``, ``operation_key_conflict``, ``terminal_apply_failure``,
    ``maintenance_active`` — and that set belongs to the coordinator's contract, not
    this one. An earlier revision of this docstring claimed exhaustiveness while
    omitting all six: a completeness promise nothing checks is worse than no promise,
    because a caller can rely on it (Copilot, musubi#771).

    ``test_every_locally_constructed_error_code_is_documented`` derives the codes THIS module
    constructs from the source and requires each to appear above, so the list cannot
    silently fall behind the code again.
    """

    code: str
    message: str
    from_state: LifecycleState | None = None
    to_state: LifecycleState | None = None
    allowed: tuple[LifecycleState, ...] = field(default_factory=tuple)


# Mapping from Qdrant collection name to the canonical ObjectType string.
# Kept here (not in names.py) because the collection → object_type coupling
# is a lifecycle concern, not a storage-layout concern.
_COLLECTION_TO_OBJECT_TYPE: dict[str, ObjectType] = {
    "musubi_episodic": "episodic",
    "musubi_curated": "curated",
    "musubi_concept": "concept",
    "musubi_artifact": "artifact",
    "musubi_thought": "thought",
}


def transition(
    client: QdrantClient,
    *,
    coordinator: LifecycleTransitionCoordinator,
    object_id: KSUID,
    target_state: LifecycleState,
    actor: str,
    reason: str,
    lineage_updates: LineageUpdates | None = None,
    correlation_id: str = "",
    sink: LifecycleEventSink | None = None,
    expected_version: int | None = None,
    namespace: str | None,
) -> Result[TransitionResult | TransitionPending, TransitionError]:
    """Apply a state change to ``object_id``, recording an audit event.

    ``namespace`` is REQUIRED but nullable, and that shape is deliberate. It has no
    default, so every caller must decide: pass the namespace, or pass ``None`` to mean
    "I genuinely do not have one." Making it optional-with-a-default let thirteen call
    sites omit it silently and inherit cross-namespace resolution -- Copilot found three
    of those, one per review round; mypy names all thirteen in one pass once the default
    is gone (musubi#771).

    ``None`` is safe rather than lax: an unqualified lookup now refuses an ambiguous id
    instead of resolving to whichever row scrolls first.

    Lookup, legal-transition checks, lineage-cycle validation, and deterministic
    intent construction remain here. The required injected ``coordinator`` owns
    durable admission, version-fenced mutation, readback, event persistence, and
    reconciliation. ``sink`` remains as a source-compatible argument for callers
    being migrated in H5; it is deliberately not a second persistence path.
    """
    if not reason:
        return Err(
            error=TransitionError(
                code="missing_reason",
                message="`reason` argument must be a non-empty string",
                to_state=target_state,
            )
        )

    try:
        located = _locate_object(client, object_id=object_id, namespace=namespace)
    except AmbiguousObjectId as exc:
        # Refusing beats guessing: picking one of two namespaces would transition a
        # stranger's row and count it as this caller's (musubi#771).
        return Err(
            error=TransitionError(
                code="ambiguous_object_id",
                message=str(exc),
                to_state=target_state,
            )
        )
    if located is None:
        return Err(
            error=TransitionError(
                code="not_found",
                message=f"no object with object_id={object_id!r} in any plane",
                to_state=target_state,
            )
        )
    collection, payload = located
    object_type = _COLLECTION_TO_OBJECT_TYPE[collection]
    current_state: LifecycleState = payload.get("state", "provisional")
    current_version = int(payload.get("version", 1))

    # Concurrent-modification check runs BEFORE the legality check so the
    # hard-fence contract (LIFE-010) explicitly rejects the mutation
    # with version_fence_violation before checking state transitions,
    # rather than allowing LWW overwrites.
    if expected_version is not None and expected_version != current_version:
        return Err(
            error=TransitionError(
                code="version_fence_violation",
                message=(
                    f"concurrent transition on {object_id}: expected_version={expected_version}, "
                    f"current_version={current_version}"
                ),
                from_state=current_state,
                to_state=target_state,
            )
        )

    if not is_legal_transition(object_type, current_state, target_state):
        allowed = tuple(sorted(legal_next_states(object_type, current_state)))
        return Err(
            error=TransitionError(
                code="illegal_transition",
                message=(
                    f"{object_type}: {current_state} → {target_state} not permitted; "
                    f"allowed from {current_state}: {list(allowed)}"
                ),
                from_state=current_state,
                to_state=target_state,
                allowed=allowed,
            )
        )

    lineage_patch = lineage_updates.to_payload_patch() if lineage_updates else {}
    try:
        cycles = _would_cause_supersession_cycle(
            client,
            collection=collection,
            object_id=object_id,
            namespace=namespace,
            new_superseded_by=lineage_patch.get("superseded_by"),
        )
    except AmbiguousObjectId as exc:
        # The walk refuses rather than guessing, but an EXCEPTION escaping `transition()`
        # is a 500 -- the caller's contract is a typed `Err`, and an ambiguous lineage id
        # is a caller error, not a server fault. Mapping it here is what makes the
        # refusal reachable as a 4xx instead of a stack trace (Copilot, musubi#771).
        return Err(
            error=TransitionError(
                code="ambiguous_object_id",
                message=str(exc),
                from_state=current_state,
                to_state=target_state,
            )
        )
    if cycles:
        return Err(
            error=TransitionError(
                code="circular_supersession",
                message=(
                    f"transition rejected: object_id={object_id!r} -> "
                    f"superseded_by={lineage_patch.get('superseded_by')!r} "
                    f"would form a cycle"
                ),
                from_state=current_state,
                to_state=target_state,
            )
        )

    del sink
    now = utc_now()
    lineage = lineage_updates or LineageUpdates()
    intent = TransitionIntent(
        collection=collection,
        object_id=object_id,
        namespace=str(payload["namespace"]),
        expected_version=current_version if expected_version is None else expected_version,
        target_state=target_state,
        actor=actor,
        reason=reason,
        updated_at=now.isoformat(),
        updated_epoch=epoch_of(now),
        superseded_by=lineage.superseded_by,
        supersedes=tuple(lineage.supersedes),
        merged_from=tuple(lineage.merged_from),
        contradicts=tuple(lineage.contradicts),
        promoted_to=lineage.promoted_to,
        promoted_at=lineage.promoted_at.isoformat() if lineage.promoted_at is not None else None,
    )
    outcome = coordinator.transition(intent)
    if isinstance(outcome, Err):
        return Err(
            error=TransitionError(
                code=outcome.error.code,
                message=f"lifecycle transition failed: {outcome.error.code}",
                from_state=current_state,
                to_state=target_state,
            )
        )
    if isinstance(outcome.value, TransitionPending):
        return Ok(value=outcome.value)
    assert isinstance(outcome.value, TransitionFinal)
    event = LifecycleEvent(
        event_id=outcome.value.event_id,
        object_id=object_id,
        object_type=object_type,
        namespace=str(payload["namespace"]),
        from_state=current_state,
        to_state=target_state,
        actor=actor,
        reason=reason,
        lineage_changes=lineage.to_event_changes(),
        correlation_id=correlation_id,
    )
    return Ok(
        value=TransitionResult(
            object_id=object_id,
            object_type=object_type,
            from_state=current_state,
            to_state=target_state,
            version=current_version + 1,
            event=event,
        )
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_IDENTITY_PROBE_LIMIT = 2
"""Rows an identity lookup needs to decide. Two disproves uniqueness; no third helps."""


def _existing_collections(client: QdrantClient) -> set[str]:
    """The authoritative set of collection names this client actually has.

    Asking once is what removes the exception guessing. Keying "does this exist?" off an
    exception's type or text is a guess about a client library -- the local client raises
    `ValueError`, the remote raises a 404 -- and any guess that is wrong silently
    converts a real fault into a miss. Discovery failures propagate like any other
    (Yua, musubi#771)."""
    return {c.name for c in client.get_collections().collections}


class AmbiguousObjectId(Exception):
    """The lookup could not identify exactly ONE authoritative row.

    Not "two namespaces and no qualifier" -- that was the original, narrower reading and
    it is what let the other shapes through. Any non-singleton result raises, whatever
    the cause:

    - an unqualified id present under several namespaces
    - DUPLICATE ANCHORS within a single namespace, which a supplied qualifier does not
      resolve
    - the same id present in more than one plane, which no qualifier can resolve because
      both rows may carry the same namespace

    `object_id` is NOT globally unique (`tests/api/test_data001_episodic_patch_fence.py`
    relies on that). The lookup used `limit=1`, so a duplicate resolved to whichever row
    the scroll returned first -- silently transitioning a stranger's row and counting it
    as this caller's. A later revision counted DISTINCT NAMESPACES, which let two rows in
    one namespace collapse to a set of size one and pass. Identity is a count of rows,
    not a count of the values one of their fields happens to take
    (Copilot/Yua, musubi#771)."""


def _locate_object(
    client: QdrantClient, *, object_id: KSUID, namespace: str | None
) -> tuple[str, dict[str, Any]] | None:
    """Scan each plane collection for ``object_id``. Returns ``(collection, payload)``.

    ``namespace`` is REQUIRED but nullable -- no default. Making it optional-with-a-
    default is what created this whole class: fourteen callers silently inherited
    cross-namespace resolution, and a fifteenth sat in this very file (the lineage walk
    at the bottom), surviving a fix that only made `transition()` strict. A runtime
    refusal cannot be seen by the compiler; a missing argument can."""
    # EVERY collection is scanned, never "until the first hit". The early return made
    # this function unable to see a cross-PLANE duplicate at all: the same
    # (namespace, object_id) in two collections resolved to whichever plane happens to
    # sit first in `_COLLECTION_TO_OBJECT_TYPE` -- iteration order deciding which
    # object a transition lands on, silently (Copilot, musubi#771). Refusing requires
    # looking at all of them, so the loop must not stop early even though the common
    # case matches exactly one. That is a handful of scrolls against an indexed field,
    # paid on a lifecycle transition; the alternative is a wrong object.
    found: list[tuple[str, dict[str, Any]]] = []
    # Ask the server which collections exist, ONCE, instead of inferring it from whether
    # a scroll threw. That inference was the fail-open: `except Exception: return []`
    # turned a timeout into a confirmed miss, so a transient fault on one plane made its
    # duplicate invisible and the lookup resolved to another plane. Discovery failures
    # propagate like any other fault (Yua, musubi#771). It also keeps test clients that
    # hold only some collections working, without a broad catch to excuse them.
    present = _existing_collections(client)
    for collection in _COLLECTION_TO_OBJECT_TYPE:
        if collection not in present:
            continue
        records = _scroll_by_object_id(
            client, collection=collection, object_id=object_id, namespace=namespace
        )
        if not records:
            continue
        # EXACTLY ONE authoritative row, or refuse. This subsumes both failures and is
        # why the distinct-namespace comparison is gone: counting DISTINCT NAMESPACES
        # let two rows in the SAME namespace collapse to a set of size one and pass, so
        # the qualifier was doing the deciding instead of the identity. Whatever the
        # cause -- an unqualified id spanning namespaces, or two anchors within one --
        # the lookup cannot name the row, and `records[0]` is a guess either way
        # (Copilot/Yua, musubi#771).
        if len(records) > 1:
            spaces = sorted({str(r.get("namespace")) for r in records})
            raise AmbiguousObjectId(
                f"object_id={object_id!r} matched at least {len(records)} authoritative "
                f"rows in {collection}, namespaces {spaces}"
                + ("; qualify the namespace" if namespace is None else " (duplicate anchors)")
            )
        found.append((collection, records[0]))

    if len(found) > 1:
        # Qualifying the namespace does NOT disambiguate this one -- both rows can carry
        # the same namespace in different planes -- so there is no argument the caller
        # could have passed to make it safe. Refuse and name the collections.
        raise AmbiguousObjectId(
            f"object_id={object_id!r} exists in collections "
            f"{sorted(c for c, _ in found)}; a transition cannot choose between planes"
        )
    return found[0] if found else None


def _scroll_by_object_id(
    client: QdrantClient, *, collection: str, object_id: KSUID, namespace: str | None
) -> list[dict[str, Any]]:
    """Return EVERY authoritative identity payload for ``object_id`` in ``collection``.

    DATA-001 P2: excludes write-once CONTENT snapshots (``point_kind == "content"``) so a
    v2 object resolves to its anchor (full mutable state), never an arbitrary content
    shell. No-op for v1 rows and concept/thought/artifact (no content points).

    **At most two rows, because the caller's question needs no more.** The invariant is
    EXACTLY ONE authoritative row: nought or one proves at most one exists, and two
    disproves uniqueness whatever their namespaces. Nothing a third row could tell us
    changes the answer.

    An earlier revision of this paginated to exhaustion and called that load-bearing.
    It was not -- and worse, no test could distinguish it from `limit=2`, so it was an
    untestable stronger claim dressed as rigour (Tama, musubi#771). The reason `limit=2`
    was insufficient BEFORE is that the caller counted DISTINCT NAMESPACES, so two rows
    from one namespace collapsed to a set of size one and a third namespace stayed
    invisible. Fixing the invariant is what made the scan cheap; the limit was never
    the defect.

    **No exception handling.** Callers confirm the collection exists first, so every
    remaining failure -- timeout, unavailable node, transport fault -- PROPAGATES. This
    used to `except Exception: return []`, which made an error indistinguishable from a
    confirmed miss: the cross-plane refusal could be skipped on a transient fault and a
    hit from another plane transitioned instead. A lookup that FAILS OPEN on error is
    worse than the ambiguity it exists to prevent.
    """
    scroll_filter = models.Filter(
        must=[
            models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
            *(
                [models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace))]
                if namespace is not None
                else []
            ),
        ],
        must_not=[
            models.FieldCondition(
                key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT)
            )
        ],
    )
    records, _ = client.scroll(
        collection_name=collection,
        scroll_filter=scroll_filter,
        limit=_IDENTITY_PROBE_LIMIT,
        with_payload=True,
    )
    return [dict(rec.payload) for rec in records if rec.payload]


def _lookup_point_id(client: QdrantClient, *, collection: str, object_id: KSUID) -> str | int:
    """Find the Qdrant point id of the AUTHORITATIVE identity row for ``object_id``. Raises if missing.

    DATA-001 P2: excludes content snapshots so an admin lineage ``set_payload`` addresses the anchor
    (or v1 row), never a write-once content shell."""
    records, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))],
            must_not=[
                models.FieldCondition(
                    key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT)
                )
            ],
        ),
        limit=1,
        with_payload=False,
    )
    if not records:
        raise LookupError(f"point for object_id={object_id!r} missing in {collection!r}")
    pid = records[0].id
    # Qdrant accepts both int and str (UUID) point ids; pass through.
    if isinstance(pid, (int, str)):
        return pid
    raise TypeError(f"unexpected point id type: {type(pid)!r}")


def _would_cause_supersession_cycle(
    client: QdrantClient,
    *,
    collection: str,
    object_id: KSUID,
    new_superseded_by: KSUID | None,
    namespace: str | None,
) -> bool:
    """Return ``True`` iff setting ``object_id.superseded_by = new`` would cycle.

    Walks the supersession chain starting at ``new_superseded_by`` and fails
    if it ever reaches ``object_id``. Bounded to 64 hops to avoid pathological
    data on disk stalling a transition.
    """
    if new_superseded_by is None:
        return False
    if new_superseded_by == object_id:
        return True
    seen: set[str] = set()
    cursor: str | None = new_superseded_by
    for _ in range(64):
        if cursor is None:
            return False
        if cursor == object_id:
            return True
        if cursor in seen:
            return False  # pre-existing unrelated loop — not our problem
        seen.add(cursor)
        records = _scroll_by_object_id(
            client, collection=collection, object_id=cursor, namespace=namespace
        )
        if not records:
            return False
        # The ambiguity refusal has to travel the WHOLE walk, not just its first step.
        # `_locate_object` refuses an unqualified duplicate, then this loop followed the
        # supersession chain and took `records[0]` at every hop -- so a cycle check could
        # miss a cycle, or attach lineage, based on whichever row scrolled first
        # (Copilot, musubi#771). Same defect as the entry lookup, one level down, which
        # is why fixing only the cited line would have been the wrong repair.
        if len(records) > 1:
            spaces = sorted({str(r.get("namespace")) for r in records})
            raise AmbiguousObjectId(
                f"supersession chain id {cursor!r} matched at least {len(records)} "
                f"authoritative rows, namespaces {spaces}"
                + ("; qualify the namespace" if namespace is None else " (duplicate anchors)")
            )
        cursor = records[0].get("superseded_by")
    return False


# Suppress unused-import hint — ``COLLECTION_NAMES`` is exported via
# ``_COLLECTION_TO_OBJECT_TYPE`` key ordering; keep the import visible so a
# future maintainer knows where the canonical list lives.
assert set(_COLLECTION_TO_OBJECT_TYPE) <= set(COLLECTION_NAMES)


__all__ = [
    "LineageUpdates",
    "TransitionError",
    "TransitionResult",
    "transition",
]
