from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient

from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.types.common import generate_ksuid
from musubi.vault.frontmatter import (
    CuratedFrontmatter,
    curated_knowledge_from_frontmatter,
    parse_frontmatter,
)


def test_vault004_fidelity_round_trip() -> None:
    fid = generate_ksuid()
    sid = generate_ksuid()
    cid = generate_ksuid()
    aid = generate_ksuid()
    promoted_ksuid = generate_ksuid()

    yaml_text = f"""---
object_id: {fid}
namespace: aoi/knowledge/curated
title: "The Binding Contract"
summary: "Full fidelity proof"
topics:
  - architecture
  - decisions
tags:
  - binding
importance: 10
state: matured
version: 3
musubi-managed: false
created: 2026-07-01T00:00:00Z
updated: 2026-07-15T00:00:00Z
valid_from: 2026-07-02T00:00:00Z
valid_until: 2027-01-01T00:00:00Z
supersedes:
  - {sid}
superseded_by: {sid}
merged_from:
  - {fid}
promoted_from: {promoted_ksuid}
promoted_at: 2026-07-10T00:00:00Z
supported_by:
  - artifact_id: {aid}
    quote: "Explicitly backed"
linked_to_topics:
  - testing
contradicts:
  - {cid}
---

Body text."""

    data, body = parse_frontmatter(yaml_text)
    fm = CuratedFrontmatter.model_validate(data)

    vault_path = "test/path.md"
    body_hash = "a" * 64
    memory = curated_knowledge_from_frontmatter(
        fm, vault_path=vault_path, body_hash=body_hash, content=body
    )

    assert memory.object_id == fid
    assert memory.namespace == "aoi/knowledge/curated"
    assert memory.title == "The Binding Contract"
    assert memory.summary == "Full fidelity proof"
    assert memory.topics == ["architecture", "decisions"]
    assert memory.tags == ["binding"]
    assert memory.importance == 10
    assert memory.state == "matured"
    assert memory.version == 3
    assert memory.musubi_managed is False
    assert memory.created_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert memory.updated_at == datetime(2026, 7, 15, tzinfo=UTC)
    assert memory.valid_from == datetime(2026, 7, 2, tzinfo=UTC)
    assert memory.valid_until == datetime(2027, 1, 1, tzinfo=UTC)
    assert memory.supersedes == [sid]
    assert memory.superseded_by == sid
    assert memory.merged_from == [fid]
    assert memory.promoted_from == promoted_ksuid
    assert memory.promoted_at == datetime(2026, 7, 10, tzinfo=UTC)
    assert len(memory.supported_by) == 1
    assert memory.supported_by[0].artifact_id == aid
    assert memory.supported_by[0].quote == "Explicitly backed"
    assert memory.linked_to_topics == ["testing"]
    assert memory.contradicts == [cid]

    assert memory.vault_path == vault_path
    assert memory.body_hash == body_hash
    assert memory.content == "Body text."


def test_vault004_visible_failure_on_unsupported_fields() -> None:
    fid = generate_ksuid()
    yaml_text = f"""---
object_id: {fid}
namespace: aoi/knowledge/curated
title: "Unsupported"
created: 2026-07-01T00:00:00Z
updated: 2026-07-15T00:00:00Z
read_by:
  - eric
---
Body."""
    data, body = parse_frontmatter(yaml_text)
    fm = CuratedFrontmatter.model_validate(data)

    with pytest.raises(ValueError, match="read_by is unsupported on CuratedKnowledge"):
        curated_knowledge_from_frontmatter(fm, vault_path="x", body_hash="a" * 64, content=body)


def test_vault004_visible_failure_on_unknown_extra_fields() -> None:
    fid = generate_ksuid()
    yaml_text = f"""---
object_id: {fid}
namespace: aoi/knowledge/curated
title: "Unsupported Extra"
created: 2026-07-01T00:00:00Z
updated: 2026-07-15T00:00:00Z
made_up_field_1: true
made_up_field_2: false
---
Body."""
    data, body = parse_frontmatter(yaml_text)
    fm = CuratedFrontmatter.model_validate(data)

    with pytest.raises(ValueError, match="Unsupported frontmatter fields detected"):
        curated_knowledge_from_frontmatter(fm, vault_path="x", body_hash="a" * 64, content=body)


@pytest.mark.asyncio
async def test_vault004_operational_reconciler_call(tmp_path: Path) -> None:

    from musubi.types.curated import CuratedKnowledge
    from musubi.vault.reconciler import VaultReconciler

    class DummyCuratedPlane:
        def __init__(self) -> None:
            self.created_memory: CuratedKnowledge | None = None

        async def create(self, memory: CuratedKnowledge) -> None:
            self.created_memory = memory

        def scan_vault_rows(self) -> Any:
            class DummyScanner:
                async def filter(self, _: Any) -> list[Any]:
                    return []

            return DummyScanner()

    qdrant = QdrantClient(":memory:")
    coordinator = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    reconciler = VaultReconciler(
        vault_root=tmp_path,
        curated_plane=DummyCuratedPlane(),  # type: ignore
        coordinator=coordinator,
    )

    fid = generate_ksuid()
    md_path = tmp_path / "test.md"
    yaml_text = f"""---
object_id: {fid}
namespace: aoi/knowledge/curated
title: "Op Test"
created: 2026-07-01T00:00:00Z
updated: 2026-07-15T00:00:00Z
musubi-managed: false
---
Body."""
    md_path.write_text(yaml_text)

    res = await reconciler._reconcile_file(md_path)
    assert res == "upserted"

    created = getattr(reconciler.curated_plane, "created_memory")
    assert created is not None
    assert created.object_id == fid
    assert created.title == "Op Test"
    assert created.musubi_managed is False
    assert created.content == "Body."
    qdrant.close()


@pytest.mark.asyncio
async def test_vault004_operational_watcher_call(tmp_path: Path) -> None:
    from musubi.types.curated import CuratedKnowledge
    from musubi.vault.watcher import VaultWatcher

    class DummyCuratedPlane:
        def __init__(self) -> None:
            self.created_memory: CuratedKnowledge | None = None

        async def create(self, memory: CuratedKnowledge) -> None:
            self.created_memory = memory

    qdrant = QdrantClient(":memory:")
    coordinator = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    watcher = VaultWatcher(
        vault_root=tmp_path,
        curated_plane=DummyCuratedPlane(),  # type: ignore
        write_log=type("WL", (), {"consume_if_exists": lambda self, a, b: False})(),
        coordinator=coordinator,
    )

    fid = generate_ksuid()
    md_path = tmp_path / "test.md"
    yaml_text = f"""---
object_id: {fid}
namespace: aoi/knowledge/curated
title: "Op Watcher"
created: 2026-07-01T00:00:00Z
updated: 2026-07-15T00:00:00Z
musubi-managed: false
---
Body."""
    md_path.write_text(yaml_text)

    await watcher._handle_event_inner(
        str(md_path), type("Event", (), {"event_type": "modified", "is_directory": False})()
    )

    created = getattr(watcher.curated_plane, "created_memory")
    assert created is not None
    assert created.object_id == fid
    assert created.title == "Op Watcher"
    assert created.musubi_managed is False
    assert created.content == "Body."
    qdrant.close()
