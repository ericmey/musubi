---
title: "ADR 0043: Lifecycle LLM via OpenAI-compatible endpoint"
section: 13-decisions
type: adr
status: accepted
date: 2026-08-14
updated: 2026-08-14
deciders: [Eric]
tags: [architecture, lifecycle, llm, deployment, type/adr, status/accepted]
supersedes: ""
superseded-by: ""
---

# ADR 0043: Lifecycle LLM via OpenAI-compatible endpoint

- **Status:** Accepted
- **Date:** 2026-08-14
- **Decider:** Eric (model-lane selection)

## Context

The lifecycle worker runs four LLM tasks: importance rescoring and topic
inference (maturation, hourly), cluster synthesis and contradiction checking
(nightly). All four ran on a qwen3:4b served by an Ollama co-located on the
musubi host — the same GPU that carries all three TEI models.

After #684 removed the synthesis livelock, two nightly passes produced zero
concepts. Remote measurement on 2026-08-14 (via the `importance_last_scored_at`
stamps #684 introduced, and per-object reads of freshly matured rows)
established a clean difficulty gradient on the 4B:

| task | structured-output schema | observed success |
|---|---|---|
| importance scoring | trivial (id → int) | ~always |
| topic inference | medium (id → string list) | ~14% of matured rows |
| cluster synthesis | hardest (title/content/rationale/tags/importance) | 0% over two nights |

Failure rate tracks schema complexity: the model, not the pipeline. #684's
skip-cluster semantics turned what used to be a livelock into quiet
zero-output — visible on the new counters, but still zero output.

The house serves larger models behind LiteLLM (OpenAI-compatible):

1. `house/main` — Qwen 3.6 35B A3B, 8× concurrency, 131K ctx (interactive lane)
2. `house/backup` — same model, 4× concurrency, 131K ctx (failover lane;
   throughput shrinks faster under concurrency)
3. `house/voice` — Qwen 3.5 9B, 16× concurrency, 8K ctx
4. (planned) Jetson Orin Nano 0.7B–3B utility models

## Decision

The lifecycle worker's LLM client speaks a second wire protocol —
`/v1/chat/completions` with `response_format: json_schema (strict)` — selected
by settings (`LIFECYCLE_LLM_API=openai`, plus base URL / model / key
overrides). The deployment targets **`house/backup` (35B, 131K ctx)** for all
four lifecycle tasks. Defaults preserve the existing Ollama path unchanged.

Alongside, maturation's batched enrichment isolates failures **per batch**
(counted on `musubi_lifecycle_enrichment_batch_failures_total{kind}`) instead
of nulling the whole sweep's field on one failed batch. This is what the spec
(§ Failure modes, § Partial batch failure) always described; the code had
implemented a stricter all-or-nothing reading that let one flaky batch erase
topics for entire sweeps — which in turn starved synthesis clustering down to
capture-source tags (the mega-cluster precondition from #684).

## Why house/backup and not the alternatives

- **The workload is the inverse of the interactive lane.** Lifecycle calls
  are serial nightly/hourly batch: latency-irrelevant, correctness-critical.
  `house/backup`'s concurrency penalty never applies to a serial caller, and
  using it keeps `house/main` uncontended for the agents. Worst case — a
  main-outage night where backup carries both — lifecycle degrades benignly
  by design (skip, candidates carry, retry next sweep).
- **`house/voice` (8K ctx) is disqualified by arithmetic**: a capped synthesis
  cluster is 20 members × 1,500 chars ≈ 8–9K tokens before instructions and
  schema. Every call would truncate; the 0% would return wearing a
  different error.
- **Jetson-class models (0.7B–3B) sit below the measured floor** — 4B already
  fails the medium schema most of the time. Fine hardware for short-form
  utility generation elsewhere; not for the memory pipeline.
- **A bigger model on the musubi host's own Ollama** would contend with the
  three TEI models for VRAM on the box whose retrieval latency matters.
  Reusing the existing LiteLLM serving adds zero new inference
  infrastructure.

## Consequences

- Model capability for the memory pipeline becomes a deployment decision
  (env), not a code path. The gradient can be re-measured against any lane
  by flipping four env values.
- One new secret in the worker environment (the LiteLLM key), materialized
  from a committed 1Password reference by `op run`; it is never rendered into
  the persistent non-secret `.env.production` file.
- If the LiteLLM backend only best-efforts `json_schema` (backend-dependent),
  the existing validate-or-None contract absorbs it: failed calls skip and
  retry next sweep, and the failure lands on the #684 counters.
- The co-located Ollama becomes retirable once the openai lane is proven,
  freeing GPU headroom for TEI (tracked as a follow-up, not part of this
  change).
- Promotion and reflection clients (`HttpxPromotionClient`,
  `HttpxReflectionClient`) still speak Ollama-native and are a deliberate
  follow-up: both are downstream of concepts existing at all, and porting
  them rides the same pattern once synthesis output is real.

## Verification

- Unit: OpenAI-wire tests (URL normalization, bearer header, strict
  `json_schema` response_format, `choices[]` extraction, error → None).
- Batch isolation: one failed batch out of N leaves the other batches'
  enrichment intact and increments the counter.
- Deployment acceptance: first nightly pass after the env flip —
  `synthesis-done … created>0`, concepts readable in
  `<family>/shared/concept` by the agents' own tokens, and
  `enrichment_batch_failures_total` rate visibly below the ~86% the 4B
  produced.

## Test Contract

1. `test_lifecycle_llm_defaults_preserve_ollama_behavior`
2. `test_lifecycle_llm_openai_override_roundtrip`
3. `test_lifecycle_llm_api_rejects_unknown_value`
4. `test_lifecycle_llm_openai_requires_explicit_base_url`
5. `test_lifecycle_llm_openai_requires_explicit_nonblank_model`
6. `test_score_importance_happy_path_openai`
7. `test_request_shape_bearer_and_strict_json_schema`
8. `test_base_url_with_trailing_v1_is_not_doubled`
9. `test_no_api_key_sends_no_authorization_header`
10. `test_http_error_returns_none`
11. `test_synthesize_cluster_happy_path_openai`
12. `test_unknown_api_value_fails_loudly`
13. `test_lifecycle_llm_key_uses_runtime_secret_boundary`
