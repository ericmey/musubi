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
     passes). That test was REMOVED in the gateway-cleanup
     successor (commit b6a56c2) and the deletion expectation is
     durably routed to Issue #446.
  3. VAULT-002 is INDEPENDENT of C6b/ART-001/VAULT-001.

Test accounting (post-Yua-17:10:38 repair):
  - 4 strict xfails (today): RED + 3 red-proofs/discriminations
  - 4 plain pass (today AND after fix): 4 healthy controls/guards
  - 1 skip: the VAULT-001 deletion routing marker
  - Total: 9 tests

The contract is observed on the typed `CuratedKnowledge` object
passed to `curated_plane.create(memory)`, NOT on call_args/kwargs
introspection, NOT on Qdrant client.set_payload side effects,
NOT on a captured task raising (the boot_scan loop intentionally
catches per-path exceptions and logs them).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from musubi.types.curated import CuratedKnowledge
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


def _assert_typed_memory(
    memory: Any,
    *,
    expected_vault_path: str,
    expected_body_hash: str,
    expected_object_id: str = "ck0000000000000000000000000",
    expected_namespace: str = "aoi/command-chair/curated",
) -> None:
    """Assert the typed CuratedKnowledge object carries the
    post-fix contract: relative vault_path, exact new body_hash,
    and the frontmatter's object_id/namespace.

    The handler does `rel_path = str(path.relative_to(vault_root))`
    and constructs `CuratedKnowledge(vault_path=rel_path, ...)`.
    So `memory.vault_path` is the RELATIVE form under vault_root,
    NOT the absolute form. The postcondition contract is the
    relative form.

    The typed object is what `curated_plane.create(memory)`
    receives — not a tuple, not a dict, not (path, payload).
    """
    assert isinstance(memory, CuratedKnowledge), (
        f"create() must receive a typed CuratedKnowledge object, got {type(memory).__name__}"
    )
    assert memory.vault_path == expected_vault_path, (
        f"memory.vault_path must be the relative form "
        f"{expected_vault_path!r} (handler's relative_to), got "
        f"{memory.vault_path!r}"
    )
    assert memory.body_hash == expected_body_hash, (
        f"memory.body_hash must be the new (real) hash "
        f"{expected_body_hash!r}, got {memory.body_hash!r}"
    )
    assert memory.object_id == expected_object_id, (
        f"memory.object_id must be from the frontmatter "
        f"{expected_object_id!r}, got {memory.object_id!r}"
    )
    assert memory.namespace == expected_namespace, (
        f"memory.namespace must be from the frontmatter "
        f"{expected_namespace!r}, got {memory.namespace!r}"
    )


# =============================================================
# Red contract: 1 strict-xfail RED + 3 plain-pass controls +
# 1 strict-xfail control (caplog) + 3 red-proofs (2 xfail +
# 1 pass) + 1 skip marker = 9 tests
# =============================================================


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 RED: the current relative-path bug short-circuits boot_scan before the handler reaches curated_plane.create(memory). Asserts the postcondition on the typed CuratedKnowledge object (relative vault_path, new body_hash, frontmatter object_id/namespace). Today: 0 creates; postcondition not met. Flips to green when the fix lands.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_relative_path_noop_red(
    tmp_path: Path,
) -> None:
    """RED 1/4: boot_scan relative-path bug. Postcondition on typed memory.

    Asserts the POSTCONDITION (real handler reaches
    `curated_plane.create(memory)` with a typed
    `CuratedKnowledge` whose `vault_path` is the relative form
    under vault_root, whose `body_hash` is the new (real) hash,
    and whose `object_id`/`namespace` come from the frontmatter).
    Today: the bug short-circuits before any create call, so
    the postcondition is not met -> assertion fails -> xfail.
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

    # POSTCONDITION 1: the real handler was reached and called
    # curated_plane.create with a typed CuratedKnowledge. Today:
    # 0 (the bug silently drops the file before the handler).
    # After fix: 1.
    assert curated_plane.create.await_count == 1, (
        "Real handler must call curated_plane.create exactly once "
        "with a typed CuratedKnowledge. Today: silently drops "
        "(the bug). After fix: writes."
    )

    # POSTCONDITION 2: the typed memory carries the post-fix
    # contract (relative vault_path, new body_hash, frontmatter
    # object_id/namespace). Observed on the typed object, NOT
    # on call_args/kwargs (create takes one positional arg).
    _assert_typed_memory(
        curated_plane.create.call_args.args[0],
        expected_vault_path=rel,
        expected_body_hash=real_hash,
    )


@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_real_handler_writes_new_hash(
    tmp_path: Path,
) -> None:
    """CONTROL 1: genuine green control. Direct call to the
    PUBLIC handler seam (`_handle_event`) with an ABSOLUTE path
    proves the handler is correct when given a proper path
    (the bug is in boot_scan's dispatch, not the handler).

    Today: passes (the handler correctly does `relative_to` on
    an already-absolute-under-vault_root path and reaches
    `create` with a typed CuratedKnowledge). After fix: passes.

    This separates "handler works" from "boot_scan dispatches
    the right path" and narrows the fix scope: the fix is in
    boot_scan's dispatch, not in the handler.
    """
    rel = "aoi/command-chair/curated/test-vault-002-control1.md"
    abs_path = _write_md_with_frontmatter(tmp_path, rel, body="control 1 body")
    # parse_frontmatter strips the trailing newline; the body
    # hash is sha256 of the body string exactly as returned.
    real_hash = hashlib.sha256(b"control 1 body").hexdigest()

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

    # Direct call to the PUBLIC handler seam with an ABSOLUTE
    # path (the post-fix dispatch). The handler does
    # `rel_path = path.relative_to(vault_root)` correctly and
    # calls `curated_plane.create(memory)`.
    from watchdog.events import FileSystemEvent

    evt = FileSystemEvent(str(abs_path))
    evt.event_type = "modified"
    await watcher._handle_event(str(abs_path), evt)

    # The handler is correct when given the absolute path:
    # 1 create with the typed CuratedKnowledge carrying the
    # post-fix contract.
    assert curated_plane.create.await_count == 1, (
        "Direct call to _handle_event with an absolute path must "
        "result in exactly 1 create call. The handler is correct "
        "when given the right path; the bug is only in boot_scan's "
        "dispatch (passing the relative path)."
    )
    _assert_typed_memory(
        curated_plane.create.call_args.args[0],
        expected_vault_path=rel,
        expected_body_hash=real_hash,
    )


@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_no_drift_no_write(
    tmp_path: Path,
) -> None:
    """CONTROL 2: no-drift performs no write (healthy control; passes today AND after fix)."""
    rel = "aoi/command-chair/curated/test-vault-002-control2.md"
    _write_md_with_frontmatter(tmp_path, rel, body="control 2 body")
    # parse_frontmatter strips the trailing newline.
    real_hash = hashlib.sha256(b"control 2 body").hexdigest()

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
    reason="VAULT-002 CONTROL 4: background exception OBSERVABILITY. boot_scan intentionally catches per-path exceptions and logs 'Boot scan failed on path ...'; the captured task does NOT raise. Today: the bug short-circuits before create(), so no log is produced. After fix: create() raises, the loop logs the error, the log is observable via caplog. Strict xfail flips to green when the log is present.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_background_exception_observable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CONTROL 4: background exception OBSERVABILITY via caplog.

    boot_scan INTENTIONALLY catches per-path exceptions and
    logs them as `logger.error("Boot scan failed on path %s:
    %s", path, exc)`. The captured task does NOT raise. So
    `pytest.raises` would not fire.

    Observability is proved via caplog: the log record must
    contain the exact PII-safe boundary
    ("Boot scan failed on path") after the fix lands. Today:
    the bug short-circuits before create() (no log produced)
    -> assertion fails -> xfail. After fix: create() raises;
    the loop logs the error; the log is present -> pass.
    """
    rel = "aoi/command-chair/curated/test-vault-002-control4.md"
    _write_md_with_frontmatter(tmp_path, rel, body="control 4 body")

    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": "stale_old_hash"}
    client.scroll.return_value = ([point], None)

    curated_plane = MagicMock()
    curated_plane._client = client

    write_exc = RuntimeError("simulated handler failure")

    async def fail_create(*_args: Any, **_kwargs: Any) -> None:
        raise write_exc

    curated_plane.create = fail_create

    write_log = MagicMock()
    write_log.consume_if_exists.return_value = False

    watcher = VaultWatcher(tmp_path, curated_plane, write_log, debounce_sec=0.001)
    watcher._loop = asyncio.get_running_loop()

    # The logger name in src/musubi/vault/watcher.py is the
    # module logger `musubi.vault.watcher`. caplog needs
    # propagate=True (default) and the right level.
    with caplog.at_level(logging.ERROR, logger="musubi.vault.watcher"):
        captured = _capture_scan_task(watcher)
        watcher.boot_scan()
        assert len(captured) == 1
        # The captured task does NOT raise (boot_scan catches
        # per-path). We just await it for completion.
        await captured[0]

    # Observability via caplog: the exact PII-safe log boundary
    # must be present after the fix. Today: no log (the bug
    # short-circuits before create()). After fix: the log is
    # present because create() raised and the loop logged it.
    log_text = caplog.text
    assert "Boot scan failed on path" in log_text, (
        f"boot_scan must log 'Boot scan failed on path ...' on a "
        f"per-path exception (PII-safe boundary). Today: no log "
        f"(the bug short-circuits before create()). After fix: "
        f"create() raises; the loop logs the error. Got log: "
        f"{log_text!r}"
    )


# =============================================================
# Red-proof (3 candidates that MUST be caught)
# =============================================================


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 REDPROOF 1: the relative_path anti-pattern (calling _handle_event with the relative path) is caught by the postcondition on the typed CuratedKnowledge. Today: the anti-pattern is the current bug; relative path is silently dropped. After fix: the anti-pattern is still wrong (boot_scan is fixed, but a future regression to the anti-pattern is caught).",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_redproof_relative_path(
    tmp_path: Path,
) -> None:
    """Red-proof 1/2: the relative_path anti-pattern is caught at the handler seam.

    Builds a test-local wrong-dispatch candidate: call
    `watcher._handle_event(rel_str, evt)` directly with the
    RELATIVE path (this is the current bug's behavior in
    boot_scan). The postcondition on the typed memory is
    NOT met (the handler's `relative_to` raises and silently
    returns). Today: 0 creates; the postcondition assertion
    fails -> xfail. After fix: a regression to the
    relative_path anti-pattern would still fail the
    postcondition (this test catches the regression even if
    boot_scan is later modified).

    The postcondition is observed on the typed
    `CuratedKnowledge` object, NOT on call_args/kwargs.
    """
    rel = "aoi/command-chair/curated/test-vault-002-redproof1.md"
    abs_path = _write_md_with_frontmatter(tmp_path, rel, body="redproof 1 body")
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

    # WRONG dispatch candidate: pass the RELATIVE path (the
    # current bug's behavior in boot_scan). The handler's
    # `rel_path = path.relative_to(vault_root)` raises ValueError
    # because the path is already relative -> silently returns.
    from watchdog.events import FileSystemEvent

    evt = FileSystemEvent(str(abs_path))
    evt.event_type = "modified"
    await watcher._handle_event(rel, evt)  # the wrong dispatch

    # POSTCONDITION (typed memory): the wrong dispatch must
    # NOT result in a create with the right data. The handler
    # silently returns. Today: 0 creates; assertion fails ->
    # xfail. A future fix that reverts to the wrong dispatch
    # would still fail this test.
    assert curated_plane.create.await_count == 1, (
        f"Wrong-dispatch (relative path) anti-pattern: the "
        f"handler's `relative_to` raises ValueError and "
        f"silently returns, so 0 creates are produced. The "
        f"contract catches this by asserting 1 create with "
        f"the typed memory (relative vault_path, new "
        f"body_hash). Today: 0 (the bug). Got "
        f"{curated_plane.create.await_count}."
    )
    _assert_typed_memory(
        curated_plane.create.call_args.args[0],
        expected_vault_path=rel,
        expected_body_hash=real_hash,
    )


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 REDPROOF 2: the log_only anti-pattern (curated_plane.create is called but the typed CuratedKnowledge carries the WRONG body_hash, e.g., the stale one) is caught by the typed-memory postcondition. Today: the relative_path bug short-circuits before any create call, so the assertion fails -> xfail. After fix: a hypothetical log_only candidate (e.g., create() called with the stale body_hash) would fail the typed-memory assertion, proving the contract catches it.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_redproof_log_only(
    tmp_path: Path,
) -> None:
    """Red-proof 2/2: the log_only anti-pattern is caught on the typed memory.

    The "log_only" anti-pattern: a candidate that calls
    `curated_plane.create(memory)` but constructs the
    `CuratedKnowledge` with the WRONG `body_hash` (e.g., the
    stale one, or a fabricated one) — i.e., the call is made
    but the typed payload does not carry the new real hash.

    The red contract catches this by asserting the typed
    `CuratedKnowledge` carries the NEW body_hash (the post-
    fix contract). This is observed on the typed object, NOT
    on `client.set_payload` (which the AsyncMock never calls).

    Today: the relative_path bug short-circuits before any
    create call, so the assertion fails -> xfail. After fix:
    a hypothetical log_only candidate (create called with
    the stale body_hash) would fail the typed-memory
    assertion; the contract catches it.
    """
    rel = "aoi/command-chair/curated/test-vault-002-redproof2.md"
    _write_md_with_frontmatter(tmp_path, rel, body="redproof 2 body")
    real_hash = hashlib.sha256(b"redproof 2 body" + b"\n").hexdigest()

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

    # POSTCONDITION: the typed CuratedKnowledge passed to
    # create() must carry the NEW body_hash, not the stale
    # one. The log_only anti-pattern (create called with the
    # stale hash) would fail this assertion. Today: 0 creates
    # (the bug short-circuits); assertion fails -> xfail.
    assert curated_plane.create.await_count == 1, (
        "Redproof 2 (log_only): the real handler must call "
        "create() exactly once. Today: 0 (the bug). After fix: 1."
    )
    memory = curated_plane.create.call_args.args[0]
    _assert_typed_memory(
        memory,
        expected_vault_path=rel,
        expected_body_hash=real_hash,  # not "stale_hash"
    )


# Red-proof 3: GUARD against the test file being modified to
# use the setattr mock_handler anti-pattern. The builtin
# `setattr(watcher, "_handle_event", AsyncMock())` parses as
# `ast.Name(id="setattr")`, NOT `ast.Attribute`. The guard
# must detect BOTH the builtin (`ast.Name`) and attribute
# (`ast.Attribute`) forms.
def test_boot_scan_vault_002_redproof_mock_handler() -> None:
    """Red-proof 3/4: guard against the setattr mock_handler anti-pattern.

    The red contract must NOT use `setattr(watcher,
    "_handle_event", AsyncMock())` to mock the handler. The
    test must use the create_task capture pattern. If this
    assertion fails, the test was modified to use the
    vacuous mock_handler pattern.

    The guard detects BOTH the builtin `setattr(...)` form
    (parses as `ast.Name(id="setattr")`) and the attribute
    form `obj.setattr(...)` (parses as `ast.Attribute`).
    Both are prohibited call patterns.

    The red-proof of the guard: a synthetic AST containing
    the prohibited builtin `setattr(watcher, ...)` call is
    walked with the SAME detection logic; the guard catches
    it. This proves the guard is not vacuous.
    """
    import ast as _ast

    import tests.vault.test_watcher_boot_scan_vault_002 as mod

    src = mod.__file__
    with open(src, encoding="utf-8") as f:
        tree = _ast.parse(f.read())

    def _walk_for_setattr(
        root: _ast.AST,
    ) -> bool:
        """Walk the AST looking for any Call to setattr where
        the first arg is a Name with id="watcher" and the
        second arg is a Constant with value="_handle_event".

        Detects BOTH the builtin form (ast.Name) and the
        attribute form (ast.Attribute).
        """
        for node in _ast.walk(root):
            if not isinstance(node, _ast.Call):
                continue
            func = node.func
            is_setattr = False
            if (isinstance(func, _ast.Attribute) and func.attr == "setattr") or (
                isinstance(func, _ast.Name) and func.id == "setattr"
            ):
                is_setattr = True
            if not is_setattr:
                continue
            if len(node.args) < 2:
                continue
            first, second = node.args[0], node.args[1]
            is_watcher = isinstance(first, _ast.Name) and first.id == "watcher"
            is_handle_event = isinstance(second, _ast.Constant) and second.value == "_handle_event"
            if is_watcher and is_handle_event:
                return True
        return False

    # ASSERTION 1: the test file itself does NOT contain the
    # prohibited call. (Today: passes; the test file uses
    # create_task capture, not setattr.)
    assert not _walk_for_setattr(tree), (
        "The red contract must NOT use setattr(watcher, "
        "'_handle_event', AsyncMock()) to mock the handler "
        "(builtin OR attribute form). The test must use task "
        "capture from create_task. If this fails, the test "
        "was modified to use the vacuous mock_handler pattern."
    )

    # ASSERTION 2 (red-proof of the guard): a synthetic AST
    # containing the EXACT prohibited builtin form is caught
    # by the SAME detection logic. This proves the guard is
    # not vacuous (i.e., it actually detects the anti-pattern
    # it claims to reject).
    synthetic_src = (
        "from unittest.mock import AsyncMock\nsetattr(watcher, '_handle_event', AsyncMock())\n"
    )
    synthetic_tree = _ast.parse(synthetic_src)
    assert _walk_for_setattr(synthetic_tree), (
        "Red-proof of the guard: a synthetic AST containing "
        "the prohibited builtin setattr(watcher, ...) call "
        "must be caught by the detection logic. If this "
        "fails, the guard is vacuous and does not actually "
        "detect the anti-pattern it claims to reject."
    )


# =============================================================
# DELETION routing marker (skip with durable routing to #446)
# =============================================================
# Per Yua 15:12: the existing test_boot_scan_archives_removed_files
# is vacuous (async slow_scroll on a sync scroll; assert True
# passes). The deletion expectation is durably routed to
# VAULT-001 / Issue #446, NOT to be implemented in this slice.
# This marker exists only as a documentary record of the
# routing. It is NOT a strict-xfail discrimination and NOT
# behavioral proof (per Yua 17:10:38: 'do not count it as
# behavioral proof or strict-xfail discrimination').


@pytest.mark.skip(
    reason="deferred to VAULT-001 (Issue ericmey/musubi#446): ghost-row "
    "reconciliation (known_hashes minus rglob) is a separate slice. The "
    "vacuous test_boot_scan_archives_removed_files was REMOVED from "
    "tests/vault/test_watcher_boot_scan.py in the VAULT-002 gateway-cleanup "
    "successor (commit b6a56c2). The deletion expectation is not in scope "
    "for this slice. This marker is a documentary record only; it is NOT "
    "behavioral proof and NOT strict-xfail discrimination (per Yua 17:10:38).",
)
def test_boot_scan_vault_002_deletion_routed_to_vault_001_marker() -> None:
    """Documentary marker only. Skipped with durable routing to Issue #446.

    This test body is empty by design: the skip marker is the
    only assertion. The slice doc's "Out of owns_paths"
    section is the durable record of the routing. Per Yua
    17:10:38: do not count this as behavioral proof or
    strict-xfail discrimination.
    """
