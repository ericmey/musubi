---
title: "C6b Phase-1 source cut plan + authoritative Pending contract (P0 appendix, REV3)"
section: 13-decisions
type: adr
status: proposed
owner: aoi
discoverer: eric
phase: "Lifecycle-audit 2026-07-13 — C6b Phase-1 source planning (P0 appendix)"
tags: [type/adr, status/proposed, lifecycle, atomicity, outbox, planning]
updated: 2026-07-13
supersedes: []
---

# C6b Phase-1 source cut plan + authoritative Pending contract — P0 appendix (REV3)

**Author:** Aoi · 2026-07-13. Companion to [[13-decisions/c6b-lifecycle-atomicity-design]]. Reflects Yua's
REV2 twelve rulings + the P0b/P0c checkpoint rulings (2026-07-13). **Planning + authoritative contract
only — NOT source authorization.** Source, migration, merge, release, deploy, host contact remain
forbidden until Yua explicitly authorizes S1. Accepted red-contract head at authoring: `ce2e527`
(P0a+P0d landed on `23c61a3`).

## A. Authoritative Pending contract (ruling P0b/6 — source must not reinvent this)

The coordinator boundary returns `Result[TransitionOutcome, TransitionError]` where
`TransitionOutcome = TransitionFinal | TransitionPending` (LOCKED API, test `:48-68`). `TransitionPending`
carries `operation_key` + `event_id` (durable intent committed; Qdrant mutation NOT yet confirmed).

**HTTP wire contract (all four transition routes):**
- **`Ok(Final)`** → HTTP **200**, existing typed success body (unchanged).
- **`Ok(Pending)`** → HTTP **202**, a TYPED body containing at least `status="pending"`, `operation_key`,
  `event_id`. No fabricated success payload; no result fields the Pending does not have.
- **`Err(TransitionError)`** → existing typed error mapping (terminal only): `not_found`→404,
  `illegal_transition`→400, others→400/4xx per current policy. Err is terminal — never a transient window.

A Pending result MUST NOT: fall through as Final, be mapped to Err, lose either identifier, or return
200/400/500.

**Internal (maturation / non-HTTP caller) contract — Pending means DEFERRED:**
- NOT counted or treated as a completed transition (does not increment `transitioned`).
- Does NOT execute post-transition dependent work (e.g. the supersession back-link at maturation `:479`
  must not run on a Pending forward transition).
- Does NOT issue an immediate direct retry — the reconciler owns eventual completion.
- RETAINS `operation_key` + `event_id` in observable deferred accounting (a `deferred` counter/log, PII-free).
- `Final` follows the existing success path; `Err` follows the existing error policy.

## B. Ownership & validation ruling (REV2 ruling 4 — corrected from REV1)

`transitions.py` KEEPS object lookup (`_locate_object`), legal-transition validation (`is_legal_transition`),
lineage/cycle validation, and event construction — it produces ONE validated deterministic
`TransitionIntent`. The **coordinator** owns durable begin, hard server-side version fence, conditional
apply, readback, outcome, finalization, and reconcile. This avoids duplicate Qdrant lookup / business
validation and avoids moving transition types into an import cycle. **The coordinator STILL rechecks the
persisted fence/patch at apply and at replay — it never trusts stale validation as permission to mutate.**

## C. Composition topology (rulings 3, P0c/1 — one coordinator PER PROCESS)

- **core (API):** ONE app-lifetime coordinator, built in `api/bootstrap.py::bootstrap_production_app`
  (alongside the Qdrant singleton at `:181-222`) and injected via
  `app.dependency_overrides[get_lifecycle_service]` (placeholder `api/dependencies.py:116`). API mutations
  use this injected coordinator. **API startup starts NO reconciler.**
- **lifecycle-worker:** ONE process-lifetime coordinator built in `lifecycle/runner.py::_main_async`
  (alongside sink/cursors at `:365-367`), plus the ONLY `reconcile_once` loop (a new interval job in
  `build_lifecycle_jobs` + one startup pass; today `:137-191` has no startup reconcile).
- Both processes share the SAME SQLite/outbox file + the cross-process `fcntl.flock` maintenance barrier.
  No per-call construction, optional fallback, global-settings singleton, or private `sink._db_path` reach.

## D. Shared schema / connection owner + concurrency (rulings 4, 8, 9, P0c/3)

One owner of the shared `lifecycle_sqlite_path` schema creates `lifecycle_events` (today private
`events.py:_SCHEMA` `:36-49`), `lifecycle_outbox`, `lifecycle_control`, and hands out policy-configured
connections. `LifecycleEventSink` migrates onto it behavior-neutrally (no DDL duplication, no private
`_SCHEMA` import, no "sink initialized first" assumption). **Connection policy (actually set, not prose —
`events.py:19` claims WAL but sets none):** `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=<ms>`,
`isolation_level=None` + explicit `BEGIN IMMEDIATE` for writers, and **cross-process-safe schema init** (a
process-local first-open guard is insufficient — concurrent `CREATE TABLE IF NOT EXISTS` under WAL +
busy_timeout, proven by a multi-process test). Named checkpoint behavior or omit the claim. The shared file
is confirmed used by `LifecycleEventSink` + `MaturationCursor` + `SynthesisCursor` (all on
`settings.lifecycle_sqlite_path`, `runner.py:365-367`) → a mandatory concurrent sink+coordinator+both-cursors
multi-process test with bounded busy handling; no lock-only-in-one-process false proof.

## E. Config-drift NAMED BLOCKER (ruling P0c/5)

Three sources disagree on the shared active-storage unit:
- **Production ansible** (`deploy/ansible/templates/docker-compose.yml.j2:16,52`; env
  `env.production.j2:13`): core + worker share the DIRECTORY `/var/lib/musubi/lifecycle`, DB
  `/var/lib/musubi/lifecycle/work.sqlite`. `restore.yml:176` agrees.
- **Root compose / examples** (`docker-compose.yml:123`, `.env.example:38`,
  `deploy/docker/.env.production.example:17`): the FILE `/var/lib/musubi/lifecycle-work.sqlite` (parent
  `/var/lib/musubi`); root compose has no lifecycle-worker service. `backup.yml:87-88` agrees.

**Blocker:** coordinator source MUST NOT land on ambiguous active storage. P0c must either prove a
deployment surface is explicitly non-production / out of scope, OR encode parity reds across every
supported core/worker/backup/restore surface. Resolve before S1.

**P0c encoding + flip mechanism (landed):** `test_p0c_deployment_active_storage_parity` (strict-xfail)
classifies each surface — ansible compose mount, `env.production.j2`, `restore.yml`, root compose,
`.env.example`, `deploy/docker/.env.production.example`, `backup.yml` — into its active-storage FAMILY
(directory `/var/lib/musubi/lifecycle` → `work.sqlite` vs the file `/var/lib/musubi/lifecycle-work.sqlite`)
and asserts a single family. It flips green ONLY when (a) the surfaces are reconciled to one family, OR
(b) every dissenting surface is named in `_OUT_OF_SCOPE_STORAGE_SURFACES` (the test-local out-of-scope
registry, each entry carrying a rationale + this pointer). The `test_p0c_config_surfaces_all_resolve`
control asserts every surface still resolves so a silently-broken extractor can't fake parity. **Backup
and restore currently disagree with each other** (backup reads the FILE variant, restore writes the DIR
variant) — reconcile before S1.

## F. Corrected source-commit series + flip matrix (rulings 3, 10, 11, S1 active-storage)

Each source commit = source + ATOMIC decorator flip in `test_c6b_atomicity.py` (no strict XPASS left) +
focused rerun. **The flip matrix below is provisional and MUST be re-derived against the exact intermediate
implementation — no paper mapping (ruling 11).** Controls green throughout: all `*_rule_discriminates_*`,
`test_red_proof_*`/`test_crash_red_proof_*`, **G1 stays strict RED** (until H5), `test_c6_event_loss.py`,
`test_tc_coverage.py`.

**Checkpoint A — shared storage + begin/finalize:**
- **S1** shared store/schema+connection owner (§D) + behavior-neutral sink migration. **ACTIVE-STORAGE**
  (ruling 10 — WAL/busy_timeout/connection policy changes live sink storage behavior; under the release
  hold). C6 overlap accepted for schema/connection ownership only; buffered accept/flush stays C6.
- **S2** outbox DAO + value types + `begin_transition` (durable PENDING) + admission/single-active-intent/
  cap. Flips the begin-only reds that DON'T require a full `coord.transition()` — verify empirically.
- **S3** conditional apply + readback + atomic finalize + outcome types → full `coord.transition()`.
  **R10 and R12 call full `coord.transition()` and flip HERE, not in S2 (ruling 11).** Flips R1–R4, R8,
  R13, and R10/R12 once the full call exists. **R22 is a coordinator two-process race and flips as soon as
  the coordinator race capability exists (S2/S3), NOT at S7 wiring (ruling 11).**

**Checkpoint B — reconciliation/leases/maintenance:**
- **S4** `reconcile_once` + leases + backoff + classification + crash matrix → R5–R7, R9, R15–R18.
- **S5** observability → R19.
- **S6** rollback/maintenance flock barrier + `cleanup_terminal` → R20 + G2b.

**Checkpoint C — injection/API/runner wiring (ACTIVE):**
- **S7** inject the app-lifetime + worker coordinators (§C); rewire `transitions.py` seam onto the required
  injected coordinator (§B, adapter never erasing Pending); map Pending→202 at the 4 routes; branch the 6
  maturation callsites to DEFERRED (§A); wire `reconcile_once` in the worker. Flips G2a, G3, R21 + the
  P0c wiring reds. **Same commit:** G1 present-denominator 6→5 (transitions.py migrated), red-proofed;
  **G1 stays strict RED** (5 plane bypasses remain until H5).

Review cadence (ruling 10): independent exact-SHA checkpoints after A, B, C — not after every mechanical
commit.

## G. Settings inventory (ruling 12) — validate finite/positive/bounded; only what composition consumes

New settings (none exist today beyond `lifecycle_sqlite_path:102`, `lifecycle_metrics_port:116`): pending
cap (int>0), lease TTL (float>0), reconcile cadence (int>0), backoff base/max (float>0, max≥base) + jitter
policy if configurable, SQLite busy_timeout (int≥0 ms), cleanup retention + batch (int>0), and any
readiness / reconcile-failure thresholds. **API and worker must resolve the SAME active-storage path**
(proven by a red). Do not add a setting production composition does not consume.

## H. Release hold + readiness (rulings 9, P0c/2)

Phase-1 source may be reviewed/merged behind the hold but MUST NOT be released/deployed as "C6b fixed"
while C6 sink durability (#433) and H5/G1 (#439) remain open. S1–S6 inert-except-S1-storage; **S7 is
active** (canonical rewire = production behavior). **Worker readiness:** the deploy healthcheck proves only
`/metrics` liveness (`docker-compose.yml.j2:57-62`) — an executable strict red pins a FUTURE readiness
signal proving coordinator storage/schema is open + reconcile can safely participate; release stays blocked
until the production healthcheck consumes it. A gauge is not a readiness gate until deployment consumes it.

**Pinned readiness signal (P0c):** `musubi_lifecycle_coordinator_ready` — a gauge on the worker metrics
port set to 1 ONLY when the coordinator's shared SQLite/outbox schema is open AND `reconcile_once` can
safely participate (equivalently, a dedicated `/readyz`-style readiness endpoint). The executable
`test_p0c_worker_healthcheck_consumes_readiness_signal` (strict-xfail) parses the REAL ansible template
and asserts the lifecycle-worker healthcheck CONSUMES this signal rather than probing `/metrics` for HTTP
200; it flips green only when the deploy healthcheck reads the pinned gauge (or endpoint).
Deploy is out of scope; live is v1.11.7 vs pinned v1.13.0, not converged.

## I. The twelve REV2 rulings — disposition

1. G1 = exactly 6 (P0a landed `34db9a0`). 2. 4 API routers + 6 maturation callsites are the acceptance
surface (P0b). 3. one coordinator per process (§C). 4. validation ownership: transitions.py builds the
intent, coordinator owns atomicity + rechecks fence (§B). 5. G2a injected boundary (P0d landed `ce2e527`).
6. R21 covers Final/Pending/Err at the boundary + 4 HTTP + 6 maturation (P0b, §A). 7. wiring proof + real
readiness red (P0c, §H). 8. shared-file concurrency confirmed → mandatory multi-process test (§D). 9.
cross-process schema init (§D). 10. S1 ACTIVE-STORAGE (§F). 11. flip matrix follows actual checks —
R10/R12 need full transition, R22 is coordinator-race (§F). 12. complete validated settings inventory (§G).
