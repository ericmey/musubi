---
title: "Slice: DATA-002 typed retraction evidence"
slice_id: slice-data002-typed-retraction-evidence
issue: 645
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "1-schema"
tags: [section/slices, status/in-progress, type/slice, data-001, data-integrity, idempotency]
updated: 2026-08-03
reviewed: false
depends-on: [slice-art004-exact-byte-escrow]
blocks: []
---

# Slice: DATA-002 typed retraction evidence

Implement ADR 0042's strict anchor-local evidence contract and the narrow
VAL-002 keyhole that recognizes a deliberate non-reembedding episodic
retraction without weakening ordinary projection-divergence checks.

## Decision boundary

- `retraction_evidence` is a strict optional episodic domain field; malformed,
  partial, or tag-only shapes fail canonical validation.
- The evidence names the derived artifact reference and namespace, exact
  original digest and UTF-8 byte length, exact prefix and omitted-byte
  accounting, original-vector basis, preserved pointer, opaque operation
  identity hash, and canonical request digest.
- VAL-002 permits divergence only for episodic v2 anchors whose evidence binds
  the current storage pointer and the current immutable content snapshot. It
  never permits curated divergence and never trusts evidence to attest its own
  target bytes.
- Legacy inline full-original tombstones remain valid. This slice adds no
  public endpoint, escrow write, or episodic mutation path.
- Endpoint construction, tombstone prose, immutable content/vector invariance
  across the write, and request replay semantics remain owned by #646.

## Specs to implement

- [[04-data-model/episodic-memory]]
- [[13-decisions/0042-escrow-backed-episodic-retraction]]

## Owned paths

- `src/musubi/types/episodic.py`
- `src/musubi/types/__init__.py`
- `src/musubi/cli/validate.py`
- `tests/types/test_episodic.py`
- `tests/cli/test_validate.py`
- `docs/Musubi/04-data-model/episodic-memory.md`
- `docs/Musubi/_slices/slice-data002-typed-retraction-evidence.md`
- `docs/Musubi/_inbox/locks/slice-data002-typed-retraction-evidence.lock`

## Test contract

The exact executable contract will be committed test-first after Aoi attacks
the evidence/storage binding boundary.

## Work log

- 2026-08-03 — Claimed #645 after ART-004 merged as #652 / `a26d82a`.
  Read-only mapping confirmed the validator already proves storage pointer
  identity and generation before projection comparison; this lane adds one
  episodic-only exception at that seam rather than a parallel pointer system.
