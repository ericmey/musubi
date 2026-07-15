"""Tests for `musubi.vault.reconciler.VaultReconciler`.

Covers the substantive contract of the partial musubi#345 fix:

- Files with `object_id` frontmatter get upserted to the curated plane.
- Files without `object_id` are skipped (watcher's job).
- Files with unchanged body-hash are skipped on re-pass (no embed churn).
- Files in hidden / underscore-prefixed dirs are excluded.
- Failures on individual files don't abort the whole pass.
- `build_vault_reconcile_jobs` produces the correct Job shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from ksuid import Ksuid

from musubi.vault.reconciler import VaultReconciler, build_vault_reconcile_jobs


def _ksuid() -> str:
    """Fresh 27-char base62 KSUID matching CuratedFrontmatter's
    validator. Tests use this rather than hardcoded strings so each
    seed produces a unique object_id."""
    return str(Ksuid())


def _seed_md(
    root: Path,
    rel: str,
    *,
    object_id: str | None = None,
    body: str = "body content",
    title: str = "Title",
    namespace: str = "aoi/shared/curated",
) -> Path:
    """Write a markdown file with valid CuratedFrontmatter shape."""
    fm_lines = ["---"]
    if object_id is not None:
        fm_lines.append(f"object_id: {object_id}")
    fm_lines.extend(
        [
            f"namespace: {namespace}",
            f"title: {title}",
            "state: matured",
            "importance: 5",
            "topics: []",
            "tags: []",
            "version: 1",
            "created: 2026-05-01T00:00:00Z",
            "updated: 2026-05-01T00:00:00Z",
            "---",
            "",
            body,
            "",
        ]
    )
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm_lines), encoding="utf-8")
    return path


@pytest.fixture
def mock_curated_plane() -> MagicMock:
    from unittest.mock import MagicMock, AsyncMock

    plane = MagicMock()
    plane._client = MagicMock()
    plane.scan_vault_rows = AsyncMock(return_value=[])
    return plane
