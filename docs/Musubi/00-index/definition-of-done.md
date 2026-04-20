---
title: Definition of Done
section: 00-index
type: index
status: complete
tags: [section/index, status/complete, type/index]
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: true
---

# Definition of Done

The universal checklist every slice must satisfy before it can be marked `status: done`. Slice notes in `_slices/` transclude this section with `![[00-index/definition-of-done]]`.

- [ ] All **Test Contract** items in the linked spec(s) are implemented as passing tests.
- [ ] **Coverage gates** met: 90% branch on `musubi/planes/**`, `musubi/retrieve/**`, 85% on `musubi/lifecycle/**`, `musubi/vault_sync/**`, 80% on `musubi/api/**`, 95% on `musubi/auth/**`.
- [ ] **`make check` passes clean** — ruff format, ruff lint, mypy strict, pytest + coverage.
- [ ] **No prohibited patterns** (see [[00-index/agent-guardrails#Prohibited patterns (automatic revert)]]): no `time.sleep()` in prod, no env reads outside `musubi/config.py`, no hardcoded hosts/ports/thresholds, no `except Exception: pass`.
- [ ] **Docs touched** where behavior changed — spec in `docs/Musubi/` updated, `spec-update: <path>` in commit trailer, spec `status:` still `complete` after the change.
- [ ] **Frontmatter updated** — slice note `status:` advanced, `updated:` bumped, PR link appended to the slice's PR list.
- [ ] **Lock released** — `_inbox/locks/<module>.lock` file removed.
- [ ] **Cross-slice tickets resolved** — any ticket this slice opened in `_inbox/cross-slice/` is either resolved or explicitly deferred.
- [ ] **Human-approved PR merged.**

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
