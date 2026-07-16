"""The raw-lookup layer and the direct SDK delete path.

These cover the code that exists so that **a memory can always be removed, no matter how
badly its payload is broken.** Router tests do not protect direct SDK callers, and the
raw helpers had two reachability holes that would each have recreated the original defect
(undeletable-because-broken):

1. ``raw_payload()`` returned ``None`` for both "point absent" and "point exists with an
   empty payload", so ``delete()`` raised ``LookupError`` on the second case and refused
   to remove it.
2. Both helpers locate a row by its ``namespace`` / ``object_id`` **payload fields** — so
   a row that has lost those very keys is invisible to them, even though its Qdrant point
   ID is derivable from the object_id. Deletion now addresses the point directly.

Both found by Yua in rev2 review of PR #398.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic import EpisodicPlane
from musubi.planes.episodic.plane import episodic_point_id
from musubi.store import bootstrap
from musubi.store.raw_lookup import point_exists, raw_payload, retrieve_by_point_id
from musubi.types.episodic import EpisodicMemory

NS = "eric/claude-code/episodic"
COLLECTION = "musubi_episodic"


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def episodic(qdrant: QdrantClient) -> EpisodicPlane:
    return EpisodicPlane(client=qdrant, embedder=FakeEmbedder())


async def _seed(episodic: EpisodicPlane, content: str) -> str:
    saved = await episodic.create(EpisodicMemory(namespace=NS, content=content))
    return str(saved.object_id)


# ---------------------------------------------------------------------------
# raw_lookup contract
# ---------------------------------------------------------------------------


async def test_raw_payload_distinguishes_absent_from_empty(
    episodic: EpisodicPlane, qdrant: QdrantClient
) -> None:
    """`None` means ABSENT. `{}` means present-but-empty. Conflating them made an
    empty-payload row undeletable, because delete() read `None` as not-found."""
    oid = await _seed(episodic, "raw-payload-contract")

    assert raw_payload(qdrant, COLLECTION, namespace=NS, object_id=oid) is not None
    assert (
        raw_payload(qdrant, COLLECTION, namespace=NS, object_id="3GnotarealKSUIDxxxxxxxxxxxx")
        is None
    )

    # Strip the payload to nothing — the point still EXISTS.
    qdrant.clear_payload(
        collection_name=COLLECTION, points_selector=[episodic_point_id(oid)], wait=True
    )
    got = retrieve_by_point_id(qdrant, COLLECTION, point_id=episodic_point_id(oid))
    assert got == {}, "an existing point with an empty payload must be {} — never None"
    assert got is not None, "an existing point must never read as absent"


async def test_point_exists_is_namespace_isolated(
    episodic: EpisodicPlane, qdrant: QdrantClient
) -> None:
    oid = await _seed(episodic, "namespace-isolation")
    assert point_exists(qdrant, COLLECTION, namespace=NS, object_id=oid)
    assert not point_exists(
        qdrant, COLLECTION, namespace="someone-else/elsewhere/episodic", object_id=oid
    )


async def test_presence_and_raw_payload_ignore_orphan_content_shell(qdrant: QdrantClient) -> None:
    """DATA-001 P2: presence + inspection answer from the IDENTITY row (v2 anchor or v1), NEVER a
    write-once content snapshot. An orphan content shell left after a missing/deleted anchor must not
    report the object present (else an existence guard keeps a half-deleted object alive); once a real
    anchor exists, both see it."""
    import uuid

    from qdrant_client import models

    from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME

    oid = "orphan-shell-oid"
    dense = (await FakeEmbedder().embed_dense(["x"]))[0]
    qdrant.upsert(
        collection_name=COLLECTION,
        points=[
            models.PointStruct(
                id=str(uuid.uuid4()),
                payload={
                    "namespace": NS,
                    "object_id": oid,
                    "point_kind": "content",
                    "content": "x",
                },
                vector={
                    DENSE_VECTOR_NAME: dense,
                    SPARSE_VECTOR_NAME: models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )
    assert point_exists(qdrant, COLLECTION, namespace=NS, object_id=oid) is False, (
        "an orphan content shell must not report the object as present"
    )
    assert raw_payload(qdrant, COLLECTION, namespace=NS, object_id=oid) is None

    # once a real anchor identity row exists, presence + inspection see it (and return the anchor).
    qdrant.upsert(
        collection_name=COLLECTION,
        points=[
            models.PointStruct(
                id=str(uuid.uuid4()),
                payload={
                    "namespace": NS,
                    "object_id": oid,
                    "point_kind": "anchor",
                    "state": "matured",
                    "content": "x",
                    "live_point": "cp",
                },
                vector={
                    DENSE_VECTOR_NAME: [0.0] * len(dense),
                    SPARSE_VECTOR_NAME: models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )
    assert point_exists(qdrant, COLLECTION, namespace=NS, object_id=oid) is True
    rp = raw_payload(qdrant, COLLECTION, namespace=NS, object_id=oid)
    assert rp is not None and rp.get("point_kind") == "anchor", (
        "raw_payload must return the authoritative anchor identity row, not the content shell"
    )


async def test_retrieve_by_point_id_finds_a_row_whose_identifiers_are_gone(
    episodic: EpisodicPlane, qdrant: QdrantClient
) -> None:
    """The lookup of last resort.

    A row that has lost its `object_id`/`namespace` payload keys is invisible to every
    payload-filtered query — but the point ID is derived deterministically from the
    object_id, so it can still be addressed. If this did not work, such a row could never
    be deleted: unreachable *because* corrupted, which is the whole defect.
    """
    oid = await _seed(episodic, "identifiers-will-be-stripped")
    pid = episodic_point_id(oid)

    qdrant.clear_payload(collection_name=COLLECTION, points_selector=[pid], wait=True)

    # Payload-filtered lookups can no longer see it...
    assert not point_exists(qdrant, COLLECTION, namespace=NS, object_id=oid)
    assert raw_payload(qdrant, COLLECTION, namespace=NS, object_id=oid) is None
    # ...but the point is still addressable, and therefore still removable.
    assert retrieve_by_point_id(qdrant, COLLECTION, point_id=pid) == {}


# ---------------------------------------------------------------------------
# Direct SDK delete — the path router tests do not protect
# ---------------------------------------------------------------------------


async def test_sdk_delete_removes_a_corrupted_row(
    episodic: EpisodicPlane, qdrant: QdrantClient
) -> None:
    """`EpisodicPlane.delete()` used to call the deserializing `self.get()`, so a row with
    an unmodeled payload key could not be deleted through the SDK at all."""
    oid = await _seed(episodic, "sdk-delete-corrupted")
    qdrant.set_payload(
        collection_name=COLLECTION,
        payload={"retracted_original": "an unmodeled key the read model forbids"},
        points=[episodic_point_id(oid)],
        wait=True,
    )
    # Precondition: unreadable, and unreadable for the REASON we think. A bare
    # `pytest.raises(Exception)` would pass on a typo, a missing collection, or a connection
    # error — proving the row is broken without proving it is broken by `extra_forbidden`,
    # which is the entire premise of the regression.
    with pytest.raises(ValidationError) as exc:
        await episodic.get(namespace=NS, object_id=oid)
    assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    event = await episodic.delete(
        namespace=NS, object_id=oid, actor="test", reason="regression", is_operator=True
    )
    assert event.to_state == "archived"
    assert not point_exists(qdrant, COLLECTION, namespace=NS, object_id=oid)


async def test_sdk_delete_normalizes_an_unreadable_prior_state(
    episodic: EpisodicPlane, qdrant: QdrantClient
) -> None:
    """A corrupted row may carry a `state` that is not a LifecycleState at all.

    `model_construct` skips validation, so passing it through raw would emit an audit
    record that violates LifecycleEvent's own declared contract. We normalize to the
    weakest honest claim and preserve the truth in `reason` — rather than fabricating a
    state that merely looks valid. (Yua, rev2 review.)
    """
    oid = await _seed(episodic, "garbage-state")
    qdrant.set_payload(
        collection_name=COLLECTION,
        payload={"state": "not-a-real-state"},
        points=[episodic_point_id(oid)],
        wait=True,
    )

    event = await episodic.delete(
        namespace=NS, object_id=oid, actor="test", reason="regression", is_operator=True
    )
    assert event.from_state == "provisional", "must normalize to a declared LifecycleState"
    assert "not-a-real-state" in event.reason, "the truth must survive in the audit trail"
    assert not point_exists(qdrant, COLLECTION, namespace=NS, object_id=oid)


async def test_sdk_delete_still_refuses_a_wrong_namespace(
    episodic: EpisodicPlane, qdrant: QdrantClient
) -> None:
    """Direct point addressing must not become a way around namespace isolation — when
    the payload can still tell us the namespace, it is enforced."""
    oid = await _seed(episodic, "isolation-holds")
    with pytest.raises(LookupError):
        await episodic.delete(
            namespace="someone-else/elsewhere/episodic",
            object_id=oid,
            actor="test",
            reason="regression",
            is_operator=True,
        )
    assert point_exists(qdrant, COLLECTION, namespace=NS, object_id=oid)


async def test_sdk_delete_requires_operator(episodic: EpisodicPlane) -> None:
    oid = await _seed(episodic, "operator-required")
    with pytest.raises(PermissionError):
        await episodic.delete(
            namespace=NS, object_id=oid, actor="test", reason="regression", is_operator=False
        )


async def test_sdk_delete_removes_a_row_whose_namespace_is_CORRUPT(
    episodic: EpisodicPlane, qdrant: QdrantClient
) -> None:
    """A malformed (not merely missing) namespace must not block deletion.

    The isolation guard read:

        stored_ns = payload.get("namespace")
        if stored_ns is not None and stored_ns != namespace:
            raise LookupError(...)

    A row whose `namespace` key is corrupted to a list/int/dict is `not None` AND
    `!= namespace` — so it raised LookupError and became **undeletable because it was
    corrupted.** That is the precise defect this entire PR exists to kill, recreated inside
    the fix for it.

    Missing namespace was handled (None → skip the check). Malformed was not. The guard
    must enforce isolation only when the payload can *reliably state* a namespace — i.e.
    when it is a string. Anything else is corruption, and corruption must be removable.
    Operator scope already gates this path.

    Found by the Copilot reviewer on PR #398 — whose five reviews I had not read.
    """
    # EVERY shape of corruption, not just the ones an example happened to name.
    #
    # The first fix was `isinstance(stored_ns, str)` — which repaired list/int/dict damage
    # and left STRING damage untouched. `""`, `"garbage"`, `"WRONG/Case/episodic"`, a bad
    # plane: all strings, all still tripped the mismatch guard, all still undeletable
    # because corrupted.
    #
    # I implemented the EXAMPLES the reviewer named instead of the CLASS it named. That is a
    # denylist of remembered mistakes — the precise unsound pattern this entire PR is about,
    # committed inside the fix for the fix. (Yua, review of 760f222.)
    #
    # The canonical contract is `validate_namespace` (tenant/presence/plane). A stored value
    # that does not satisfy it is not a namespace — it is damage, and damage is removable.
    for bad_ns in (
        ["a", "b"],  # non-string
        12345,  # non-string
        {"ns": "x"},  # non-string
        "",  # string, empty
        "garbage",  # string, no structure
        "eric/claude-code",  # string, missing the plane component
        "eric/claude-code/not-a-plane",  # string, invalid plane
        "Eric/Claude-Code/episodic",  # string, invalid casing
    ):
        oid = await _seed(episodic, f"malformed-ns-{bad_ns!r}")
        qdrant.set_payload(
            collection_name=COLLECTION,
            payload={"namespace": bad_ns},
            points=[episodic_point_id(oid)],
            wait=True,
        )
        event = await episodic.delete(
            namespace=NS, object_id=oid, actor="test", reason="regression", is_operator=True
        )
        assert event.to_state == "archived"
        assert retrieve_by_point_id(qdrant, COLLECTION, point_id=episodic_point_id(oid)) is None, (
            f"a row with a malformed namespace ({bad_ns!r}) must still be removable"
        )
