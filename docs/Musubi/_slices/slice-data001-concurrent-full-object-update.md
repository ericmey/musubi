---
owner: claude-code-opus48
status: in-progress
issue: 530
title: "Slice: DATA-001 concurrency-safe full-object updates"
slice_id: slice-data001-concurrent-full-object-update
section: _slices
type: slice
phase: "Retrieval"
tags:
  - section/slices
  - status/in-progress
  - type/slice
updated: 2026-07-15
reviewed: false
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
3. VECTORS (proven owner only) — `update_vectors` runs inside the held critical section, so a loser
   never reaches it and can never overwrite a vector.
4. PUBLISH — `set_payload` of ONLY the intended-change fields + `version = read_version+1` +
   `update_lease_token = None`, fenced on the exact token. Narrow write ⇒ unrelated fields compose;
   a same-field conflict surfaces as a retry against the fresh row. Single commit point.
5. ATTRIBUTE PUBLISH — landed iff token cleared and version advanced; else retry, never a silent
   overwrite. Bounded retry + jitter; exhaustion raises `MutationLeaseConflict` (typed).

Crash safety: the fenced publish is the only commit point, so a crash before it abandons the whole
update (no partial commit) and the row is taken over on its exact expired token; a crash between the
vector publish and the payload publish leaves vectors ahead of a not-yet-committed payload under a
held token, converged by the next owner re-deriving from the committed content.

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

## Definition of Done
- Every full-object UPDATE routes through the attributable mutation lease; no full-payload bypass.
- Unrelated-field updates compose; same-field conflicts are explicit + retryable; a loser cannot
  change a vector.
- Single internal field only (`update_lease_token`, exclude=True); wire/OpenAPI additive.
- Full gate green; real-Qdrant proofs green; exact-head CI; independent review; no self-merge.

## Work log

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
