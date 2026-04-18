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

## Test Contract coverage

Walk through the `## Test Contract` bullets from the spec(s). For each: covered / deferred (name follow-up slice) / intentionally-out-of-scope.

- [ ] `test_…_…` — covered
- [ ] `test_…_…` — covered
- [ ] `test_…_…` — deferred to `slice-…` (reason)

## Definition of Done

- [ ] Slice frontmatter: `status: in-progress → in-review`, `owner` set.
- [ ] First commit in branch history is the test file.
- [ ] `make check` passes (ruff + mypy + pytest + coverage ≥ 85 % on owned paths; 90 % on `planes/**` and `retrieve/**`).
- [ ] Import discipline respected (`sdk` → `types` only; `adapters` → `sdk+types` only; `api` composes `planes`/`retrieve`/`lifecycle`).
- [ ] No `src/musubi/types/`, `src/musubi/api/`, `openapi.yaml`, or `proto/` edits unless this slice owns them.
- [ ] Spec `status:` updated if prose changed. Commit trailer `spec-update: <doc-path>` present.
- [ ] Slice note's `## Work log` has a handoff entry describing what landed.
- [ ] If this realises a spec: an entry in `docs/architecture/00-index/work-log.md` too.

## Agent attribution

Agent(s) that worked on this PR (one per line): `<agent-id>` (e.g., `eric-cc-opus47`, `yua-cowork`, `codex-gpt5`, `gemini-3-1`). Include commit-author / co-author mapping so human reviewers know what tool shipped what.

## Risk + rollback

- Risk level: low / medium / high (one-line justification).
- Rollback plan: `git revert <sha>` is sufficient / requires also doing X / migration needed.
