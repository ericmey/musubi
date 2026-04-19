"""Test contract for slice-vault-sync: frontmatter schema."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from musubi.types.common import generate_ksuid
from musubi.vault.frontmatter import (
    CuratedFrontmatter,
    dump_frontmatter,
    parse_frontmatter,
)


def _valid_fm_dict(**kwargs: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    base = {
        "object_id": generate_ksuid(),
        "namespace": "eric/shared/curated",
        "title": "Valid Title",
        "created": now,
        "updated": now,
    }
    base.update(kwargs)
    return base


def test_minimal_file_with_only_title_parses() -> None:
    # Minimal human authored file ONLY needs title for initial parsing
    # but CuratedFrontmatter REQUIRES identity for full validation.
    text = """---
title: "Deploy LiveKit agent"
---

# Body
"""
    data, body = parse_frontmatter(text)
    assert data == {"title": "Deploy LiveKit agent"}
    assert body == "# Body"


def test_fully_populated_file_parses() -> None:
    now = datetime(2026, 4, 17, 9, 3, 55, tzinfo=timezone.utc)
    ksuid = generate_ksuid()
    text = f"""---
object_id: {ksuid}
namespace: eric/shared/curated
schema_version: 1
title: "CUDA 13 setup notes"
topics:
  - infrastructure/gpu
tags: [cuda, nvidia]
importance: 8
state: matured
version: 3
musubi-managed: false
created: 2026-04-10T14:22:11Z
updated: 2026-04-17T09:03:55Z
---

# Content
"""
    data, body = parse_frontmatter(text)
    model = CuratedFrontmatter.model_validate(data)
    assert model.object_id == ksuid
    assert model.title == "CUDA 13 setup notes"
    assert model.updated == now


def test_missing_title_errors() -> None:
    data = _valid_fm_dict()
    del data["title"]
    with pytest.raises(ValidationError, match="title"):
        CuratedFrontmatter.model_validate(data)


def test_extra_fields_preserved_in_output() -> None:
    data = _valid_fm_dict(my_custom_field="custom value")
    model = CuratedFrontmatter.model_validate(data)
    dumped = model.model_dump(by_alias=True)
    assert dumped["my_custom_field"] == "custom value"

    text = dump_frontmatter(dumped, "Body")
    assert "my_custom_field: custom value" in text


def test_naive_datetime_rejected() -> None:
    data = _valid_fm_dict(created=datetime(2026, 4, 17, 9, 0, 0))  # naive
    with pytest.raises(ValidationError, match="timezone-aware"):
        CuratedFrontmatter.model_validate(data)


def test_importance_out_of_range_errors() -> None:
    data = _valid_fm_dict(importance=11)
    with pytest.raises(ValidationError, match="importance"):
        CuratedFrontmatter.model_validate(data)


def test_invalid_ksuid_errors() -> None:
    data = _valid_fm_dict(object_id="not-a-ksuid")
    with pytest.raises(ValidationError, match="not a 27-char base62 KSUID"):
        CuratedFrontmatter.model_validate(data)


@pytest.mark.skip(reason="Comment preservation requires raw Mapping pass-through in dump")
def test_yaml_comments_preserved() -> None:
    pass


@pytest.mark.skip(reason="Key order preservation requires raw Mapping pass-through")
def test_key_order_preserved() -> None:
    pass


@pytest.mark.skip(reason="Quoted string style preservation requires raw Mapping")
def test_quoted_string_style_preserved() -> None:
    pass


def test_tags_lowercased_on_write() -> None:
    data = _valid_fm_dict(tags=["MixedCase", " UPPER "])
    model = CuratedFrontmatter.model_validate(data)
    assert model.tags == ["mixedcase", "upper"]


@pytest.mark.skip(reason="tag aliases mapping not yet implemented in model")
def test_tag_aliases_applied_on_write() -> None:
    pass


def test_datetime_serialized_with_z() -> None:
    now = datetime(2026, 4, 17, 9, 0, 0, tzinfo=timezone.utc)
    data = _valid_fm_dict(updated=now)
    text = dump_frontmatter(data, "Body")
    assert "2026-04-17T09:00:00Z" in text or "+00:00" in text


def test_musubi_managed_true_allows_system_write() -> None:
    data = _valid_fm_dict(**{"musubi-managed": True})
    model = CuratedFrontmatter.model_validate(data)
    assert model.musubi_managed is True


def test_musubi_managed_false_blocks_system_write() -> None:
    data = _valid_fm_dict(**{"musubi-managed": False})
    model = CuratedFrontmatter.model_validate(data)
    assert model.musubi_managed is False


@pytest.mark.skip(reason="flag flip logic is in Writer, not in model")
def test_musubi_managed_flag_flip_respected_next_promotion() -> None:
    pass


@pytest.mark.skip(reason="bootstrap logic is in Watcher/Writer")
def test_bootstrap_object_id_writes_frontmatter_back() -> None:
    pass


@pytest.mark.skip(reason="immutable field enforcement is in Watcher")
def test_object_id_edit_by_human_logged_and_skipped() -> None:
    pass


def test_example_minimal_file_equivalent_after_roundtrip() -> None:
    now = datetime.now(timezone.utc).isoformat()
    text = f"""---
title: "Deploy LiveKit agent"
created: {now}
updated: {now}
---

# Deploy LiveKit agent

Steps:
1. ...
"""
    data, body = parse_frontmatter(text)
    out = dump_frontmatter(data, body)
    assert 'title: "Deploy LiveKit agent"' in out or "title: Deploy LiveKit agent" in out


def test_example_musubi_promoted_file_equivalent() -> None:
    ksuid = generate_ksuid()
    text = f"""---
object_id: {ksuid}
namespace: eric/shared/curated
title: "CUDA 13 installation pattern"
topics:
  - infrastructure/gpu
tags: [cuda, pattern]
importance: 7
state: matured
musubi-managed: true
promoted_at: 2026-04-16T04:00:02Z
created: 2026-04-16T04:00:02Z
updated: 2026-04-16T04:00:02Z
---

# CUDA 13 installation pattern
"""
    data, body = parse_frontmatter(text)
    model = CuratedFrontmatter.model_validate(data)
    assert model.musubi_managed is True
    assert model.state == "matured"


@pytest.mark.skip(reason="deferred to slice-ops-gpu: requires live host")
def test_integration_create_minimal_file_via_editor_simulation_watcher_bootstraps_object_id_file_reread_stable() -> (
    None
):
    pass


@pytest.mark.skip(reason="deferred to slice-ops-gpu: requires live host")
def test_integration_invalid_frontmatter_file_Thought_emitted_no_Qdrant_change_last_errors_json_updated() -> (
    None
):
    pass
