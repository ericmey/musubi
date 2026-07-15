---
title: "Slice: DQ-001 retrieval content-truncation metadata"
slice_id: slice-dq001-content-truncation
issue: 443
section: _slices
type: slice
status: in-progress
owner: minimax-m3
phase: "Retrieval"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---

# Slice: DQ-001 retrieval content-truncation metadata

## What

Closes the Musubi-core wire contract gap: ``POST /v1/retrieve`` (and
``build_context_pack``) must surface the silent-truncation state and the
original (pre-cap) character length on every retrieval row so callers
can detect a silent cut and fetch the full body via ``object_id``.

This slice is bounded to the 5 source files in the Musubi core:
- ``src/musubi/api/responses.py``
- ``src/musubi/api/routers/retrieve.py``
- ``src/musubi/retrieve/fast.py``
- ``src/musubi/retrieve/recent.py``
- ``src/musubi/retrieve/orchestration.py``
plus ``src/musubi/retrieve/context_pack.py`` (5th + 1, included for
the context-pack surface) and ``openapi.yaml``.

## Why

Retrieval content was silently projected into snippets and exposed as
``content`` with no truncation signal. The 200/300/300-char caps
applied by fast / recent / ranked were not visible to callers; a
load-bearing fact after the cutoff could be unavailable with no
indication that more content existed.

## Contract

1. ``RankedResultRow`` and ``RecentResultRow`` carry
   ``content_truncated: bool = False`` and
   ``content_length: int | None = None``. Both default to no-truncation
   to preserve legacy serialization.
2. ``ContextPackItem`` (context-pack surface) carries the same two fields.
3. ``fast._snippet`` (fixed cap 200), ``recent._snippet`` (default 300),
   and ``orchestration._snippet`` (configurable, default 300) all
   return ``(snippet, content_truncated, content_length)``.
4. The router propagates the metadata from the hit into both row types
   at the wire projection.
5. ``openapi.yaml`` is regenerated to include the new fields in the
   corresponding schemas.

## Acceptance

- omitted field → ``False`` (default; backward-compat)
- explicit truncation (content > cap) → ``True``; original length preserved
- exact-cap content → ``False`` (no false truncation)
- multibyte/Unicode → length is the character count, not the byte count
- mode parity: fast / deep / blended / recent / context-pack all surface the
  same metadata with the same semantics.
- facts at ordinal characters 301 and 1501, and at the final character, are
  never omitted silently: the row reports truncation, the original length,
  and the stable ``object_id`` fetch handle.
- decomposed combining marks and multi-codepoint ZWJ emoji at the projection
  boundary are covered. The current projection remains code-point bounded;
  grapheme-safe cutting stays open under Issue #443 rather than being claimed
  complete by this metadata slice.

## Process drift disclosure

This slice did NOT begin test-first. The original production change
was committed first (5 source files + context_pack + openapi.yaml),
and the contract test file was committed in a follow-up commit. This
is a packaging correction disclosed in the PR body, not a redesign.
The contract tests in ``tests/api/test_retrieve_dq_001_content_truncation.py``
are the production proof.

## Out of scope (NOT closed by this slice)

- Deep and blended use the ranked projection path and its 300-character cap;
  both are covered by the same explicit metadata contract.
- ``ContextPackItem`` cap is explicit (``query.max_chars`` from the request
  body). The cap is the documented contract; the new metadata surfaces
  whether the cap was applied.
- **Cross-adapter acceptance and grapheme-safe cutting are NOT closed by this slice** (per Issue
  #443 acceptance). The fleet-tools Hermes provider and the live
  Nyla/Sumi adapters each have separate 200/300/400-char caps in
  their own repos. The contract tests cover the Musubi core; the
  fleet-tools and live-adapter tracking is a follow-up.
- Test-first process: documented as drift above; future slices should
  be tests-first.

## Work log

### 2026-07-15 — tama (this slice, non-test-first, packaged with follow-up contract commit)

Commits in branch order (additive, no amend, no force):

1. ``feat(dq001): surface context-pack truncation metadata on ContextPackItem``
   — adds the two metadata fields to ``ContextPackItem`` and
   ``_to_item``; ``openapi.yaml`` regenerated.
2. ``feat(dq001): surface silent-truncation metadata on retrieval rows`` —
   the bounded 5-file scope: response models, router projection, and
   the three ``_snippet`` helpers (fast / recent / ranked).
3. ``test(dq001): add the bounded 11-test contract file`` — the
   production-proof test file (untracked at handoff).
4. ``docs(dq001): slice-dq001-content-truncation.md`` — this slice doc.

## Out-of-band continuation

- Cross-adapter acceptance: fleet-tools Hermes provider and live
  Nyla/Sumi adapter (separate repos). This slice closes the Musubi
  core; the cross-adapter tracking is a separate scope.
- Grapheme-safe cutting: metadata makes a boundary cut explicit, but a future
  #443 slice must avoid splitting combining sequences and ZWJ emoji.
- Rename contract: a future breaking change (``content`` → ``snippet`` +
  new ``content`` field) was considered and explicitly deferred.
