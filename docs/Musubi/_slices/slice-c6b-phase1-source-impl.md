---
title: "Slice: C6b Phase-1 source cut (S1-S7 implementation)"
slice_id: slice-c6b-phase1-source-impl
section: _slices
type: slice
status: in-progress
owner: aoi
phase: "Lifecycle 2026-07-14 — C6b Phase-1 source cut S1-S7"
tags: [section/slices, status/in-progress, type/slice, lifecycle, atomicity, source]
updated: 2026-07-14
reviewed: false
depends-on: ["[[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]]"]
blocks: []
issue: 456
---

# Slice: C6b Phase-1 source cut (S1-S7 implementation)

The source implementation that flips the accepted C6b tests-only red contract
([[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]], Issue #437) green,
following the corrected source-commit series in
[[13-decisions/c6b-phase1-source-cut-plan]] §F (S1-S7). Authorized by Yua
(2026-07-14) as a SEPARATE implementation branch/slice, preserving the accepted
contract. **G1 stays strict-RED throughout Phase 1** (flips only under H5,
[[_slices/slice-h5-unify-state-mutation]]). No merge/deploy until independent
review.

## Specs to implement

- [[_slices/slice-c6b-phase1-source-impl]] — this slice's own Test Contract (below): the full accepted
  C6b atomicity contract this source cut flips (`tests/lifecycle/test_c6b_atomicity.py`, 90 functions
  incl. 6 async R21 maturation reds), plus the S1/D0 source proofs it introduced.
- [[08-deployment/compose-stack]] — the compose-stack spec whose Test Contract owns the 8 root-compose
  regression tests that D0's `lifecycle-worker` co-change touches.

## Sequencing

- **Deliverable-0 (this slice, pre-S1, ZERO src):** §E config-drift resolution —
  reconcile the deploy/docs active-storage surfaces to the LOCKED DIR family
  (`/var/lib/musubi/lifecycle/work.sqlite`), and add the root-compose
  `lifecycle-worker` service (parity with the ansible template, §E.1). Flips the
  6 `test_p0c_drift_*` reds + `test_p0c_deployment_active_storage_parity`. The
  root-compose service addition is an authorized narrow co-change to the
  `slice-ops-compose` (`status: done`) service-inventory test — only the expected
  inventory/bind-mount whitelist moves with the new service; unrelated ops tests
  are preserved.
- **S1+ :** shared store/schema+connection owner + connection policy (WAL +
  busy_timeout), then S2-S7 per §F — each the smallest owned-red flip, routed
  for independent review at its exact SHA.

## Owned paths

- `docs/Musubi/_slices/slice-c6b-phase1-source-impl.md`
- `src/musubi/lifecycle`
- `src/musubi/settings.py`
- `src/musubi/api/routers/ops.py`
- `src/musubi/lifecycle/runner.py`

### §F file/function boundary note (S1)

- **`src/musubi/lifecycle/events.py`** (owned by the ACTIVE `slice-c6-lifecycle-event-loss`):
  S1 touches ONLY the `LifecycleEventSink.__init__` connection/schema acquisition (delegate to
  `lifecycle/store.py`, keep `self._conn`, add a backward-compatible `busy_timeout_ms=5000` param).
  **C6 retains exclusive ownership of `record`/`flush`/`close`/`__del__`/durable-accept semantics** —
  S1 does NOT modify them. events.py is NOT claimed as an owned path (that would be a hard both-active
  conflict); this boundary note is the coordination record. No R4 destructor / R6 barrier changes (not S1).
- **`src/musubi/settings.py`** (slice-auth-boundary-phase-a, done — advisory): add the
  `lifecycle_sqlite_busy_timeout_ms` field only.
- **`src/musubi/api/routers/ops.py`** (slice-ops-observability, done — advisory) +
  **`src/musubi/lifecycle/runner.py`** (slice-lifecycle-reflection-builder, done — advisory; also under
  the `src/musubi/lifecycle` dir claim): wire `busy_timeout_ms=settings.lifecycle_sqlite_busy_timeout_ms`
  at the two production composition sites only.
- `docker-compose.yml`
- `.env.example`
- `deploy/docker/.env.production.example`
- `deploy/backup/backup.yml`
- `deploy/backup/README.md`
- `deploy/runbooks/manual-recovery.md`
- `tests/ops/test_compose.py`
- `tests/lifecycle/test_c6b_atomicity.py`
- `tests/lifecycle/test_s1_store_policy.py`
- `tests/lifecycle/test_s2_coordinator_admission.py`
- `tests/lifecycle/test_s3_coordinator_apply.py`
- `tests/ops/test_lifecycle_storage_doc_drift.py`
- `docs/Musubi/08-deployment/compose-stack.md`
- `docs/Musubi/08-deployment/host-profile.md`
- `docs/Musubi/09-operations/runbooks.md`
- `docs/Musubi/09-operations/index.md`
- `docs/Musubi/09-operations/asset-matrix.md`
- `docs/Musubi/09-operations/backup-restore.md`
- `docs/Musubi/10-security/data-handling.md`
- `docs/Musubi/11-migration/phase-2-hybrid-search.md`
- `docs/Musubi/11-migration/re-embedding.md`
- `docs/Musubi/11-migration/phase-6-lifecycle.md`
- `docs/Musubi/_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity.md`

*(The red-contract slice doc is claimed here ONLY for the reciprocal-DAG `blocks` entry
(edit⇒own, AGENTS.md); its test contract + red assertions are untouched. It has no
`## Owned paths` section, so this is a clean first claim. Overlap:
`docker-compose.yml`+`tests/ops/test_compose.py`→`slice-ops-compose` (done);
`.env.example`+`compose-stack.md`→`slice-config` (done); `runbooks.md`→`slice-ops-first-deploy`
(done); `index.md`→`slice-ops-core-image-publish` (done); all other named docs UNOWNED. Every
overlap is with a `done` slice — advisory only, no active-lane conflict.)*

## Red-contract provenance (accepted, immutable) vs successor (mechanical flips)

The **red contract at `c7b95da`** ([[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]],
Issue #437, PR434) is the ACCEPTED, IMMUTABLE red provenance — the 22-red + 3-guard
tests-only contract, Yua + Shiori + Tama approved. This successor owns ONLY the
**mechanical decorator flips** of those reds in `tests/lifecycle/test_c6b_atomicity.py`:
each owned red's strict-xfail marker is removed as source/config makes it pass, and
every assertion body stays **byte-identical** to `c7b95da`. Ownership of the edited
file is claimed here honestly — this slice edits it, so this slice owns it (AGENTS.md:
if you edit a file, own it; do not evade the checker by omission).

Verified before the claim: the red-contract slice+lock never listed this path in an
`## Owned paths` section, so this is a clean first claim, not a contested transfer —
nothing to remove from the red-contract side. Both histories are preserved: `c7b95da`
stays the frozen, accepted contract (PR434 draft), and this branch carries only the
flips. `docker-compose.yml` + `tests/ops/test_compose.py` overlap only with the `done`
`slice-ops-compose` (advisory warning by design; the ops-inventory co-change is
Yua-authorized narrow).

## Test Contract

> Full executable denominator (AST-enumerated, incl. `AsyncFunctionDef`). **116** functions in this
> slice's own contract below + **8** from `08-deployment/compose-stack` = **124** `tc-coverage` bullets,
> 0 missing. Status from runtime collection across the self files (atomicity file **84 passed / 49
> xfailed / 0 failed / 0 XPASS** after the S2+S3 flips + stage guards), NOT from tc-coverage's classifier
> — which mislabels variable-reason strict-xfail reds as "passing". **S2 flipped R2, R11, R14 two-process
> race, `lifecycle_pending_cap`; S3 flips R1, R10, R12, R13, R14-single, R22** — EMPIRICALLY re-derived
> (not the §F paper matrix): R4/R7/R8 reach `reconcile_once` for their crash-recovery/no-resurrection
> halves and stay owed S4. Every other acceptance red stays strict-xfail: a NO-OP-under-candidate stage
> guard (`_require_real_stage`) keeps each owed red raising its OWN DefectStillPresent — not the
> AttributeError/OperationalError a partially-built coordinator would surface (`reconcile_once`=S4,
> `rollback`=S6). **G1 is closure-only and flips ONLY under H5** ([[_slices/slice-h5-unify-state-mutation]]).
> Direct real-source proofs: `tests/lifecycle/test_s2_coordinator_admission.py` (admission layer,
> client-free) + `tests/lifecycle/test_s3_coordinator_apply.py` (full apply/finalize contract, wrong-shape
> discriminators, and the collection->object_type parity). See the **Guard-removal checklist** below.

### Accepted atomicity — R acceptance reds & controls (`test_c6b_atomicity.py`) (46)
1. `test_r1_durable_intent_persisted_before_qdrant_mutation` — GREEN — S3 flip (non-vacuity: requires a real attempted apply)
2. `test_r2_durable_begin_failure_blocks_qdrant_mutation` — GREEN — S2 flip (load-bearing; direct proof in test_s2)
3. `test_r3_transient_failure_is_ok_pending_then_reconciles` — strict-xfail — acceptance red (owed S2-S7)
4. `test_r4_terminal_failure_is_err_abandoned_no_final` — strict-xfail — owed S4 (ABANDONED works; the no-resurrection half calls reconcile_once; reconcile_once guard)
5. `test_r5_crash_after_pending_before_qdrant` — strict-xfail — acceptance red (owed S2-S7)
6. `test_r6_crash_after_qdrant_before_applied` — strict-xfail — acceptance red (owed S2-S7)
7. `test_r7_crash_after_applied_before_finalize` — strict-xfail — owed S4 (crash-after-applied recovery needs reconcile_once; guard)
8. `test_r8_finalize_transaction_is_atomic` — strict-xfail — owed S4 (finalize atomicity works; the reconcile-completes-finalize half needs reconcile_once; guard)
9. `test_r9_idempotent_replay` — strict-xfail — acceptance red (owed S2-S7)
10. `test_r10_operation_key_idempotent_across_caller_retries` — GREEN — S3 flip (idempotency + digest conflict; delimiter fixture repaired to legal matured→demoted)
11. `test_r11_single_active_intent_per_object` — GREEN — S2 flip (import-gate mechanical; behavioral proof in test_s2)
12. `test_r12_hard_version_fence_refuses_stale` — GREEN — S3 flip (server-side version fence)
13. `test_r13_conditional_apply_full_readback_patch_sha` — GREEN — S3 flip (full-readback identity + patch-SHA)
14. `test_r14_hard_pending_cap_admission_backpressure` — GREEN — S3 flip (cap backpressure via full transition)
15. `test_r14_two_process_admission_race_holds_cap` — GREEN — S2 flip (import-gate mechanical; behavioral proof in test_s2)
16. `test_r15_transient_never_abandoned_by_attempt_count` — strict-xfail — acceptance red (owed S2-S7)
17. `test_r16_two_process_claim_race_one_owner` — strict-xfail — acceptance red (owed S2-S7)
18. `test_r16_valid_lease_exclusive_processing` — strict-xfail — acceptance red (owed S2-S7)
19. `test_r17_crash_reclaim_readback_confirms_no_reapply` — strict-xfail — acceptance red (owed S2-S7)
20. `test_r17_expired_owner_reclaim_safe` — strict-xfail — acceptance red (owed S2-S7)
21. `test_r18_no_poison_row_starvation` — strict-xfail — acceptance red (owed S2-S7)
22. `test_r19_pii_free_content_and_bounded_observability` — strict-xfail — acceptance red (owed S2-S7)
23. `test_r20_rollback_refuses_nonterminal_maintenance_lifecycle_and_cleanup` — strict-xfail — acceptance red (owed S2-S7)
24. `test_r20_two_process_admission_drain_barrier_no_overlap` — strict-xfail — acceptance red (owed S2-S7)
25. `test_r20_two_process_reconciler_drain_barrier_no_overlap` — strict-xfail — acceptance red (owed S2-S7)
26. `test_r21_callsite_pending_arm_rule_discriminates` — GREEN — control/discriminator
27. `test_r21_deferred_accounting_check_discriminates` — GREEN — control/discriminator
28. `test_r21_full_defer_acceptance_discriminates` — GREEN — control/discriminator
29. `test_r21_maturation_callsite_inventory_control_sees_exact_six` — GREEN — control/discriminator
30. `test_r21_maturation_callsite_pending_arm_inventory` — strict-xfail — acceptance red (owed S2-S7)
31. `test_r21_maturation_concept_defers_pending` — strict-xfail — acceptance red (owed S2-S7)
32. `test_r21_maturation_concept_demotion_defers_pending` — strict-xfail — acceptance red (owed S2-S7)
33. `test_r21_maturation_episodic_defers_pending` — strict-xfail — acceptance red (owed S2-S7)
34. `test_r21_maturation_episodic_demotion_defers_pending` — strict-xfail — acceptance red (owed S2-S7)
35. `test_r21_maturation_provisional_ttl_defers_pending` — strict-xfail — acceptance red (owed S2-S7)
36. `test_r21_maturation_supersession_backlink_not_run_on_pending` — strict-xfail — acceptance red (owed S2-S7)
37. `test_r21_pending_body_schema_discriminates` — GREEN — control/discriminator
38. `test_r21_route_artifact_pending_maps_to_202` — strict-xfail — acceptance red (owed S2-S7)
39. `test_r21_route_controls_final_200_and_err_typed` — GREEN — route control
40. `test_r21_route_curated_pending_maps_to_202` — strict-xfail — acceptance red (owed S2-S7)
41. `test_r21_route_episodic_pending_maps_to_202` — strict-xfail — acceptance red (owed S2-S7)
42. `test_r21_route_lifecycle_pending_maps_to_202` — strict-xfail — acceptance red (owed S2-S7)
43. `test_r21_route_pending_body_matches_typed_schema` — strict-xfail — acceptance red (owed S2-S7)
44. `test_r21_route_pending_check_discriminates_each_failure_mode` — GREEN — control/discriminator
45. `test_r22_outcome_validator_discriminates` — GREEN — control/discriminator
46. `test_r22_two_process_race_one_winner_mutates_loser_fenced` — GREEN — S3 flip (two-process apply race; deterministic barrier proof in test_s3)

### Accepted atomicity — guards G1/G2a/G2b/G3 (9)
47. `test_g1_no_direct_state_transition_setpayload_outside_coordinator` — strict-xfail — G1 closure-only, flips ONLY under H5
48. `test_g1_present_denominator_control_sees_all_known_bypasses` — GREEN — control/discriminator
49. `test_g1_rule_discriminates_state_dataflow_from_unrelated_payloads` — GREEN — control/discriminator
50. `test_g2a_coordinator_transition_callsite_inventory` — strict-xfail — acceptance red (owed S2-S7)
51. `test_g2a_rule_discriminates_coordinator_callsites` — GREEN — control/discriminator
52. `test_g2b_cleanup_terminal_sql_shape` — strict-xfail — acceptance red (owed S2-S7)
53. `test_g2b_rule_discriminates_cleanup_sql_shape` — GREEN — control/discriminator
54. `test_g3_coordinator_transition_result_consumed` — strict-xfail — acceptance red (owed S2-S7)
55. `test_g3_rule_discriminates_result_consumed` — GREEN — control/discriminator

### Accepted atomicity — P0c wiring/storage/settings (33)
56. `test_p0c_active_storage_parity_rule_discriminates` — GREEN — control/discriminator
57. `test_p0c_anchor_ansible_compose_dir_mount_and_worker` — GREEN — control/discriminator
58. `test_p0c_anchor_ansible_env_production_dir` — GREEN — control/discriminator
59. `test_p0c_anchor_bootstrap_creates_lifecycle_dir_with_musubi_0750` — GREEN — control/discriminator
60. `test_p0c_anchor_live_scheduler_backup_dir` — GREEN — control/discriminator
61. `test_p0c_anchor_restore_yml_dir` — GREEN — control/discriminator
62. `test_p0c_api_and_worker_resolve_same_active_storage_path` — strict-xfail — acceptance red (owed S2-S7)
63. `test_p0c_bootstrap_injection_rule_discriminates` — GREEN — control/discriminator
64. `test_p0c_bootstrap_injects_app_lifetime_coordinator` — strict-xfail — acceptance red (owed S2-S7)
65. `test_p0c_config_surfaces_all_resolve` — GREEN — control/discriminator
66. `test_p0c_connection_policy_rule_discriminates` — GREEN — control/discriminator
67. `test_p0c_deployment_active_storage_parity` — GREEN — D0/S1 flip
68. `test_p0c_drift_backup_readme` — GREEN — D0/S1 flip
69. `test_p0c_drift_backup_yml` — GREEN — D0/S1 flip
70. `test_p0c_drift_docker_env_production_example` — GREEN — D0/S1 flip
71. `test_p0c_drift_env_example` — GREEN — D0/S1 flip
72. `test_p0c_drift_manual_recovery_runbook` — GREEN — D0/S1 flip
73. `test_p0c_drift_parsers_discriminate` — GREEN — control/discriminator
74. `test_p0c_drift_root_compose_dir_mount_and_worker` — GREEN — D0/S1 flip
75. `test_p0c_new_lifecycle_setting_exists_and_validates` — MIXED — busy_timeout (S1) + lifecycle_pending_cap (S2) GREEN; 7 other params strict-xfail (S3-S6)
76. `test_p0c_readiness_probe_rule_discriminates` — GREEN — control/discriminator
77. `test_p0c_reconcile_is_worker_only` — strict-xfail — acceptance red (owed S2-S7)
78. `test_p0c_same_active_storage_rule_discriminates` — GREEN — control/discriminator
79. `test_p0c_settings_validators_discriminate` — GREEN — control/discriminator
80. `test_p0c_shared_file_requires_wal_and_busy_timeout` — GREEN — D0/S1 flip
81. `test_p0c_storage_migration_contract_red_proof` — GREEN — migration contract red-proof control
82. `test_p0c_storage_migration_task_detection_discriminates` — GREEN — control/discriminator
83. `test_p0c_storage_migration_task_unbuilt` — strict-xfail — acceptance red (owed S2-S7)
84. `test_p0c_storage_migration_verify_checks_all_three` — GREEN — D0/S1 flip
85. `test_p0c_worker_builds_coordinator_and_wires_reconcile` — strict-xfail — acceptance red (owed S2-S7)
86. `test_p0c_worker_healthcheck_consumes_readiness_signal` — strict-xfail — acceptance red (owed S2-S7)
87. `test_p0c_worker_only_reconcile_rule_discriminates` — GREEN — control/discriminator
88. `test_p0c_worker_reconcile_rule_discriminates` — GREEN — control/discriminator

### Accepted atomicity — red-proof discriminator controls (2)
89. `test_crash_red_proof_correct_passes_and_wrong_fails` — GREEN — crash red-proof discriminator control
90. `test_red_proof_correct_passes_and_wrong_fails` — GREEN — red-proof discriminator control

### S1 store-policy proofs (`test_s1_store_policy.py`) (8)
91. `test_post_close_read_uses_shared_store_policy_with_configured_timeout` — GREEN — D0/S1 flip
92. `test_establish_wal_does_not_retry_a_non_lock_error` — GREEN — D0/S1 flip
93. `test_establish_wal_zero_timeout_is_a_single_attempt` — GREEN — D0/S1 flip
94. `test_establish_wal_retry_respects_total_deadline_without_multiplying` — GREEN — D0/S1 flip
95. `test_establish_wal_succeeds_after_lock_and_restores_configured_timeout` — GREEN — D0/S1 flip
96. `test_connect_rejects_invalid_busy_timeout_before_opening_sqlite` — GREEN — D0/S1 flip
97. `test_settings_busy_timeout_accepts_in_bounds` — GREEN — D0/S1 flip
98. `test_settings_busy_timeout_rejects_out_of_bounds` — GREEN — D0/S1 flip

### D0 lifecycle-storage doc-drift (`test_lifecycle_storage_doc_drift.py`) (2)
99. `test_named_current_state_docs_reject_the_retired_lifecycle_file` — GREEN — D0/S1 flip
100. `test_scan_discriminates_retired_vs_canonical` — GREEN — control/discriminator

### S2 direct real-source ADMISSION proofs (`test_s2_coordinator_admission.py`) (4, client-free)
101. `test_cap_rejects_at_cap` — GREEN — admission-direct: at-cap admission → `cap_exceeded` (and the raw `_write_pending` raises `_CapExceeded`), no row
102. `test_single_active_same_object_rejects` — GREEN — admission-direct: a second active intent for the object → `active_intent_exists`, one survives
103. `test_operation_key_reuse_is_conflict_or_durable_begin` — GREEN — admission-direct: operation_key reuse for a different intent → `operation_key_conflict` at replay; a durable-path `store.connect` failure → `durable_begin_failed` (relocated WARN-2)
104. `test_admission_crash_seam_faults` — GREEN — admission-direct: before_pending_commit fault → `durable_begin_failed`/no row; after_pending_commit fault PROPAGATES on a committed PENDING row (relocated WARN-1)

### S3 direct real-source APPLY/FINALIZE proofs (`test_s3_coordinator_apply.py`) (12)
105. `test_happy_full_transition_reaches_final` — GREEN — full transition → Ok(Final): FINAL row, exactly one event + one marker, object at v+1/matured
106. `test_event_is_persisted_before_the_qdrant_mutation` — GREEN — the canonical event payload is durably persisted BEFORE `set_payload` (correction 1)
107. `test_persist_event_requires_exactly_one_pending_row` — GREEN — a vanished PENDING row → pre-mutation terminal; Qdrant untouched (integrity hole 1)
108. `test_two_process_single_active_admits_one_rejects_conflict` — GREEN — deterministic barrier: winner→Final, loser→`active_intent_exists` with ZERO Qdrant, one FINAL/event/marker
109. `test_two_process_cap_admission_holds_cap` — GREEN — deterministic barrier: winner→Final, loser→`cap_exceeded` on the same production cap predicate
110. `test_concurrent_same_key_one_wins_other_replays` — GREEN — same key+digest: one Final, the other replays (Ok), exactly one event/marker (integrity hole 4)
111. `test_confirm_discriminates_wrong_readbacks` — GREEN — server-filter/exactly-one, version-only, wrong namespace, wrong object, and partial/mismatched patch-hash all discriminated (fence vs corrupt vs confirmed)
112. `test_stale_version_fences_server_side` — GREEN — a stale intent → `version_fence_violation` + ABANDONED, object unchanged (server-side fence, not client read-then-write)
113. `test_duplicate_operation_key_replay_is_idempotent` — GREEN — exact retry → same Final/stable event_id, no second event/marker; a different intent on the key → `operation_key_conflict`
114. `test_terminal_vs_transient_apply_classification` — GREEN — known-terminal → ABANDONED/`terminal_apply_failure`; transient/unknown → PENDING (never abandoned by uncertainty)
115. `test_finalize_fault_is_atomic_and_post_commit_crash_truth` — GREEN — a fault inside finalize rolls back the event too → EXACTLY APPLIED, no event, mutation durable, caller sees Pending
116. `test_collection_object_type_mapping_matches_canonical` — GREEN — the private mapping stays in parity with `transitions.py`; an unknown collection fails closed (parity/coverage)

**Cross-slice regression gate (NOT part of this parsed Test Contract):**
`tests/lifecycle/test_c6_event_loss.py` (1 passed / 8 xfailed, frozen) is owned by the active C6 slice
[[_slices/slice-c6-lifecycle-event-loss]]. This source cut keeps its disposition byte-for-byte unchanged
(verified every gate) but does not implement or own it, so it is excluded from the parsed denominator.

## Guard-removal checklist (so no `_require_real_stage` xfail masks a regression)

Every owed-later red is kept strict-xfail by a `_require_real_stage(<capability>, <reason>)` guard in
`test_c6b_atomicity.py` that NO-OPs under a red-proof candidate and, for the REAL source, raises the red's
OWN `DefectStillPresent` until the named capability lands. **When a future slice builds a capability it
MUST delete the matching guard(s) and re-derive the flip empirically (`--runxfail`), flipping only the reds
whose real semantics are now load-bearing — never leave a guard on a red that would otherwise pass.**

- **S3 `_apply_conditional` — DONE this slice.** The R22 guard was removed and R22 flipped once the real
  `transition()` gained conditional apply. No `_apply_conditional` guard remains.
- **S4 `reconcile_once`** (remove when S4 lands `reconcile_once`; then re-derive): `_R3_REASON`,
  `_R4_REASON`, `_R5_REASON`, `_R7_REASON`, `_R8_REASON`, `_R9_REASON`, `_R15_REASON`, `_R16_REASON`,
  `_R16_RACE_REASON`, `_R17_REASON`, `_R18_REASON`, `_R19_REASON` (12 guards). NOTE: R4/R7/R8 already have
  their S3 halves working (terminal-abandon / crash-after-applied / finalize atomicity) and only their
  reconcile-recovery halves remain owed — expect them to flip at S4.
- **S6 `rollback`** (remove when S6 lands rollback/maintenance): `_R20_REASON`, `_R20_DRAIN_REASON`,
  `_R20_RECONCILER_DRAIN_REASON` (3 guards).
- **R1** carries no `_require_real_stage` guard — its non-vacuity is a body assertion (it requires a real
  attempted apply), so it self-flipped at S3 and needs no checklist entry beyond this note.

## Status

**`in-progress`** (2026-07-14) — Deliverable-0 (config-drift §E resolution) in
flight. G1 held strict-RED. Blocked-by nothing; consumes the accepted #437 red
contract. No merge/deploy until independent review.
