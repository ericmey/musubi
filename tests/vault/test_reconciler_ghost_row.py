from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from musubi.lifecycle.coordinator import TransitionPending
from musubi.types.common import Err, Ok
from musubi.vault.reconciler import VaultReconciler


@pytest.fixture
def mock_curated_plane() -> MagicMock:
    plane = MagicMock()
    plane._client = MagicMock()
    plane.transition = AsyncMock(return_value=Ok(value=None))
    return plane


@pytest.fixture
def mock_coordinator() -> MagicMock:
    return MagicMock()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


@pytest.mark.anyio
async def test_reconciler_archives_missing_ghost_row(
    vault_root: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    reconciler = VaultReconciler(vault_root, mock_curated_plane, mock_coordinator)

    # Missing from disk
    row = MagicMock()
    row.vault_path = "eric/ghosts/missing.md"
    row.state = "matured"
    row.namespace = "eric/ghosts/curated"
    row.object_id = "test_id"
    row.version = 1

    mock_curated_plane.scan_vault_rows = AsyncMock(return_value=[row])

    await reconciler.reconcile()

    mock_curated_plane.transition.assert_called_once_with(
        namespace="eric/ghosts/curated",
        object_id="test_id",
        to_state="archived",
        actor="system/vault-reconciler",
        reason="Ghost row reconciliation (deleted from disk): eric/ghosts/missing.md",
        coordinator=mock_coordinator,
    )


@pytest.mark.anyio
async def test_reconciler_ignores_present_or_archived_rows(
    vault_root: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    reconciler = VaultReconciler(vault_root, mock_curated_plane, mock_coordinator)

    # 1. Present on disk
    f1 = vault_root / "eric" / "ghosts" / "present.md"
    f1.parent.mkdir(parents=True, exist_ok=True)
    f1.write_text("---\ntitle: t\nobject_id: id1\nnamespace: eric/ghosts/curated\n---\nbody")

    row_present = MagicMock()
    row_present.vault_path = "eric/ghosts/present.md"
    row_present.state = "matured"
    row_present.namespace = "eric/ghosts/curated"
    row_present.object_id = "id1"
    row_present.version = 1

    # 2. Archived (not on disk)
    row_archived = MagicMock()
    row_archived.vault_path = "eric/ghosts/missing_archived.md"
    row_archived.state = "archived"
    row_archived.namespace = "eric/ghosts/curated"
    row_archived.object_id = "id2"
    row_archived.version = 1

    mock_curated_plane.scan_vault_rows = AsyncMock(return_value=[row_present, row_archived])

    reconciler._reconcile_file = AsyncMock(return_value="upserted")  # type: ignore

    await reconciler.reconcile()

    assert mock_curated_plane.transition.call_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("ignored_dir", [".obsidian", "_sketch"])
async def test_reconciler_does_not_archive_present_rows_under_ignored_directories(
    vault_root: Path,
    mock_curated_plane: MagicMock,
    mock_coordinator: MagicMock,
    ignored_dir: str,
) -> None:
    reconciler = VaultReconciler(vault_root, mock_curated_plane, mock_coordinator)
    rel_path = f"{ignored_dir}/present.md"
    path = vault_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ignored by indexing but present on disk")

    row = MagicMock()
    row.vault_path = rel_path
    row.state = "matured"
    row.namespace = "eric/ignored/curated"
    row.object_id = "id-ignored"
    mock_curated_plane.scan_vault_rows = AsyncMock(return_value=[row])
    reconciler._reconcile_file = AsyncMock(return_value="upserted")  # type: ignore

    await reconciler.reconcile()

    reconciler._reconcile_file.assert_not_awaited()
    mock_curated_plane.transition.assert_not_awaited()


@pytest.mark.anyio
async def test_reconciler_normalizes_legacy_windows_path_before_ghost_comparison(
    vault_root: Path,
    mock_curated_plane: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    reconciler = VaultReconciler(vault_root, mock_curated_plane, mock_coordinator)
    path = vault_root / "eric" / "ghosts" / "present.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("present")

    row = MagicMock()
    row.vault_path = r"eric\ghosts\present.md"
    row.state = "matured"
    row.namespace = "eric/ghosts/curated"
    row.object_id = "id-windows-path"
    mock_curated_plane.scan_vault_rows = AsyncMock(return_value=[row])
    reconciler._reconcile_file = AsyncMock(return_value="unchanged")  # type: ignore

    await reconciler.reconcile()

    mock_curated_plane.transition.assert_not_awaited()


@pytest.mark.anyio
async def test_reconciler_failure_visibility(
    vault_root: Path,
    mock_curated_plane: MagicMock,
    mock_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reconciler = VaultReconciler(vault_root, mock_curated_plane, mock_coordinator)

    row = MagicMock()
    row.vault_path = "eric/ghosts/fail.md"
    row.state = "matured"
    row.namespace = "eric/ghosts/curated"
    row.object_id = "id"
    row.version = 1
    mock_curated_plane.scan_vault_rows = AsyncMock(return_value=[row])

    class MockError:
        message = "injected_failure"

    mock_curated_plane.transition.return_value = Err(error=MockError())

    await reconciler.reconcile()

    assert "Failed to archive ghost row eric/ghosts/fail.md: injected_failure" in caplog.text


@pytest.mark.anyio
async def test_reconciler_ignores_namespace_mismatch(
    vault_root: Path,
    mock_curated_plane: MagicMock,
    mock_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reconciler = VaultReconciler(vault_root, mock_curated_plane, mock_coordinator)

    row = MagicMock()
    row.vault_path = "eric/ghosts/fail.md"
    row.state = "matured"
    row.namespace = "some/other/namespace"
    row.object_id = "id"
    row.version = 1
    mock_curated_plane.scan_vault_rows = AsyncMock(return_value=[row])

    import logging

    with caplog.at_level(logging.DEBUG):
        await reconciler.reconcile()
    assert mock_curated_plane.transition.call_count == 0
    assert (
        "Ghost row candidate eric/ghosts/fail.md namespace some/other/namespace does not match expected eric/ghosts/curated"
        in caplog.text
    )


@pytest.mark.anyio
async def test_reconciler_handles_pending_transition(
    vault_root: Path,
    mock_curated_plane: MagicMock,
    mock_coordinator: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reconciler = VaultReconciler(vault_root, mock_curated_plane, mock_coordinator)

    row = MagicMock()
    row.vault_path = "eric/ghosts/pending.md"
    row.state = "matured"
    row.namespace = "eric/ghosts/curated"
    row.object_id = "id"
    row.version = 1
    mock_curated_plane.scan_vault_rows = AsyncMock(return_value=[row])

    mock_curated_plane.transition.return_value = Ok(
        value=TransitionPending(operation_key="op", event_id="ev")
    )

    import logging

    with caplog.at_level(logging.INFO):
        await reconciler.reconcile()

    assert "Ghost row archive pending for eric/ghosts/pending.md" in caplog.text


@pytest.mark.anyio
async def test_reconciler_surfaces_scan_failure(
    vault_root: Path, mock_curated_plane: MagicMock, mock_coordinator: MagicMock
) -> None:
    from unittest.mock import AsyncMock

    reconciler = VaultReconciler(vault_root, mock_curated_plane, mock_coordinator)

    mock_curated_plane.scan_vault_rows = AsyncMock(
        side_effect=Exception("Validation Error in scan")
    )

    with pytest.raises(Exception, match="Validation Error in scan"):
        await reconciler.reconcile()
