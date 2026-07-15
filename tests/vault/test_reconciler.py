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
    plane = MagicMock()
    plane.create = AsyncMock(return_value=None)
    plane.scan_vault_rows = AsyncMock(return_value=[])
    return plane


@pytest.fixture
def mock_coordinator() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_reconcile_upserts_stamped_file(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    _seed_md(tmp_path, "note.md", object_id=_ksuid())
    rec = VaultReconciler(
        vault_root=tmp_path, curated_plane=mock_curated_plane, coordinator=mock_coordinator
    )
    upserted = await rec.reconcile()
    assert upserted == 1
    mock_curated_plane.create.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_skips_files_without_object_id(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    _seed_md(tmp_path, "no-id.md", object_id=None)
    rec = VaultReconciler(
        vault_root=tmp_path, curated_plane=mock_curated_plane, coordinator=mock_coordinator
    )
    upserted = await rec.reconcile()
    assert upserted == 0
    mock_curated_plane.create.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_skips_unchanged_on_second_pass(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    """Body-hash cache: same body → no second upsert. The core scope
    of the musubi#345 cleanup — without this, every 6h tick re-embeds
    the entire vault."""
    _seed_md(tmp_path, "stable.md", object_id=_ksuid(), body="unchanged body")
    rec = VaultReconciler(
        vault_root=tmp_path, curated_plane=mock_curated_plane, coordinator=mock_coordinator
    )

    first = await rec.reconcile()
    second = await rec.reconcile()
    assert first == 1
    assert second == 0
    assert mock_curated_plane.create.call_count == 1


@pytest.mark.asyncio
async def test_reconcile_reupserts_when_body_changes(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    obj_id = _ksuid()
    _seed_md(tmp_path, "evolving.md", object_id=obj_id, body="first body")
    rec = VaultReconciler(
        vault_root=tmp_path, curated_plane=mock_curated_plane, coordinator=mock_coordinator
    )

    await rec.reconcile()
    # Re-seed with a different body but same object_id.
    _seed_md(tmp_path, "evolving.md", object_id=obj_id, body="second body")
    second = await rec.reconcile()
    assert second == 1
    assert mock_curated_plane.create.call_count == 2


@pytest.mark.asyncio
async def test_reconcile_excludes_hidden_and_underscore_dirs(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    _seed_md(tmp_path, "_secrets/keychain.md", object_id=_ksuid())
    _seed_md(tmp_path, ".obsidian/workspace.md", object_id=_ksuid())
    _seed_md(tmp_path, "normal.md", object_id=_ksuid())
    rec = VaultReconciler(
        vault_root=tmp_path, curated_plane=mock_curated_plane, coordinator=mock_coordinator
    )
    upserted = await rec.reconcile()
    assert upserted == 1


@pytest.mark.asyncio
async def test_reconcile_non_md_files_ignored(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    (tmp_path / "note.txt").write_text("not markdown")
    (tmp_path / "image.png").write_bytes(b"binary")
    rec = VaultReconciler(
        vault_root=tmp_path, curated_plane=mock_curated_plane, coordinator=mock_coordinator
    )
    upserted = await rec.reconcile()
    assert upserted == 0


@pytest.mark.asyncio
async def test_reconcile_missing_root_returns_zero(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    nonexistent = tmp_path / "does-not-exist"
    rec = VaultReconciler(
        vault_root=nonexistent, curated_plane=mock_curated_plane, coordinator=mock_coordinator
    )
    upserted = await rec.reconcile()
    assert upserted == 0


@pytest.mark.asyncio
async def test_reconcile_individual_failure_doesnt_abort_pass(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    """One bad file shouldn't take down the whole pass."""
    _seed_md(tmp_path, "ok-1.md", object_id=_ksuid())
    _seed_md(tmp_path, "ok-2.md", object_id=_ksuid())
    # Corrupted frontmatter on the third file.
    (tmp_path / "broken.md").write_text(
        "---\n[not valid yaml because: of: this\n---\n\nbody\n", encoding="utf-8"
    )
    rec = VaultReconciler(
        vault_root=tmp_path, curated_plane=mock_curated_plane, coordinator=mock_coordinator
    )
    upserted = await rec.reconcile()
    # Two good files upserted; broken one logged + skipped.
    assert upserted == 2


def test_build_vault_reconcile_jobs_produces_correct_shape(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    jobs = build_vault_reconcile_jobs(
        vault_root=tmp_path,
        curated_plane=mock_curated_plane,
        lock_dir=lock_dir,
        coordinator=mock_coordinator,
    )
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "vault_reconcile"
    assert job.trigger_kind == "interval"
    assert job.trigger_kwargs == {"hours": 6}
    # Callable wires correctly — invoking it should acquire the lock,
    # run the (empty) reconciler, and release. No exception.
    job.func()
    assert (lock_dir / "vault_reconcile.lock").exists()


def test_build_lifecycle_jobs_includes_real_vault_reconcile_job(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    """The lifecycle wiring should pick up the real vault_reconcile job
    instead of the placeholder. Without this, the lifecycle worker
    would keep logging 'not yet implemented; skipping' even after the
    reconciler is shipped."""
    from musubi.lifecycle.runner import build_lifecycle_jobs

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    vault_reconcile_jobs = build_vault_reconcile_jobs(
        vault_root=tmp_path,
        curated_plane=mock_curated_plane,
        lock_dir=lock_dir,
        coordinator=mock_coordinator,
    )
    composed: list[Any] = build_lifecycle_jobs(vault_reconcile_jobs=vault_reconcile_jobs)
    vr_jobs = [j for j in composed if j.name == "vault_reconcile"]
    assert len(vr_jobs) == 1
    # And it's our real one, not the placeholder lambda that logs "not yet implemented".
    assert vr_jobs[0].trigger_kind == "interval"
    assert vr_jobs[0].trigger_kwargs == {"hours": 6}
