---
name: Spec update
about: Propose or track a change to an architecture spec or ADR, independent of code.
title: "spec: <short description>"
labels: ["spec"]
---

## Spec

- Path: `docs/architecture/<NN-section>/<doc>.md`
- Current status: `complete | draft | stub | research-needed`

## What changes and why

Prose description of the change. Include the "why" — what drove this, what constraint or discovery forced it.

## Impact on slices

Which slices' `owns_paths`, Test Contracts, or `depends-on` graphs are affected?

- `slice-<id>` — impact description
- `slice-<id>` — impact description

## Proposed by

Agent or human who opened this. If this came out of implementation work (a coding agent discovered the spec was wrong), link the PR.
