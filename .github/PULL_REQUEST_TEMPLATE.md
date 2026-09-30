<!--
First line of the body: `Closes #<n>.` (GitHub only links on Closes/Fixes/Resolves),
or `No tracking Issue: <one-sentence reason>` for a chore/docs change with no issue.
If this PR changes a spec, add the `spec-update: <doc-path>` trailer to that commit.
-->

Closes #

## Summary

<One or two sentences: what landed and why. The diff shows the what; the why matters more.>

Spec(s) implemented or changed: `docs/Musubi/<NN>/<doc>.md#<section>` (one per line, or "none")

## Test Contract coverage (required when a spec is implemented)

Per the Test Contract Closure Rule in [AGENTS.md](../AGENTS.md), every bullet in the spec's
`## Test Contract` is in exactly one state: **passing test**, **skipped with a reason naming the
follow-up issue**, or **declared out of scope here**. One row per bullet. No silent omissions.

| # | Bullet | State | Evidence |
|---|---|---|---|
| 1 | `test_foo_does_bar` | ✓ passing | `tests/module/test_foo.py:42` |
| 2 | `test_baz_edge_case` | ⏭ skipped (deferred to #123: reason) | `tests/module/test_foo.py:110` |
| 3 | `test_out_of_scope_behavior` | ⊘ out of scope | reason + follow-up issue, stated here |

## Definition of Done

- [ ] Behaviour change: the first commit is the test file (`test(...)` before any `feat(...)`). Docs/chore PR: n/a.
- [ ] `make check` passes (ruff format --check + ruff check + mypy --strict + pytest + coverage).
- [ ] `make agent-check` passes (docs frontmatter, spec Test Contracts, wikilinks).
- [ ] `gh pr checks` green.
- [ ] Import discipline respected (`sdk` → `types` only; `adapters` → `sdk` + `types` only; `api` composes `planes`/`retrieve`/`lifecycle`).
- [ ] No changes to `src/musubi/api/`, `openapi.yaml` or `proto/` without an ADR (additive) or a version bump (breaking).
- [ ] Spec updated in this PR if behaviour changed (`spec-update:` trailer).
- [ ] The body above describes what shipped, not the original plan.

## Agent attribution

Agent(s) that worked on this PR, one per line (e.g. `claude-code-opus`, `codex-gpt5`, `gemini-3-1`),
with the commit-author mapping so reviewers know which tool shipped what.

## Risk + rollback

- Risk level: low / medium / high (one-line justification).
- Rollback plan: `git revert <sha>` is sufficient / also requires X / migration needed.
