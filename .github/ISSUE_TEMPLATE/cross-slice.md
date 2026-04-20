---
name: Cross-slice coordination
about: A slice needs another slice to change before it can finish. Use this to coordinate without blocking on a file-based ticket.
title: "cross-slice: <source-slice> needs <target-slice> — <one-line ask>"
labels: ["cross-slice", "status:blocked"]
---

## Source slice (your in-progress work)

- ID: `slice-<id>`
- PR: #
- What you're trying to do and why it forces a change outside your `owns_paths`.

## Target slice (the one that needs to change)

- ID: `slice-<id>`
- Owned paths that need modification.
- Minimal diff description: add endpoint X, add field Y to model Z, etc.

## Proposed approach

Your suggestion — not a demand. The target slice's owner may prefer a different shape.

## What happens next

- Flip your source slice to `status: blocked` (frontmatter + Issue label).
- The target slice's owner picks this up, lands the change, comments here.
- Re-pickup your source slice, unblock, continue.

## References

- `docs/Musubi/_slices/slice-<source>.md`
- `docs/Musubi/_slices/slice-<target>.md`
- If a spec disagreement drove this, name the spec: `docs/Musubi/<NN>/<doc>.md#<section>`.
