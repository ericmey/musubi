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
  the current storage pointer, the current immutable content snapshot, and the
  exact stored-unindexed escrow artifact head. It never permits curated
  divergence and never trusts evidence to attest its own target bytes.
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

1. Strict typed evidence round-trips through the public episodic model without
   exposing DATA-001 layout fields.
2. Wrong artifact-id shape, non-whole-artifact references, noncanonical hashes,
   extra fields, partial evidence, a non-sibling artifact namespace, inconsistent
   byte accounting, or an ambiguous preserved-pointer shape fail closed.
3. Ordinary accidental episodic and curated projection divergence remains
   broken.
4. A fully bound v2 retraction divergence is clean only when pointer,
   generation, stored original digest/length, non-empty literal prefix,
   omitted-byte accounting, and exact stored-unindexed escrow head all verify
   against scanned storage.
5. Wrong live point, original digest/length, prefix accounting, stale
   generation, valid-but-wrong deterministic artifact id, missing artifact,
   wrong artifact state, or artifact digest/length mismatch each fails closed
   with a discriminating error.
6. A partial episodic scan with an unseen content target returns
   incomplete/unknown and makes neither a broken nor a clean cross-row claim.
7. Existing inline legacy tombstones remain valid without evidence.
8. Whole-dict content-point and vector invariance across the actual mutation is
   owned and mechanically required by #646; this schema/validator lane writes
   no episodic row and does not manufacture a mutation proof.

## Work log

- 2026-08-03 — Claimed #645 after ART-004 merged as #652 / `a26d82a`.
  Read-only mapping confirmed the validator already proves storage pointer
  identity and generation before projection comparison; this lane adds one
  episodic-only exception at that seam rather than a parallel pointer system.
- 2026-08-03 — Aoi's plan attack found the only evidence field still bound to
  nothing: `artifact_ref.artifact_id`. A purged escrow would otherwise leave a
  permanently clean-looking tombstone pointing at no supported original. The
  contract now requires VAL-002 to resolve the deterministic address against
  the scanned artifact plane and verify stored-unindexed state, digest, and
  length. Cross-plane conclusions are suppressed when either required scan is
  incomplete. Prefix length is `>= 1`; zero would make literal containment
  vacuous. #646 owns the fixed requirement that every server-built tombstone
  embeds that literal storage-derived prefix.
- 2026-08-03 — Test Contract closure for the broad episodic-memory spec keeps
  these pre-existing bullets explicitly out of this schema/validator lane:
  `test_maturation_sets_matured_after_ttl_and_scores_importance` and
  `test_maturation_skips_already_matured` belong to the completed
  `slice-lifecycle-maturation`; `test_query_hybrid_returns_scored_results_in_descending_order`
  belongs to completed `slice-retrieval-hybrid`;
  `test_forward_compat_reads_schema_version_0_point` remains owned by a future
  schema-migration slice; `test_perf_create_under_100ms_p95_on_reference_host`
  and `test_perf_dedup_query_under_30ms_p95` belong to the integration/perf
  harness; `hypothesis: idempotency — re-ingesting same content N times produces 1 memory with reinforcement_count == N`
  and `hypothesis: lifecycle monotonicity — state transitions never go backwards (except explicit revive operation)`
  belong to `slice-plane-episodic-followup`. DATA-002 changes neither plane
  behavior nor these deferred contracts.
