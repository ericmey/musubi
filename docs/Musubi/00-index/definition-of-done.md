---
title: Definition of Done
section: 00-index
type: index
status: complete
tags: [section/index, status/complete, type/index]
updated: 2026-09-30
up: "[[00-index/index]]"
reviewed: true
---

# Definition of Done

The checklist every pull request must satisfy before it merges. The short form is in [AGENTS.md](../../../AGENTS.md); this page holds the per-module coverage gates.

- [ ] All **Test Contract** items in the linked spec(s) are implemented as passing tests.
- [ ] **Coverage gates** met: 90% branch on `musubi/planes/**`, `musubi/retrieve/**`, 85% on `musubi/lifecycle/**`, `musubi/vault_sync/**`, 80% on `musubi/api/**`, 95% on `musubi/auth/**`.
- [ ] **`make check` passes clean** — ruff format, ruff lint, mypy strict, pytest + coverage.
- [ ] **No prohibited patterns** (see [[00-index/agent-guardrails#Prohibited patterns (automatic revert)]]): no `time.sleep()` in prod, no env reads outside `musubi/config.py`, no hardcoded hosts/ports/thresholds, no `except Exception: pass`.
- [ ] **Docs touched** where behavior changed — spec in `docs/Musubi/` updated, `spec-update: <path>` in commit trailer, spec `status:` still `complete` after the change.
- [ ] **PR body linked and current** — first line `Closes #<n>.` (or `No tracking Issue: <reason>`), and the body describes what shipped.
- [ ] **Deferred Test Contract bullets** are skipped with `reason="deferred to #<issue>: ..."` or declared out of scope in the PR description.
- [ ] **Reviewed and merged by someone other than the author.**

## Merge gates (CI-enforced)

These must be green automatically:

- `ruff format --check`
- `ruff check`
- `mypy --strict`
- `pytest --cov-fail-under=85`
- Contract tests (for API surface changes only) in `musubi-contract-tests`

## Post-merge signals

Not blocking, but track:

- **Latency budgets** (fast p95 < 400ms, deep p95 < 5s) — smoke runs in CI with reference fixtures.
- **Nightly chaos tests** green (kill Qdrant mid-write, race vault writes).
