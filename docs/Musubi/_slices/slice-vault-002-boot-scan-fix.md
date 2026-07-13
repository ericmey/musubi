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
- The vacuous `test_boot_scan_archives_removed_files` (formerly in `tests/vault/test_watcher_boot_scan.py`) was REMOVED in the prior commit because its deletion expectation belongs to VAULT-001, not VAULT-002. The deletion handling is routed to a separate VAULT-001 issue / slice.

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

## Acceptance requirement

- The red contract test strict-xfails on the current `main` head (boot_scan relative-path bug).
- The 4 healthy controls pass on the current `main` head.
- The 3 red-proof candidates either strict-xfail or FAIL the test (proving the contract is meaningful).
- The repair of `test_boot_scan_archives_removed_files` routes its deletion expectation to VAULT-001 (separate named xfail/issue only if needed).
- ZERO src/ changes in this PR; src changes land in a separate follow-up slice.
- Lint clean: `ruff check`, `ruff format --check`, `mypy`.
- CI green on the exact head.

## Source of truth

- `../../projects/active/hermes-musubi-provider/artifacts/vault-002-boot-scan-noop.md` (locked evidence doc, harem-ops; same-shape, same-line)
- The bug at `src/musubi/vault/watcher.py:386, 390, 396, 230-232` is the relative path silently swallowed by `except ValueError: return`.
