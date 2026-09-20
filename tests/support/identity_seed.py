"""Production-shaped identity seeding for tests that used to seed via `publish()`.

`ImmutableVectorPublisher` is an UPDATE/REINFORCE path. Measured on musubi#732: its only
production callers are `EpisodicPlane._reinforce` -> `reinforce_publish` and
`CuratedPlane` -> `curated_publish`. Bare `publish()` has **no production caller at
all** -- `EpisodicPlane.create` uses the plane's own `_upsert`.

Round 29 removed the branch that let `publish()` create an anchor for an absent object,
because an anchor this path creates is an anchor it can resurrect after a retraction.
Five test files were leaning on that create as a seeding convenience, which means they
were exercising a code path nothing ships.

This seeds the way production does: a v1 legacy identity through the plane's own create,
which the publisher then converts on its first write -- the migration path that IS real.

**The seeded state is deliberately NOT the old one.** `publish()` produced a v2 anchor
directly; this produces a v1 row that becomes a v2 anchor through the publisher's own
conversion. `tests/store/test_identity_seed_equivalence.py` asserts where the two
converge and names where they do not, so the difference is a recorded decision rather
than fifteen tests that happen to pass (Aoi's condition, 2026-09-20).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic import EpisodicPlane
from musubi.types.episodic import EpisodicMemory

#: Deterministic, VALID 27-char base62 KSUIDs for fixtures that need a stable id.
#: `EpisodicMemory` rejects anything else, so literals like ``"payload-only"`` name an
#: object production could never create -- unreal state of the same family as the
#: unshipped create path, and fixed in the same pass (musubi#732 round 29).
FIXTURE_KSUIDS = {
    "payload-only": "2ZrFhTgQJmVqKxLnBdWcYpSvE4t",
    "vector-change": "2ZrFhTgQJmVqKxLnBdWcYpSvE5u",
}


def seed_legacy_identity(
    client: Any,
    *,
    namespace: str,
    object_id: str,
    content: str = "seed body",
    summary: str | None = "s",
    tags: list[str] | None = None,
    embedder: Any | None = None,
) -> EpisodicMemory:
    """Create ONE v1 legacy identity row the way `EpisodicPlane.create` does.

    Asserts the identity exists before returning, so a seeding failure is loud here
    rather than surfacing as an unrelated assertion three lines into the caller.
    """
    plane = EpisodicPlane(client=client, embedder=embedder or FakeEmbedder())
    asyncio.run(
        plane.create(
            EpisodicMemory(
                namespace=namespace,
                object_id=object_id,
                content=content,
                summary=summary,
                tags=list(tags or []),
                state="matured",
            )
        )
    )
    stored = asyncio.run(plane.get(namespace=namespace, object_id=object_id))
    assert stored is not None, (
        f"seed_legacy_identity did not create ({namespace!r}, {object_id!r}); "
        "every caller depends on this row existing before it publishes"
    )
    return stored


def seed_v2_identity_via_migration(
    client: Any,
    coordinator: Any,
    publisher: Any,
    *,
    namespace: str,
    object_id: str,
    content: str = "seed body",
    tags: list[str] | None = None,
    embedder: Any | None = None,
) -> dict[str, Any]:
    """Seed a v1 identity, then reach the v2 anchor THROUGH THE PRODUCTION MIGRATION PATH.

    For any cell that asserts on the anchor envelope or layout. It deliberately does NOT
    construct the anchor+content pair with a raw upsert: that would fix "tests depend on
    a create path nothing ships" by introducing "tests depend on a fixture shape nothing
    ships" -- the same defect in a new location, chosen on purpose this time (Aoi's
    condition, 2026-09-20).

    **`summary` is forced absent on the seed, and that is load-bearing.** The episodic
    embedding projection is ``summary or content`` (`_projection`, immutable_vectors.py),
    so a seed carrying a summary makes a content-only reinforce project IDENTICAL text,
    `vector_changed` stays False, the publish takes the payload-only branch, and no
    conversion happens. Diagnosed the hard way: the migration path looked broken when the
    fixture was the thing at fault.

    Asserts the resulting topology before returning, so a silent non-conversion fails
    here instead of as an unrelated anchor assertion inside the caller.
    """
    from qdrant_client import models as _m

    seed_legacy_identity(
        client,
        namespace=namespace,
        object_id=object_id,
        content=content,
        summary=None,
        tags=tags,
        embedder=embedder,
    )
    committed = asyncio.run(
        publisher.reinforce_publish(
            coordinator,
            object_id=object_id,
            namespace=namespace,
            new_memory={
                "content": f"{content} -- migrated body that moves the embedding projection",
                "tags": list(tags or []),
            },
            merge_strategy="longer-wins",
        )
    )
    rows, _ = client.scroll(
        collection_name=publisher._collection,
        scroll_filter=_m.Filter(
            must=[_m.FieldCondition(key="object_id", match=_m.MatchValue(value=object_id))]
        ),
        limit=8,
        with_payload=True,
    )
    kinds = sorted(str((r.payload or {}).get("point_kind")) for r in rows)
    assert kinds == ["anchor", "content"], (
        f"migration did not yield the v2 topology for ({namespace!r}, {object_id!r}): "
        f"got {kinds}. If the projection did not move, the reinforce takes the "
        "payload-only branch and no conversion happens."
    )
    return dict(committed) if isinstance(committed, dict) else {}


def seed_curated_v2_identity_via_migration(
    client: Any,
    coordinator: Any,
    publisher: Any,
    *,
    namespace: str,
    object_id: str,
    title: str = "seed title",
    content: str = "seed body",
    embedder: Any | None = None,
) -> dict[str, Any]:
    """Curated sibling of :func:`seed_v2_identity_via_migration`.

    A curated row is a genuinely different object, so it gets its own seeder rather than
    a flag on the episodic one -- per-layout variation is allowed exactly when the cell is
    about a different thing (Aoi, 2026-09-20).

    **The curated projection is ``title\\n\\nsummary-or-content``** (`_projection`), so the
    migration write moves the TITLE. Changing only the body would leave the projection
    identical for a row that carries a summary, take the payload-only branch, and never
    convert -- the same trap the episodic helper documents, one field over.

    Asserts the resulting topology before returning.
    """
    from qdrant_client import models as _m

    from musubi.planes.curated import CuratedPlane
    from musubi.types.curated import CuratedKnowledge

    plane = CuratedPlane(client=client, embedder=embedder or FakeEmbedder())
    asyncio.run(
        plane.create(
            CuratedKnowledge(
                namespace=namespace,
                object_id=object_id,
                title=title,
                content=content,
                vault_path=f"seed/{object_id}.md",
                # sha256 of the body, exactly what the vault watcher computes
                body_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                topics=["projects/musubi"],
            )
        )
    )
    committed = publisher.curated_publish(
        coordinator,
        object_id=object_id,
        namespace=namespace,
        set_fields={"title": f"{title} -- migrated", "content": content},
    )
    rows, _ = client.scroll(
        collection_name=publisher._collection,
        scroll_filter=_m.Filter(
            must=[_m.FieldCondition(key="object_id", match=_m.MatchValue(value=object_id))]
        ),
        limit=8,
        with_payload=True,
    )
    kinds = sorted(str((r.payload or {}).get("point_kind")) for r in rows)
    assert kinds == ["anchor", "content"], (
        f"curated migration did not yield the v2 topology for ({namespace!r}, {object_id!r}): "
        f"got {kinds}. The curated projection is title + summary-or-content; if the title "
        "did not move, the publish takes the payload-only branch."
    )
    return dict(committed) if isinstance(committed, dict) else {}
