---
title: "Slice: IDEM-005 typed episodic oversize rejection"
slice_id: slice-idem005-typed-oversize
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/in-progress, type/slice, api, idempotency]
updated: 2026-08-02
reviewed: false
issue: 606
depends-on: [slice-idem004-operation-evidence-spec]
blocks: []
---

# Slice: IDEM-005 typed episodic oversize rejection

Replace the intentional create and batch-create pre-mutation 32 KiB policy guard's
untyped HTTP 500 with a client-terminal error that cannot be confused with the
existing embedding-time HTTP 413 path.

## Scope

- Add `CONTENT_TOO_LARGE` to the fixed error vocabulary at HTTP 422.
- Measure UTF-8 content bytes, not characters or total request-body bytes.
- Run the check after namespace write authorization but before idempotency
  acquisition and handler/plane execution.
- Preflight every batch item before executing any item.
- Preserve the plane-level guard as defense in depth.
- Keep batch capture outside the durable-receipt eligibility set.
- Scope this contract to create and batch-create. PATCH content replacement,
  including retraction, remains unguarded pending Issue #611's policy decision.

`CONTENT_TOO_LARGE` is the discriminator. A client must not infer this guarantee
from HTTP 422 alone because ordinary validation failures share that status. HTTP
413 remains an embedding/proxy signal and carries no equivalent pre-mutation
guarantee.

## Owned paths

- `src/musubi/api/errors.py`
- `src/musubi/api/routers/writes_episodic.py`
- `tests/api/test_idem005_typed_oversize.py`
- `docs/Musubi/07-interfaces/canonical-api.md`
- `docs/Musubi/13-decisions/0040-durable-operation-evidence-and-legacy-resolution.md`
- `docs/Musubi/_slices/slice-idem005-typed-oversize.md`

## Test Contract

1. Exactly 32,768 ASCII UTF-8 bytes reaches the episodic plane.
2. Exactly 32,768 multibyte UTF-8 bytes reaches the episodic plane.
3. Exactly 32,769 ASCII or multibyte UTF-8 bytes returns 422 with
   `CONTENT_TOO_LARGE`.
4. Rejection occurs before idempotency acquisition, durable-receipt access, and
   plane execution.
5. A later oversized batch item prevents every earlier item from executing.
6. Namespace write authorization precedes the size verdict.

## Work log

- 2026-08-02 — Live v1.18.2 probe returned untyped HTTP 500 for 32,769 ASCII
  content bytes with no receipt or namespace-count change; 32,768 was initially
  untested.
- 2026-08-02 — Aoi rejected HTTP 413 for the new contract because Musubi already
  observes embedding-time 413 after plane entry. The accepted design uses 422
  plus a mandatory code discriminator.
- 2026-08-02 — Aoi found that PATCH content replacement, including retraction,
  does not pass through either the API or plane create guards. The contract was
  scoped to create and batch-create, with the PATCH policy deferred explicitly.
