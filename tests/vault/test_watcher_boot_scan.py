from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from musubi.vault.watcher import VaultWatcher


@pytest.fixture
def mock_curated_plane() -> MagicMock:
    plane = MagicMock()
    plane._client = MagicMock()
    return plane


@pytest.fixture
def mock_write_log() -> MagicMock:
    wl = MagicMock()
    wl.consume_if_exists.return_value = False
    return wl


# test_boot_scan_archives_removed_files was REMOVED (vacuous:
# async slow_scroll on sync scroll; assert True passed). The
# deletion case is now a routing marker in
# test_boot_scan_vault_002_deletion_routed_to_vault_001_marker
# (in tests/vault/test_watcher_boot_scan_vault_002.py).


@pytest.mark.asyncio
async def test_boot_scan_detects_body_hash_change(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_write_log: MagicMock
) -> None:
    watcher = VaultWatcher(tmp_path, mock_curated_plane, mock_write_log)
    watcher._loop = asyncio.get_running_loop()

    # Setup the vault with one file
    f1 = tmp_path / "file1.md"
    f1.write_text("---\ntitle: t\n---\nbody")

    # Qdrant mock returns an old hash
    point = MagicMock()
    point.payload = {"vault_path": "file1.md", "body_hash": "old_hash"}
    mock_curated_plane._client.scroll.return_value = ([point], None)

    # We mock _handle_event to just record calls
    from unittest.mock import AsyncMock

    setattr(watcher, "_handle_event", AsyncMock())

    watcher.boot_scan()
    await asyncio.sleep(0.1)

    assert cast(AsyncMock, watcher._handle_event).call_count == 1
    assert cast(AsyncMock, watcher._handle_event).call_args[0][0] == "file1.md"


@pytest.mark.asyncio
async def test_boot_scan_indexes_new_files(
    tmp_path: Path, mock_curated_plane: MagicMock, mock_write_log: MagicMock
) -> None:
    import hashlib

    watcher = VaultWatcher(tmp_path, mock_curated_plane, mock_write_log)
    watcher._loop = asyncio.get_running_loop()

    # Setup the vault with one file
    f1 = tmp_path / "file1.md"
    f1.write_text("---\ntitle: t\n---\nbody")
    real_hash = hashlib.sha256(b"body").hexdigest()

    # Qdrant mock returns the current hash
    point = MagicMock()
    point.payload = {"vault_path": "file1.md", "body_hash": real_hash}
    mock_curated_plane._client.scroll.return_value = ([point], None)

    from unittest.mock import AsyncMock

    setattr(watcher, "_handle_event", AsyncMock())

    watcher.boot_scan()
    await asyncio.sleep(0.1)

    assert cast(AsyncMock, watcher._handle_event).call_count == 0
