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

## Status

**`in-progress`** (2026-07-14) — Deliverable-0 (config-drift §E resolution) in
flight. G1 held strict-RED. Blocked-by nothing; consumes the accepted #437 red
contract. No merge/deploy until independent review.
