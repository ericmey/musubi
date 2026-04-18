<!--
Close the related Issue by putting `Closes #<n>` in the body (GitHub resolves it on merge).
If this PR changes a spec in the same commit set, add the `spec-update: <doc-path>` trailer
to the relevant commit.
-->

## Slice

- ID: `slice-<id>`
- Issue: #
- Spec(s) implemented: `docs/architecture/<NN>/<doc>.md#<section>` (one per line)

## Summary

<One or two sentences — what landed, why. The "why" matters more than the "what"; the diff shows the what.>

## Test Contract coverage matrix (required)

Per [agent-guardrails.md §Test Contract Closure Rule](../docs/architecture/00-index/agent-guardrails.md#test-contract-closure-rule), every bullet in the spec's `## Test Contract` section must be in one of three states: **passing test** / **skipped with reason** / **declared out-of-scope in slice work log**. Fill in one row per bullet. No silent omissions.

| Bullet | State | Evidence |
|---|---|---|
| `test_foo_does_bar` | ✓ passing | `tests/module/test_foo.py:42` |
| `test_baz_edge_case` | ⏭ skipped (deferred to `slice-xyz`: reason) | `tests/module/test_foo.py:110` (marker present) |
| `test_out_of_scope_behavior` | ⊘ declared out-of-scope | `_slices/<slice-id>.md#Work log` |

## Definition of Done

- [ ] Slice frontmatter: `status: in-progress → in-review`, `owner` set.
- [ ] First commit in branch history is the test file (`test(...)` commit precedes any `feat(...)`).
- [ ] `make check` passes (ruff format --check + ruff check + mypy --strict + pytest + coverage `fail_under=85`).
- [ ] `make agent-check` passes (vault frontmatter + slice DAG + spec hygiene via `docs/architecture/_tools/check.py`).
- [ ] Import discipline respected (`sdk` → `types` only; `adapters` → `sdk+types` only; `api` composes `planes`/`retrieve`/`lifecycle`).
- [ ] No edits to `src/musubi/types/`, `src/musubi/api/`, `openapi.yaml`, or `proto/` unless this slice owns them.
- [ ] Spec `status:` updated if prose changed. Commit trailer `spec-update: <doc-path>` present.
- [ ] Method-ownership honoured — no methods deferred to a slice whose `owns_paths` wouldn't contain their implementation (per [agent-guardrails.md §Method-ownership rule](../docs/architecture/00-index/agent-guardrails.md#method-ownership-rule)).
- [ ] Slice note's `## Work log` has a handoff entry describing what landed and naming any deferred Test Contract bullets + their follow-up home.
- [ ] If this realises a spec: an entry in `docs/architecture/00-index/work-log.md` too.

## Agent attribution

Agent(s) that worked on this PR (one per line): `<agent-id>` (e.g., `eric-cc-opus47`, `yua-cowork`, `codex-gpt5`, `gemini-3-1`). Include commit-author / co-author mapping so human reviewers know what tool shipped what.

## Risk + rollback

- Risk level: low / medium / high (one-line justification).
- Rollback plan: `git revert <sha>` is sufficient / requires also doing X / migration needed.
