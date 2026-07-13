from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
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

    # VAULT-002 fix (c0c91ba): boot_scan now passes
    # str(path) (the ABSOLUTE in-root path) to
    # _handle_event, not the relative path string.
    # The dispatch-shape expectation must therefore be
    # the absolute in-root path, not "file1.md".
    # Also: prefer deterministic task completion over
    # the fixed asyncio.sleep(0.1) — capture the boot_scan
    # task and await its completion.
    captured: list[asyncio.Task[Any]] = []
    original_create_task = watcher._loop.create_task

    def capture(coro: Any) -> Any:
        t = original_create_task(coro)
        captured.append(t)
        return t

    watcher._loop.create_task = capture  # type: ignore[assignment]
    watcher.boot_scan()
    assert len(captured) == 1
    await captured[0]

    assert cast(AsyncMock, watcher._handle_event).call_count == 1
    # POST VAULT-002 FIX: the dispatch is the absolute
    # in-root path (str(path) from rglob), not the
    # relative path string.
    assert cast(AsyncMock, watcher._handle_event).call_args[0][0] == str(f1)


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

    # Prefer deterministic task completion over the fixed
    # asyncio.sleep(0.1) — capture the boot_scan task and
    # await its completion.
    captured: list[asyncio.Task[Any]] = []
    original_create_task = watcher._loop.create_task

    def capture(coro: Any) -> Any:
        t = original_create_task(coro)
        captured.append(t)
        return t

    watcher._loop.create_task = capture  # type: ignore[assignment]
    watcher.boot_scan()
    assert len(captured) == 1
    await captured[0]

    assert cast(AsyncMock, watcher._handle_event).call_count == 0
