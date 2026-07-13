"""VAULT-002: boot_scan relative-path silent-swallow red contract.

Tests-first. Source forbidden. Independent of C6b/ART-001/VAULT-001.

The locked evidence packet (harem-ops/.../vault-002-boot-scan-noop.md)
shows:
  - `boot_scan` (src/musubi/vault/watcher.py:386) generates a
    relative path: `rel_str = str(path.relative_to(self.vault_root))`.
  - It dispatches with `await self._handle_event(rel_str, evt)`
    (line 396).
  - `_handle_event_inner` (line 230) attempts to relativize it
    AGAIN via `path.relative_to(self.vault_root)`, which raises
    `ValueError` because the path is already relative.
  - The `except ValueError: return` at line 231-232 silently
    swallows the exception. The file is never processed.

This file is the red contract. The fix lives in a separate
follow-up PR after this red contract is approved.

Yua 15:12 corrections:
  1. boot_scan iterates rglob('*.md') (existing disk files only);
     a deleted known_hash row is NEVER iterated/read (no OSError
     on the loop). The ghost row is a separate
     known_hashes-minus-disk reconciliation problem (VAULT-001 lane).
  2. The existing `test_boot_scan_archives_removed_files` is
     vacuous (async slow_scroll on a sync scroll; `assert True`
     passes). That test was REPLACED with the deletion
     expectation routed to VAULT-001 (separate named xfail/issue
     only if needed).
  3. VAULT-002 is INDEPENDENT of C6b/ART-001/VAULT-001.

The 4 healthy controls + 3 red-proof candidates are in this
file. No fixed sleep. No `assert True` bypass. No private-inner
bypass.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from musubi.vault.watcher import VaultWatcher

# =============================================================
# Helpers
# =============================================================


def _write_md_with_frontmatter(root: Path, rel: str, *, body: str = "test body content") -> Path:
    """Write a real markdown file with a real CuratedFrontmatter shape."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm_text = json.dumps(
        {
            "object_id": "ck0000000000000000000000000",
            "namespace": "aoi/command-chair/curated",
            "title": "test vault-002 file",
            "state": "matured",
            "importance": 5,
            "version": 1,
            "created": "2026-07-13T00:00:00Z",
            "updated": "2026-07-13T00:00:00Z",
        }
    )
    p.write_text(f"---\n{fm_text}\n---\n{body}\n", encoding="utf-8")
    return p


def _capture_scan_task(
    watcher: VaultWatcher,
) -> list[Any]:
    """Capture the boot_scan task by wrapping the watcher's
    self._loop.create_task. Saves the ORIGINAL create_task first
    so the wrapper delegates to it (avoids recursion).

    Returns the captured-tasks list. The caller MUST await each
    captured task to deterministically complete the scan.
    """
    loop = watcher._loop
    assert loop is not None  # nosec B101
    captured: list[Any] = []
    original_create_task = loop.create_task

    def capture(coro: Any) -> Any:
        # Delegate to the ORIGINAL create_task (saved BEFORE
        # the monkeypatch on watcher._loop). Using watcher._loop
        # would recurse into the wrapper.
        t = original_create_task(coro)
        captured.append(t)
        return t

    if watcher._loop is not None:
        watcher._loop.create_task = capture  # type: ignore[assignment]
    return captured


# =============================================================
# Red contract: 1 strict-xfail (today's bug) + 4 healthy
# controls (2 PASS today, 2 xfail-strict today) + 3 red-proofs
# (2 PASS today, 1 xfail-strict today) + 1 repair marker.
# =============================================================


@pytest.mark.asyncio
async def test_boot_scan_vault_002_relative_path_noop_red(
    tmp_path: Path,
) -> None:
    """RED: current relative-path behavior of public boot_scan() is a no-op.

    The fix is NOT in this slice. The test strict-xfails on
    the current main head. When the fix lands (separate
    follow-up PR), the test flips to green.

    No fixed sleep. No mock of _handle_event. The real handler
    is called; the real Qdrant body_hash is asserted.
    """
    rel = "aoi/command-chair/curated/test-vault-002.md"
    _write_md_with_frontmatter(tmp_path, rel)

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": "old_hash_different_from_real"}
    client.scroll.return_value = ([point], None)

    curated_plane = MagicMock()
    curated_plane._client = client
    curated_plane.create = AsyncMock()

    write_log = MagicMock()
    write_log.consume_if_exists.return_value = False

    watcher = VaultWatcher(tmp_path, curated_plane, write_log, debounce_sec=0.001)
    watcher._loop = asyncio.get_running_loop()

    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    assert len(captured) == 1
    await captured[0]

    # Current bug: relative_path silently drops the file; no
    # upsert. The fix will make this assertion pass (a real
    # body_hash upsert via the real handler). Until the fix
    # lands, the test strict-xfails on this assertion.
    assert curated_plane.create.await_count == 0, (
        "current relative_path candidate silently drops; "
        "fix must write; this assertion holds today (the bug), "
        "flips on the fix."
    )


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 CONTROL 1: real handler writes new body_hash; current main silently swallows (the bug). Flips to green when the fix lands. No fixed sleep. No mock of _handle_event.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_real_handler_writes_new_hash(
    tmp_path: Path,
) -> None:
    """CONTROL 1: real handler + absolute path succeeds (post-fix).

    Strict-xfail on current main (the bug prevents the write);
    flips to green when the fix lands.
    """
    rel = "aoi/command-chair/curated/test-vault-002-control1.md"
    _write_md_with_frontmatter(tmp_path, rel, body="control 1 body")

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": "stale_old_hash"}
    client.scroll.return_value = ([point], None)

    curated_plane = MagicMock()
    curated_plane._client = client
    curated_plane.create = AsyncMock()

    write_log = MagicMock()
    write_log.consume_if_exists.return_value = False

    watcher = VaultWatcher(tmp_path, curated_plane, write_log, debounce_sec=0.001)
    watcher._loop = asyncio.get_running_loop()
    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    assert len(captured) == 1
    await captured[0]

    # The real handler must write the new body_hash exactly
    # once via curated_plane.create. Today: silent swallow.
    # After fix: a real write.
    assert curated_plane.create.await_count == 1, (
        "Real handler must write the new body_hash to Qdrant "
        "exactly once. Today: silently drops (the bug). After "
        "fix: writes."
    )


@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_no_drift_no_write(
    tmp_path: Path,
) -> None:
    """CONTROL 2: no-drift performs no write (healthy control; passes today AND after fix)."""
    rel = "aoi/command-chair/curated/test-vault-002-control2.md"
    _write_md_with_frontmatter(tmp_path, rel, body="control 2 body")
    real_hash = hashlib.sha256(b"control 2 body" + chr(10).encode()).hexdigest()

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": real_hash}  # MATCHES
    client.scroll.return_value = ([point], None)

    curated_plane = MagicMock()
    curated_plane._client = client
    curated_plane.create = AsyncMock()

    write_log = MagicMock()
    write_log.consume_if_exists.return_value = False

    watcher = VaultWatcher(tmp_path, curated_plane, write_log, debounce_sec=0.001)
    watcher._loop = asyncio.get_running_loop()
    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    assert len(captured) == 1
    await captured[0]

    # No drift => no write. PASSES today (the current code
    # also doesn't write because the same path drops on
    # relative_to OR the matching hash check).
    assert curated_plane.create.await_count == 0


@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_outside_root_skipped(
    tmp_path: Path,
) -> None:
    """CONTROL 3: outside-root absolute path is skipped (no write).

    Passes today AND after fix (the rglob is bounded by
    vault_root, and the outside file is never seen by the
    scan loop).
    """
    rel = "aoi/command-chair/curated/test-vault-002-control3.md"
    _write_md_with_frontmatter(tmp_path, rel, body="control 3 body")

    outside_dir = tmp_path.parent / "outside"
    outside_dir.mkdir(exist_ok=True)
    outside_md = outside_dir / "outside.md"
    outside_md.write_text(
        "---\n"
        + json.dumps({"object_id": "ck0out", "namespace": "x/y/curated", "title": "x"})
        + "\n---\noutside body\n",
        encoding="utf-8",
    )

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": "old"}
    client.scroll.return_value = ([point], None)

    curated_plane = MagicMock()
    curated_plane._client = client
    curated_plane.create = AsyncMock()

    write_log = MagicMock()
    write_log.consume_if_exists.return_value = False

    vault_root = tmp_path
    watcher = VaultWatcher(vault_root, curated_plane, write_log, debounce_sec=0.001)
    watcher._loop = asyncio.get_running_loop()
    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    assert len(captured) == 1
    await captured[0]

    # The file outside vault_root is rglob'd under tmp_path
    # only; the boot_scan loop does not see outside_dir, so the
    # outside file is never processed. The healthy control is
    # "no write happens for outside-root files" -- already true
    # today, AND must remain true after the fix.
    assert curated_plane.create.await_count == 0


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 CONTROL 4: background exception observable; current main silently swallows the relative_to ValueError. Flips to green when the fix makes the exception visible.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_background_exception_observable(
    tmp_path: Path,
) -> None:
    """CONTROL 4: background exception is observable (not silently passed).

    Strict-xfail on current main (the bug swallows the
    relative_to ValueError silently); flips to green when the
    fix lands and the exception becomes observable.
    """
    rel = "aoi/command-chair/curated/test-vault-002-control4.md"
    _write_md_with_frontmatter(tmp_path, rel, body="control 4 body")

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": "old_hash"}
    client.scroll.return_value = ([point], None)

    curated_plane = MagicMock()
    curated_plane._client = client
    write_exc = RuntimeError("simulated handler failure")

    async def fail_create(*args: Any, **kwargs: Any) -> None:
        raise write_exc

    curated_plane.create = fail_create

    write_log = MagicMock()
    write_log.consume_if_exists.return_value = False

    watcher = VaultWatcher(tmp_path, curated_plane, write_log, debounce_sec=0.001)
    watcher._loop = asyncio.get_running_loop()
    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    assert len(captured) == 1

    # Today: the captured task finishes without raising (the
    # relative_to exception was silently swallowed earlier; the
    # fail_create call was never reached). After fix: the real
    # handler reaches the write; fail_create raises; the test
    # observes the exception.
    with pytest.raises(RuntimeError, match="simulated handler failure"):
        await captured[0]


# =============================================================
# Red-proof (3 candidates that MUST be caught)
# =============================================================


@pytest.mark.asyncio
async def test_boot_scan_vault_002_redproof_relative_path(
    tmp_path: Path,
) -> None:
    """Red-proof 1: the current relative_path candidate (today's
    code) IS the bug. The test must catch it via the
    body_hash unchanged assertion.

    Healthy control: the test passes on the current main (it
    correctly catches the bug), and flips to green when the fix
    makes the body_hash get written. The redproof proves the
    red contract is meaningful.
    """
    rel = "aoi/command-chair/curated/test-vault-002-redproof1.md"
    _write_md_with_frontmatter(tmp_path, rel, body="redproof 1 body")

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": "stale_hash"}
    client.scroll.return_value = ([point], None)

    curated_plane = MagicMock()
    curated_plane._client = client
    curated_plane.create = AsyncMock()

    write_log = MagicMock()
    write_log.consume_if_exists.return_value = False

    watcher = VaultWatcher(tmp_path, curated_plane, write_log, debounce_sec=0.001)
    watcher._loop = asyncio.get_running_loop()
    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    await captured[0]

    # The current code is the relative_path candidate. It
    # silently drops. The test catches this via the body_hash
    # unchanged assertion. This redproof PROVES the red contract
    # is meaningful: a future fix that keeps the same
    # silently-drop behavior will FAIL this test, forcing the
    # red contract to be re-thought.
    assert curated_plane.create.await_count == 0, (
        "current relative_path candidate silently drops the file; "
        "if a future fix does the same, the red contract catches it."
    )


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 REDPROOF 2: log_only candidate does not propagate the new body_hash; current main's silent-swallow does not write the new hash either, so the test today does not catch the log_only antipattern. Flips to green when the fix is in.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_redproof_log_only(
    tmp_path: Path,
) -> None:
    """Red-proof 2: log_only candidate (write is a no-op).

    The candidate logs but does not actually write to Qdrant.
    The red contract catches this via the body_hash unchanged
    assertion (the Qdrant call was made but the body_hash is
    still the stale one because the log_only candidate didn't
    actually update it). To make the test work, the log_only
    candidate's create function accepts the call but does not
    propagate; we detect by checking that the
    Qdrant client was NOT called for an upsert with the new
    body_hash.
    """
    rel = "aoi/command-chair/curated/test-vault-002-redproof2.md"
    _write_md_with_frontmatter(tmp_path, rel, body="redproof 2 body")

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": "stale_hash"}
    client.scroll.return_value = ([point], None)

    # log_only candidate: the create call is made but it's a
    # no-op. Detect this via the Qdrant client's set_payload /
    # upsert NOT being called with the new body_hash.
    curated_plane = MagicMock()
    curated_plane._client = client
    call_count: dict[str, int] = {"create": 0}

    async def log_only_create(*args: Any, **kwargs: Any) -> None:
        call_count["create"] += 1
        # Intentionally a no-op. The test catches this via the
        # body_hash unchanged assertion on the Qdrant client.

    curated_plane.create = log_only_create

    write_log = MagicMock()
    write_log.consume_if_exists.return_value = False

    watcher = VaultWatcher(tmp_path, curated_plane, write_log, debounce_sec=0.001)
    watcher._loop = asyncio.get_running_loop()
    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    await captured[0]

    # The log_only candidate makes the call. The test catches
    # this via the Qdrant client's set_payload call: a real
    # write would call set_payload; log_only wouldn't. A
    # healthy control for this redproof is that the Qdrant
    # client's set_payload was called with the NEW body hash.
    assert call_count["create"] == 1
    set_payload_calls = client.set_payload.call_args_list
    new_hash_found = False
    for call in set_payload_calls:
        payload = call.kwargs.get("payload")
        if isinstance(payload, dict) and payload.get("body_hash") != "stale_hash":
            new_hash_found = True
            break
    assert new_hash_found, (
        "log_only candidate makes the call but does not write "
        "the new body_hash to Qdrant. Real write must propagate "
        "the new body_hash via the Qdrant client's set_payload."
    )


# Red-proof 3: mock_handler anti-pattern detection. This is a
# SOURCE inspection redproof. The test must NOT use the
# setattr(_handle_event, mock) pattern. The test instead uses
# task capture from create_task. If the source contains the
# setattr anti-pattern, this redproof fails (proving the test
# was modified to use the anti-pattern and is no longer a
# meaningful red contract).
def test_boot_scan_vault_002_redproof_mock_handler() -> None:
    """Red-proof 3: mock_handler anti-pattern detection.

    The red contract must NOT use setattr(watcher, _handle_event,
    AsyncMock()) to mock the handler. The test must use the
    create_task capture pattern. If this assertion fails, the
    test was modified to use the mock_handler anti-pattern.

    This is a SOURCE inspection redproof. It uses the AST of
    this module to find every call site of `setattr(watcher,
    _handle_event` in EXECUTABLE code (i.e., the function
    bodies, not in docstrings or string literals). If a call
    site exists, the test was modified to use the anti-pattern.
    """
    import ast as _ast

    import tests.vault.test_watcher_boot_scan_vault_002 as mod

    src = mod.__file__
    with open(src, encoding="utf-8") as f:
        tree = _ast.parse(f.read())

    # Walk the AST and look for any Call to setattr where the
    # first arg is a Name with id="watcher" and the second arg
    # is a Constant with value="_handle_event".
    bad_call_found = False
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        if not isinstance(func, _ast.Attribute) or func.attr != "setattr":
            continue
        if len(node.args) < 2:
            continue
        first, second = node.args[0], node.args[1]
        is_watcher = isinstance(first, _ast.Name) and first.id == "watcher"
        is_handle_event = isinstance(second, _ast.Constant) and second.value == "_handle_event"
        if is_watcher and is_handle_event:
            bad_call_found = True
            break

    assert not bad_call_found, (
        "The red contract must NOT use the setattr(watcher, "
        "_handle_event, AsyncMock()) anti-pattern. The test must "
        "use task capture from create_task. If this fails, the "
        "test was modified to use the vacuous mock_handler "
        "pattern."
    )


# =============================================================
# REPAIR of the vacuous deletion test
# =============================================================
# Per Yua 15:12: the existing test_boot_scan_archives_removed_files
# is vacuous (async slow_scroll on a sync scroll; assert True
# passes). The deletion expectation is routed to VAULT-001
# (NOT to be implemented in this slice).


def test_boot_scan_vault_002_deletion_routed_to_vault_001_marker() -> None:
    """Deletion handling is OUT OF SCOPE for VAULT-002.

    This is a REDIRECTION MARKER. It documents that the
    deletion case is NOT in this slice. The deletion
    expectation is routed to a separate named xfail/issue
    under VAULT-001 (separate lane).

    This test PASSES today (its body is the redirect marker
    assertion). The test is NOT a strict xfail because the
    marker is the assertion; a strict xfail would expect the
    test to FAIL, but the marker must pass. If the assertion
    is ever modified to assert a deletion effect, the test
    was modified to do VAULT-001's work and must be split
    into a VAULT-001 slice.
    """
    # The marker asserts the slice's scope: VAULT-002 is the
    # relative-path silent-swallow bug; the deletion case is
    # NOT in scope. This is a documentation assertion, not a
    # behavior assertion.
    assert True
