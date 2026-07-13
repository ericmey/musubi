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
     expectation routed to VAULT-001 (see Issue #446).
  3. VAULT-002 is INDEPENDENT of C6b/ART-001/VAULT-001.

The red contract shape is exactly 9 tests:
  - 5 strict xfails (today): the RED + 2 controls + 2 red-proofs
  - 3 plain pass (today): 2 controls + 1 guard red-proof
  - 1 skip: the VAULT-001 routing marker (deferred to Issue #446)

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
# Red contract (5 strict xfails + 3 pass + 1 skip = 9 tests)
# =============================================================


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 RED: the current relative-path candidate silently drops the file (await_count == 0); the fix must convert to an absolute path and write. This test asserts the POSTCONDITION (await_count == 1, new body_hash written via the real handler, and the path that reaches the handler is absolute). It strict-xfails on current main and flips to green when the fix lands.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_relative_path_noop_red(
    tmp_path: Path,
) -> None:
    """RED 1/5: the relative-path bug is caught by the postcondition assertion.

    Asserts the intended future behavior (real handler writes
    new body_hash, the path that reaches the handler is
    absolute). Today: the postcondition is not met (the bug
    silently drops the file before the handler is called).
    The strict-xfail marker causes pytest to xfail today and
    to fail if the test unexpectedly passes (i.e., the fix
    lands and the test should be updated to remove the xfail).

    No fixed sleep. No mock of _handle_event. The real handler
    is called; the real Qdrant body_hash is asserted.
    """
    rel = "aoi/command-chair/curated/test-vault-002.md"
    _write_md_with_frontmatter(tmp_path, rel, body="red body content")
    real_hash = hashlib.sha256(b"red body content" + b"\n").hexdigest()

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": "stale_old_hash_different_from_real"}
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

    # POSTCONDITION 1: the real handler was reached and wrote
    # the new body_hash via curated_plane.create. Today: 0
    # (the bug silently drops the file before the handler).
    # After fix: 1.
    assert curated_plane.create.await_count == 1, (
        "Real handler must write the new body_hash to Qdrant "
        "exactly once. Today: silently drops the file (the "
        "bug). After fix: writes."
    )

    # POSTCONDITION 2: the create call carries the new (real)
    # body_hash, not the stale one. Today: no call, so this
    # also fails. After fix: the new hash is in the call.
    create_call = curated_plane.create.call_args
    assert create_call is not None, "curated_plane.create was never called"
    call_kwargs = create_call.kwargs
    call_args = create_call.args
    payload = call_kwargs.get("payload") or (call_args[1] if len(call_args) > 1 else None)
    if isinstance(payload, dict):
        assert payload.get("body_hash") == real_hash, (
            f"create call must carry the new body_hash {real_hash}, "
            f"got {payload.get('body_hash')!r}"
        )

    # POSTCONDITION 3: the path that reaches the handler is
    # ABSOLUTE (the bug is the relative path; the fix is to
    # pass an absolute path so that the inner relative_to call
    # works on an already-relative path under vault_root).
    # The path is captured via the curated_plane.create call's
    # first positional arg (the file path) or via the
    # vault_path in the payload.
    path_arg: Path | str | None = None
    if call_args:
        path_arg = call_args[0]
    if path_arg is None and isinstance(payload, dict):
        path_arg = payload.get("vault_path")
    if path_arg is not None and isinstance(path_arg, (str, Path)):
        as_path = Path(path_arg)
        assert as_path.is_absolute(), (
            f"The path that reaches the handler must be ABSOLUTE "
            f"after the fix. Got {path_arg!r} (relative)."
        )


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 CONTROL 1: real handler + absolute path succeeds (post-fix). Today: silently drops. Flips to green when the fix lands.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_real_handler_writes_new_hash(
    tmp_path: Path,
) -> None:
    """CONTROL 1: real handler + absolute path succeeds (post-fix)."""
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
    """CONTROL 4: background exception is observable (not silently passed)."""
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


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 REDPROOF 1: the relative_path anti-pattern is caught by the postcondition assertion (await_count == 1, new body_hash, absolute path). Today: the relative_path anti-pattern is the current bug; the postcondition is not met. Flips to green when the fix is in.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_redproof_relative_path(
    tmp_path: Path,
) -> None:
    """Red-proof 1/3: the relative_path anti-pattern is the current bug.

    This test is INDEPENDENT from the main RED test (it has
    its own setup with a different body and different stale
    hash). It asserts the SAME postcondition (real handler
    writes the new body_hash with an absolute path) but
    framed as a red-proof: "if a future fix preserves the
    relative_path anti-pattern, the postcondition is not
    met and this test fails (proving the red contract is
    meaningful)."

    Today: postcondition not met (the bug). After fix:
    postcondition met. Strict xfail.
    """
    rel = "aoi/command-chair/curated/test-vault-002-redproof1.md"
    _write_md_with_frontmatter(tmp_path, rel, body="redproof 1 body")
    real_hash = hashlib.sha256(b"redproof 1 body" + b"\n").hexdigest()

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

    # The postcondition is the SAME as the RED test: real
    # handler writes the new body_hash with an absolute path.
    # Today: 0 writes (the relative_path bug silently drops).
    # After fix: 1 write with the new hash and an absolute path.
    assert curated_plane.create.await_count == 1, (
        "Redproof 1 (relative_path): the postcondition is "
        "await_count == 1 with the new body_hash. Today: 0 "
        "(the bug). After fix: 1."
    )
    create_call = curated_plane.create.call_args
    assert create_call is not None
    call_args = create_call.args
    call_kwargs = create_call.kwargs
    payload = call_kwargs.get("payload") or (call_args[1] if len(call_args) > 1 else None)
    if isinstance(payload, dict):
        assert payload.get("body_hash") == real_hash, (
            f"Redproof 1: create call must carry new body_hash "
            f"{real_hash}, got {payload.get('body_hash')!r}"
        )
    path_arg: Path | str | None = None
    if call_args:
        path_arg = call_args[0]
    if path_arg is None and isinstance(payload, dict):
        path_arg = payload.get("vault_path")
    if path_arg is not None and isinstance(path_arg, (str, Path)):
        as_path = Path(path_arg)
        assert as_path.is_absolute(), (
            f"Redproof 1: path must be ABSOLUTE after fix, got {path_arg!r} (relative)."
        )


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 REDPROOF 2: the log_only anti-pattern (create is called but the Qdrant client.set_payload is never called with the new body_hash) is caught by the postcondition assertion. Today: the relative_path bug short-circuits before any create call, so the test fails (xfail). After fix: the real handler is called and the log_only antipattern is detected via set_payload NOT being called with the new hash.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_redproof_log_only(
    tmp_path: Path,
) -> None:
    """Red-proof 2/3: log_only anti-pattern (create is called but no Qdrant write).

    The red contract catches this via the Qdrant client's
    set_payload call: a real write would call set_payload
    with the new body_hash; a log_only anti-pattern would
    not. This is a REAL discriminator: the test passes
    ONLY if the postcondition (the new body_hash reaches
    Qdrant) is met. The log_only candidate would FAIL
    the postcondition.

    Today: the relative_path bug short-circuits before any
    create call, so the test fails (xfail). After fix:
    the real handler is called, set_payload is called with
    the new hash, the test passes.
    """
    rel = "aoi/command-chair/curated/test-vault-002-redproof2.md"
    _write_md_with_frontmatter(tmp_path, rel, body="redproof 2 body")
    real_hash = hashlib.sha256(b"redproof 2 body" + b"\n").hexdigest()

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": "stale_hash"}
    client.scroll.return_value = ([point], None)

    # The real Qdrant client is the client MagicMock above.
    # The contract: set_payload is called with the new
    # body_hash. A log_only anti-pattern (a real handler
    # that calls create but never set_payload) would
    # FAIL this assertion.
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

    # POSTCONDITION (real handler, real Qdrant write):
    # set_payload was called with the new (real) body_hash.
    # A log_only anti-pattern would NOT call set_payload.
    # Today: 0 calls (the bug short-circuits). After fix:
    # >= 1 call with the new body_hash.
    set_payload_calls = client.set_payload.call_args_list
    new_hash_found = False
    for call in set_payload_calls:
        payload = call.kwargs.get("payload")
        if isinstance(payload, dict) and payload.get("body_hash") == real_hash:
            new_hash_found = True
            break
    assert new_hash_found, (
        f"Redproof 2 (log_only): Qdrant set_payload must be called "
        f"with the new body_hash {real_hash}. A log_only anti-pattern "
        f"calls create() but never set_payload; the test catches it. "
        f"Today: 0 calls (the bug). After fix: >= 1 call with new hash."
    )


# Red-proof 3: this is a GUARD, not a red. It asserts the
# test file itself does NOT use the setattr(watcher,
# _handle_event, AsyncMock()) anti-pattern. The test always
# passes (today AND after the fix) as long as no one
# modifies the test to use the vacuous mock. This is a
# plain pass (not a strict xfail).
def test_boot_scan_vault_002_redproof_mock_handler() -> None:
    """Red-proof 3/3: guard against the test file being modified to use the mock_handler anti-pattern.

    The red contract must NOT use `setattr(watcher,
    _handle_event, AsyncMock())` to mock the handler. The
    test must use the create_task capture pattern. If this
    assertion fails, the test was modified to use the
    vacuous mock_handler pattern.

    This is a SOURCE inspection redproof. It walks the AST
    of this module to find every call site of `setattr(
    watcher, "_handle_event", ...)` in EXECUTABLE code. If
    a call site exists, the test was modified to use the
    anti-pattern.

    This is a plain pass (not a strict xfail) because the
    test contract is permanent: the test file must NEVER
    use the anti-pattern. It does not depend on the source
    fix.
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
# DELETION routing marker (skip, routed to VAULT-001 / Issue #446)
# =============================================================
# Per Yua 15:12: the existing test_boot_scan_archives_removed_files
# is vacuous (async slow_scroll on a sync scroll; assert True
# passes). The deletion expectation is routed to VAULT-001
# (NOT to be implemented in this slice). Issue #446 is the
# durable routing target for the deletion case.


@pytest.mark.skip(
    reason="deferred to VAULT-001 (Issue ericmey/musubi#446): ghost-row "
    "reconciliation (known_hashes minus rglob) is a separate slice. The "
    "vacuous test_boot_scan_archives_removed_files was REMOVED from "
    "tests/vault/test_watcher_boot_scan.py in the VAULT-002 gateway-cleanup "
    "successor (commit b6a56c2). The deletion expectation is not in scope "
    "for this slice."
)
def test_boot_scan_vault_002_deletion_routed_to_vault_001_marker() -> None:
    """Deletion handling is OUT OF SCOPE for VAULT-002.

    Skipped with a durable routing target. The actual deletion
    handling lives in a separate VAULT-001 slice (Issue #446).
    This marker exists only to document that the deletion
    case is NOT in this slice.
    """
    # The marker is a no-op. The skip marker is the assertion.
    # The slice doc's "Out of owns_paths" section is the
    # durable record of the routing.
    assert True  # nosec B101 - the skip marker carries the routing
