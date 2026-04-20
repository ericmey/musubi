---
title: "05 — Retrieval"
section: 05-retrieval
tags: [retrieval, scoring, search, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# 05 — Retrieval

How Musubi turns a query into a ranked, blended result across planes. Two paths, one scorer.

## Documents in this section

- [[05-retrieval/scoring-model]] — The unified score function. Relevance + recency + importance + provenance.
- [[05-retrieval/hybrid-search]] — Dense + sparse + RRF fusion; how we configure Qdrant for hybrid.
- [[05-retrieval/fast-path]] — Sub-400ms recall for voice/chat. Cache + lightweight fusion, no rerank.
- [[05-retrieval/reranker]] — Cross-encoder pass for deep retrieval. BGE-reranker-v2-m3 via TEI.
- [[05-retrieval/orchestration]] — The retrieval pipeline as code: filter → hybrid → rerank → score → pack.
- [[05-retrieval/blended]] — Blending results from multiple planes: weights, dedup, lineage-aware merging.
- [[05-retrieval/deep-path]] — LLM-in-the-loop retrieval for planning tasks (Slow Thinker pattern).
- [[05-retrieval/evals]] — How we evaluate retrieval quality. Golden sets, regression tests, ragas metrics.

## Two paths

Musubi offers two retrieval paths. Callers choose; the Core implements both.

### Fast path (voice / chat / autocomplete)

- **Budget**: p50 ≤ 150ms, p95 ≤ 400ms end-to-end.
- **Plan**: namespace filter → hybrid (dense + sparse + RRF) → score → top K.
- **No reranker**, no deep-traversal, no LLM.
- **Optional cache** keyed by (namespace, query-hash, filters) with 30s TTL for identical reads.
- Used by the **Fast Talker** agent in LiveKit and by chat adapters that need an immediate answer.

### Deep path (planning / analysis / Slow Thinker)

- **Budget**: p50 ≤ 2s, p95 ≤ 5s.
- **Plan**: namespace filter → hybrid → cross-encoder rerank → score → optional lineage hydrate (chunk, supersedes chain) → pack.
- Used by the **Slow Thinker** agent for pre-fetching context, by coding-agent planning loops, and by the Reflection job.

Both paths run through the same orchestration function with a `mode: Literal["fast", "deep"]` parameter. No branching logic duplication.

## Scoring (unified)

All retrieval results carry a composite score:

```
score =
    0.55 * relevance       # dense + sparse hybrid (post-RRF)
  + 0.15 * recency         # exp decay over (now - updated_epoch)
  + 0.10 * importance      # normalized 1-10 → 0-1
  + 0.15 * provenance      # curated(1.0) > concept(0.6) > episodic-matured(0.5) > episodic-provisional(0.2)
  + 0.05 * reinforcement   # log-scaled reinforcement_count
```

Weights are tunables in `config.py` (`SCORE_WEIGHTS`). Rationale for the Generative-Agents-style combination in [[05-retrieval/scoring-model]].

## Plane blending

A search can target one plane or many. Default: **curated + concept + episodic-matured**, in one call, deduped by content-similarity (threshold 0.92) and by explicit lineage (if concept A promoted_to curated B and both surface, keep the curated and drop the concept). See [[05-retrieval/blended]].

## Typed inputs

The retrieval API accepts a typed query:

```python
class RetrievalQuery(BaseModel):
    namespace: str                               # required
    query_text: str                              # natural language
    mode: Literal["fast", "deep"] = "fast"
    planes: list[Literal["episodic", "concept", "curated", "artifact"]] = ["curated", "concept", "episodic"]
    limit: int = 20
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    include_archived: bool = False
    include_superseded: bool = False
    query_presence: str | None = None            # for logging + optional per-presence weighting

class RetrievalFilters(BaseModel):
    tags_any: list[str] | None = None
    tags_all: list[str] | None = None
    topics_any: list[str] | None = None
    min_importance: int | None = None
    since: datetime | None = None
    until: datetime | None = None
    content_type: list[str] | None = None
    capture_source: list[str] | None = None
```

## Output shape

```python
class RetrievalResult(BaseModel):
    object_id: KSUID
    namespace: str
    plane: Literal["episodic", "concept", "curated", "artifact"]
    title: str | None
    snippet: str                                  # max 300 chars by default
    score: float
    score_components: ScoreComponents             # for debugging
    lineage: LineageSummary                       # supersedes, promoted_from, etc.
    payload: dict                                  # full payload when brief=False
```

## Test contract

The section specs each declare their own test contract. Aggregated:

- Deterministic fusion given fixed seeds + corpus.
- Namespace filter is always applied.
- Fast path never invokes the reranker.
- Score components sum to total within float tolerance.
- Dedup collapses near-identical results.
- Blended results never include a concept that was promoted to a curated that is also in the results.
- Eval golden sets track retrieval MRR and NDCG@10.

## Principles

1. **One scorer.** No plane has a secret boost. If you want plane-weighting, do it via the provenance component.
2. **Deterministic.** Given the same corpus and query, retrieval is reproducible. RNG is banned in the pipeline.
3. **Cheap before expensive.** Filter first. Hybrid next. Rerank last (deep path only). LLM never, in retrieval itself — only in `deep-path` orchestration, which is its own thing.
4. **Explain yourself.** Every result carries score components. We can always answer "why did this surface?"
5. **Small-world-aware.** For a household-sized corpus (10K–1M points), we optimize for single-box latency over sharded throughput.
