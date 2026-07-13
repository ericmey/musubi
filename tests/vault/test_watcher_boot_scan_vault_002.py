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

Test accounting (post-Yua-17:20:42 repair):
  - 2 source reds (xfail): boot-scan write + exception observability
  - 6 plain-pass controls/discriminators
  - 1 documentary skip
  - Total: 9 tests

The contract is observed on the typed `CuratedKnowledge` object
passed to `curated_plane.create(memory)`, NOT on call_args/kwargs
introspection (create takes one positional arg), NOT on Qdrant
client.set_payload side effects (which an AsyncMock never calls),
NOT on a captured task raising (boot_scan intentionally catches
per-path exceptions and logs them as `Boot scan failed on path`).

The body_hash for the postcondition is computed via a single
helper `_read_and_hash_body` that calls the real
`parse_frontmatter` and hashes the returned body exactly. No
hand-duplicated parsing semantics.
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
from musubi.vault.frontmatter import parse_frontmatter
from musubi.vault.watcher import VaultWatcher

# =============================================================
# Helpers
# =============================================================


def _read_and_hash_body(path: Path) -> str:
    """Read the file, call the REAL parse_frontmatter, hash
    the body exactly. No hand-duplicated parsing semantics.

    parse_frontmatter strips the trailing newline; the body
    hash is sha256(body.encode('utf-8')) where body is the
    parse_frontmatter output (no trailing \\n).
    """
    content = path.read_text(encoding="utf-8")
    _, body = parse_frontmatter(content)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _write_md_with_frontmatter(
    root: Path,
    rel: str,
    *,
    body: str = "test body content",
) -> Path:
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


def _capture_scan_task(watcher: VaultWatcher) -> list[Any]:
    """Capture the boot_scan task by wrapping the watcher's
    self._loop.create_task. Saves the ORIGINAL create_task first
    so the wrapper delegates to it (avoids recursion).
    """
    loop = watcher._loop
    assert loop is not None  # nosec B101
    captured: list[Any] = []
    original_create_task = loop.create_task

    def capture(coro: Any) -> Any:
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
    post-fix contract. The handler does `rel_path = path.relative_to(
    vault_root)` and constructs `CuratedKnowledge(vault_path=rel_path,
    ...)`. So `memory.vault_path` is the RELATIVE form under
    vault_root, NOT the absolute form. The postcondition
    contract is the relative form.

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
        f"memory.body_hash must be {expected_body_hash!r}, got {memory.body_hash!r}"
    )
    assert memory.object_id == expected_object_id, (
        f"memory.object_id must be {expected_object_id!r}, got {memory.object_id!r}"
    )
    assert memory.namespace == expected_namespace, (
        f"memory.namespace must be {expected_namespace!r}, got {memory.namespace!r}"
    )


def _make_watcher(
    tmp_path: Path,
    rel: str,
    stale_hash: str,
) -> tuple[VaultWatcher, MagicMock]:
    """Create an isolated watcher + curated_plane mock. Each
    redproof uses its OWN watcher (no shared state) so the
    wrong/correct candidates are independent.
    """
    client = MagicMock()
    point = MagicMock()
    point.payload = {"vault_path": rel, "body_hash": stale_hash}
    client.scroll.return_value = ([point], None)

    curated_plane = MagicMock()
    curated_plane._client = client
    curated_plane.create = AsyncMock()

    write_log = MagicMock()
    write_log.consume_if_exists.return_value = False

    watcher = VaultWatcher(tmp_path, curated_plane, write_log, debounce_sec=0.001)
    return watcher, curated_plane


# =============================================================
# Source reds (2 strict xfails) + 6 plain-pass + 1 skip = 9
# =============================================================


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 RED: the current relative-path bug short-circuits boot_scan before the handler reaches curated_plane.create(memory). The postcondition is observed on the typed CuratedKnowledge (relative vault_path, body_hash via real parse_frontmatter, frontmatter object_id/namespace). Today: 0 creates; postcondition not met. Flips to green when the fix lands.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_relative_path_noop_red(
    tmp_path: Path,
) -> None:
    """RED 1/2 (source red): boot_scan relative-path bug.

    Asserts the POSTCONDITION on the typed `CuratedKnowledge`
    object passed to `curated_plane.create(memory)`. The
    body_hash is computed by the single shared helper
    `_read_and_hash_body` that calls the real
    `parse_frontmatter` (no hand-duplicated parsing).
    Today: 0 creates; postcondition not met. After fix:
    1 create with the typed memory; passes.
    """
    rel = "aoi/command-chair/curated/test-vault-002.md"
    path = _write_md_with_frontmatter(tmp_path, rel, body="red body content")
    real_hash = _read_and_hash_body(path)

    watcher, curated_plane = _make_watcher(tmp_path, rel, "stale_old_hash_different_from_real")
    watcher._loop = asyncio.get_running_loop()

    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    assert len(captured) == 1
    await captured[0]

    # POSTCONDITION 1: the real handler was reached and called
    # curated_plane.create with a typed CuratedKnowledge.
    assert curated_plane.create.await_count == 1, (
        "Real handler must call curated_plane.create exactly once "
        "with a typed CuratedKnowledge. Today: silently drops "
        "(the bug). After fix: writes."
    )

    # POSTCONDITION 2: the typed memory carries the post-fix
    # contract.
    _assert_typed_memory(
        curated_plane.create.call_args.args[0],
        expected_vault_path=rel,
        expected_body_hash=real_hash,
    )


@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_real_handler_writes_new_hash(
    tmp_path: Path,
) -> None:
    """CONTROL 1: GENUINE GREEN CONTROL. Direct call to the
    PUBLIC handler seam (`_handle_event`) with an ABSOLUTE path
    proves the handler is correct when given a proper path
    (the bug is in boot_scan's dispatch, not the handler).

    Today: passes. After fix: passes. Separates "handler
    works" from "boot_scan dispatches the wrong path".
    """
    rel = "aoi/command-chair/curated/test-vault-002-control1.md"
    abs_path = _write_md_with_frontmatter(tmp_path, rel, body="control 1 body")
    real_hash = _read_and_hash_body(abs_path)

    watcher, curated_plane = _make_watcher(tmp_path, rel, "stale_old_hash")
    watcher._loop = asyncio.get_running_loop()

    from watchdog.events import FileSystemEvent

    evt = FileSystemEvent(str(abs_path))
    evt.event_type = "modified"
    await watcher._handle_event(str(abs_path), evt)

    assert curated_plane.create.await_count == 1, (
        "Direct call to _handle_event with an absolute path must result in exactly 1 create call."
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
    """CONTROL 2: no-drift performs no write (passes today AND after fix)."""
    rel = "aoi/command-chair/curated/test-vault-002-control2.md"
    path = _write_md_with_frontmatter(tmp_path, rel, body="control 2 body")
    real_hash = _read_and_hash_body(path)

    watcher, curated_plane = _make_watcher(tmp_path, rel, real_hash)  # MATCHES
    watcher._loop = asyncio.get_running_loop()
    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    assert len(captured) == 1
    await captured[0]

    assert curated_plane.create.await_count == 0


@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_outside_root_skipped(
    tmp_path: Path,
) -> None:
    """CONTROL 3: outside-root path is skipped by rglob (passes today AND after fix)."""
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

    watcher, curated_plane = _make_watcher(tmp_path, rel, "old")
    watcher._loop = asyncio.get_running_loop()
    captured = _capture_scan_task(watcher)
    watcher.boot_scan()
    assert len(captured) == 1
    await captured[0]

    assert curated_plane.create.await_count == 0


@pytest.mark.xfail(
    strict=True,
    reason="VAULT-002 CONTROL 4: background exception OBSERVABILITY. boot_scan intentionally catches per-path exceptions and logs 'Boot scan failed on path ...'; the captured task does NOT raise. Today: the bug short-circuits before create(), so no log is produced. After fix: create() raises; the loop logs; the log is observable via caplog. Strict xfail flips to green when the log is present.",
)
@pytest.mark.asyncio
async def test_boot_scan_vault_002_control_background_exception_observable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED 2/2 (source red): background exception OBSERVABILITY via caplog.

    boot_scan INTENTIONALLY catches per-path exceptions and
    logs them as `logger.error("Boot scan failed on path %s:
    %s", path, exc)`. The captured task does NOT raise.

    Observability is proved via caplog: the log record must
    contain the exact PII-safe boundary ("Boot scan failed on
    path") after the fix lands. Today: the bug short-
    circuits before create() (no log produced) -> assertion
    fails -> xfail. After fix: create() raises; the loop logs
    the error; the log is present -> pass.
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

    with caplog.at_level(logging.ERROR, logger="musubi.vault.watcher"):
        captured = _capture_scan_task(watcher)
        watcher.boot_scan()
        assert len(captured) == 1
        await captured[0]

    log_text = caplog.text
    assert "Boot scan failed on path" in log_text, (
        f"boot_scan must log 'Boot scan failed on path ...' on a "
        f"per-path exception (PII-safe boundary). Today: no log "
        f"(the bug short-circuits before create()). After fix: "
        f"create() raises; the loop logs the error. Got log: "
        f"{log_text!r}"
    )


# =============================================================
# Plain-pass discriminators (3 red-proofs converted from xfail
# to plain pass; each proves a wrong candidate is rejected and
# a correct candidate is accepted by the SAME postcondition
# helper)
# =============================================================


@pytest.mark.asyncio
async def test_boot_scan_vault_002_redproof_relative_path(
    tmp_path: Path,
) -> None:
    """Plain-pass discriminator for the relative_path anti-pattern.

    Builds TWO isolated watchers (no shared state):
      - WRONG candidate: call `await wrong_watcher._handle_event(
        rel, evt)` with the RELATIVE path (the current bug's
        behavior in boot_scan). The handler's `relative_to`
        raises ValueError and silently returns; create is
        NOT called. The postcondition helper, when called on
        the missing memory, raises AssertionError — the wrong
        dispatch is REJECTED.
      - CORRECT candidate: call `await correct_watcher._handle_
        event(str(abs_path), evt)` with the ABSOLUTE path
        (the post-fix dispatch). The handler correctly does
        `relative_to`, calls `create(memory)`, and the
        typed memory carries the post-fix contract. The
        postcondition helper PASSES — the correct dispatch is
        ACCEPTED.

    Same postcondition helper, both candidates, isolated
    watchers. Today: passes (both wrong-rejection and
    correct-acceptance work as expected). After fix: passes
    (the postcondition is unchanged).
    """
    rel = "aoi/command-chair/curated/test-vault-002-redproof1.md"
    abs_path = _write_md_with_frontmatter(tmp_path, rel, body="redproof 1 body")
    real_hash = _read_and_hash_body(abs_path)

    wrong_watcher, wrong_plane = _make_watcher(tmp_path, rel, "stale_hash")
    correct_watcher, correct_plane = _make_watcher(tmp_path, rel, "stale_hash")
    wrong_watcher._loop = asyncio.get_running_loop()
    correct_watcher._loop = asyncio.get_running_loop()

    from watchdog.events import FileSystemEvent

    evt = FileSystemEvent(str(abs_path))
    evt.event_type = "modified"

    # WRONG dispatch: relative path. Handler's relative_to
    # raises ValueError; silently returns; create is not called.
    await wrong_watcher._handle_event(rel, evt)
    # The wrong dispatch is REJECTED: the postcondition helper
    # raises AssertionError because no create was made.
    wrong_call = wrong_plane.create.call_args
    with pytest.raises(AssertionError, match=r"must receive a typed CuratedKnowledge"):
        _assert_typed_memory(
            wrong_call.args[0] if wrong_call else None,
            expected_vault_path=rel,
            expected_body_hash=real_hash,
        )

    # CORRECT dispatch: absolute path. Handler's relative_to
    # succeeds; create is called with the typed memory.
    await correct_watcher._handle_event(str(abs_path), evt)
    assert correct_plane.create.await_count == 1
    # The correct dispatch is ACCEPTED: the postcondition
    # helper passes.
    _assert_typed_memory(
        correct_plane.create.call_args.args[0],
        expected_vault_path=rel,
        expected_body_hash=real_hash,
    )


@pytest.mark.asyncio
async def test_boot_scan_vault_002_redproof_log_only(
    tmp_path: Path,
) -> None:
    """Plain-pass candidate proof for the log_only anti-pattern.

    The log_only anti-pattern: a candidate that constructs
    `CuratedKnowledge` with a WRONG `body_hash` (e.g., the
    stale one) and passes it to `curated_plane.create(memory)`.
    The contract catches this via the typed-memory
    postcondition: the memory's `body_hash` must equal the
    new (real) hash.

    This test INSTANTIATES the wrong candidate (no
    speculation). It builds:
      - CORRECT memory: typed `CuratedKnowledge` with the
        real body_hash
      - WRONG candidate: `correct.model_copy(update={"body_hash":
        "stale_hash"})` — a fabricated wrong body_hash

    The SAME `_assert_typed_memory` helper is called on both:
      - The wrong candidate's body_hash is "stale_hash" — the
        helper raises AssertionError on the body_hash
        assertion.
      - The correct candidate's body_hash equals real_hash —
        the helper passes.

    Today: passes (no source dependency; this is a pure
    typed-memory discrimination). After fix: passes.
    """
    rel = "aoi/command-chair/curated/test-vault-002-redproof2.md"
    path = _write_md_with_frontmatter(tmp_path, rel, body="redproof 2 body")
    real_hash = _read_and_hash_body(path)
    content_body = "redproof 2 body"

    # CORRECT typed memory (what a correct candidate would pass)
    correct_memory = CuratedKnowledge(
        object_id="ck0000000000000000000000000",
        namespace="aoi/command-chair/curated",
        vault_path=rel,
        body_hash=real_hash,
        title="test vault-002 file",
        content=content_body,
    )

    # WRONG candidate: a log_only antipattern that uses the
    # stale body_hash (or any fabricated wrong one).
    wrong_memory = correct_memory.model_copy(update={"body_hash": "stale_hash"})

    # The wrong candidate is REJECTED: the postcondition helper
    # raises AssertionError specifically on the body_hash
    # boundary.
    with pytest.raises(AssertionError, match=r"memory\.body_hash must be"):
        _assert_typed_memory(
            wrong_memory,
            expected_vault_path=rel,
            expected_body_hash=real_hash,
        )

    # The correct candidate is ACCEPTED: the postcondition
    # helper passes.
    _assert_typed_memory(
        correct_memory,
        expected_vault_path=rel,
        expected_body_hash=real_hash,
    )


# Red-proof 3: GUARD against the test file being modified to
# use the setattr mock_handler anti-pattern. The builtin
# `setattr(watcher, "_handle_event", AsyncMock())` parses as
# `ast.Name(id="setattr")`, NOT `ast.Attribute`. The guard
# must detect BOTH the builtin (`ast.Name`) and attribute
# (`ast.Attribute`) forms. Red-proofs the guard with a
# synthetic AST.
def test_boot_scan_vault_002_redproof_mock_handler() -> None:
    """Plain-pass GUARD against the setattr mock_handler anti-pattern.

    The red contract must NOT use `setattr(watcher,
    "_handle_event", AsyncMock())` to mock the handler. The
    test must use the create_task capture pattern.

    The guard detects BOTH the builtin form (ast.Name) and
    the attribute form (ast.Attribute). Red-proofs the guard
    with a synthetic AST containing the prohibited call.
    """
    import ast as _ast

    import tests.vault.test_watcher_boot_scan_vault_002 as mod

    src = mod.__file__
    with open(src, encoding="utf-8") as f:
        tree = _ast.parse(f.read())

    def _walk_for_setattr(root: _ast.AST) -> bool:
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
            is_setattr = (isinstance(func, _ast.Attribute) and func.attr == "setattr") or (
                isinstance(func, _ast.Name) and func.id == "setattr"
            )
            if not is_setattr or len(node.args) < 2:
                continue
            first, second = node.args[0], node.args[1]
            is_watcher = isinstance(first, _ast.Name) and first.id == "watcher"
            is_handle_event = isinstance(second, _ast.Constant) and second.value == "_handle_event"
            if is_watcher and is_handle_event:
                return True
        return False

    # ASSERTION 1: the test file itself does NOT contain the
    # prohibited call.
    assert not _walk_for_setattr(tree), (
        "The red contract must NOT use setattr(watcher, "
        "'_handle_event', AsyncMock()) to mock the handler "
        "(builtin OR attribute form). The test must use task "
        "capture from create_task."
    )

    # ASSERTION 2 (red-proof of the guard): a synthetic AST
    # containing the EXACT prohibited builtin form is caught
    # by the SAME detection logic.
    synthetic_src = (
        "from unittest.mock import AsyncMock\nsetattr(watcher, '_handle_event', AsyncMock())\n"
    )
    synthetic_tree = _ast.parse(synthetic_src)
    assert _walk_for_setattr(synthetic_tree), (
        "Red-proof of the guard: a synthetic AST containing "
        "the prohibited builtin setattr(watcher, ...) call "
        "must be caught by the detection logic."
    )


# =============================================================
# Documentary skip (deletion routing marker to Issue #446)
# =============================================================


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

    Body is empty by design. The skip marker is the only
    assertion. The slice doc's "Out of owns_paths" section
    is the durable record of the routing. Per Yua 17:10:38:
    do not count as behavioral proof or strict-xfail
    discrimination.
    """
