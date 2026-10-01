---
title: Blended Retrieval
section: 05-retrieval
tags: [blending, dedup, planes, retrieval, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
implements: ["src/musubi/retrieve/blended.py", "tests/retrieve/test_blended.py"]
---
# Blended Retrieval

A single query that returns the best results from multiple planes, with cross-plane deduplication. Select it with `mode="blended"`.

## Why blend

A naive setup runs three separate queries ("search curated", "search concepts", "search episodic") and lets the client combine them. That:

- Triples client complexity.
- Prevents cross-plane deduplication.
- Gives up on lineage-aware dropping (e.g., showing both a concept and its promoted curated is redundant).

Blended retrieval centralizes this in the Core.

## The merge algorithm

`run_blended_retrieve` (`src/musubi/retrieve/blended.py`) runs a **full deep retrieval per plane**: each leg is its own `run_deep_retrieve` call (hybrid → cross-encoder rerank → score → lineage hydrate) with `limit = query.limit * 2`. Blended always runs deep internally. Then:

```
1. Flatten the per-plane ScoredHits into a single list.
2. Content-dedup (hash, or tag-Jaccard + cosine).
3. Lineage-aware drop (concept→curated collapse, supersession).
4. Sort by the existing score, desc.
5. Trim to limit.
```

There is no separate rerank or rescoring after the merge: each hit keeps the score its deep leg gave it.

### Content dedup

Two hits are "duplicates" if:

- Their first-300-character content SHA-256 hashes match exactly, OR
- Their tag-set Jaccard ≥ 0.5 AND their content cosine similarity ≥ 0.92.

For the cosine check, blended first collects every candidate pair that passes the Jaccard test, then embeds the first 500 characters of each candidate's content in **one batched `embed_dense` call** and compares those vectors.

Before dedup, hits are sorted by provenance (curated > concept > episodic-matured/promoted > episodic-provisional), then by score, so the kept copy is the one with the highest provenance, and if tied, the highest score.

### Lineage-aware drop

A concept that has been promoted to a curated file is redundant with that curated file — we already have the more-authoritative version. Drop the concept.

Algorithm:

```python
promoted_curateds = {h.object_id for h in hits if h.plane == "curated"}
to_drop = {
    h.object_id for h in hits
    if h.plane == "concept" and h.lineage.promoted_to.object_id in promoted_curateds
}
```

Symmetric rule for supersession:

```python
to_drop |= {h.object_id for h in hits if h.lineage.superseded_by.object_id in {x.object_id for x in hits}}
```

A hit that's been superseded, if its superseder is also in the result set, is dropped. If the superseder isn't in the result set, the old hit stays (the caller wanted it for a reason; we don't hide it silently). Both rules read the lineage that the deep leg hydrated.

### Rerank happens inside each leg

Each deep leg reranks its own candidates with the cross-encoder (see [[05-retrieval/reranker]]); plane does not influence the cross-encoder score. Provenance enters through the unified score each leg computes. The merge sorts on those existing scores.

### Through `/v1/retrieve`

The orchestrator splits every request into one target per `(namespace, plane)` and runs `run_blended_retrieve` once per target with a single plane ([[05-retrieval/orchestration]]). So over HTTP, content dedup and lineage drops apply to the hits **within each target**, and the cross-target merge afterwards dedups by `object_id` only. Cross-plane content dedup and the concept→curated drop only take effect when `run_blended_retrieve` is called directly with several planes.

## Default plane scope

```python
planes = ("curated", "concept", "episodic")   # BlendedRetrievalQuery default
```

Artifacts are not in the default set because artifact chunks are usually too granular for blended — they're queried explicitly when a citation is being resolved. Callers can opt in with `planes=["curated", "concept", "episodic", "artifact"]`.

With artifacts enabled, chunks surface alongside the other planes, scored with the same unified scorer (see [[05-retrieval/scoring-model]]).

## Namespace scope

`blended` is a retrieval mode, not a namespace plane. The public API rejects a
three-segment namespace whose final plane is `/blended`. A two-segment
`tenant/blended` is a valid literal presence through the API. The older
internal blended function expands `tenant/blended` across presences only when
the caller supplies an explicit nonempty `presences` list; it rejects the
implicit form rather than falling back to a default presence list. Public callers
should use an explicit namespace for one presence or a scoped wildcard
retrieve across presences. Authorization still applies to every namespace
returned.

See [[10-security/auth]] for the token-scope mapping.

## Score normalization within a blend

Not implemented as a separate step. Blended does not re-normalize RRF across planes; it sorts on the scores the deep legs already produced (relevance there is the sigmoid of the cross-encoder score when the leg reranked). Across targets, the orchestrator's RET-012 seam re-anchors relevance before the merge ([[05-retrieval/cross-plane-ranking]]).

## When blend is wrong

Blended is wrong when the caller knows exactly which plane it wants:

- "Show me the runbook for deploying the voice agent" → **curated only**, top-1.
- "What did the assistant say about the release check this morning?" → **episodic** for that presence.

Both cases are expressible via `planes=[...]` and an explicit namespace. Don't blend when you shouldn't.

## Edge cases

### Empty single plane

If one plane returns zero hits, the merge treats it as an empty list and proceeds. No error, no warning.

### All planes empty

Results = `[]` with **no** warning: a plane that ran and matched nothing is healthy. Only genuine degradation (a failed plane leg, a sparse fallback, a reranker fallback) adds a warning.

### Plane failures

A failed leg adds `plane_timeout_<plane>` (timeout) or `plane_error_<plane>` (other errors) and the other planes continue. If every leg fails, the result is an error: `all_planes_timeout` when they all timed out, otherwise `all_planes_failed`.

### Massive skew

If one plane returns 100 hits and another returns 2, each is reranked within its own leg and all survivors compete on score at the merge. No per-plane rate-limiting at the merge step.

### Cross-namespace retrieval

Use a scoped wildcard retrieve to search multiple namespaces. Token scope must
cover the requested tenant; cross-tenant retrieval remains disallowed in v1.

## Test Contract

**Module under test:** `src/musubi/retrieve/blended.py`

Merge:

1. `test_merge_flattens_per_plane_lists`
2. `test_content_dedup_hash_exact`
3. `test_content_dedup_jaccard_plus_cosine_deep_only`
4. `test_dedup_keeps_highest_provenance`

Lineage:

5. `test_concept_dropped_when_promoted_curated_present`
6. `test_concept_kept_when_promoted_curated_absent`
7. `test_superseded_dropped_when_superseder_present`
8. `test_superseded_kept_when_superseder_absent`

Scope:

9. `test_default_planes_cover_curated_concept_episodic`
10. `test_artifact_opted_in_surfaces_chunks`
11. `test_legacy_blended_namespace_requires_explicit_presences`

Scoring (deferred: empty stubs):

12. `test_relevance_normalized_across_planes_pre_score` — deferred
13. `test_plane_agnostic_rerank_orders_ignoring_plane` — deferred
14. `test_provenance_still_influences_final_rank` — deferred

Edge cases:

15. `test_one_plane_empty_merge_succeeds`
16. `test_all_planes_empty_returns_empty_warning` (asserts no warning on a healthy empty result)
17. `test_cross_tenant_blend_forbidden` — deferred (empty stub; enforced by the auth layer)

Property and integration (deferred: skipped stubs):

18. `test_hypothesis_blend_result_contains_no_pair_of_lineage_ancestor_and_descendant` — deferred
19. `test_hypothesis_content_dedup_is_idempotent` — deferred
20. `test_integration_real_corpus_with_3_planes_blended_vs_per_plane_manual_shows_dedup_removes_10_percent_redundant_hits` — deferred
