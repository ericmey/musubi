---
title: "H5 canonical plane transition boundary"
section: 13-decisions
type: adr
status: proposed
owner: codex-gpt5
phase: "Lifecycle-audit 2026-07-14 — H5 mutation-path unification"
tags: [type/adr, status/proposed, lifecycle, atomicity]
updated: 2026-07-14
supersedes: []
---

# H5 canonical plane transition boundary

## Decision

The five plane `transition()` methods delegate to
`lifecycle.transitions.transition()` and expose its typed three-way result. They do not call Qdrant
`set_payload` directly and do not translate `Pending` into a historical tuple success.

The coordinator is required at every transition call. Read-only plane construction remains valid, but a
transition without an injected coordinator fails closed as a typed error; production transition callers
receive the process-lifetime coordinator from the S7 composition roots.

Callers branch explicitly:

- `Final`: run existing success/dependent work exactly once;
- `Pending`: retain the operation/event identifiers and defer dependent work;
- `Err`: follow the existing terminal error policy.

Concept promotion's `promoted_to` and `promoted_at` are part of `TransitionIntent` and the canonical
intended patch. They therefore participate in the operation digest, server-side version-fenced update,
full readback confirmation, replay, and event lineage. H5 forbids a second post-transition payload write.

The concept promote and soft-delete HTTP routes use the existing `TransitionPendingBody` and declare the
same exact 202 OpenAPI schema as the S7 transition routes. Final response shapes remain unchanged.

## Rejected alternatives

- Keep plane-local `set_payload` after calling the coordinator: two writers and mutation-without-audit.
- Block or immediately retry Pending inside the request/sweep: defeats durable deferral and can duplicate
  work.
- Return the old `(model, event)` tuple for Pending: fabricates an applied row that does not exist.
- Apply `promoted_to` after Final: loses atomicity and replay/readback coverage.
- Optional coordinator with a direct-write fallback: silently reopens G1.

## Release boundary

H5 may merge after its exact-head independent review. C6b still may not be released or deployed as fixed
until the FILE-to-DIR migration artifact is authored, executed under maintenance quiescence, and its
rollback/readiness evidence is accepted.
