---
title: "Slice: VAULT-002 boot_scan relative path no-op"
slice_id: slice-vault-002-boot-scan-fix
issue: 444
section: _slices
type: slice
status: in-progress
owner: unassigned
phase: "Retrieval"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-07-13
reviewed: false
depends-on: []
blocks: []
---

# Slice: VAULT-002 boot_scan relative path no-op

> Tests-first red contract for the boot_scan relative-path silent-swallow bug. Source is forbidden in this slice; the fix lands in a separate follow-up PR after the red contract is approved.

**Phase:** Retrieval · **Status:** `in-progress` · **Owner:** `unassigned`

## Specs to implement

- [[_slices/slice-vault-002-boot-scan-fix|the locked contract for VAULT-002, this slice itself]] (the locked evidence doc lives at `../../projects/active/hermes-musubi-provider/artifacts/vault-002-boot-scan-noop` in the harem-ops vault; same-shape, same-line as the harem-ops slice)

## Owned paths

- `tests/vault/test_watcher_boot_scan_vault_002.py` (red contract test, tests only; no src/ changes)
- `docs/Musubi/_slices/slice-vault-002-boot-scan-fix.md`
- `docs/Musubi/_inbox/locks/slice-vault-002-boot-scan-fix.lock`

## Out of owns_paths (intentionally not claimed by this slice)

- `tests/vault/test_watcher_boot_scan.py` (overlaps with `slice-ops-hardening-suite`; that file is owned by the hardening slice, NOT by this slice; the VAULT-002 red contract adds a new test file, `test_watcher_boot_scan_vault_002.py`, instead of modifying the existing one)
- The vacuous `test_boot_scan_archives_removed_files` (formerly in `tests/vault/test_watcher_boot_scan.py`) was REMOVED in the gateway-cleanup successor (commit b6a56c2) because its deletion expectation belongs to VAULT-001, not VAULT-002. The deletion handling is durably routed to **Issue #446** (VAULT-001: ghost rows (known_hashes minus disk) are not reconciled), NOT claimed by this slice.

## Forbidden paths

- `src/musubi/vault/watcher.py` (the fix lands in a SEPARATE follow-up PR; this slice is tests-only)
- `src/musubi/vault/reconciler.py` (VAULT-001's lane; do NOT conflate)

## Critical corrections (per Yua 2026-07-13 15:12)

1. `boot_scan` iterates `vault_root.rglob("*.md")` (existing disk files only). A deleted known_hash row is NEVER iterated/read — no OSError path on the loop. The ghost row is a separate known_hashes-minus-disk reconciliation problem (VAULT-001 lane, not VAULT-002).
2. The existing `test_boot_scan_archives_removed_files` is vacuous (async `slow_scroll` on a synchronous scroll call; the scan fails internally while `assert True` passes). That test must be REPAIRED with its deletion expectation ROUTED to VAULT-001 (separate named xfail/issue only if needed).
3. VAULT-002 is INDEPENDENT of C6b/ART-001/VAULT-001 — no shared dependency, no shared fix.

## Red contract (via PUBLIC boot_scan, no fixed sleep, no mock of _handle_event)

1. Seed a valid markdown file in `tmp_path` with a real `CuratedFrontmatter` (object_id, namespace, title, state, importance, topics, tags, version, created, updated).
2. Seed an OLD body_hash in the Qdrant in-memory curated plane for the same `vault_path`.
3. Capture the actual created scan task deterministically (via `self._loop.create_task(...)` — do NOT use a fixed `await asyncio.sleep(0.1)`; instead await the captured task's completion via `asyncio.Event` or similar).
4. Call `watcher.boot_scan()` (the PUBLIC entrypoint).
5. Assert that the REAL `_handle_event_inner` was called with the file's actual path.
6. Assert that the new body_hash was written to Qdrant (via `curated_plane.get` or the in-memory client's scroll).
7. CURRENT relative-path behavior: assert that the file is NOT processed (the old body_hash remains in Qdrant; the test strict-xfails until the fix lands).

## Controls (4 healthy controls)

1. **Real handler + absolute path succeeds**: same as red contract, but boot_scan is called with the file as an ABSOLUTE path (the fix). Asserts the file IS processed, new body_hash written.
2. **No-drift performs no write**: seed current body_hash; assert no upsert / no body_hash change.
3. **Outside-root absolute path skipped**: pass a path NOT under vault_root as an absolute path; assert it is skipped (no write).
4. **Background exception is observable**: inject a candidate that raises in the real handler; assert the scan reports the exception (not silently pass).

## Red-proof (3 candidates that MUST be caught)

- `relative_path`: current bug — relative path from boot_scan → _handle_event_inner → relative_to raises ValueError → silently swallowed. Strict-xfails today; the fix flips it to green.
- `log_only`: candidate that LOGS but does not actually write to Qdrant. Caught by the body_hash change assertion.
- `mock_handler`: candidate that uses `setattr(watcher, "_handle_event", AsyncMock())` to mock the handler. The test asserts the REAL handler was called; the mock prevents that; the test FAILS, proving the red contract is meaningful and not vacuous.

## Source-level invariant (lands in the implementation PR, not this red PR)

The path representation crossing internal component boundaries must be normalized:
- Option A: `boot_scan` passes an ABSOLUTE path (or a `Path` object) to `_handle_event` (the verifier checks `is_absolute()` first).
- Option B: `_handle_event_inner` checks `is_absolute()` before calling `relative_to`.
- Option C: `os.path.relpath` + normalization (no exception swallow).

## Test accounting (post-Yua-17:10:38 repair)

The red contract shape is exactly **9 tests** = 4 strict xfails + 4 plain pass + 1 skip.

The contract is observed on the typed `CuratedKnowledge` object
passed to `curated_plane.create(memory)`, NOT on
`call_args`/`kwargs` introspection (create takes one positional
arg), NOT on `Qdrant client.set_payload` side effects (which
an `AsyncMock` never calls), NOT on a captured task raising
(`boot_scan` intentionally catches per-path exceptions and
logs them as `Boot scan failed on path ...`).

- **4 strict xfails (today)**:
  1. `test_boot_scan_vault_002_relative_path_noop_red` (RED) — calls `boot_scan()`; asserts the postcondition on the typed `CuratedKnowledge` (relative `vault_path`, new `body_hash`, frontmatter `object_id`/`namespace`); today: 0 creates (the bug short-circuits before the handler); assertion fails → xfail. After fix: 1 create with the right typed memory; passes.
  2. `test_boot_scan_vault_002_control_background_exception_observable` (CONTROL 4) — observability via `caplog`; today: no log (the bug short-circuits before `create()`); assertion fails → xfail. After fix: `create()` raises; the loop logs the error; the log "Boot scan failed on path" is present; passes. NOTE: `pytest.raises` is NOT used because `boot_scan` catches per-path exceptions and the captured task does not raise.
  3. `test_boot_scan_vault_002_redproof_relative_path` (REDPROOF 1) — wrong-dispatch candidate via `await watcher._handle_event(rel, evt)` directly with the RELATIVE path (the current bug's behavior); asserts the postcondition on the typed memory; today: 0 creates; assertion fails → xfail. After fix: a future regression to the relative_path anti-pattern still fails the postcondition.
  4. `test_boot_scan_vault_002_redproof_log_only` (REDPROOF 2) — log_only anti-pattern: `create()` is called but the typed `CuratedKnowledge` carries the WRONG `body_hash` (e.g., the stale one). The contract catches this by asserting the typed memory's `body_hash == real_hash`. Today: 0 creates (the bug short-circuits); assertion fails → xfail. After fix: a hypothetical log_only candidate would fail the typed-memory assertion.
- **4 plain pass (today AND after fix)**:
  1. `test_boot_scan_vault_002_control_real_handler_writes_new_hash` (CONTROL 1) — GENUINE GREEN CONTROL: direct call to `await watcher._handle_event(abs_path, evt)` with an ABSOLUTE path bypasses the boot_scan dispatch bug. Today: passes (the handler is correct when given the absolute path; the bug is only in boot_scan's dispatch). After fix: passes. This separates "handler works" from "boot_scan dispatches the wrong path" and narrows the fix.
  2. `test_boot_scan_vault_002_control_no_drift_no_write` (CONTROL 2) — no-drift produces no write; passes today (the bug also doesn't write) AND after fix.
  3. `test_boot_scan_vault_002_control_outside_root_skipped` (CONTROL 3) — outside-root path is skipped by rglob; passes today AND after fix.
  4. `test_boot_scan_vault_002_redproof_mock_handler` (REDPROOF 3) — GUARD: AST inspection detects the prohibited `setattr(watcher, "_handle_event", ...)` call in BOTH the builtin form (`ast.Name(id="setattr")`) and the attribute form (`ast.Attribute.attr == "setattr"`); red-proofs the guard with a synthetic AST containing the exact prohibited call. Passes today AND after fix (the test contract is permanent).
- **1 skip (deletion routing marker)**:
  1. `test_boot_scan_vault_002_deletion_routed_to_vault_001_marker` — documentary marker; body is empty (no `assert True`); skip with durable routing to Issue #446. NOT a behavioral proof and NOT strict-xfail discrimination (per Yua 17:10:38).

## Source of truth

- `../../projects/active/hermes-musubi-provider/artifacts/vault-002-boot-scan-noop.md` (locked evidence doc, harem-ops; same-shape, same-line)
- The bug at `src/musubi/vault/watcher.py:386, 390, 396, 230-232` is the relative path silently swallowed by `except ValueError: return`.
