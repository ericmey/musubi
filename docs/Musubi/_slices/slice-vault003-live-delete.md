---
title: "Slice: VAULT-003 live vault delete must archive the curated row (H12 P0)"
slice_id: slice-vault003-live-delete
issue: 552
section: _slices
type: slice
status: in-review
owner: cowork-tama
phase: "5 Vault"
tags: [section/slices, status/in-review, type/slice, phase/5-vault]
updated: 2026-07-15
reviewed: true
depends-on: []
blocks: []
---

# Slice: VAULT-003 live vault delete must archive the curated row (H12 P0)

## What

Closes the live-delete gap (Issue #552, H12 P0 from the 2026-07-12
integrity review). `VaultWatcher._handle_deleted()` in
`src/musubi/vault/watcher.py` is currently a log-only TODO:
the live filesystem delete event is observed but does NOT
archive the matching curated row. VAULT-001 (`#446`, periodic
ghost reconciliation) is closed and does NOT cover this path —
VAULT-001 reconciles during periodic scanning; VAULT-003 covers
the live event from the VaultWatcher's `on_deleted` watchdog hook.

This slice replaces the TODO with a canonical archive transition
through the existing `LifecycleTransitionCoordinator` seam.

## Why

Without VAULT-003 the curated plane silently diverges from the
filesystem on every user delete: the file is gone, the row stays
in `state='matured'`, and default retrieval continues to surface
content whose source file no longer exists. Operators relying on
"default retrieval matches the live vault" experience phantom
results.

The fix is not a raw Qdrant delete or a `set_payload` mutation.
Both bypass the canonical lifecycle path and lose audit / fence /
versioning. The fix routes through `curated_plane.transition(
... to_state='archived', coordinator=...)` — the same path that
maturation, supersession, and demotion already use.

## Contract

### Read-only seam identity

The watcher must NOT scroll Qdrant directly. Identity resolution
from a stored `vault_path` lives on `CuratedPlane` as a typed
public method:

```python
class CuratedPlane:
    async def find_by_vault_path(
        self, vault_path: str
    ) -> CuratedKnowledge | None:
        """Exact-match scroll on payload.vault_path; returns the row or None.

        Uses Qdrant FieldCondition equality, NOT startswith/regex/prefix.
        Sibling and prefix-collision paths cannot match by construction.
        Returns None when no row matches (callers must treat as a
        clean observable no-op, not an error).
        """
```

The watcher's delete handler does:

1. `current = await self.curated_plane.find_by_vault_path(rel_path)`
2. If `current is None`: log at `info` level, return cleanly.
3. Otherwise: `result = await self.curated_plane.transition(
       namespace=current.namespace,
       object_id=current.object_id,
       to_state='archived',
       actor='vault-watcher',
       reason=f'vault file deleted: {rel_path}',
       coordinator=self.coordinator,
   )`
4. Handle the result:
   - `Ok(TransitionFinal | TransitionPending)` — log `info` (success).
   - `Err(TransitionError)` with `code='illegal_transition'` AND
     `to_state='archived'` AND current row already `archived` —
     treat as idempotent success (repeat delete). Log `debug`.
   - Any other `Err(TransitionError)` (including
     `version_fence_violation`, `not_found`, `terminal_apply_failure`,
     `lifecycle_event_write_failed`, `invariant_violation`,
     `missing_reason`, `circular_supersession`, `active_intent_exists`,
     `durable_begin_failed`, `operation_key_conflict`,
     `cap_exceeded`, `maintenance_active`) — log structured warning
     with `code`, `message`, `path`. **Do NOT retry in this handler.**
     A later filesystem event or a periodic reconcile may retry
     naturally; an in-handler retry loop is forbidden because it
     could recurse unboundedly.

### Constructor seam (production-wiring discriminator)

`VaultWatcher.__init__` adds a REQUIRED keyword-only `coordinator`
parameter:

```python
def __init__(
    self,
    vault_root: Path,
    curated_plane: CuratedPlane,
    write_log: WriteLog,
    coordinator: LifecycleTransitionCoordinator,  # NEW — REQUIRED
    debounce_sec: float = 2.0,
    event_rate_per_sec: float = _DEFAULT_EVENT_RATE_PER_SEC,
    indexing_concurrency: int = _DEFAULT_INDEXING_CONCURRENCY,
) -> None: ...
```

No default. A caller that omits the argument fails at the Python
call site (`TypeError: missing 1 required keyword-only argument`)
— there is no silent fallback to a no-op coordinator.

The existing `tests/vault/test_sync.py` watcher fixture is updated
to construct a real `LifecycleTransitionCoordinator` with the
in-memory SQLite path; tests exercise the canonical seam end-to-end.

### Out of scope

- VAULT-001 periodic ghost reconciliation (closed).
- VAULT-002 boot-scan path handling.
- H13 frontmatter fidelity.
- Broad move / rename redesign. If a concrete move ghost is
  discovered during test design, REPORT a separate ticket before
  expanding scope.

## Test Contract (10 bullets, state 1 = passing at handoff)

1. `test_delete_archives_matching_row_via_canonical_transition`
   — RED. Delete resolves to exact stored `vault_path`; the row's
   `state` transitions to `'archived'` through the canonical
   coordinator (NOT raw `set_payload`).
2. `test_archived_row_excluded_from_default_retrieval`
   — RED. Post-archive, the curated default-retrieval query returns
   nothing; the row remains readable by `object_id`.
3. `test_audit_and_history_retain_archived_row`
   — RED. The `lifecycle_events` table contains a row with
   `reason='vault file deleted: ...'`, `to_state='archived'`,
   `actor='vault-watcher'`.
4. `test_repeat_delete_is_idempotent`
   — RED. A second delete on an already-archived row returns
   `illegal_transition` from the canonical state machine; the
   watcher treats that single error code as success (no warning,
   no mutation, no retry).
5. `test_sibling_path_does_not_archive_target`
   — RED. Delete `foo/bar.md`; `foo/bar-2.md` and `foo/bar.md.bak`
   both remain in `matured`. The exact-match lookup excludes both.
6. `test_prefix_collision_does_not_archive`
   — RED. Delete `dir/sub/file.md`; `dir/subfile.md` and
   `dir/sub2/file.md` remain in `matured`.
7. `test_missing_row_is_observable_noop`
   — RED. Delete a path with no curated row; an `info`-level log
   records the path; no mutation, no error, no warning.
8. `test_transition_failure_remains_visible`
   — RED. Coordinator returns `Err(TransitionError(
   code='version_fence_violation'))`; the watcher logs a structured
   `warning` with `code`, `message`, `path`. No retry. No
   success-log. The row's state is unchanged.
9. GREEN preservation guard: existing
   `test_on_created_indexes_new_file` continues to pass.
10. GREEN preservation guard: existing `test_dotfile_ignored`
    continues to pass.

## Specs to implement

- [[06-ingestion/vault-sync#Delete event handling]]
- [[06-ingestion/vault-sync#Identity resolution from stored vault_path]]

## Test Contract (10 bullets, state 1 = passing at handoff)

The VAULT-003 slice narrows the parent slice-vault-sync Test Contract
to the 10 bullets that close the live-delete gap. The 26 parent
bullets outside this scope are unchanged by VAULT-003 and remain the
parent slice's concern; this slice does NOT close them.

1. `test_delete_archives_matching_row_via_canonical_transition`
   — RED. Delete resolves to exact stored `vault_path`; the row's
   `state` transitions to `'archived'` through the canonical
   coordinator (NOT raw `set_payload`).
2. `test_archived_row_excluded_from_default_retrieval`
   — RED. Post-archive, the curated default-retrieval query returns
   nothing; the row remains readable by `object_id`.
3. `test_audit_and_history_retain_archived_row`
   — RED. The `lifecycle_events` table contains a row with
   `reason='vault file deleted: ...'`, `to_state='archived'`,
   `actor='vault-watcher'`.
4. `test_repeat_delete_is_idempotent`
   — RED. A second delete on an already-archived row returns
   `illegal_transition` from the canonical state machine; the
   watcher treats that single error code as success (no warning,
   no mutation, no retry).
5. `test_sibling_path_does_not_archive_target`
   — RED. Delete `foo/bar.md`; `foo/bar-2.md` and `foo/bar.md.bak`
   both remain in `matured`. The exact-match lookup excludes both.
6. `test_prefix_collision_does_not_archive`
   — RED. Delete `dir/sub/file.md`; `dir/subfile.md` and
   `dir/sub2/file.md` remain in `matured`.
7. `test_missing_row_is_observable_noop`
   — RED. Delete a path with no curated row; an `info`-level log
   records the path; no mutation, no error, no warning.
8. `test_transition_failure_remains_visible`
   — RED. Coordinator returns `Err(TransitionError(
   code='version_fence_violation'))`; the watcher logs a structured
   `warning` with `code`, `message`, `path`. No retry. No
   success-log. The row's state is unchanged.
9. GREEN preservation guard: existing
   `test_on_created_indexes_new_file` continues to pass.
10. GREEN preservation guard: existing `test_dotfile_ignored`
    continues to pass.

## Issue #552 assignment path (work-log audit trail)

The GitHub Issue #552 was created by Eric with the
"cowork-tama" assignee (the GraphQL `replaceActorsForAssignable`
error on `minimax-m3` is the same pre-existing failure affecting
Issues #512, #523, #532 — logged as a non-blocking open-defect
on the slice doc; owner frontmatter is the authoritative record).

## Work log

- 2026-07-15 — cowork-tama (claim + wiring checkpoint + RED test
  contract).

### Deferrals (parent slice-vault-sync Test Contract bullets outside VAULT-003 scope)

The parent slice-vault-sync `## Test Contract` covers 33 bullets.
VAULT-003 narrows the live-delete scope; the following parent bullets
remain out of scope for this slice and are tracked in the parent
slice's contract:

- `test_on_deleted_archives_point` — the vacuous parent test was
  removed in this slice; the 8 RED bullets in this slice's Test
  Contract are the discriminating replacement (covers all parent
  concerns: archive-via-canonical, default-retrieval exclusion,
  audit, idempotency, sibling/prefix safety, missing-row no-op,
  transition-failure visibility).
- `test_boot_scan_archives_removed_files` — VAULT-001 periodic
  ghost reconciliation (closed, Issue #446); not the live-delete
  path.
- `test_large_file_body_chunked_as_artifact` — H13 frontmatter
  fidelity; out of VAULT-003 scope.
- `test_large_file_curated_embeds_summary` — H13 frontmatter
  fidelity; out of VAULT-003 scope.