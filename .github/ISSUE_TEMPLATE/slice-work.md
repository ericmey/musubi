---
name: Slice work
about: Track implementation of one slice from the architecture vault.
title: "slice: <slice-id>"
labels: ["slice", "status:ready"]
---

<!--
Create this issue *before* starting work on a slice. It is the coordination lock
for multi-agent development — assignee = owner; only one agent at a time.
-->

## Slice

- ID: `slice-<id>`
- Path: `docs/Musubi/_slices/slice-<id>.md`
- Phase: `<1 Schema | 2 Infra | 3 … etc>`

## Depends on (must be `status: done` or first-cut merged)

- [ ] `slice-...`
- [ ] `slice-...`

## Unblocks

- `slice-...`
- `slice-...`

## Specs to implement

- `docs/Musubi/<NN-section>/<doc>.md`

## Test Contract (reference)

Point at the spec section that lists the bullets. Don't duplicate them here — drift hazard.

## Agent assignment

Claim this issue by assigning yourself:

```
gh issue edit <n> --add-assignee @me --add-label "status:in-progress" --remove-label "status:ready"
```

Labels tell other agents the state at a glance:

- `status:ready` — unassigned, any agent may claim.
- `status:in-progress` — an agent is actively coding.
- `status:in-review` — PR ready for review; `musubi-reviewer` sub-agent is a good first pass.
- `status:blocked` — see the issue comments or the cross-slice ticket it references.
- `status:done` — implementation merged; issue should close automatically via the PR's `Closes #<n>`.
