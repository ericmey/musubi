---
title: Orchestration
section: 05-retrieval
tags: [orchestration, pipeline, retrieval, section/retrieval, status/complete, type/spec]
type: spec
status: complete
implements: src/musubi/retrieve/orchestration.py
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
---
# Orchestration

The single function that runs the retrieval pipeline. All four modes (`fast`, `deep`, `blended`, `recent`) go through it; `mode` selects the runner.

## Signature

```python
# src/musubi/retrieve/orchestration.py

async def retrieve(
    client: QdrantClient,
    embedder: Embedder,
    reranker: TEIRerankerClient | None = None,
    *,
    query: RetrievalQuery | dict[str, Any],
    llm: DeepRetrievalLLM | None = None,
    now: float | None = None,
    account_access: bool = True,
    fast_timing: FastTiming | None = None,
) -> Result[RetrievalEnvelope, RetrievalError]:
    ...
```

Pure function over clients. No globals. Takes `now` injection for test determinism. `RetrievalEnvelope` carries `results` plus a tuple of structured `warnings`. `reranker` is required for `deep` and `blended`; `llm` is the optional deep-path query-expansion hook (the HTTP router does not pass one). `fast_timing` carries the three fast-path deadlines (see [[05-retrieval/fast-path]]).

## Flow

```
 ┌─────────────────────────────┐
 │ 1. validate query           │   pydantic (authorization already done by the router)
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 2. expand targets           │   one target per (namespace, plane)
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 3. run each target          │   fast / deep / blended / recent runner,
 │    concurrently             │   each under its own whole-call deadline
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 4. calibrate + merge        │   RET-012 cross-plane seam, then dedup by object_id
 │    (multi-target only)      │   and sort by (-score, object_id, plane)
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 5. limit                    │   top query.limit
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 6. account access           │   delivered rows only (RET-002)
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ 7. finalize                 │   bounded warnings, metrics counted once
 └────────────┬────────────────┘
              ▼
          RetrievalEnvelope
```

The encode → hybrid → (rerank) → score → (lineage) work happens **inside each runner**, per target: see [[05-retrieval/fast-path]] and [[05-retrieval/deep-path]].

## Step-by-step semantics

### 1. Validate

`retrieve()` re-validates the query as the internal `RetrievalQuery` model. A validation failure returns `Err(RetrievalError(kind="bad_query", ...))`. Ranked modes require non-empty `query_text`; `recent` may omit it.

Authorization is **not** done here. The HTTP router resolves the namespace targets and checks each one against the token's scope before it calls `retrieve()` (see [[10-security/auth]] and [[05-retrieval/auth001-token-scope]]).

### 2. Expand targets

If the router supplied `namespace_targets`, each becomes one `(namespace, plane)` target. Otherwise a single target is derived from the three-segment `namespace` (direct callers that bypass the router).

### 3. Run each target

Each target runs one single-plane pipeline through `_run_single`, which dispatches on `mode`:

- **fast** — `run_fast_retrieve` (`src/musubi/retrieve/fast.py`) under `fast_timing.whole_timeout_s`. The query is encoded inside each target run, so "encode once" holds per target, not per request.
- **deep** — `run_deep_retrieve` (`src/musubi/retrieve/deep.py`) under a 5 s deadline.
- **blended** — `run_blended_retrieve` (`src/musubi/retrieve/blended.py`) under a 5 s deadline; it runs deep internally.
- **recent** — `run_recent_retrieve` (`src/musubi/retrieve/recent.py`) under a 2 s deadline.

Targets run concurrently with `asyncio.gather(return_exceptions=True)`. A single target goes straight to step 5 with no merge.

Per-target outcomes:

- `kind="timeout"` → warning `plane_timeout_<plane>`; the other targets continue.
- `kind="internal"` or `"bad_query"` from any target → the whole call returns that error, because a merged response would silently under-report.
- A raised exception → `kind="internal"`.
- If every target timed out and nothing survived → `Err(kind="timeout")`.

### 4. Calibrate + merge (multi-target only)

Each leg scored relevance against its own batch maximum, so leg scores are not directly comparable. The RET-012 seam (`calibrate_global_relevance`, `src/musubi/retrieve/scoring.py`) re-anchors every candidate against the working-set maximum **before** dedup; see [[05-retrieval/cross-plane-ranking]].

The merge then keeps the highest-scoring copy **per `object_id`** (equal scores: the lexicographically smaller plane wins). This merge does no content-similarity or lineage dedup; that happens only inside `blended`. The final sort key is `(-score, object_id, plane)`.

### 5. Limit

`results[: query.limit]`.

### 6. Account access (final delivery boundary — RET-002 / #500)

After the envelope is finalized (fanout, dedup, sort, and limit all applied), account each
**delivered** row exactly once — never a dropped candidate, and identically whether or not
lineage was hydrated. One async step in `retrieve()` immediately before `_finalize`, over the
candidate envelope's final `results`, so accounting failures also pass through the shared telemetry
boundary exactly once:

- Group delivered rows by plane; account only planes whose type carries `access_count`
  (episodic, curated, concept). artifact and thought lack the field → explicit no-op.
- Per accountable collection: **one batched read** of current counts + **one batched write**
  (`access_count += 1`, stamp `last_accessed_at`). Never N+1.
- The same seam covers `POST /v1/retrieve` and `POST /v1/retrieve/stream` — both call
  `retrieve()`, whose delivered set IS the returned envelope.
- **`/v1/context` delivers a DIFFERENT final set.** It retrieves `candidate_limit` candidates,
  then `build_context_pack` trims by `max_items`/`max_chars`/filler. So the router passes
  `retrieve(account_access=False)` and calls `account_delivered` itself on the flattened final
  pack items (each carries `namespace` + `object_id` + `plane`) — a trimmed candidate is never
  counted. An empty pack accounts nothing.
- Concurrency: batched read-modify-write, **not** atomic. True concurrent-counter safety is
  tracked separately (Issue #502). Typed results and warnings are unchanged by this step.

## Timeouts (layered)

| Layer | Fast | Deep | Blended | Recent |
|---|---|---|---|---|
| Whole target run | `retrieval_fast_whole_timeout_s` (0.4 s) | 5 s | 5 s | 2 s |
| Query encoding | `retrieval_fast_encoding_timeout_s` (0.25 s) | sparse 1.0 s, then dense-only | as deep | — |
| Per-plane hybrid | `retrieval_fast_plane_timeout_s` (0.25 s) | 1.5 s | as deep | — |
| Reranker | — | `retrieval_rerank_timeout_s` (1.5 s) | as deep | — |
| Lineage hydrate | — | `retrieval_lineage_timeout_s` (0.5 s per hit) | as deep | — |

The whole-run deadline wraps each target with `asyncio.wait_for`. Sub-stage timeouts degrade rather than fail where the stage is optional.

The deep-stage budgets are runtime settings, not call-site literals:
`retrieval_rerank_timeout_s` defaults to `1.5` and
`retrieval_lineage_timeout_s` defaults to `0.5`. Both must remain positive and
below the whole-call budget in production configuration. Rerank expiry returns
the hybrid ordering with `reranker_failed` plus a bounded additive cause code;
lineage expiry returns the affected
hit without hydrated lineage. Neither optional stage may consume the whole-call
budget and turn an otherwise healthy retrieval into a 503. The deep per-plane
hybrid (1.5 s) and sparse-encoding (1.0 s) budgets are code defaults in
`src/musubi/retrieve/deep.py`, not settings.

Hybrid Qdrant queries, authoritative-anchor resolution, and deep lineage
hydration use the synchronous Qdrant client. The deep/hybrid orchestrator
offloads those reads from the asyncio event-loop thread; under concurrent
blended calls they must not serialize unrelated requests behind one caller's
Qdrant round trips. Two dedicated pools provide a 16-worker per-process ceiling:
eight slots for required hybrid queries and authoritative resolution, plus eight
isolated slots for optional lineage hydration. Excess calls queue without
occupying the shared asyncio executor. The submitting request and trace context
is copied into every worker call. The supported regression load is 20 concurrent
callers through the public deep path with more than five candidates, exercising
query, authoritative resolution, live reranking, scoring, and lineage stages.
Saturation is stage-local: expired lineage work degrades to the original hit,
cannot occupy required-query capacity, and does not produce a whole-request 503.

The recent/context retrieval path is not covered by this executor ceiling.

The lineage worker invokes a synchronous hydration seam, not `asyncio.run()`.
Plane `get` methods used there must complete without suspending on a loop-bound
awaitable; the seam detects suspension and fails explicitly so future async I/O
cannot be driven on a fresh worker-thread event loop by accident.

The 1.5 s rerank default is calibrated from a ten-caller burst on the
reference deployment: p50 0.684 s, p95 1.226 s, and p99 1.268 s for 200
candidate predictions. The prior 800 ms value sat only 125 ms above an observed
675 ms average and would have converted the 503 cliff into routine hybrid-only
degradation under load.

## Error propagation

`retrieve()` returns `Result[RetrievalEnvelope, RetrievalError]` (`Result`, `Ok` and `Err` live in `src/musubi/types/common.py`). The error variant:

```python
class RetrievalError(BaseModel):
    kind: Literal["bad_query", "forbidden", "timeout", "internal"]
    detail: str
    warnings: list[str] = Field(default_factory=list)
```

The success variant carries structured `warnings` (`RetrievalWarning`: `code`, `plane`, optional reranker `cause`; `src/musubi/retrieve/warnings.py`). The router maps `kind` to HTTP: `bad_query` → 400, `forbidden` → 403, `timeout` → 503 `BACKEND_UNAVAILABLE`, `internal` → 500.

### Exhaustive sub-layer code classification

Every literal error code emitted inside `src/musubi/retrieve/` has one exact
entry in the orchestration code-to-kind registry. Codes intentionally mapped to
`internal` are named separately; an unknown code raises a diagnostic programmer
error instead of silently falling through to `internal`.

The retrieve package is the closed input domain for this classifier. Dynamic
forwarders may propagate a code from another retrieve error, but no external
package constructs the internal `RetrievalError`. A focused source-inventory
test preserves that premise, inventories both arms of conditional code
expressions, and fails if the error-constructor naming convention silently
excludes a new callee. Warning codes remain a separate taxonomy even when a
literal such as `sparse_embedding_failed` intentionally exists in both.

The closed-domain conversion deliberately retires three unreachable classifier
inputs: the literal `bad_query` code and codes containing `forbidden` or
`unauthorized`. No retrieve error producer emits any of them. Public
`kind="bad_query"` remains reachable through the registry's emitted bad-query
codes, while `kind="forbidden"` remains reachable through HTTP status 403.
Each retired input now raises `ValueError` rather than classifying; the closure
that makes those inputs unreachable is asserted by
`test_retrieval_error_construction_remains_closed_over_retrieve_package`.

## Observability hooks

`_finalize` is the one place retrieval telemetry is counted (`src/musubi/observability/retrieval_metrics.py`):

- `musubi_retrieval_warnings_total{warning, plane}` — once per distinct warning on a degraded success.
- `musubi_retrieval_errors_total{kind}` — once per total-failure request.
- `musubi_reranker_degradation_causes_total{cause, plane}` — bounded reranker cause detail.

Only allowlisted warnings survive onto the envelope, so a free-text code can never become a Prometheus label. There are no per-step latency or result-count histograms. Tracing (OpenTelemetry) wraps the call in one `retrieve.orchestration` span with namespace, mode, limit and target-count attributes; it is a no-op unless OTLP export is configured (`src/musubi/observability/tracing.py`).

## Idempotency + determinism

Given:
- same `corpus_version` (snapshot of all points in scope)
- same query
- same `now`
- same weights

the pipeline returns identical results. Tests rely on this. RNG is banned; any randomness in upstream libs (Qdrant has none; TEI is deterministic for fixed weights) is either seeded or caught via an allow-list.

## Test Contract

**Module under test:** `src/musubi/retrieve/orchestration.py`

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
9. `test_fast_timing_override_reaches_pipeline_and_whole_call`
10. `test_per_plane_timeout_deep_1500ms`
11. `test_rerank_timeout_returns_with_warning`

Determinism:

12. `test_deterministic_for_fixed_inputs`
13. `test_tiebreak_on_object_id`

Error paths:

14. `test_bad_query_returns_typed_error`
15. `test_no_retrieval_channels_is_classified_as_bad_query`
16. `test_forbidden_namespace_returns_typed_error` — deferred (skipped stub: authorization is enforced at the HTTP boundary, not in `retrieve()`)
17. `test_partial_plane_failure_returns_partial_with_warning`

Integration (deferred: skipped stubs):

18. `test_integration_end_to_end_fast_path_on_10K_corpus_with_real_TEI_Qdrant_p95_le_400ms` — deferred
19. `test_integration_end_to_end_deep_path_with_rerank_NDCG_10_on_golden_set_ge_threshold` — deferred
20. `test_integration_kill_TEI_mid_request_pipeline_returns_with_documented_degradation` — deferred

Access accounting (RET-002 / #500) — realized in `tests/retrieve/test_ret002_access_accounting.py`,
`tests/api/test_ret002_streaming_access.py`, and `tests/api/test_ret002_context_accounting.py`:

21. `test_delivered_episodic_row_accounted_once_per_mode`
22. `test_deep_include_lineage_false_still_accounts_delivered`
23. `test_deep_accounting_identical_regardless_of_include_lineage`
24. `test_limit_drop_accounts_only_delivered_not_dropped_candidates`
25. `test_delivered_curated_row_accounted`
26. `test_delivered_concept_row_accounted`
27. `test_non_accountable_plane_delivery_is_noop`
28. `test_account_delivered_scopes_to_exact_namespace_object_id_pair`
29. `test_accounting_is_batched_per_collection_not_n_plus_1`
30. `test_streaming_retrieval_accounts_each_delivered_row_once`
31. `test_context_accounts_only_surfaced_pack_items_not_dropped_candidates`
32. `test_retrieve_normalizes_accounting_failure_to_typed_err`
33. `test_context_accounting_failure_returns_internal_not_raw`

Exhaustive error classification (RET-014 / #619):

34. `test_every_literal_retrieve_error_code_has_an_explicit_classification`
35. `test_existing_error_code_classifications_preserve_their_semantics`
36. `test_unknown_retrieve_error_code_is_rejected_instead_of_implicitly_internal`
37. `test_intentional_internal_error_codes_are_named_and_complete`
38. `test_error_code_collector_rejects_new_unrecognised_code_callee`
39. `test_error_code_collector_accounts_for_dynamic_forwarding_sites`
40. `test_error_code_collector_walks_both_conditional_expression_arms`
41. `test_retrieval_error_construction_remains_closed_over_retrieve_package`
42. `test_sparse_embedding_failed_remains_distinct_in_error_and_warning_taxonomies`


Grapheme-safe truncation:
43. `test_truncation_bypasses_short_text`
44. `test_truncation_cuts_at_grapheme_boundaries_safely`
45. `test_truncation_respects_max_chars_lte_3`
46. `test_truncation_prevents_emoji_zwj_bisection`
47. `test_truncation_preserves_single_emoji`
48. `test_truncation_prevents_combined_diacritic_bisection`
49. `test_truncation_prevents_regional_indicator_bisection`
50. `test_truncation_preserves_internal_whitespace`
51. `test_truncation_preserves_trailing_whitespace_if_within_budget`
52. `test_truncation_prevents_skin_tone_modifier_bisection`
53. `test_fast_retrieval_uses_grapheme_truncation_for_long_content`
54. `test_recent_retrieval_uses_grapheme_truncation_for_long_content`
55. `test_orchestration_uses_grapheme_truncation_for_long_content`
56. `test_context_pack_uses_grapheme_truncation_for_long_content`
