---
title: "Slice: Config & environment loading"
slice_id: slice-config
section: _slices
type: slice
status: done
owner: cowork-auto
phase: "1 Schema"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: []
blocks: ["[[_slices/slice-api-v0-read]]", "[[_slices/slice-auth]]", "[[_slices/slice-embedding]]"]
---
# Slice: Config & environment loading

> Single source of truth for environment variables. All config reads go through one module; agents must not read os.environ directly elsewhere.

**Phase:** 1 Schema · **Status:** `done` · **Owner:** `cowork-auto`

## Specs to implement

- [[00-index/conventions]]

## Owned paths (you MAY write here)

- `musubi/config.py`
- `musubi/settings.py`
- `.env.example`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/api/`
- `musubi/planes/`

## Depends on

- _(no upstream slices)_

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-api-v0-read]]
- [[_slices/slice-auth]]

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

### 2026-04-18 — cowork-auto — first cut landed; `status: ready → in-review`

Cowork shipped the first cut during the unsupervised session on 2026-04-18, landing as direct commits to `v2` (pre-branch-protection, pre-full-PR-lifecycle). Commits:

- `4502bab` — `test(config): initial test contract for slice-config`
- `1d6f410` — `feat(config): settings loader for slice-config`

Delivery:
- `src/musubi/config.py` — `get_settings()` singleton (lru_cache-backed) + `MUSUBI_DOTENV` env escape hatch for tests; repo-wide guardrail against `os.environ` reads outside this module is enforced by a lint-style test.
- `src/musubi/settings.py` — pydantic-settings `BaseSettings` subclass, frozen, 20+ typed fields (Qdrant, TEI, Ollama, Core ports/paths, auth, feature flags), `SecretStr` on secrets with masked `__repr__`.
- `.env.example` at repo root with commented guidance matching `docs/architecture/08-deployment/compose-stack.md §Env`.
- `tests/test_config.py` — 15 tests (instance-caching, env ↔ dotenv precedence, fail-fast on missing required, repr masking, type coercion, invalid values rejected, feature-flag defaults, and a lint-style test asserting no other module reads `os.environ`).

Test Contract Closure state: **✓ satisfied** — the referenced spec ([[00-index/conventions]]) has no `## Test Contract` section with bullets to track; `make tc-coverage SLICE=slice-config` reports 0 bullets, 0 missing. Coverage on owned files is 100 %.

This slice needs a **review pass** (by a different agent or a human) before flipping to `done` — the bar Cowork's session didn't meet because it bypassed the PR/review lifecycle. Per the "no self-approval" rule, neither Cowork nor the slice-worker that invokes the follow-up can approve their own first cut.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
