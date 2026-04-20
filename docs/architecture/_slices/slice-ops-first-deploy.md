---
title: "Slice: First-deploy runbook + validation"
slice_id: slice-ops-first-deploy
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/done, type/slice, ops, deploy, phase-2]
updated: 2026-04-20
reviewed: true
depends-on: ["[[_slices/slice-ops-ansible]]", "[[_slices/slice-ops-compose]]", "[[_slices/slice-ops-backup]]", "[[_slices/slice-ops-observability]]"]
blocks: []
---

# Slice: First-deploy runbook + validation

> Authors the step-by-step first-deploy procedure for `musubi.mey.house`, the systemd units + Kong config stitching the shipped Ansible/compose/backup/observability work together, and the post-deploy smoke + verify scripts. Ships the operator runbook; operator (Eric) executes the deploy with the runbook open.

**Phase:** 8 Ops · **Status:** `done` · **Owner:** `codex-gpt5`

## Why this slice exists

The four foundational ops slices are shipped:

- `slice-ops-ansible` — Ansible roles
- `slice-ops-compose` — docker-compose stack
- `slice-ops-backup` — backup/restore scripts
- `slice-ops-observability` — metrics + logs + traces instrumentation + dashboards

What's missing: **the authored procedure that binds them into a single "go live on musubi.mey.house" sequence.** Without this slice, the operator (Eric) has to stitch ansible + compose + DNS + Kong + certs + systemd + smoke-verify from memory each time. The first real deploy needs a deterministic, ordered, resumable runbook.

This slice does NOT execute the deploy — Eric does that. This slice ships the **runbook + scripts + systemd units + verification harness** so Eric can pull the trigger with confidence.

## Specs to implement

- [[08-deployment/compose-stack]] (integrate with the authored runbook)
- [[08-deployment/ansible-layout]] (roles already shipped; document the first-deploy invocation)
- [[09-operations/runbooks]] (the runbook itself lands here)

## Owned paths (you MAY write here)

- `deploy/runbooks/first-deploy.md`                  (new — the authored step-by-step procedure)
- `deploy/systemd/`                                  (new — unit files for api, lifecycle-worker, vault-sync)
- `deploy/smoke/`                                    (new — post-deploy verification scripts)
- `deploy/kong/`                                     (parent done in ops-compose — extend with route stubs for prod)
- `tests/ops/test_first_deploy_smoke.py`             (new — unit tests for smoke scripts)
- `docs/architecture/09-operations/runbooks.md`      (parent done — add first-deploy cross-link)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/`                   (no code changes; this is deploy orchestration)
- `docs/architecture/07-interfaces/`  (API contract)
- `openapi.yaml`, `proto/`
- `.github/workflows/`            (CI is not deploy; separate concern)

## Depends on

- [[_slices/slice-ops-ansible]]          (done — playbooks authored)
- [[_slices/slice-ops-compose]]          (done — compose stack authored)
- [[_slices/slice-ops-backup]]           (done — backup strategy authored)
- [[_slices/slice-ops-observability]]    (done — health-probe endpoints wired)

## Unblocks

- **First real deploy to `musubi.mey.house`** — Eric executes the runbook; this slice ships the procedure.
- **POC → v1 migration execution** — migration (#109) needs a running target Musubi; this slice makes v1 runnable.
- **OpenClaw v0.1 integration test** — Aoi's client needs a real Musubi endpoint to hit; this slice gets one running.
- **All Phase 2 activities** — perf baselines, load tests, chaos scenarios — all need a running deployed instance.

## What lands in this slice

### 1. `deploy/runbooks/first-deploy.md` — the runbook

Step-by-step operator procedure. Structure:

1. **Pre-flight** — DNS A record for musubi.mey.house pointing at 10.0.20.45; operator has SSH access; ansible control node at 10.0.20.53 (yua) has the musubi repo cloned; secrets materialized per `.env.example`.
2. **Snapshot target host** — ZFS snapshot / system-image backup of musubi.mey.house before first boot.
3. **Run ansible playbook** — `ansible-playbook deploy/ansible/site.yml -i inventory/prod.yml`. Expected output. Failure modes + recovery.
4. **Bring up compose stack** — `docker compose -f docker-compose.yml up -d`. Health-gate on Qdrant + TEI + Ollama ready.
5. **Install systemd units** — `deploy/systemd/*.service` placed at /etc/systemd/system/; enable + start.
6. **Configure Kong** — route stubs for `/v1/*` → Musubi API, `/mcp/*` → MCP adapter. OAuth 2.1 provider config.
7. **TLS certificate** — operator chooses letsencrypt vs internal CA; runbook covers both paths.
8. **Smoke verify** — `deploy/smoke/verify.sh` runs every check. Expected output listed.
9. **Rollback procedure** — ZFS snapshot rollback steps for emergency.
10. **Go-live checklist** — DNS propagation, OAuth client registered, first real request, health dashboard green.

Every step has: command, expected output, failure-mode notes, operator decision points explicitly marked.

### 2. `deploy/systemd/` — unit files

- `musubi-api.service` — the FastAPI server (running via uvicorn against the compose stack).
- `musubi-lifecycle-worker.service` — the lifecycle sweep workers.
- `musubi-vault-sync.service` — the vault watcher.
- Each with `Restart=on-failure`, `RestartSec=10`, structured journal logging, dependency ordering on `docker.service`.

### 3. `deploy/smoke/` — post-deploy verification

- `smoke/verify.sh` — orchestrates the full check battery.
- `smoke/check_api.sh` — curls `/v1/ops/health` + `/v1/ops/status`; asserts `components` map has all 5 dependencies healthy.
- `smoke/check_capture.sh` — captures a synthetic memory, retrieves it, verifies round-trip content.
- `smoke/check_thoughts.sh` — sends a synthetic thought, checks via `/thoughts/check`, verifies.
- `smoke/check_observability.sh` — scrapes `/ops/metrics`, asserts prometheus text format valid + N metric families present.

Scripts are shell (bash) for operator clarity. Each emits a clear `[PASS]` / `[FAIL]` per check + exits non-zero on any failure.

### 4. `deploy/kong/` — prod route stubs

Extend the skeleton shipped in `slice-ops-compose` with:
- Route entries for every `/v1/*` endpoint family.
- OAuth 2.1 plugin config (provider + audience + JWKS URL).
- Rate-limit plugin config (matching the in-app rate-limit plan from `slice-ops-hardening-suite`).

Use Kong decK for declarative config. The file is YAML; operator `deck sync` applies.

### 5. `tests/ops/test_first_deploy_smoke.py` — unit tests for the smoke scripts

- Test each `check_*.sh` against a mock Musubi stack (containerized with canned HTTP responses).
- Assert exit codes + output contain the expected `[PASS]` / `[FAIL]` patterns.
- Validates the smoke harness before operator ever runs it on a real deploy.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] `deploy/runbooks/first-deploy.md` is complete + reviewable in one sitting (<2k lines).
- [ ] Every numbered step in the runbook has command, expected output, failure-mode guidance.
- [ ] Systemd units install cleanly on a Ubuntu 24.04 VM (dry-run test — a VM on harem-001 is fine; doesn't need the real box).
- [ ] Smoke scripts unit-tested via `tests/ops/test_first_deploy_smoke.py` against mocked Musubi responses.
- [ ] Kong config is declarative (decK) + example-documented per endpoint family.
- [ ] Rollback procedure is documented + tested against the VM dry-run target.
- [ ] Branch coverage ≥ 80% on the smoke-test module.
- [ ] Standard handoff path + Issue dual-update.

**Explicitly NOT in scope** (and Codex should NOT do these — these are Eric's operator work):

- Executing the actual first deploy on `musubi.mey.house`.
- Registering the OAuth client with whatever identity provider Eric chooses.
- Obtaining real TLS certificates.
- Configuring the DNS A record.
- Monitoring the first production traffic.

Those are operator actions with the runbook open.

## Test Contract

**Runbook structural:**

1. `test_runbook_has_all_10_sections`
2. `test_runbook_every_command_has_expected_output_block`
3. `test_runbook_mentions_rollback_path_for_every_destructive_step`

**Systemd units:**

4. `test_systemd_unit_api_has_restart_on_failure`
5. `test_systemd_unit_lifecycle_worker_depends_on_docker`
6. `test_systemd_unit_vault_sync_logs_to_journal`

**Smoke scripts:**

7. `test_check_api_passes_when_all_components_healthy`
8. `test_check_api_fails_when_qdrant_unhealthy`
9. `test_check_capture_round_trip_passes_with_real_response`
10. `test_check_capture_fails_when_content_mismatch`
11. `test_check_thoughts_send_check_roundtrip`
12. `test_check_observability_scrapes_valid_prometheus_text`
13. `test_verify_sh_aggregates_all_checks`
14. `test_verify_sh_exits_non_zero_on_any_failure`

**Kong config:**

15. `test_kong_config_yaml_parses_via_deck_validate`
16. `test_kong_routes_cover_every_v1_endpoint_family`

## Work log

### 2026-04-19 — operator — slice carved

- Phase 2 critical path; binds the shipped ops slices (ansible/compose/backup/observability) into a deterministic first-deploy procedure.
- Routed to Codex: his track record owns three of the four dependency slices; this is the composition of that body of work.
- Does NOT execute the deploy — Eric does that with the runbook open. This slice ships the authored procedure + validated scripts + systemd units + post-deploy smoke harness.

### 2026-04-19 23:46 — codex-gpt5 — claimed slice

- Claimed Issue #116 and flipped slice frontmatter from `ready` to `in-progress`.

### 2026-04-20 00:23 — codex-gpt5 — handoff to in-review

- Shipped the first-deploy runbook, systemd unit templates, smoke verification scripts, Kong decK production config, and mocked smoke-script tests.
- Added the first-deploy cross-link and quarterly game-day cycle to [[09-operations/runbooks]].
- Verification: `make check`, `make tc-coverage SLICE=slice-ops-first-deploy`.

| Test Contract bullet | State | Evidence |
|---|---|---|
| `test_runbook_has_all_10_sections` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_runbook_every_command_has_expected_output_block` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_runbook_mentions_rollback_path_for_every_destructive_step` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_systemd_unit_api_has_restart_on_failure` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_systemd_unit_lifecycle_worker_depends_on_docker` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_systemd_unit_vault_sync_logs_to_journal` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_check_api_passes_when_all_components_healthy` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_check_api_fails_when_qdrant_unhealthy` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_check_capture_round_trip_passes_with_real_response` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_check_capture_fails_when_content_mismatch` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_check_thoughts_send_check_roundtrip` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_check_observability_scrapes_valid_prometheus_text` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_verify_sh_aggregates_all_checks` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_verify_sh_exits_non_zero_on_any_failure` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_kong_config_yaml_parses_via_deck_validate` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_kong_routes_cover_every_v1_endpoint_family` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_every_alert_has_a_runbook_section` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_runbooks_reference_real_files_and_commands` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_each_runbook_lists_success_criteria` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |
| `test_quarterly_game_day_drills_cycle_through_runbooks` | ✓ passing | `tests/ops/test_first_deploy_smoke.py` |

## Cross-slice tickets opened by this slice

- _(none yet; may open one to slice-api-rate-limits for Kong rate-limit plugin config if the in-app enforcement shape demands it)_

## PR links

- [PR #121](https://github.com/ericmey/musubi/pull/121)
