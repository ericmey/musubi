---
name: Spec update
about: Propose or track a change to an architecture spec or ADR, independent of code.
title: "spec: <short description>"
labels: ["spec"]
---

## Spec

- Path: `docs/Musubi/<NN-section>/<doc>.md`
- Current status: `complete | draft | stub | research-needed`

## What changes and why

Prose description of the change. Include the "why" — what drove this, what constraint or discovery forced it.

## Impact on code and tests

Which modules, Test Contract bullets or open issues does this change affect?

- `src/musubi/<module>/` or `#<issue>`: impact description
- `test_<name>`: added / changed / removed

## Proposed by

Agent or human who opened this. If this came out of implementation work (a coding agent discovered the spec was wrong), link the PR.
