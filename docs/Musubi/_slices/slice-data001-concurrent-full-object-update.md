---
owner: claude-code-opus48
status: in-review
issue: 530
title: "Slice: DATA-001 concurrency-safe full-object updates"
slice_id: slice-data001-concurrent-full-object-update
section: _slices
type: slice
phase: "Retrieval"
tags:
  - section/slices
  - status/in-review
  - type/slice
updated: 2026-07-15
reviewed: true
depends-on: [slice-ret008-concurrent-accounting]
blocks: []
---
# Slice: DATA-001 concurrency-safe full-object updates

## Context

RET-008 (#502) made access-writer-vs-access-writer concurrency safe (the fenced access lease) and
preserved the lease-owned access fields across full-point upserts. It did NOT close the broader
race: every full-object UPDATE path reads a whole object and later writes it back, carrying the
read-time snapshot of every field it did not mean to change — so an unrelated concurrent mutation
(or a leased access increment) that lands in the read-to-upsert window is silently overwritten.
This is the documented residual from RET-008 PR #527, tracked here as the DATA-001 P0.

## Invariant (Yua, 2026-07-15)

No full-object update may silently overwrite a concurrent access increment or unrelated field
mutation. Same-field/version conflicts must be explicit and retryable; unrelated-field updates must
compose. `update_vectors` is not filter-fenced, so a payload-CAS loser must never be able to
overwrite a vector; and `version == expected+1` is not attributable (two contenders can propose the
same next version), so the only sound win signal is an exact, unique, never-reused owner token.

## Mechanism — attributable owner-token mutation lease (one internal field)

`store/mutation_lease.py::owned_update` (single-row, two-phase, on the dedicated
`update_lease_token` payload field — distinct from `access_lease_token`, never overloaded):

1. ACQUIRE — write `own:<issued_us>:<nonce>` fenced on the row being at the EXACT read `version`
   AND the token empty, or on the EXACT observed EXPIRED token (crash takeover, never a blind steal).
2. ATTRIBUTE — read back; proceed only if the stored token is our exact token. The only win signal.
3. VECTORS (best-effort, **NOT safe — Phase-2 gap**) — `update_vectors` for the two vector-changing
   paths. `update_vectors` is **unfenceable on the deployed Qdrant (server 1.15 silently ignores
   `update_filter`, verified)**, so a stalled old owner's late write can corrupt a newer committed
   vector. Vector atomicity is explicitly Phase-2 (immutable point + fenced pointer); this path is
   out of Phase-1's safety claim. If plan() raises, the own token is released before re-raising.
4. COMMIT — `set_payload` of ONLY the intended-change fields + `version = read_version+1` +
   `update_lease_token = "done:<issued>:<nonce>"`, fenced on the exact `own` token. Narrow write ⇒
   unrelated fields compose; a same-field conflict retries against the fresh row.
5. ATTRIBUTE — landed **iff our EXACT `done` token is read back** (mirrors `access_lease`);
   `{token==None AND version==read+1}` is NOT attributable — a takeover publishing a different change
   at the same next version would be falsely claimed as ours. Then CLEAR fenced on the exact `done`.
   Bounded retry + jitter; exhaustion raises `MutationLeaseConflict` (typed).

Crash safety (payload): the done-token commit is the single commit point. A crash before it abandons
the whole update (no partial commit); a crash after it (expired `done`) self-heals — the next writer
takes over the exact expired token and applies its change at the next version, never re-applying or
losing the committed change. **Vectors are NOT crash-safe** (Phase-2, above); a crash between the
vector write and the payload commit can leave vectors mismatched with content, and no takeover
repairs it — the reason vector-changing paths are excluded from Phase-1's guarantee.

## Full-point / full-object UPDATE inventory (all routed through the mutation lease)

| Path | Old shape | Vectors |
|---|---|---|
| `EpisodicPlane._reinforce` (dedup-merge) | full-point upsert | yes (new-content only) |
| `EpisodicPlane.batch_create` reinforce branch | full-point upsert | yes (new-content only) |
| `CuratedPlane.create` same-id UPDATE | full-point upsert | yes |
| `EpisodicPlane.patch` | `set_payload` full-minus-lease | no |
| `CuratedPlane.create` supersede old-row | `set_payload` full-minus-lease | no |
| `ConceptPlane.reinforce` / `record_promotion_rejection` | `set_payload` full-minus-lease | no |

CREATE paths (fresh inserts, curated supersede NEW row) keep the full-point upsert — new objects,
no prior row to lose.

## Test Contract (real Qdrant unless noted)
- `test_dedup_merge_upsert_loses_concurrent_unrelated_field_update` — the RED: episodic reinforce
  loses a concurrent unrelated `importance` mutation on the old shape; composes on the new one.
- `test_owned_update_publishes_narrow_change_and_bumps_version` — narrow publish + version bump.
- `test_unrelated_concurrent_field_composes` — an unrelated field set concurrently survives.
- `test_two_writers_same_next_version_both_land_attributably` — two same-next-version contenders both
  land (attributable via the exact token); version==expected+1 is not treated as a win.
- `test_loser_cannot_change_vector` — a writer that loses the owner token never touches the vector.
- `test_expired_owner_token_takeover_recovers` — crash-recovery takeover of an exact expired token.
- `test_skip_plan_is_noop_and_releases` — an idempotent no-op releases without bumping version.
- `test_seam_owned_field_in_changes_is_rejected` — a change-set carrying `version`/token is rejected.
- `test_exhaustion_is_fail_loud` (unit) — bounded exhaustion raises `MutationLeaseConflict`.
- `test_reinforce_composes_concurrent_access_increment` — RET-008 + DATA-001 compose (leased
  access_count preserved, reinforce still lands).
- `test_curated_same_id_update_preserves_concurrent_lineage` — the vault sync takes lineage from the
  FRESH row, so a concurrent `superseded_by` survives.

## Specs to implement
- [[05-retrieval/orchestration]] — § 9 (access accounting + full-object update concurrency; the
  broader read-to-upsert lost-update race left open by RET-008, closed here by the mutation lease).

## Owned paths
- `src/musubi/store/mutation_lease.py`
- `tests/store/test_mutation_lease.py`
- `tests/store/test_data001_full_object_occ.py`
- `docs/Musubi/_slices/slice-data001-concurrent-full-object-update.md`

## Modified (owned by shipped slices)
- `src/musubi/types/base.py` — the single `update_lease_token` nullable field (exclude=True).
- `src/musubi/planes/episodic/plane.py`, `.../curated/plane.py`, `.../concept/plane.py` — every
  full-object UPDATE path routed through `owned_update`; `_upsert`/`_make_point` are CREATE-only.

## Delivery split (Yua, 2026-07-15) — #530 is NOT complete until BOTH phases merge

DATA-001 ships in two phases. **This PR (#539) is Phase 1 only** and `Tracks #530` — it does not
close it.

**Verified Qdrant constraint (real server 1.15):** `update_vectors`' `update_filter` is silently
ignored — a non-matching filter still overwrites the vector — so a **vector write cannot be
token-fenced** on the deployed Qdrant. Therefore an in-place vector update cannot be made
concurrency-safe or crash-atomic, and no `update_vectors`-based protocol (fence / TTL / readback /
vpend) closes it.

**Phase 1 — payload-only mutation safety (this PR):** every full-object UPDATE publishes its
intended fields through the attributable mutation lease's narrow fenced `set_payload`. Unrelated
fields compose; same-field conflicts are explicit + retryable; the narrow write never touches the
RET-008 access fields. The two vector-CHANGING paths (episodic reinforce with new content, curated
same-id body change) keep their current best-effort vector behavior — their vector atomicity is
**explicitly OUT of Phase 1 and unproven**; the module docstring and `owned_update` say so plainly.

**Phase 2 — the completion gate for #530 (separate ADR + slice, not this PR):** immutable new point
for a content/vector change + fenced live-point pointer publication; all reads follow only the
committed pointer; loser cleanup scoped by owner/generation. Required proofs: old-owner-late-write,
crash-before-pointer, crash-after-pointer, concurrent access-lease, no-future-mutation recovery.

## Definition of Done (Phase 1)
- Every full-object payload UPDATE routes through the attributable mutation lease's narrow fenced
  write; unrelated fields compose; same-field conflicts are explicit + retryable; access-lease
  composition preserved.
- Single internal field only (`update_lease_token`, exclude=True); wire/OpenAPI additive.
- `owned_update` is async (non-blocking backoff); skip-release is exact-token-readback verified;
  a vanished row raises `MutationRowVanished` (a `LookupError`).
- The two vector-changing paths' vector atomicity is disclosed as Phase-2 open, NOT claimed safe.
- Full gate green; real-Qdrant proofs green; exact-head CI; independent review; no self-merge.
- `Tracks #530` (NOT `Closes`); #530 stays open until Phase 2 merges.

## Work log

### 2026-07-15 — Phase-1 narrowing + review repairs (Yua #539 review)

Yua's exact-head CI passed but review found a real P0 (my crash-safety docstring claimed a vector
convergence that does not happen) + five bounded items. Repaired under the delivery split:
- **#3** curated same-id update binds one `utc_now()` per plan round (was two calls → skewed
  updated_at/updated_epoch).
- **#5** removed a `_set_token` bare-name no-op in a test.
- **#1** `owned_update` is now `async` (non-blocking `asyncio.sleep` backoff); all six plane call
  sites + `EpisodicPlane._reinforce` (and its `create`/`batch_create` callers) await it.
- **#2** skip-release does an exact-token readback and retries / fails loud — never assumes the
  fenced clear landed.
- **#4** a vanished row raises `MutationRowVanished` (subclass of `LookupError`) instead of returning
  `{}` into a `model_validate({})` crash — preserves each plane's not-found semantics.
- **P0 (#6)** the false crash-convergence docstring is replaced with the honest, verified truth:
  `update_vectors` is unfenceable on server 1.15, so the vector publish is best-effort and its
  atomicity is deferred to Phase 2 (immutable point + fenced pointer). The two vector-changing
  paths keep current behavior; nothing claims them safe. New test
  `test_vanished_row_raises_lookup_error`. Verified: mutation_lease + data001 + access_lease + plane
  + capture suites green; `make check` green.

### 2026-07-15 — done-token attribution repair (Yua #539 review 2)

Review 2 found a real payload-attribution hole: phase 5 attributed on `{token==None AND
version==read+1}`, which is NOT attributable — a takeover that published a different change at the
same next version was falsely claimed as ours, silently losing our change (violating this module's
own stated "version+1 is not attributable" invariant). **Repaired** by mirroring the RET-008 access
lease's two-phase token: commit stamps `done:<issued>:<nonce>` fenced on the exact `own` token; the
EXACT `done` readback is the ONLY success signal; clear is fenced on exact `done`; an expired `done`
(crash-after-commit) self-heals on takeover (the next writer applies its change at the next version,
never re-applying or losing the committed change). Proofs:
`test_stalled_owner_does_not_falsely_attribute_a_takeover_commit` (A-stall/B-takeover discriminator,
verified RED on the old attribution, GREEN now) and
`test_crash_after_done_before_clear_recovers_without_reapply`. Vector paths remain Phase-2 open.


### Out-of-scope: pre-existing `05-retrieval/orchestration` Test Contract bullets

This slice cites `[[05-retrieval/orchestration]]` for the accounting/update seam. That spec's
fast/deep-mode, parallelism, timeout, and determinism bullets are pre-existing gaps owned by the
shipped `slice-retrieval-orchestration` (follow-up **Issue #509**), not introduced or in scope for
DATA-001. Declared out-of-scope so the Closure Rule is honestly machine-green; NOT implemented here:
`test_fast_mode_skips_rerank`, `test_deep_mode_invokes_rerank`, `test_fast_mode_skips_lineage_hydrate`,
`test_deep_mode_hydrates_when_flag_true`, `test_steps_run_in_documented_order`,
`test_planes_run_in_parallel`, `test_hydrate_fetches_run_in_parallel`,
`test_whole_call_timeout_fast_400ms`, `test_per_plane_timeout_deep_1500ms`,
`test_rerank_timeout_returns_with_warning`, `test_deterministic_for_fixed_inputs`,
`test_tiebreak_on_object_id`.

- Built the attributable owner-token mutation lease (`store/mutation_lease.py`) after Yua's
  correction that version-readback is not attributable and `update_vectors` is unfenced. One
  internal field `update_lease_token`, distinct lifecycle from `access_lease_token`.
- Converted all six active full-object UPDATE paths to `owned_update` narrow publishes; `_upsert` /
  `_make_point` are now CREATE-only. RED (`..._loses_concurrent_unrelated_field_update`) verified
  failing on the old shape, green on the new. RET-008 access-lease suite unaffected (11/11).
- Gate: `make check` 2134 passed, mypy clean, ruff clean, coverage ≥85.
