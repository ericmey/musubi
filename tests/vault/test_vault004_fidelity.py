import pytest
from datetime import datetime, timezone
from musubi.types.common import generate_ksuid, ArtifactRef
from musubi.vault.frontmatter import CuratedFrontmatter, curated_knowledge_from_frontmatter, parse_frontmatter

def test_vault004_fidelity_round_trip():
    # 1. Provide all explicitly supported fields in frontmatter
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
    
    # 2. Reconstruct CuratedKnowledge via shared seam
    vault_path = "test/path.md"
    body_hash = "a" * 64
    memory = curated_knowledge_from_frontmatter(fm, vault_path=vault_path, body_hash=body_hash, content=body)
    
    # 3. Assert all supported fields are correctly copied
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
    assert memory.created_at == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert memory.updated_at == datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert memory.valid_from == datetime(2026, 7, 2, tzinfo=timezone.utc)
    assert memory.valid_until == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert memory.supersedes == [sid]
    assert memory.superseded_by == sid
    assert memory.merged_from == [fid]
    assert memory.promoted_from == promoted_ksuid
    assert memory.promoted_at == datetime(2026, 7, 10, tzinfo=timezone.utc)
    assert len(memory.supported_by) == 1
    assert memory.supported_by[0].artifact_id == aid
    assert memory.supported_by[0].quote == "Explicitly backed"
    assert memory.linked_to_topics == ["testing"]
    assert memory.contradicts == [cid]
    
    assert memory.vault_path == vault_path
    assert memory.body_hash == body_hash
    assert memory.content == "Body text."

def test_vault004_visible_failure_on_unsupported_fields():
    # Provide frontmatter with unsupported read_by list
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
    
    # The seam MUST fail visibly
    with pytest.raises(ValueError, match="read_by is unsupported on CuratedKnowledge"):
        curated_knowledge_from_frontmatter(fm, vault_path="x", body_hash="a"*64, content=body)
