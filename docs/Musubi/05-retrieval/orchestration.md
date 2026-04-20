---
title: Orchestration
section: 05-retrieval
tags: [orchestration, pipeline, retrieval, section/retrieval, status/complete, type/spec]
type: spec
status: complete
implements: src/musubi/retrieve/orchestration.py
updated: 2026-04-19
up: "[[05-retrieval/index]]"
reviewed: false
---
# Orchestration

The single function that runs the retrieval pipeline. Fast and deep share this; mode parameter selects branches.

## Signature

```python
# musubi/retrieval/orchestration.py

async def retrieve(
    client: QdrantClient,
    tei: TEIClient,
    *,
    query: RetrievalQuery,
    now: float | None = None,
) -> Result[list[RetrievalResult], RetrievalError]:
    ...
```

Pure function over clients. No globals. Takes `now` injection for test determinism.

## Steps (deep path)

```
 ┌─────────────────────────────┐
 │ 1. validate query           │   pydantic + authz
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 2. encode query             │   TEI dense+sparse, parallel, cached
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 3. hybrid fan-out           │   per-plane Qdrant hybrid, parallel
 │    (per plane in scope)     │   RRF fusion server-side
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 4. merge + dedup            │   content+lineage
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 5. rerank (DEEP only)       │   BGE-reranker-v2-m3
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 6. score                    │   unified scorer
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 7. lineage hydrate (DEEP)   │   optional chunk / supersedes hydration
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 8. pack                     │   snippet, score components, lineage summary
 └────────────┬────────────────┘
              ▼
          response
```

Fast path: steps 5 and 7 are skipped.

## Step-by-step semantics

### 1. Validate

```python
try:
    query = RetrievalQuery.model_validate(query.model_dump())  # idempotent
except ValidationError as e:
    return Err(RetrievalError.bad_query(e))

if not _authorize(query.namespace, current_token):
    return Err(RetrievalError.forbidden())
```

Authorization: namespace must be in the token scope. See [[10-security/auth]].

### 2. Encode

```python
dense, sparse = await encode_query(tei, query.query_text)
```

Cache: in-memory LRU, keyed by raw text. See [[05-retrieval/hybrid-search#caching]].

### 3. Hybrid fan-out

```python
tasks = [
    hybrid_plane(client, plane=p, query=query, dense=dense, sparse=sparse)
    for p in query.planes
]
per_plane = await asyncio.gather(*tasks, return_exceptions=True)
```

Each `hybrid_plane` call returns a list of `Hit` or an Exception. Exceptions are logged and produce a warning in the response; they don't fail the whole retrieval.

### 4. Merge + dedup

```python
flat: list[Hit] = [h for plane_hits in per_plane if isinstance(plane_hits, list) for h in plane_hits]
flat = dedup_content(flat, similarity_threshold=0.92)
flat = drop_lineage_ancestors(flat)   # concept if curated derived from it is also present
```

See [[05-retrieval/blended]] for the full merge algorithm.

### 5. Rerank (deep only)

```python
if query.mode == "deep" and len(flat) >= 5:
    try:
        flat = await rerank(tei, query.query_text, flat, top_k=query.limit * 3)
    except TEIUnavailable:
        warnings.append("rerank unavailable; using hybrid-only relevance")
```

See [[05-retrieval/reranker]].

### 6. Score

```python
now_epoch = now or time.time()
for h in flat:
    h.score, h.score_components = score(h, now=now_epoch)
flat.sort(key=lambda h: (-h.score, h.object_id))  # deterministic tiebreak
```

### 7. Lineage hydrate (deep only)

If `query.include_lineage == True` (default on deep, false on fast):

- For each result whose content was truncated at index time (large curated), fetch the full body from vault.
- For results with `superseded_by`, attach the chain head metadata.
- For artifact chunks, attach the parent artifact's title + source_ref.

Each hydration is a targeted read; parallelized via `asyncio.gather`.

### 8. Pack

```python
results = [
    RetrievalResult(
        object_id=h.object_id,
        namespace=h.namespace,
        plane=h.plane,
        title=h.title,
        snippet=_snippet(h, max_chars=300 if query.mode == "deep" else 200),
        score=h.score,
        score_components=h.score_components,
        lineage=_summarize_lineage(h),
        payload=h.payload if not query.brief else None,
    )
    for h in flat[: query.limit]
]
return Ok(results)
```

## Timeouts (layered)

| Layer | Fast | Deep |
|---|---|---|
| Whole `retrieve()` | 400ms | 5s |
| Query encoding | 80ms | 150ms |
| Per-plane hybrid | 250ms | 1500ms |
| Reranker | — | 800ms |
| Lineage hydrate | — | 500ms |

Whole-call timeout wraps everything with `asyncio.wait_for`. Sub-timeouts use `asyncio.wait_for` individually and produce warnings on hit.

## Error propagation

Musubi uses `Result[T, E]` for this function (see [[02-current-state/preserved]]). The error variant:

```python
class RetrievalError(BaseModel):
    kind: Literal["bad_query", "forbidden", "timeout", "internal"]
    detail: str
    warnings: list[str] = Field(default_factory=list)
```

Success variant carries a `warnings` list too (non-fatal issues). Callers check `is_ok` and inspect `warnings` for partial-degradation signaling.

## Observability hooks

Every step emits:

- `retrieval.step_latency_ms{step=1..8, mode=fast|deep}` histogram
- `retrieval.plane_hits_count{plane=...}` histogram
- `retrieval.warnings{reason=...}` counter
- `retrieval.result_count{mode=...}` histogram

Traces (OpenTelemetry) span the full `retrieve()` call with child spans per step. Hooks in `musubi/observability/tracing.py`.

## Idempotency + determinism

Given:
- same `corpus_version` (snapshot of all points in scope)
- same query
- same `now`
- same weights

the pipeline returns byte-identical results. Tests rely on this. RNG is banned; any randomness in upstream libs (Qdrant has none; TEI is deterministic for fixed weights) is either seeded or caught via an allow-list.

## Test Contract

**Module under test:** `musubi/retrieval/orchestration.py`

Structural:

1. `test_fast_mode_skips_rerank` (mock assert)
2. `test_deep_mode_invokes_rerank`
3. `test_fast_mode_skips_lineage_hydrate`
4. `test_deep_mode_hydrates_when_flag_true`
5. `test_steps_run_in_documented_order` (instrumented)

Concurrency:

6. `test_planes_run_in_parallel`
7. `test_hydrate_fetches_run_in_parallel`

Timeouts:

8. `test_whole_call_timeout_fast_400ms`
9. `test_per_plane_timeout_deep_1500ms`
10. `test_rerank_timeout_returns_with_warning`

Determinism:

11. `test_deterministic_for_fixed_inputs`
12. `test_tiebreak_on_object_id`

Error paths:

13. `test_bad_query_returns_typed_error`
14. `test_forbidden_namespace_returns_typed_error`
15. `test_partial_plane_failure_returns_partial_with_warning`

Integration:

16. `integration: end-to-end fast-path on 10K corpus with real TEI + Qdrant, p95 ≤ 400ms`
17. `integration: end-to-end deep-path with rerank, NDCG@10 on golden set ≥ threshold`
18. `integration: kill TEI mid-request, pipeline returns with documented degradation`
