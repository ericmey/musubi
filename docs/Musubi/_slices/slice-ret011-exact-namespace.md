---
owner: claude-code-opus48
status: in-progress
issue: 510
title: "Slice: RET-011 exact deployment-namespace retrieval"
slice_id: slice-ret011-exact-namespace
section: _slices
type: slice
phase: "Retrieval"
tags:
  - section/slices
  - status/in-progress
  - type/slice
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---
# Slice: RET-011 exact deployment-namespace retrieval

## Context

Single-target retrieval could return a row from another presence in the same identity family.
`hybrid._build_filter` scoped to `identity_family` (the #332 federation key), not the exact
namespace, and — the actual leak — `hybrid._build_prefetch` built each vector prefetch with NO
filter, so candidate generation crossed presences and the top-level fusion filter did not remove
them (the hybrid spec already states "filters are not applied after fusion"). A second surface,
`fast._cache_key`, keyed on `family_of(namespace)`, so two presences shared a fast-response cache
entry. `recent` already filtered exact namespace and was the reference-correct behavior. Fix: #510.

## Invariant (Yua, 2026-07-15; #510 supersedes #332 for a CONCRETE target)

A concrete deployment namespace target (`tenant/presence/plane`) returns ONLY that presence's
rows — never a sibling presence in the same identity family. Cross-presence / identity-family
retrieval is authorized ONLY when the request explicitly resolves multiple concrete
`namespace_targets` (a wildcard expanded upstream by `retrieve._expand_wildcard_targets`, each
concrete leg exact-filtered and unioned). Scope/auth wildcard matching and lifecycle **synthesis**
family federation are UNCHANGED. This slice does not touch lifecycle-state semantics.

## Specs to implement
- [[05-retrieval/hybrid-search]] — § "Filter pushdown" decision note (#510 supersedes #332)

## Owned paths
- `tests/retrieve/test_ret011_exact_namespace.py`
- `tests/api/test_ret011_streaming_namespace.py`
- `tests/retrieve/test_ret011_exact_namespace_integration.py`
- `docs/Musubi/_slices/slice-ret011-exact-namespace.md`

## Forbidden paths
- Authorization / namespace naming policy; lifecycle-state filtering semantics; synthesis
  family federation; `recent` (already exact). No ADR.

## Modified (owned by shipped slices — coordinated via the lock)
- `src/musubi/retrieve/hybrid.py` (slice-retrieval-hybrid, done) — `_build_filter` exact
  namespace; `_build_prefetch` namespace-scopes each prefetch (the leak fix); `_namespace_filter`
  / `_namespace_condition` helpers.
- `src/musubi/retrieve/fast.py` (slice-retrieval-fast, done) — `_cache_key` keys on exact
  namespace, not `family_of`.
- `docs/Musubi/05-retrieval/hybrid-search.md` (slice-retrieval-hybrid, done) — decision note +
  Test Contract (bullet 3 renamed to the real test; RET-011 bullets 17-22 added).
- `tests/retrieve/test_hybrid.py` (slice-retrieval-hybrid, done) — the `identity_family` test is
  the explicit #510-over-#332 contract reversal, now `test_namespace_filter_applied_not_identity_family`.

## Test Contract
Two presences of one identity family with IDENTICAL content (vector cannot discriminate — only
the namespace filter can):
- `test_concrete_target_does_not_leak_sibling_presence` (fast / deep / blended) — RED pre-fix.
- `test_fast_cache_does_not_serve_sibling_presence` — the fast-cache leak, RED pre-fix.
- `test_streaming_concrete_target_is_presence_exact` — `/v1/retrieve/stream` agrees, RED pre-fix.
- `test_recent_concrete_target_is_presence_exact` — green guard (recent already exact).
- `test_explicit_multi_target_still_returns_all_presences` (fast/deep/blended/recent) — wildcard
  non-regression: explicit multi-target still unions both presences.
- `test_namespace_filter_applied_not_identity_family` (test_hybrid.py) — top-level + prefetch
  both namespace-scoped, identity_family absent.
- `test_concrete_target_exact_namespace_real_qdrant` (integration) — real-Qdrant proof.

## Definition of Done
- fast/deep/blended/recent + streaming all presence-exact for a concrete target.
- Wildcard / explicit multi-target still unions presences; auth + synthesis unchanged.
- Full gate green; real-Qdrant integration proof green; exact-head CI.

## Work log
- Grounded the leak: `identity_family` filter + UNFILTERED prefetch (the real leak site — top-level
  fusion filter does not gate candidates) + `family_of` cache key. Wrote the 5-RED matrix; proved
  RED pre-fix.
- Fix is three bounded changes: exact `_build_filter`, namespace-scoped prefetch (namespace-ONLY,
  to not silently broaden into lifecycle-state semantics), exact `_cache_key`. Updated the
  `identity_family` test as the explicit reversal. All green; real-Qdrant proof green.
- Discovered separately (filed as its own issue, NOT fixed here): the top-level fusion filter does
  not constrain prefetch candidates, so lifecycle **state** filtering (only on the top-level filter,
  not the prefetch) is not enforced in the hybrid path — a real-Qdrant reproduction confirms it.

### Out-of-scope: pre-existing hybrid-search Test Contract bullet

`test_hybrid_timeout_returns_partial_results` (hybrid-search.md § Test Contract, bullet 9) is a
STALE name owned by `slice-retrieval-hybrid`: RET-007 changed the timeout contract from
partial-results to a typed `Err`, and the live test is `test_hybrid_timeout_returns_err`
(`tests/retrieve/test_hybrid.py:288`). Not RET-011's to re-contract; declared out-of-scope here so
the Closure Rule is honestly machine-green. Doc-hygiene follow-up for the hybrid/RET-007 owner.
