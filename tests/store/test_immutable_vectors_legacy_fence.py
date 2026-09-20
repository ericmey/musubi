"""The legacy evidence fence must EXTEND the base filter, never rebuild it.

Round 26 on musubi#732: `_legacy_fence_not_retracted` rebuilt the filter as
`Filter(must=[*base.must, _not_retracted()])` and silently dropped `must_not`. The two
branches lose different things, so they need different cells -- one cannot cover both.
"""

from __future__ import annotations

import asyncio
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.store.immutable_vectors import (
    _legacy_conversion_filter,
    _legacy_fence_not_retracted,
    patch_non_embedding_payload,
)
from musubi.store.names import collection_for_plane
from musubi.store.specs import POINT_KIND_CONTENT, POINT_KIND_FIELD
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory

_COLL = collection_for_plane("episodic")
_NS = "eric/claude-code/episodic"
_OID = "3JbLEGACYFENCEOBJECTID0000"


def _keys(arm: Any) -> list[str]:
    """Condition keys on one Filter arm, normalised.

    A qdrant Filter arm is `list[Condition] | Condition | None` -- a BARE condition is
    legal, not only a list. Iterating it directly would walk the wrong object for that
    case, which is the error mypy actually caught here rather than a typing nuisance.
    """
    if arm is None:
        return []
    conditions = arm if isinstance(arm, list) else [arm]
    return [str(getattr(c, "key", type(c).__name__)) for c in conditions]


def test_nonzero_version_branch_keeps_the_point_kind_exclusion() -> None:
    """obs_version != 0: the version fence lives in `must` and survives a rebuild.

    What a rebuild loses here is the point_kind exclusion, which is what makes an orphan
    content snapshot or a stray anchor writable -- and convertible into an anchor.
    """
    base = _legacy_conversion_filter(_NS, _OID, 3)
    fenced = _legacy_fence_not_retracted(_NS, _OID, 3)

    assert _keys(base.must_not) == ["point_kind"], _keys(base.must_not)
    assert _keys(fenced.must_not) == _keys(base.must_not), (
        "the point_kind exclusion was dropped; an orphan content/anchor row becomes writable"
    )
    # and the predicate really was added
    assert len(_keys(fenced.must)) == len(_keys(base.must)) + 1


def test_zero_version_branch_keeps_the_version_fence_which_lives_in_must_not() -> None:
    """obs_version == 0: the ENTIRE version fence is the `must_not` clause `version > 0`.

    Nothing is appended to `must` on this branch, so a rebuild that forwards only `must`
    degrades the filter to object_id + namespace + not_retracted. A concurrent Phase-1
    bump then matches where it used to match zero and force a retry -- strictly worse
    than having no evidence predicate at all.
    """
    base = _legacy_conversion_filter(_NS, _OID, 0)
    fenced = _legacy_fence_not_retracted(_NS, _OID, 0)

    # the premise this cell rests on, asserted rather than assumed
    assert "version" not in _keys(base.must), (
        "premise moved: the zero-version branch now fences version in `must`, "
        "so this cell no longer discriminates"
    )
    assert "version" in _keys(base.must_not), _keys(base.must_not)

    assert "version" in _keys(fenced.must_not), (
        "the version fence was dropped on the zero-version branch; a concurrent "
        "Phase-1 bump now matches instead of forcing a retry"
    )
    assert _keys(fenced.must_not) == _keys(base.must_not)


def test_every_base_arm_survives_on_both_branches() -> None:
    """The general property, so a future arm added to the base cannot be silently lost."""
    for obs_version in (0, 3):
        base = _legacy_conversion_filter(_NS, _OID, obs_version)
        fenced = _legacy_fence_not_retracted(_NS, _OID, obs_version)
        for arm in ("must_not", "should", "min_should"):
            assert getattr(fenced, arm) == getattr(base, arm), (
                f"arm {arm!r} diverged at obs_version={obs_version}"
            )


def _seed_row(client: Any) -> tuple[str, str]:
    ns = f"prim-{generate_ksuid()[:8].lower()}/dev/episodic"
    row = asyncio.run(
        EpisodicPlane(client=client, embedder=FakeEmbedder()).create(
            EpisodicMemory(namespace=ns, content="pre-release receipt invariant", state="matured")
        )
    )
    return ns, row.object_id


def _identity(client: Any, oid: str) -> dict[str, Any]:
    recs, _ = client.scroll(
        collection_name=_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))],
            must_not=[
                models.FieldCondition(
                    key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT)
                )
            ],
        ),
        limit=1,
        with_payload=True,
    )
    return dict(recs[0].payload or {}) if recs else {}


def _identity_point_id(client: Any, oid: str) -> Any:
    recs, _ = client.scroll(
        collection_name=_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))],
            must_not=[
                models.FieldCondition(
                    key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT)
                )
            ],
        ),
        limit=1,
    )
    return recs[0].id


def test_publish_primitive_answers_from_the_pre_release_snapshot() -> None:
    """The receipt must be the state this CAS produced, not a post-release reread.

    `_publish_non_embedding_payload` releases its token and then re-reads to confirm the
    deletion. Returning that read makes a concurrent PATCH landing in the gap into THIS
    request's receipt. Round 28 named the retraction repair path; the defect is in the
    primitive, so `patch_non_embedding_payload` had it too -- this cell drives the
    ORDINARY patch caller on purpose, because a cell aimed only at the retraction path
    would pass against a fix aimed only at the retraction path.
    """
    client = QdrantClient(":memory:")
    try:
        bootstrap(client)
        ns, oid = _seed_row(client)
        observed = _identity(client, oid)
        base_version = int(observed["version"])
        intruder_version = base_version + 9
        point_id = _identity_point_id(client, oid)
        real_delete = client.delete_payload
        fired: list[bool] = []

        def delete_then_intrude(*args: Any, **kwargs: Any) -> Any:
            outcome = real_delete(*args, **kwargs)
            if not fired:
                fired.append(True)
                client.set_payload(
                    collection_name=_COLL,
                    payload={"version": intruder_version},
                    points=[point_id],
                    wait=True,
                )
            return outcome

        client.delete_payload = delete_then_intrude  # type: ignore[method-assign]
        try:
            published = patch_non_embedding_payload(
                client,
                _COLL,
                namespace=ns,
                object_id=oid,
                observed_payload=observed,
                changes={"tags": ["patched"]},
                tag_mode="replace",
            )
        finally:
            client.delete_payload = real_delete  # type: ignore[method-assign]

        assert fired, "the intruder never landed; this cell proves nothing"
        assert _identity(client, oid)["version"] == intruder_version, (
            "setup broken: the intruder write did not stick"
        )
        assert published["version"] != intruder_version, (
            "the receipt carried a concurrent writer's version instead of this CAS's"
        )
        assert published["version"] == base_version + 1
        assert "update_lease_token" not in published
    finally:
        client.close()
