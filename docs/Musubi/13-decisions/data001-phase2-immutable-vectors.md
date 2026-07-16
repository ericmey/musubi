---
title: "DATA-001 Phase 2: immutable vectors + fenced committed pointer (#530)"
section: 13-decisions
type: adr
status: accepted
owner: aoi
discoverer: yua
phase: "Integrity remediation 2026-07-15 — DATA-001 Phase 2"
tags: [type/adr, status/accepted, data-001, concurrency, vectors, outbox, coordinator]
updated: 2026-07-15
supersedes: []
---

# DATA-001 Phase 2: immutable vectors + fenced committed pointer (#530)

Direction + four corrections approved by Yua 2026-07-15. Supersedes the Phase-1 best-effort
`update_vectors` publish for the two vector-changing paths. Phase-1 payload-only safety shipped on
[PR #539]; this closes the deferred vector-atomicity half of #530.

## Context / constraint (verified, frozen)

Deployed Qdrant (server 1.15) **silently ignores** `update_vectors`' `update_filter` — proven on real
Qdrant 6339: a wrong-token filter still overwrote the vector (`[0.020,0.071,…]` → `[0.575,0.064,…]`).
Consequences:

- A vector write **cannot** be token-fenced on this deployment.
- A full-point `upsert` clobbers RET-008 `access_count` (whole-point write).
- No in-place vector protocol (vpend / TTL / readback / epoch-flag) is crash-atomic against a stalled
  owner's late unfenced write.

Only two production paths change vectors: `EpisodicPlane._reinforce` when NEW content wins
(existing-content-wins already leaves vectors untouched), and `CuratedPlane` same-id body update.

## Decision (architecture A + Yua rulings 1–4)

**Stable anchor + immutable content points + a single fenced pointer swap, versioned.**

- **Stable anchor point** keyed by the public `object_id` (deterministic id). Owns identity, `version`,
  RET-008 access accounting (`access_count` / `last_accessed_at` / `access_lease_token`), the mutation
  lease (`update_lease_token`), `vector_layout_version`, and `live_point` naming the current content
  point. The anchor carries a **zero / non-search vector** + an anchor-kind payload marker so it can
  **never** rank in a vector search (ruling 4).
- **Content point** is write-once: its vector never changes. A vector/content change writes a NEW
  content point; `update_vectors` is never used on a live point.
- **Content-point identity is durable, not the claim token (ruling 1).** `content_point_id` +
  generation derive from the STABLE `operation_key` (persisted in the intent), so a reconcile re-drive
  reuses the SAME staged point instead of accumulating orphans. `CustomIntentContext.owner_token` is
  fresh per claim (changes on reconcile) and is used ONLY to acquire / take over the anchor mutation
  lease — never as the durable generation identity.
- **Commit boundary = a single fenced `set_payload` on the anchor** publishing `live_point` + the
  intended narrow payload + `version+1` + `done`-token, fenced on our exact `own`. No head-flag on
  content points — a two-point flip cannot be the atomic commit boundary (ruling 1).

## Layout versioning + legacy (ruling 4)

- `vector_layout_version` absent / `1` = LEGACY single-point row → served as a **self-pointer** during
  rollout (the row is its own content point).
- `vector_layout_version = 2` (anchor) MUST carry a `live_point`; a v2 anchor with a missing pointer
  **fails closed** (not served). **Never** interpret an arbitrary missing pointer as legacy — legacy is
  only the explicit v1 state.
- The first vector-changing mutation (or an explicit migration) bootstraps a v1 row → an immutable
  content point + a v2 anchor with a committed pointer.

## Read algorithm (ruling 4)

- **Vector search:** filter to CONTENT points (+ v1 self-pointer rows), OVERFETCH, resolve each row's
  stable anchor, and expose a candidate ONLY when `anchor.live_point == candidate.id` (v2) or the row is
  a v1 self-pointer. Anchors never appear (zero-vector + anchor-kind filter).
- **`get(object_id)`:** read the anchor, then hydrate the committed content through `anchor.live_point`
  (v2) or the row itself (v1). A v2 anchor with an absent `live_point` fails closed.

## Durable intent (ruling 2)

Before staging, persist the COMPLETE intended mutation — new content + the EXACT narrow payload fields +
the vector source — in the coordinator outbox `patch_json` (or an equivalent existing durable field),
keyed by `operation_key`. A crash after enqueue / before apply must be replayable from DISK with no
caller memory: the coordinator reconstructs the intent and re-drives the handler to completion.

## Reconciliation — rides the EXISTING coordinator seam (ruling 2; verified, no new worker)

`LifecycleTransitionCoordinator.register_intent_handler(kind, handler)` (`coordinator.py:1475`) already
supports additive custom intent kinds (C4/ART-001 uses it). The coordinator owns admission, claim/lease,
attempts/backoff, RECONCILE, terminal, and re-invokes the handler via `_drive_custom_intent`
(`coordinator.py:1180`) on both apply and crash reconcile. `CustomIntentContext` (`coordinator.py:187`)
hands the handler `operation_key` / `object_id` / `collection` / `namespace` / `owner_token`.

Phase-2 registers ONE idempotent kind (`immutable_vector_publish`). The handler derives its staged
content id from `operation_key`, uses the fresh `owner_token` only to hold the anchor lease, and runs
the success sequence below. **No new worker / subsystem.**

## Cleanup is terminal correctness (ruling 3)

The handler returns **confirmed only after** required owner/generation-scoped cleanup of superseded /
loser content points succeeds — OR after a separate durable cleanup intent was ATOMICALLY admitted. If
cleanup fails, the handler returns **retry**: the already-published `live_point` stays attributable
(exact `done` readback), and the coordinator reconcile re-drives, exact-readbacks the pointer (no
double-publish), and retries only the cleanup. **Never "confirmed on best-effort cleanup"** — this is
what makes no-future-mutation cleanup truthful.

## Narrow success sequence (frozen)

`durable intent (complete mutation persisted)` → `stage deterministic content point (id from
operation_key)` → `single fenced anchor publish + done attribution (exact readback)` → `required scoped
cleanup` → `confirmed`. Any failure before "confirmed" → retry; the coordinator reconcile completes it
from disk.

## Invariants preserved

- RET-008 lease-owned fields (`access_count` / `last_accessed_at` / `access_lease_token`) are never in
  the Phase-2 anchor change-set; a concurrent access-lease increment on the anchor is not clobbered.
- Phase-1 narrow fenced payload mutation unchanged for non-vector updates.
- Losers cannot change visible vectors: a loser content point is never named by a committed `live_point`
  and cannot win the fenced anchor swap.

## RED test contract (tests-first, real Qdrant; all RED before GREEN)

1. `old_owner_late_write_never_becomes_visible`
2. `content_point_id_is_stable_across_reconcile` (id derives from `operation_key`, not the per-claim token)
3. `crash_before_pointer_replays_from_disk` (reconstruct coordinator+handler from disk, no caller memory)
4. `crash_after_pointer_no_double_apply`
5. `cleanup_failure_returns_retry_pointer_stays_attributable`
6. `concurrent_access_lease_composition`
7. `no_future_mutation_orphan_reconciled`
8. `read_follows_committed_pointer_only`
9. `anchor_never_ranks_in_vector_search`
10. `legacy_v1_served_as_self_pointer_and_v2_missing_pointer_fails_closed`
11. `first_vector_mutation_bootstraps_v1_to_v2`

## Alternatives rejected

- **In-place `update_vectors` with a fence** (vpend / TTL / readback / epoch-flag): the `update_filter`
  is silently ignored on server 1.15, so a stalled owner's late unfenced write lands last regardless.
- **Full-point `upsert`**: clobbers the RET-008 `access_count` on the same point.
- **Head-flag on content points**: flipping old/new flags is a two-point operation and cannot be the
  atomic commit boundary.
- **A new reconciliation worker**: unnecessary — the lifecycle coordinator already owns durable
  admission + reconcile for custom intent kinds (ART-001 precedent).

[PR #539]: https://github.com/ericmey/musubi/pull/539
