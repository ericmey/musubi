---
title: "H5 canonical plane transition boundary"
section: 13-decisions
type: adr
status: accepted
owner: codex-gpt5
phase: "Lifecycle-audit 2026-07-14 — H5 mutation-path unification"
tags: [type/adr, status/accepted, lifecycle, atomicity]
updated: 2026-07-14
supersedes: []
---

# H5 canonical plane transition boundary

## Decision

The five plane `transition()` methods delegate to
`lifecycle.transitions.transition()` and expose its typed three-way result. They do not call Qdrant
`set_payload` directly and do not translate `Pending` into a historical tuple success.

The coordinator is required at every transition call. Read-only plane construction remains valid, but a
transition without an injected coordinator fails closed as a typed error; production transition callers
receive the process-lifetime coordinator from the S7 composition roots.

Callers branch explicitly:

- `Final`: run existing success/dependent work exactly once;
- `Pending`: retain the operation/event identifiers and defer dependent work;
- `Err`: follow the existing terminal error policy.

Concept promotion's `promoted_to` and `promoted_at` are part of `TransitionIntent` and the canonical
intended patch. They therefore participate in the operation digest, server-side version-fenced update,
full readback confirmation, replay, and event lineage. H5 forbids a second post-transition payload write.

The concept promote and soft-delete HTTP routes use the existing `TransitionPendingBody` and declare the
same exact 202 OpenAPI schema as the S7 transition routes. Final response shapes remain unchanged.

## Rejected alternatives

- Keep plane-local `set_payload` after calling the coordinator: two writers and mutation-without-audit.
- Block or immediately retry Pending inside the request/sweep: defeats durable deferral and can duplicate
  work.
- Return the old `(model, event)` tuple for Pending: fabricates an applied row that does not exist.
- Apply `promoted_to` after Final: loses atomicity and replay/readback coverage.
- Optional coordinator with a direct-write fallback: silently reopens G1.

## Release boundary

H5 may merge after its exact-head independent review. C6b still may not be released or deployed as fixed
until the FILE-to-DIR migration artifact is authored, executed under maintenance quiescence, and its
rollback/readiness evidence is accepted.

## Test Contract

1. `test_h5_g1_no_direct_state_transition_setpayload_outside_coordinator`
2. `test_h5_present_denominator_is_empty_after_accounted_migration`
3. `test_h5_each_plane_transition_requires_coordinator_and_preserves_final_pending_err`
4. `test_h5_concept_promotion_receipt_is_in_the_atomic_intended_patch`
5. `test_h5_concept_promotion_receipt_participates_in_replay_and_full_readback`
6. `test_h5_promotion_pending_defers_notification_and_rejection`
7. `test_h5_promotion_final_runs_dependent_work_once`
8. `test_h5_demotion_pending_does_not_increment_completed`
9. `test_h5_demotion_final_increments_completed_once`
10. `test_h5_concept_promote_http_pending_is_typed_202`
11. `test_h5_concept_delete_http_pending_is_typed_202`
12. `test_h5_coordinator_result_is_consumed_at_every_migrated_caller`
