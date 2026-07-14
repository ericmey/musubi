---
title: "C6b: lifecycle Qdrant↔SQLite atomicity — design v2 (durable-intent outbox + coordinator)"
section: 13-decisions
type: adr
status: proposed
owner: aoi
discoverer: eric
phase: "Lifecycle-audit 2026-07-13 — C6b atomicity"
tags: [type/adr, status/proposed, lifecycle, audit, atomicity, outbox]
updated: 2026-07-13
supersedes: []
---

# C6b: lifecycle Qdrant↔SQLite atomicity — design v2 (durable-intent outbox + coordinator)

**Author:** Aoi · 2026-07-13 · **Status:** PROPOSED v2 — revised for Yua's fork rulings + corrections A–J
(2026-07-13). Slice: [[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]] (Issue #437). Direction
(durable-intent outbox) ACCEPTED; this v2 is the contract that makes the outbox truthful across callers,
retries, bypass paths, and long-term operation. Zero source until the red contract is encoded + reviewed.

## The gap (verified against `src/musubi/lifecycle/transitions.py` + the plane layer)

`transition()` mutates Qdrant FIRST (`set_payload`, l.252-256) then records the audit (l.267-268) — two
failure/crash windows: mutation-without-audit, and `expected_version` is warn-only "last writer wins"
(l.180-187). C6 durable-on-accept closes neither. **And `transitions.py` is not the only mutation path
(correction G).**

## Fork rulings (Yua 2026-07-13)

1. **Boundary:** a distinct **`LifecycleTransitionCoordinator`** public API, backed by a distinct
   **`LifecycleOutbox`** and a **shared SQLite event+outbox store** (same DB). begin/finalize do NOT go
   on `LifecycleEventSink`; `record(event)` stays a standalone FINAL-append for no-mutation audits.
   **`finalize` inserts the FINAL lifecycle event AND marks the outbox row FINAL in ONE SQLite
   transaction.**
2. **Hard version fence is canonical for ALL state transitions** — never sink-dependent warn-only.
3. **Separate `lifecycle_outbox` table in the SAME DB.**
4. **Hard configurable cap:** at cap, `begin` returns Err and Qdrant is untouched. Alert-only rejected.
5. **One mandatory reconciliation job in `lifecycle-runner`** — startup + periodic, deployment/
   readiness-gated. No optional second daemon/path.

## API + outcome semantics (correction A)

`LifecycleTransitionCoordinator.transition(intent) -> Result[TransitionOutcome, TransitionError]`:

- **`Ok(Final(result))`** — synchronous completion: Qdrant mutation confirmed by readback AND the atomic
  FINAL transaction committed.
- **`Ok(Pending(operation_key, event_id))`** — durable intent committed, mutation NOT yet confirmed
  (transient/unknown Qdrant failure). The reconciliation job will complete it. **HTTP maps this to 202;
  internal callers MUST branch on Final vs Pending.**
- **`Err(TransitionError)`** — ONLY when NO future mutation will occur: `begin` failed (SQLite down / cap
  hit), or a proven terminal condition (fence violated, illegal transition, invalid) → terminal abandon.

A transient Qdrant failure MUST NOT return ordinary `Err` while the worker may still mutate — that is the
false-terminal bug A forbids.

## State machine (corrections B, F, J)

```
  ∅ ──begin──▶ PENDING ──qdrant apply (conditional)──▶ APPLIED ──atomic FINAL txn──▶ FINAL
                 │ ▲                                      │  (insert FINAL event +      (terminal;
   transient/    │ │ reconcile: readback-confirm          │   mark outbox FINAL,        mutation confirmed
   unknown fail  │ │ (version+state+patch SHA)            │   ONE txn)                  AT finalize time)
   → stays       │ │                                      │
   PENDING       ▼ │                          crash before atomic txn → reconcile redoes it (idempotent)
   (retry+alert  (retry indefinitely
    indefinitely, within the hard CAP)
    within cap)   │
                  │ proven terminal only (fence stale / illegal / invalid)
                  ▼
              ABANDONED   (terminal; PII-free reason code; NEVER writes a FINAL lifecycle event)
```

- **PENDING** — durable intent; Qdrant outcome unknown/unconfirmed. The only retryable state.
- **APPLIED** — Qdrant mutation CONFIRMED by readback (exact version + state + canonical patch SHA), but
  the atomic FINAL transaction not yet committed.
- **FINAL** — the ONE SQLite transaction (insert FINAL `LifecycleEvent` + mark outbox FINAL) committed.
  **FINAL means the mutation was confirmed AT FINALIZATION TIME — not eternal current equality** (a later
  transition may move the object past this version).
- **ABANDONED** — PROVEN terminal (fence/illegal/invalid) ONLY. **Never** from transient N-attempts
  (correction B). **Never** creates a FINAL lifecycle event.

Every SQL edge is a guarded `UPDATE ... WHERE state = <expected>` → idempotent.

## Idempotency + single active intent (corrections C, D, E)

- **`operation_key UNIQUE` (correction C):** an `event_id` minted inside each call is insufficient — a
  CALLER retry mints a new `event_id` and would create a second intent. `operation_key` is either a
  caller-supplied idempotency key OR the canonical transition identity (SHA of
  collection+object_id+from_version+to_state+patch). `begin` with an existing `operation_key` REUSES the
  live intent. Test: the same logical request twice ⇒ one operation, one FINAL, one event.
- **One nonterminal intent per `(collection, object_id)` (correction D):** a partial-unique guard
  (`UNIQUE(collection, object_id) WHERE state IN ('PENDING','APPLIED')`). Concurrent `begin`s serialize —
  the second reuses (same operation_key) or is rejected (`Err: active_intent_exists`) until the first
  resolves. Prevents a later v3 from hiding a crash-applied v2 before reconciliation. **No next
  transition while an active intent is unresolved.**
- **Server-side conditional apply + full readback (correction E):** the Qdrant apply is conditional on
  the expected version (a `set_payload` with a filter that matches only `version == expected` for that
  object/namespace). Success requires READBACK of the exact object/namespace + target **version + state +
  canonical intended-patch SHA** — **not version alone**.

## Crash matrix (correction F — expanded; invariant: FINAL ⟺ confirmed-at-finalize mutation)

| # | crash point | reconciliation must… | forbidden |
|---|---|---|---|
| C1 | after PENDING, before Qdrant | readback; not applied & fence valid → replay; proven-terminal → ABANDONED | FINAL with no matching mutation |
| C2 | after Qdrant apply, before APPLIED mark | readback confirms (version+state+SHA) → mark APPLIED | losing the audit / re-applying |
| C3 | after APPLIED, before the atomic FINAL txn | redo the atomic FINAL txn (idempotent on event_id) | duplicate/again mutate |
| C4 | after FINAL | no-op (terminal) | double FINAL |
| C5 | mid-reconciliation | lease reclaim (expired lease), resume | two workers finalizing one row |
| C6 | network/client retry mid-apply | idempotent via operation_key + conditional apply | duplicate apply |

## Failure classification (correction J)

- **Known terminal** (object-not-found, illegal transition, permanently-violated fence, invalid) →
  `ABANDONED` / `Err`. No FINAL event.
- **Transient or unknown** (network, timeout, 5xx, client error of unknown kind) → stays `PENDING` /
  `Ok(Pending)`. Retried by the reconciler with **bounded backoff** + a durable `attempts` count
  (observability only) + alert, **indefinitely** — never abandoned by attempt count. The only
  pending-depth bound is R14's **global hard cap**, which gates *storage admission* only
  (`begin` → `Err(cap_exceeded)`); "within the hard cap" is an admission bound, **never** a retry
  terminator. `attempts` is not a retry cap.
- **No poison-row starvation:** one perpetually-transient row must NOT block other PENDING rows — the
  reconciler processes rows independently (per-row lease + backoff), never head-of-line-blocks the queue.

## Content + observability (corrections I + fork 4)

- **Store a minimal deterministic target patch + SHA (correction I):** the outbox row holds the specific
  target fields (state, version, lineage keys) + a canonical SHA — NOT an arbitrary full payload.
  **Outbox content NEVER enters logs or metric labels.**
- **Metrics** (bounded / PII-free, codes only): `musubi_lifecycle_outbox_pending` (gauge, no labels,
  the cap backstop), `..._reconciled_total`, `..._abandoned_total`, `..._mutation_failures_total`
  (low-cardinality `{class="terminal|transient"}` at most — never object/namespace/reason).
- **Hard cap (fork 4):** `begin` returns `Err(cap_exceeded)` with Qdrant untouched once pending depth ≥
  the configured cap.

## Migration / rollback (correction H)

- Additive `CREATE TABLE IF NOT EXISTS lifecycle_outbox (operation_key UNIQUE, event_id, collection,
  object_id, target_patch, patch_sha, expected_version, state, attempts, next_attempt_epoch, lease_owner,
  lease_expires_epoch, created_epoch, updated_epoch)` in the SAME DB as `lifecycle_events`; partial-unique
  guard on `(collection,object_id) WHERE state IN ('PENDING','APPLIED')`.
- **Rollback refuses while ANY nonterminal (PENDING/APPLIED/leased) row exists**, and **stops the
  reconciliation worker first.** Terminal rows (FINAL/ABANDONED) get a **retention/cleanup** policy so
  they don't grow forever.

## Structural blind spot — every mutation path (correction G)

**Verified: `transitions.py` is NOT the only state-mutation path.** The precise inventory is by an AST
rule (a `set_payload` in a function that writes a `state` field), NOT the raw line refs — several
`set_payload` sites named in the ruling write **non-state** fields and are correctly excluded (verified
2026-07-13): `maturation.py:893` writes `tags/importance/topics` ("non-state enrichment", per its own
docstring), `synthesis.py:718` writes `contradicts`, `demotion.py:380` writes the reinforcement clock.
The actual **state-writing TRANSITION** `set_payload` sites (G1 covers post-create transitions only —
Yua repair 3) are **6 sites across 6 files**:

- **5 plane `transition()` methods** — `planes/episodic/plane.py:812`, `planes/concept/plane.py:436`,
  `planes/thoughts/plane.py:488`, `planes/artifact/plane.py:295`, `planes/curated/plane.py:449` (each
  `set_payload`s `state`+`version` and emits its own event), called by `lifecycle/promotion.py`,
  `lifecycle/demotion.py` (×5), `api/routers/writes_concept.py` (×2).
- **`lifecycle/transitions.py:252`** — the canonical path.

**Out of G1's transition scope (repair 3):** `planes/curated/plane.py:224` (`create`) writes an INITIAL
state on creation — a capture/create-atomicity concern (M9 / a deliberately-approved C6b extension), NOT
forced through the transition coordinator. The scanner excludes `create` functions. (Only curated's
create writes `state=` explicitly; other planes' creates carry state as a typed-model default and are
correctly not flagged — so scoping to transitions avoids an inconsistent partial-create rule.)

**The G1 scanner** (repairs 1–4, `tests/lifecycle/test_c6b_atomicity.py`): (1) exempts exactly the
canonical relative path `lifecycle/coordinator.py`, not a basename; (2) a **per-call taint fixpoint**
ties a `state` write to the SPECIFIC `set_payload` payload via dataflow, so a function that computes state
AND separately writes an unrelated enrichment `set_payload` is NOT flagged (the false-association
repair 2); (3) excludes creates; (4) a green **present-denominator control** pins the exact 6 current
bypasses so a silently-vanishing site fails loudly (red-proofed: blinding the scanner to one plane makes
the control fail `missing=…`).

**Decision:** C6b does NOT migrate all of these in-scope (too large). C6b **depends on a concrete H5
unification slice** ([[_slices/slice-h5-unify-state-mutation]], Issue TBD) that routes ALL state mutation
through `LifecycleTransitionCoordinator`; **C6b atomicity closure is BLOCKED on H5.** C6b ships:

- a **mechanical guard red** (AST/rg) that FORBIDS direct `state`-writing `set_payload` outside the
  coordinator — it is **RED today** (it lists the **6** current post-create transition violators — see the
  committed `_PRESENT_TRANSITION_BYPASSES` control) and flips green only when H5 lands. C6b must NOT claim
  atomicity for the maturation/API canonical paths alone.

## Behavior-shaped RED INVENTORY v2 (to encode; strict-xfail vs current no-outbox code)

Fixtures: in-memory Qdrant (`QdrantClient(":memory:")`, seeded object), real shared SQLite (events +
outbox), fault injectors for the conditional `set_payload` (transient vs terminal) and the PENDING write,
an env-selected crash subprocess (C1/C2/C3), a reconciliation entrypoint, bounded waits/joins.

Reds:
- R1 durable PENDING intent committed BEFORE the Qdrant mutation.
- R2 SQLite unavailable at `begin` ⇒ Qdrant UNTOUCHED + `Err`.
- R3 **transient** Qdrant failure ⇒ `Ok(Pending(operation_key,event_id))`, NO FINAL, reconciler
  completes later.
- R4 **terminal** Qdrant/fence failure ⇒ `Err`/`ABANDONED`, NO FINAL event (J).
- R5 crash C1 ⇒ reconcile replays or abandons; never a false FINAL.
- R6 crash C2 ⇒ reconcile readback-confirms, marks APPLIED→FINAL; audit recovered (the C6b hole).
- R7 crash C3 (after APPLIED, before atomic FINAL txn) ⇒ reconcile redoes the txn; exactly one FINAL.
- R8 finalize atomicity ⇒ FINAL event insert + outbox→FINAL in ONE txn (inject mid-txn failure ⇒ neither
  persists) (fork 1).
- R9 idempotent replay ⇒ replaying a PENDING twice yields one FINAL + one effective apply.
- R10 `operation_key` idempotency across CALLER retries ⇒ same logical request twice = one operation/
  FINAL/event (C).
- R11 single active intent per `(collection,object_id)` ⇒ concurrent begins serialize/reuse/reject; never
  two nonterminal intents (D).
- R12 hard expected-version fence ⇒ stale expected ⇒ Err/abandon, mutation not applied; stale replay
  abandons rather than clobbers (fork 2, E).
- R13 conditional apply + full readback ⇒ success requires version+state+patch-SHA readback, not version
  alone (E).
- R14 hard cap ⇒ at pending cap, `begin` ⇒ `Err(cap_exceeded)`, Qdrant untouched (fork 4).
- R15 transient failure NEVER ABANDONED by attempt count ⇒ N transient failures keep it PENDING (B).
- R16 reconciliation lease prevents double-processing; R17 expired-lease reclaim (fork 5).
- R18 no poison-row starvation ⇒ one stuck transient row does not block other PENDING rows (J).
- R19 outbox content never in logs/metric labels; row stores minimal patch + SHA, not arbitrary payload
  (I).
- R20 rollback refuses on any nonterminal row + stops the worker first; terminal-row cleanup exists (H).
- R21 caller outcome is three-way (Final/Pending/Err) at the coordinator boundary; Pending carries the
  operation/event id (A).
- R22 **two DIFFERENT requested transitions race on one object** (e.g. v1→matured vs v1→demoted): exactly
  one wins (creates the intent + applies); the loser **cannot mutate and cannot overwrite the winner's
  intent** — it is fenced/rejected (`Err: active_intent_exists` or stale-fence abandon), never a silent
  lost-update. Distinct from R11 (concurrent begins of the *same* operation) — this proves the
  single-active-intent guard + hard fence together defeat a genuine two-writer conflict (Yua 2026-07-13).

Guards:
- G1 **mechanical AST/rg guard: NO direct `state`-writing `set_payload` outside
  `LifecycleTransitionCoordinator`** — RED today (lists the **6** post-create transition violators:
  `lifecycle/transitions.py::transition` + the 5 plane `transition()` methods, per the committed
  `_PRESENT_TRANSITION_BYPASSES`); flips green only under H5 (G). **Closure-gate, not Phase-1 acceptance.**
- G2 callsite inventory: `coordinator.transition(` callsites are exactly the reviewed set.
- G3 AST "Result consumed": no caller may drop the three-way `TransitionOutcome`.

## Phase-1 acceptance vs defect closure (Yua sequencing 2026-07-13 — no circular dependency)

The red contract **labels each item**:

- **Phase-1 source acceptance** — R1–R22, G2, G3. These flip to `XPASS(strict)` when the
  `LifecycleTransitionCoordinator` + `LifecycleOutbox` (Phase 1) is implemented. C6b Phase 1 may land with
  **C6b still OPEN** on this evidence.
- **Defect closure** — **G1** only. It stays RED through Phase 1 and flips green **only when
  [[_slices/slice-h5-unify-state-mutation]] (Issue #439)** migrates every mutation path onto the
  coordinator. C6b closes as a defect only then.

Red-proof plan: a temporary minimal coordinator+outbox (begin→PENDING commit; conditional set_payload;
readback; atomic finalize; single-pass reconcile with leases; classified failures) flips R1–R22 to
`XPASS(strict)`; G1 is red-proofed separately by a temporary scoped migration of the violators; source
restored, zero `src/` committed.

## Dependencies (acyclic)

- **C6b `blocks`:** the C6 source slice [[_slices/slice-c6-lifecycle-event-loss]] **and**
  [[_slices/slice-h5-unify-state-mutation]] (H5 consumes C6b's coordinator API).
- **H5 `depends-on` C6b**, not the reverse — so no cycle. C6b **closure** is gated by H5 via the G1
  closure-gate (a documented state, NOT a DAG edge).

No source, host, or merge until the red contract is encoded, red-proofed, and reviewed.
