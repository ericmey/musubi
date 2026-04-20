---
title: Blended Retrieval
section: 05-retrieval
tags: [blending, dedup, planes, retrieval, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[05-retrieval/index]]"
reviewed: false
implements: ["src/musubi/retrieve/blended.py", "tests/retrieve/test_blended.py"]
---
# Blended Retrieval

A single query that returns the best results from multiple planes. This is the default mode for human-facing assistants ("help me remember…") and for coding-agent planning.

## Why blend

A naive setup runs three separate queries ("search curated", "search concepts", "search episodic") and lets the client combine them. That:

- Triples client complexity.
- Prevents cross-plane deduplication.
- Gives up on lineage-aware dropping (e.g., showing both a concept and its promoted curated is redundant).

Blended retrieval centralizes this in the Core.

## The merge algorithm

Input: `list[list[Hit]]` — one list per plane queried. Each hit is already RRF-scored within its plane.

```
1. Flatten into a single list.
2. Content-dedup (fuzzy, hash+Jaccard).
3. Lineage-aware drop (concept→curated collapse).
4. Rerank (deep only) — plane-agnostic.
5. Unified score.
6. Sort desc.
7. Trim to limit.
```

### Content dedup

Two hits are "duplicates" if:

- Their first-300-char content hash matches exactly, OR
- Their tag-set Jaccard ≥ 0.5 AND their content cosine similarity ≥ 0.92.

Fast path uses only the hash check (cheaper). Deep path uses both.

When duplicates are found, we keep the one with highest provenance (curated > concept > episodic-matured > episodic-provisional), and if tied, highest score.

### Lineage-aware drop

A concept that has been promoted to a curated file is redundant with that curated file — we already have the more-authoritative version. Drop the concept.

Algorithm:

```python
promoted_curateds = {h.object_id for h in hits if h.plane == "curated"}
to_drop = {
    h.object_id for h in hits
    if h.plane == "concept" and h.promoted_to in promoted_curateds
}
hits = [h for h in hits if h.object_id not in to_drop]
```

Symmetric rule for supersession:

```python
to_drop |= {h.object_id for h in hits if h.superseded_by in {x.object_id for x in hits}}
```

A hit that's been superseded, if its superseder is also in the result set, is dropped. If the superseder isn't in the result set, the old hit stays (user wanted it for a reason; we don't hide it silently).

### Plane-agnostic rerank

See [[05-retrieval/reranker]]. All hits are fed flat to the reranker; plane doesn't influence the cross-encoder score. Provenance weighting re-enters at the scoring step.

## Default plane scope

```python
DEFAULT_PLANES = ["curated", "concept", "episodic"]
```

Artifacts are not in the default set because artifact chunks are usually too granular for blended — they're queried explicitly when a citation is being resolved. Callers can opt in:

```python
query = RetrievalQuery(
    ...,
    planes=["curated", "concept", "episodic", "artifact"],
)
```

With artifacts enabled, chunks surface alongside the other planes. Chunks are scored with provenance 0.7 (see [[05-retrieval/scoring-model]]).

## Blended scope for the voice agent

`"blended"` is also a convention for a *namespace scope* that means "search across all my planes in this tenant." The voice agent uses:

```python
namespace = "eric/livekit-voice/blended"
```

The Core expands this at query time to:

```python
namespaces = [
    "eric/_shared/curated",
    "eric/_shared/concept",
    "eric/livekit-voice/episodic",
    "eric/claude-code/episodic",      # yes, the voice agent can pull from other presences' episodic
    "eric/claude-desktop/episodic",
]
```

Which presences' episodic are included in the blend is configurable per-presence (privacy scope). By default: same tenant, all presences; user can exclude specific presences via config.

See [[10-security/auth]] for the token-scope mapping.

## Score normalization within a blend

Before scoring, per-plane `rrf_score` values need normalizing to [0, 1] within the batch. We do this once on the flattened list:

```python
batch_max = max(h.rrf_score for h in hits) or 1.0
for h in hits:
    h.relevance_normalized = h.rrf_score / batch_max
```

Then the unified scorer consumes `relevance_normalized`. This approach handles the case where one plane has systematically higher RRF scores than another (e.g., small curated collection → less rank-collision → higher RRF peaks).

## When blend is wrong

Blended is wrong when the caller knows exactly which plane it wants:

- "Show me the runbook for deploying the voice agent" → **curated only**, top-1.
- "What did Claude say about the GPU check this morning?" → **episodic** filtered on capture_presence.

Both cases are expressible via `planes=[...]` on `RetrievalQuery`. Don't blend when you shouldn't.

## Edge cases

### Empty single plane

If one plane returns zero hits, the merge treats it as an empty list and proceeds. No error.

### All planes empty

Results = `[]`, response is 200 with `warnings: ["no hits in any plane"]`. Caller decides next step.

### Massive skew

If one plane returns 100 hits and another returns 2, we still rerank everything together — the cross-encoder is plane-agnostic. No per-plane rate-limiting at the merge step.

### Cross-namespace in blended scope

Explicitly allowed. Token must carry scope for all expanded namespaces. Cross-tenant is still disallowed in v1.

## Test Contract

**Module under test:** `musubi/retrieval/blending.py`

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
11. `test_blended_namespace_expands_to_tenant_presences`

Scoring:

12. `test_relevance_normalized_across_planes_pre_score`
13. `test_plane_agnostic_rerank_orders_ignoring_plane`
14. `test_provenance_still_influences_final_rank`

Edge cases:

15. `test_one_plane_empty_merge_succeeds`
16. `test_all_planes_empty_returns_empty_warning`
17. `test_cross_tenant_blend_forbidden`

Property:

18. `hypothesis: blend result contains no pair of lineage-ancestor + descendant`
19. `hypothesis: content dedup is idempotent`

Integration:

20. `integration: real corpus with 3 planes, blended vs per-plane manual shows dedup removes ~10% of redundant hits`
