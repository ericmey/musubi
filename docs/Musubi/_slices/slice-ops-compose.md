---
title: "Slice: Docker Compose stack"
slice_id: slice-ops-compose
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-ops-ansible]]"]
blocks: ["[[_slices/slice-ops-first-deploy]]", "[[_slices/slice-ops-integration-harness]]", "[[_slices/slice-ops-observability]]"]
---

# Slice: Docker Compose stack

> Compose file covering Qdrant, Core, Lifecycle, TEI, Ollama, Kong. Health checks + startup order.

**Phase:** 8 Ops · **Status:** `done` · **Owner:** `codex-gpt5`

## Specs to implement

- [[08-deployment/compose-stack]]
- [[08-deployment/kong]]

## Owned paths (you MAY write here)

  - `deploy/docker/`
  - `docker-compose.yml`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/`

## Depends on

  - [[_slices/slice-ops-ansible]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-ops-observability]]

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-19 19:36 — codex-gpt5 — claimed slice

- Claimed Issue #18 and flipped slice frontmatter from `ready` to `in-progress`.

### 2026-04-19 19:39 — codex-gpt5 — handoff to in-review

- Added the canonical root `docker-compose.yml` for Qdrant, TEI dense/sparse/reranker, Ollama, and Core.
- Added `deploy/docker/` operator artifacts: README, production env example, Kong declarative config, and a bounded warm-cache smoke script.
- Verification: `make check` passed; `make tc-coverage SLICE=slice-ops-compose` passed; `make agent-check` reported warnings only and no `✗` hard errors.

| Test Contract bullet | State | Evidence |
|---|---|---|
| `test_compose_config_valid` | ✓ passing | `tests/ops/test_compose.py:65` |
| `test_every_service_has_healthcheck` | ✓ passing | `tests/ops/test_compose.py:81` |
| `test_every_image_pinned_by_digest` | ✓ passing | `tests/ops/test_compose.py:91` |
| `test_core_depends_on_all_dependencies_healthy` | ✓ passing | `tests/ops/test_compose.py:98` |
| `test_only_core_publishes_a_host_port` | ✓ passing | `tests/ops/test_compose.py:107` |
| `test_gpu_services_list_gpu_reservation` | ✓ passing | `tests/ops/test_compose.py:117` |
| `test_bind_mounts_exist_on_host` | ✓ passing | `tests/ops/test_compose.py:130` |
| `test_compose_up_to_healthy_under_5min_on_warm_cache` | ✓ passing | `tests/ops/test_compose.py:138` |

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- [PR #85](https://github.com/ericmey/musubi/pull/85)
