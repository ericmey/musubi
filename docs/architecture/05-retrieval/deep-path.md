---
title: Deep Path
section: 05-retrieval
tags: [deep, planning, retrieval, section/retrieval, slow-thinker, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[05-retrieval/index]]"
reviewed: false
implements: ["src/musubi/retrieve/deep.py", "tests/retrieve/test_deep.py"]
---
# Deep Path

Retrieval for planning, analysis, and background pre-fetch. Uses the full pipeline including reranker + lineage hydration. Budget is loose (p95 ≤ 5s) because the caller isn't a human waiting on a keystroke.

## Typical callers

- **Slow Thinker** in LiveKit: pre-fetches context while the user is mid-sentence, holds the results ready for the Fast Talker to consume.
- **Coding agent planning loops** (e.g., Claude Code, OpenClaw extension): ahead-of-action retrieval to ground the plan in memory.
- **Reflection job** (Lifecycle Worker): daily summary pass.
- **Evals harness**: replays golden queries against a corpus.

## Invocation

```python
results = await musubi.retrieve(
    RetrievalQuery(
        namespace="eric/claude-code/blended",
        query_text="how did we decide to promote concepts",
        mode="deep",
        limit=25,
        planes=["curated", "concept", "episodic"],
        include_lineage=True,
    )
)
```

`include_lineage=True` is the default on deep. It enables step 7 (lineage hydrate) in the orchestration pipeline.

## What deep path adds over fast path

1. **Cross-encoder rerank** (step 5 in orchestration). BGE-reranker-v2-m3 scores each candidate against the query. Replaces the `relevance` component.
2. **Lineage hydration** (step 7). Fetches:
   - Full body text for large-curated results truncated in the index.
   - Supersession chain tips (so the caller can follow "what replaced this?").
   - Source artifact metadata for any `supported_by` references.
   - Promoted-from / promoted-to for concepts and curated.
3. **Larger budgets** — more prefetch, looser timeouts, deeper merge.

## Slow Thinker pattern (LiveKit)

The voice agent runs two parallel loops:

```
User utterance stream
  │
  ├─► Fast Talker: ASR tokens → minimal retrieval → speak fragment
  │                 (fast path, 150ms budget per mini-query)
  │
  └─► Slow Thinker: accumulating transcript → deep-path retrieval
                    (deep path, 2s budget, running concurrently)
                    → result cache available for Fast Talker's next turn
```

When the Fast Talker needs context during speech generation, it checks the Slow Thinker's cache first. If hot, it uses that (richer, reranked) context. If cold, it falls back to fast-path retrieval.

This gives us the effect of "deep retrieval at conversational latency" without blocking speech. See [[07-interfaces/livekit-adapter]] for the adapter's implementation plan.

## Result shape additions

Deep-path results include hydrated lineage:

```json
{
  "object_id": "...",
  "plane": "curated",
  "title": "CUDA 13 setup notes",
  "snippet": "...",
  "score": 0.82,
  "score_components": { ... },
  "lineage": {
    "supersedes": [
      {"object_id": "...", "title": "CUDA 12 setup notes", "state": "superseded"}
    ],
    "superseded_by": null,
    "promoted_from": {"object_id": "...", "title": "CUDA install pattern"},
    "supported_by": [
      {"artifact_id": "...", "chunk_id": "...", "title": "nvidia-smi output 2026-04-10"}
    ]
  },
  "payload": { ... full body or large snippet ... }
}
```

Fast-path results have a `lineage` field too, but with references only (IDs, no hydrated titles/bodies).

## Caching at this tier

We do **not** response-cache deep-path results by default. The queries are varied, the corpus changes, and a stale deep-path result is worse than a fresh one.

The **Slow Thinker cache** is a different cache, per-session, short-lived (<= 2 minutes), and keyed on the full conversation state — not an orchestration-level cache. That cache belongs to the LiveKit adapter, not to Musubi Core.

## Deep path + reflection

The reflection job (daily) runs deep-path retrieval for selected "reflection prompts" to surface patterns across memory. Example prompts:

- "What did I work on this week that I haven't documented?"
- "Which concepts have reinforced past the promotion threshold?"
- "What contradictions surfaced in the last 7 days?"

Results drive the reflection output written to `vault/reflections/YYYY-MM/YYYY-MM-DD.md`. See [[06-ingestion/reflection]].

## LLM-in-the-loop (advanced deep)

A special mode `"deep_llm"` (future; post-v1) would use an LLM to:

- Expand the query (synonym / multi-hop reformulation).
- Filter results for factuality.
- Summarize across the top-N into a structured response.

This is a full RAG loop with tool-use. Not in v1 — Musubi v1 stops at returning ranked passages. The caller does the LLM work. This keeps the Core's responsibility tight: "give me the right passages"; the caller decides what to do with them.

## Failure handling (deep)

Softer than fast path — deep path callers generally can retry or degrade:

| Failure | Response |
|---|---|
| Rerank down | Fall back to hybrid-only relevance + warning. |
| Lineage hydrate partial | Return hits with partial lineage + warning. |
| One plane slow | Wait up to per-plane timeout, then return without that plane + warning. |
| TEI query encoding slow | Timeout at 150ms; fall back to cached embedding if within TTL. |

No 5xx on deep path unless everything's down.

## Observability

- `retrieval.deep.latency_ms` histogram
- `retrieval.deep.rerank_used` counter (when we did rerank vs skipped)
- `retrieval.deep.lineage_hydrate_ms` histogram
- `retrieval.deep.degraded` counter with `reason=` label

## Test Contract

**Module under test:** `musubi/retrieval/deep.py` (glue over `orchestration.py`)

Happy path:

1. `test_deep_path_invokes_rerank`
2. `test_deep_path_hydrates_lineage_by_default`
3. `test_deep_path_snippet_longer_than_fast`
4. `test_deep_path_p95_under_5s_on_100k_corpus` (benchmark)

Slow Thinker integration shape:

5. `test_deep_path_parallel_safe_under_concurrent_callers`
6. `test_deep_path_no_response_cache_by_default`

Degradation:

7. `test_deep_path_rerank_down_falls_back_with_warning`
8. `test_deep_path_hydrate_missing_artifact_partial_lineage`
9. `test_deep_path_one_plane_timeout_degrades`

Reflection integration:

10. `test_reflection_prompts_resolved_via_deep_path`
11. `test_reflection_results_include_provenance_for_audit`

Property:

12. `hypothesis: deep path result ordering is stable for fixed inputs and weights`

Integration:

13. `integration: LiveKit Slow Thinker scenario — pre-fetched context available within 2s while user is speaking`
14. `integration: deep path vs fast path on the same query — deep NDCG@10 higher by ≥ 5 points on evals corpus`
