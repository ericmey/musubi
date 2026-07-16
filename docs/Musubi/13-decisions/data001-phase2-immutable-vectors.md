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

## Consumer integration (Yua-approved coupled scope; landed — see the inventory)

The multi-point layout is only correct if EVERY consumer of a `(namespace, object_id)` resolves the
anchor. The 19-seam grep sweep + 1 discovered seam are reconciled in
[`data001-phase2-identity-consumer-inventory.md`](./data001-phase2-identity-consumer-inventory.md) (all
**DONE + proven**). The integration crystallized into a few reusable rules:

- **One shared ranked-read seam (no per-plane fork).** `store/immutable_vectors.py` exports
  `resolve_ranked_candidate` (hydrate a dense/RRF-ranked candidate to the authoritative payload, or
  `None`), `not_anchor_condition()` / `not_content_condition()` (the dual prefilters), and
  `ranked_overfetch(limit)` / `ranked_dedup_budget()` (bounded, never-unbounded fetch). Episodic `query`
  + `_find_dedup_candidate`, curated `query`, and gated `hybrid_search` all call these — the retrieval
  rule cannot drift between planes.
- **Ranked reads exclude anchors; identity reads exclude content.** A ranked read (`query`/dedup/hybrid)
  prefilters `must_not anchor`, moves state (and curated bitemporal) POST-hydration onto the *validated*
  model — never raw payload epochs (a malformed epoch would `TypeError`/500) — and skips a candidate that
  will not model-validate. An identity read (`_scroll`, `namespace_stats`, transitions, recent,
  `_find_by_vault_path`, `scan_vault_rows`) prefilters `must_not content` — because a content point
  re-resolves to its own anchor, so without it a scan double-counts, and it is the fail-closed defense
  against a corrupt/future content shell carrying an identity field.
- **A content point carries only its projection** (`content`/`summary`/`title` + `object_id`/`namespace`/
  `point_kind`), never `vault_path`/`state`/validity — those live on the anchor. Verified against the
  publisher source; the inventory's "Key discriminator" is the canonical statement.
- **Ranked-read vs identity-read fail direction is INVERSE.** A ranked read of a broken row FAILS CLOSED
  (skip it from the view — never 500 the whole query). An identity read of a broken row FAILS LOUD:
  `_find_by_vault_path` and `scan_vault_rows` RAISE, public `find_by_vault_path` returns a typed
  `invalid_row`, and the vault watcher warns + refuses to archive (only `not_found` is a clean no-op;
  any unknown future code also fails closed). Rationale: a dropped ranked hit is a smaller lie than a
  reconciler archiving on an incomplete inventory, or `create` manufacturing a duplicate for a slot that
  is occupied-but-broken.
- **Delete removes the COMPLETE layout.** `delete_object_layout` removes every content generation
  (content-first, `wait=True`) then the identity row in BOTH deterministic id spaces (legacy `_point_id`
  AND `anchor_point_id`), so a converted-in-place OR brand-new anchor is always reached and no content is
  orphaned; a corrupted-payload row stays removable (addressed by deterministic id, not by its payload).
- **Three write compositions inject the publisher** (API bootstrap, lifecycle runner, vault runtime;
  runner registers handlers before the boot reconcile). A vector-changing write with no wired publisher
  fails closed, never silently best-effort.
- **Discovered gap (D1):** curated `create` true-supersession validated the OLD row's fresh payload
  without stripping layout keys, so superseding a v2 anchor raised `extra_forbidden`. Fixed by
  strip-before-validate; the narrow lease write is unchanged. Surfaced by a hybrid-bitemporal test
  collision — the inventory-completeness discipline catching the 20th seam before merge, not in prod.

## Durable intent — single store (Option B, decided 2026-07-15)

Two candidate durable homes were considered. **Option A** (stage the mutation as a Qdrant content
point at admit + enqueue the intent in SQLite) was REJECTED: it is a two-store write with an
unavoidable cross-store crash gap — stage-first leaves an orphan content point with no intent;
enqueue-first leaves a durable intent with no replayable mutation. **Option B** (adopted): the COMPLETE
mutation lives in ONE store — the coordinator outbox `patch_json` — so admission is a single atomic
SQLite write and there is no cross-store gap.

Before staging, persist the COMPLETE intended mutation — the canonical content + the EXACT narrow
payload fields + a recompute FINGERPRINT (embedder identity + content length), never a raw vector blob
— in `patch_json`, keyed by `operation_key`, JSON- and size-validated at admission. The handler
RECOMPUTES the vector from that content (deterministic for a given embedder), so a crash after
admission replays from disk with no caller memory.

Coordinator generalization (additive, bounded, authorized under this slice): `enqueue_custom_intent(kind,
object_id, namespace, collection, patch_json)` generalizes the artifact-only `enqueue_index_intent`
(now a backward-compatible wrapper); `CustomIntentContext.patch_json` threads the persisted payload
through `_drive_custom_intent` on every normal/reconcile path. Same cap gate + `ux_active_intent`
idempotency (one active intent per object — so a "loser" is a stale claim's late write, fenced on
`pointer_version`, not a second simultaneous intent).

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
