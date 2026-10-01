---
title: Definition of Done
section: 00-index
type: index
status: complete
tags: [section/index, status/complete, type/index]
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: true
---

# Definition of Done

The checklist every pull request must satisfy before it merges. The short form is in [AGENTS.md](../../../AGENTS.md); this page holds the coverage expectations and the CI gates.

- [ ] All **Test Contract** items in the linked spec(s) are implemented as passing tests.
- [ ] **Coverage:** the 85% branch-coverage floor is enforced (`fail_under = 85` in `pyproject.toml`). Changes under `src/musubi/planes/**` and `src/musubi/retrieve/**` are expected to reach 90%; that expectation is checked in review, not by CI (coverage.py has no per-module floor).
- [ ] **`make check` passes clean**: ruff format, ruff lint, mypy strict, pytest + coverage.
- [ ] **No prohibited patterns** (see [AGENTS.md "Hard prohibitions"](../../../AGENTS.md#hard-prohibitions-automatic-revert)): no `time.sleep()` in prod, no env reads outside `src/musubi/config.py`, no hardcoded hosts/ports/thresholds, no `except Exception: pass`.
- [ ] **Docs touched** where behavior changed: spec in `docs/Musubi/` updated, `spec-update: <path>` in commit trailer, spec `status:` still `complete` after the change.
- [ ] **PR body linked and current**: first line `Closes #<n>.` (or `No tracking Issue: <reason>`), and the body describes what shipped.
- [ ] **Deferred Test Contract bullets** are skipped with `reason="deferred to #<issue>: ..."` or declared out of scope in the PR description.
- [ ] **Reviewed and merged by someone other than the author.**

## Merge gates (CI-enforced)

These must be green automatically (`.github/workflows/ci.yml`, `vault-check.yml`, `evals.yml`):

- `ruff format --check`
- `ruff check`
- `mypy src tests` (strict)
- `pytest` with coverage (85% floor)
- `docs/Musubi/_tools/check.py all` when `docs/Musubi/` changes
- the deterministic retrieval smoke gate in `evals.yml` (no real models)

## Post-merge signals

Not blocking, but track:

- **Integration suite** (`integration.yml`): nightly, three runs against a Qdrant + TEI + Ollama test stack.
- **Scheduled live quality gate** (`evals.yml`): nightly retrieval-quality evals against a real Qdrant + TEI stack.
