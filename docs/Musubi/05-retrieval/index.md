---
title: "05 — Retrieval"
section: 05-retrieval
tags: [retrieval, scoring, search, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# 05 — Retrieval

How Musubi turns a query into a ranked result across planes. Four modes, one scorer.

## Documents in this section

- [[05-retrieval/scoring-model]] — The unified score function. Relevance + recency + importance + provenance + reinforcement.
- [[05-retrieval/hybrid-search]] — Dense + sparse + RRF fusion; how we configure Qdrant for hybrid.
- [[05-retrieval/fast-path]] — Sub-400 ms recall (with colocated inference) for voice/chat. Lightweight fusion, no rerank.
- [[05-retrieval/reranker]] — Cross-encoder pass for deep retrieval. BGE-reranker-v2-m3 via TEI.
- [[05-retrieval/reranker-batching]] — How rerank requests are chunked to the TEI batch limit.
- [[05-retrieval/orchestration]] — The retrieval pipeline as code: targets → per-target run → cross-plane calibration → merge.
- [[05-retrieval/cross-plane-ranking]] — How scores from different plane legs are made comparable before the merge.
- [[05-retrieval/context-pack]] — Startup/readiness context packs with BM25 ranking, closed kinds, staleness suppression, and grouped output.
- [[05-retrieval/blended]] — Blending results from multiple planes: dedup and lineage-aware merging.
- [[05-retrieval/deep-path]] — Hybrid + rerank + lineage hydration, with an optional LLM query-expansion hook.
- [[05-retrieval/content-truncation]] — How snippets are cut and how callers detect a cut.
- [[05-retrieval/auth001-token-scope]] — Default-to-all recall across a token's authorized namespaces.
- [[05-retrieval/evals]] — How we evaluate retrieval quality: corpora, metrics, gates, the nightly workflow.

## Modes

`/v1/retrieve` accepts `mode` = `fast`, `deep`, `blended` or `recent` (`src/musubi/retrieve/orchestration.py`). The wire default is `fast`.

### Fast (voice / chat / autocomplete)

- **Target**: p95 ≤ 400 ms end-to-end **with colocated inference** (TEI and Qdrant on the same host as core). The deadlines are settings: `retrieval_fast_encoding_timeout_s`, `retrieval_fast_plane_timeout_s`, `retrieval_fast_whole_timeout_s` (defaults 0.25 / 0.25 / 0.4 s).
- **Plan**: namespace filter → hybrid (dense + sparse + RRF) → score → top K.
- **No reranker**, no lineage hydration, no LLM.
- `src/musubi/retrieve/fast.py` defines a small exact-query TTL cache (`FastResponseCache`, 30 s), but the API does not wire it in: every HTTP request runs the pipeline.
- Used by voice and chat adapters that need an immediate answer.

### Deep (planning / analysis)

- **Target**: p95 ≤ 5 s; each target run is bounded by a 5 s deadline.
- **Plan**: namespace filter → hybrid → cross-encoder rerank → score → lineage hydrate (supersession chain, promotion links, supporting artifacts). Deep is reranked and lineage-hydrated; it also has an optional LLM query-expansion hook (`DeepRetrievalLLM`, `src/musubi/retrieve/deep.py`) that the HTTP router does not wire.

### Blended

Runs a full deep retrieval per plane, then content-dedups and drops lineage duplicates. See [[05-retrieval/blended]].

### Recent

A pure time-ordered scroll (newest first) with optional `since` and `tags`; no embedding, no rerank.

All four modes run through the same orchestration function, `retrieve()`, which dispatches on `mode`.

## Scoring (unified)

All ranked results carry a composite score:

```
score =
    0.55 * relevance       # sigmoid(rerank score) if reranked, else RRF / batch max
  + 0.15 * recency         # exp decay over (now - updated_epoch)
  + 0.10 * importance      # normalized 1-10 → 0-1
  + 0.15 * provenance      # (plane, state) table: curated-matured 1.0 … episodic-provisional 0.2
  + 0.05 * reinforcement   # log-scaled reinforcement_count (or access_count)
```

The weights are the `ScoreWeights` dataclass in `src/musubi/retrieve/scoring.py`. Rationale in [[05-retrieval/scoring-model]].

## Plane blending

A search can target one plane or many. Default planes are **curated + concept + episodic**. Fast and deep merge per-plane legs by `object_id` only (the highest-scoring copy wins). Content-similarity dedup (exact hash of the first 300 characters, or cosine ≥ 0.92 on tag-overlapping candidates) and lineage-aware drops (if concept A was promoted to curated B and both surface, keep the curated) happen only in `blended` mode. See [[05-retrieval/blended]].

## Typed inputs

The HTTP body is `RetrieveQuery` (`src/musubi/api/routers/retrieve.py`):

```python
class RetrieveQuery(BaseModel):
    namespace: str | None = None        # optional; omit to recall across all authorized namespaces
    query_text: str = ""                # required for fast/deep/blended; ignored by recent
    mode: Literal["fast", "deep", "blended", "recent"] = "fast"
    limit: int = 10
    planes: list[str] | None = None
    include_archived: bool = False
    since: float | None = None          # recent only: epoch-seconds floor
    tags: list[str] | None = None       # recent only: tag-AND filter
    state_filter: list[str] | None = None
```

`namespace` accepts a concrete `tenant/presence/plane`, a two-segment `tenant/presence` with `planes`, or a wildcard (see [[05-retrieval/auth001-token-scope]]).

## Output shape

The orchestration layer produces `RetrievalResult` (`src/musubi/retrieve/orchestration.py`); the router projects it to the wire rows in `src/musubi/api/responses.py`.

```python
class RetrievalResult(BaseModel):
    object_id: str
    namespace: str
    plane: str
    title: str | None
    snippet: str                     # 300 chars for deep/blended, 200 for fast
    content_truncated: bool          # True when the snippet was cut
    content_length: int | None       # original character length
    score: float
    score_components: dict[str, float]   # relevance, recency, importance, provenance, reinforcement
    lineage: dict
    payload: dict | None
    state: LifecycleState | None
    importance: int | None
    provenance_score: float | None   # recent mode only
```

## Test contract

The section specs each declare their own test contract. Aggregated:

- Deterministic fusion given fixed seeds + corpus.
- Namespace filter is always applied.
- Fast path never invokes the reranker.
- Score components combine to the total within float tolerance.
- Blended dedup collapses near-identical results.
- Blended results never include a concept that was promoted to a curated that is also in the results.
- Eval corpora track retrieval MRR and NDCG@10 (see [[05-retrieval/evals]]).

## Principles

1. **One scorer.** No plane has a secret boost. If you want plane-weighting, do it via the provenance component.
2. **Deterministic.** Given the same corpus and query, retrieval is reproducible. RNG is banned in the pipeline.
3. **Cheap before expensive.** Filter first. Hybrid next. Rerank last (deep and blended only). No LLM in retrieval itself, except the optional deep-path query-expansion hook.
4. **Explain yourself.** Every ranked result carries score components. We can always answer "why did this surface?"
5. **Small-world-aware.** For a small-team-sized corpus (10K–1M points), we optimize for single-box latency over sharded throughput.
