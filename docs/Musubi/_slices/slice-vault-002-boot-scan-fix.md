---
title: "Slice: VAULT-002 boot_scan relative path no-op"
slice_id: slice-vault-002-boot-scan-fix
issue: 444
section: _slices
type: slice
status: in-progress
owner: tama
phase: "Retrieval"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-07-13
reviewed: true
depends-on: []
blocks: []
---

# Slice: VAULT-002 boot_scan relative path no-op

> Combined contract+implementation: tests-first red contract for the boot_scan relative-path silent-swallow bug PLUS the accepted source fix (Option A: pass absolute path in boot_scan, commit c0c91ba). Both the red contract and the implementation are in this slice on branch slice/vault-002-boot-scan-fix (PR #445). History is preserved in the commit log; the red-contract work and the source-fix work are additive, not a rewrite. No claim of merge/closure; status remains in-progress until deployment proof and independent sign-off per the final completion gate.

**Phase:** Retrieval · **Status:** `in-progress` · **Owner:** `tama` · **Reviewed by:** Yua (exact-head review and acceptance), Shiori (independent second-read APPROVE)

## Specs to implement

- [[_slices/slice-vault-002-boot-scan-fix|the locked contract for VAULT-002, this slice itself]] (the locked evidence doc lives at `../../projects/active/hermes-musubi-provider/artifacts/vault-002-boot-scan-noop` in the harem-ops vault; same-shape, same-line as the harem-ops slice)

## Owned paths

- `tests/vault/test_watcher_boot_scan_vault_002.py` (red contract test, tests only; no src/ changes)
- `tests/vault/test_watcher_boot_scan.py` (transfer of ownership from `slice-ops-hardening-suite` per Yua 17:54:34: the dispatch-shape expectation in `test_boot_scan_detects_body_hash_change` must match the accepted source fix (c0c91ba) which passes `str(path)` (the ABSOLUTE in-root path) instead of the relative path string; also prefers deterministic task completion over fixed `asyncio.sleep(0.1)`)
- `docs/Musubi/_slices/slice-vault-002-boot-scan-fix.md`
- `docs/Musubi/_inbox/locks/slice-vault-002-boot-scan-fix.lock`

## Out of owns_paths (intentionally not claimed by this slice)

- The vacuous `test_boot_scan_archives_removed_files` (formerly in `tests/vault/test_watcher_boot_scan.py`) was REMOVED in this slice's hygiene-cleanup successor (per Yua 2026-07-13 18:01:24 WITHHOLD on dbef1a4) because its deletion expectation belongs to VAULT-001, not VAULT-002. The deletion handling is durably routed to **Issue #446** (VAULT-001: ghost rows (known_hashes minus disk) are not reconciled), NOT claimed by this slice. The PR445 marker / earlier b6a56c2 claim that the vacuous test was already removed was FALSE on dbef1a4; the actual removal is in this slice's hygiene-cleanup commit on top of dbef1a4. The slice doc's prose now reflects the exact truth.

## Forbidden paths

- `src/musubi/vault/reconciler.py` (VAULT-001's lane; do NOT conflate)

## Critical corrections (per Yua 2026-07-13 15:12)

1. `boot_scan` iterates `vault_root.rglob("*.md")` (existing disk files only). A deleted known_hash row is NEVER iterated/read — no OSError path on the loop. The ghost row is a separate known_hashes-minus-disk reconciliation problem (VAULT-001 lane, not VAULT-002).
2. The existing `test_boot_scan_archives_removed_files` is vacuous (async `slow_scroll` on a synchronous scroll call; the scan fails internally while `assert True` passes). The test was REMOVED from `tests/vault/test_watcher_boot_scan.py` in this slice's hygiene-cleanup successor on top of dbef1a4 (per Yua 2026-07-13 18:01:24). The deletion expectation is durably routed to VAULT-001 (Issue #446, separate named skip in `test_watcher_boot_scan_vault_002.py`).
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

## Source-level invariant (implementation in c0c91ba; this PR)

The path representation crossing internal component boundaries is normalized by the IMPLEMENTATION in this slice (commit c0c91ba, Option A — the accepted design per Yua 17:43:21). History: the slice was originally tests-first (source forbidden); Yua 17:43:21 granted NARROW source authorization; Option A (boot_scan passes `str(path)` absolute) was ACCEPTED and Option B (handler normalizes) was REJECTED (per Yua 17:54:34, because joining arbitrary relative input to `vault_root` can admit `../` traversal lexically and broadens handler semantics).

- Option A (ACCEPTED; landed at c0c91ba IN THIS PR): `boot_scan` passes `str(path)` (the ABSOLUTE path from rglob) to `_handle_event`. The handler's `path.relative_to(self.vault_root)` succeeds; the file is processed; the typed `CuratedKnowledge` is constructed with the relative `vault_path` (the handler's `rel_path`). The handler's outside-root fail-closed boundary is preserved: an absolute path NOT under `vault_root` causes `relative_to` to raise ValueError, which is silently swallowed by `except ValueError: return` — the outside-root file is NOT processed.
- Option B (REJECTED per Yua 17:54:34): enforce absolute-before-relative_to in `_handle_event_inner` by joining the relative path to `vault_root`. The implementation was REJECTED because joining arbitrary relative input to `vault_root` can admit `../` traversal lexically and broadens handler semantics. The rejected implementation exists on the preserved local branch `slice/vault-002-boot-scan-fix` at commit `1f63692` (kept as evidence per Yua 17:54:34).
- Option C (not implemented): `os.path.relpath` + normalization (no exception swallow).

## Test accounting (post-Yua-17:20:42 repair; post-c0c91ba implementation)

The red contract shape is exactly **9 tests** = 2 source reds (xfail, now flipped to PASS by the c0c91ba implementation) + 6 plain-pass controls/discriminators + 1 documentary skip.

CURRENT STATE (after c0c91ba landed in this PR): 8 passed + 1 skipped = 9 tests. The two source reds (RED, CONTROL 4) have their `@pytest.mark.xfail(strict=True, ...)` markers REMOVED; the tests now pass cleanly. The xfail markers were removed in commit `ee3693e` (the test-update commit that landed with the source fix).

The contract is observed on the typed `CuratedKnowledge` object passed to `curated_plane.create(memory)`, NOT on `call_args`/`kwargs` introspection (create takes one positional arg), NOT on `Qdrant client.set_payload` side effects (which an `AsyncMock` never calls), NOT on a captured task raising (`boot_scan` intentionally catches per-path exceptions and logs them as `Boot scan failed on path ...`). The body_hash is computed by a single shared helper `_read_and_hash_body` that calls the real `parse_frontmatter` (no hand-duplicated parsing semantics).

- **2 source reds (xfail; flip under the minimal path fix)**:
  1. `test_boot_scan_vault_002_relative_path_noop_red` (RED) — calls `boot_scan()`; asserts the postcondition on the typed `CuratedKnowledge` (relative `vault_path`, body_hash via the shared helper, frontmatter `object_id`/`namespace`); today: 0 creates (the bug short-circuits before the handler); assertion fails → xfail. After fix: 1 create with the right typed memory; passes.
  2. `test_boot_scan_vault_002_control_background_exception_observable` (CONTROL 4) — observability via `caplog` at logger `musubi.vault.watcher` level=ERROR; today: no log (the bug short-circuits before `create()`); assertion fails → xfail. After fix: `create()` raises; the loop logs the error; the log "Boot scan failed on path" is present; passes. NOTE: `pytest.raises` is NOT used because `boot_scan` catches per-path exceptions and the captured task does not raise.
- **6 plain-pass (today AND after fix)**:
  1. `test_boot_scan_vault_002_control_real_handler_writes_new_hash` (CONTROL 1) — GENUINE GREEN: direct `await watcher._handle_event(abs, evt)` with an ABSOLUTE path bypasses the boot_scan dispatch bug. Separates "handler works" from "boot_scan dispatches the wrong path".
  2. `test_boot_scan_vault_002_control_no_drift_no_write` (CONTROL 2) — no-drift produces no write.
  3. `test_boot_scan_vault_002_control_outside_root_skipped` (CONTROL 3) — outside-root path is skipped by rglob.
  4. `test_boot_scan_vault_002_redproof_relative_path` (REDPROOF 1) — plain-pass discriminator with TWO isolated watchers: WRONG dispatch (relative path) → postcondition helper raises AssertionError on missing typed memory; CORRECT dispatch (absolute path) → postcondition helper passes. Same helper, both candidates.
  5. `test_boot_scan_vault_002_redproof_log_only` (REDPROOF 2) — plain-pass candidate proof: instantiates a WRONG candidate via `correct_memory.model_copy(update={"body_hash": "stale_hash"})` (no speculation); same postcondition helper rejects the wrong (raises AssertionError on body_hash) and passes the correct.
  6. `test_boot_scan_vault_002_redproof_mock_handler` (REDPROOF 3) — GUARD: AST inspection detects the prohibited `setattr(watcher, "_handle_event", ...)` call in BOTH the builtin form (`ast.Name(id="setattr")`) and the attribute form (`ast.Attribute.attr == "setattr"`); red-proofs the guard with a synthetic AST containing the exact prohibited call.
- **1 skip (documentary marker)**:
  1. `test_boot_scan_vault_002_deletion_routed_to_vault_001_marker` — body is empty (no `assert True`); skip with durable routing to Issue #446. NOT a behavioral proof and NOT strict-xfail discrimination (per Yua 17:10:38).

## Source of truth

- `../../projects/active/hermes-musubi-provider/artifacts/vault-002-boot-scan-noop.md` (locked evidence doc, harem-ops; same-shape, same-line)
- The bug at `src/musubi/vault/watcher.py:386, 390, 396, 230-232` is the relative path silently swallowed by `except ValueError: return`.
