---
title: GPU Inference Topology
section: 08-deployment
tags: [deployment, gpu, inference, ollama, section/deployment, status/complete, tei, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[08-deployment/index]]"
reviewed: false
implements: "deploy/docker/compose.local-gpu.yml"
---
# GPU Inference Topology

How the optional GPU overlay (`deploy/docker/compose.local-gpu.yml`) fits BGE-M3,
SPLADE v3, BGE-reranker-v2-m3 and a Qwen 3 4B LLM on one 10 GB card. If you point
`TEI_*_URL` and `OLLAMA_URL` at endpoints elsewhere, none of this applies to the
Musubi host.

**Sizing basis:** the measured reference host (RTX 3080, 10 GB VRAM; see
[[08-deployment/host-profile]]). See [[13-decisions/0019-qwen-on-musubi-gpu-phase-1]]
for the decision behind the LLM choice.

## The budget

| Model | Role | VRAM (estimate) |
|---|---|---|
| BGE-M3 (dense encoder, 560M) | Embedding | ~2.3 GB |
| SPLADE v3 (~110M) | Sparse encoder | ~0.5 GB |
| BGE-reranker-v2-m3 (~560M) | Cross-encoder rerank | ~2.0 GB |
| **Qwen 3 4B, Q4** | LLM for synthesis / rendering / enrichment | **~2.5 GB** |
| CUDA context + KV cache (across services) | — | ~1.5-2.0 GB |
| **Total** | | **~8.8-9.3 GB** |

These are planning estimates, not measurements. One observation on the reference host,
with all four services running and idle, was about 3.3 GiB of VRAM in use; whether the
LLM was resident at that moment was not recorded. Measure your own card under load
before adding a model.

## When to move the LLM

Move the LLM to a second host with a larger GPU (point `OLLAMA_URL` at it), or upgrade
the card, if any of:

- VRAM out-of-memory kills more than about once a week
- Lifecycle jobs overrun their schedule (hourly maturation, nightly synthesis)
- Retrieval latency regresses while lifecycle jobs run
- The LLM's output quality is not good enough for synthesis and promotion

Full decision and alternatives: [[13-decisions/0019-qwen-on-musubi-gpu-phase-1]].

## Why Qwen 3 4B (not Qwen 2.5 7B)

An earlier plan named Qwen 2.5 7B Q4 (~4.8 GB). On a 10 GB card alongside three TEI
services that left about 0.4 GB of headroom. Qwen 3 4B Q4 (~2.5 GB) leaves about 1 GB,
the minimum for stable operation with KV-cache growth. `.env.example` sets
`LLM_MODEL=qwen3:4b`.

## Service layout

The overlay adds four GPU services. None publishes a host port; Core reaches them by
Compose service name. Set `TEI_DENSE_URL=http://tei-dense`,
`TEI_SPARSE_URL=http://tei-sparse`, `TEI_RERANKER_URL=http://tei-reranker` and
`OLLAMA_URL=http://ollama:11434` in `.env`.

| Service | Image | Command / notes |
|---|---|---|
| `tei-dense` | `${MUSUBI_TEI_IMAGE}@sha256:${MUSUBI_TEI_DIGEST}` | `--model-id ${EMBEDDING_MODEL:-BAAI/bge-m3} --max-batch-tokens 32768 --max-client-batch-size 16` |
| `tei-sparse` | same image | `--model-id ${SPARSE_MODEL:-naver/splade-v3} --pooling splade --max-batch-tokens 16384` |
| `tei-reranker` | same image | `--model-id ${RERANKER_MODEL:-BAAI/bge-reranker-v2-m3}` |
| `ollama` | `${MUSUBI_OLLAMA_IMAGE}@sha256:${MUSUBI_OLLAMA_DIGEST}` | default command; model cache in the `ollama-models` volume |

The operator picks the TEI image and tag for their GPU architecture and pins both images
by digest; the overlay refuses to start without all four values. TEI services share the
`tei-models` cache volume. Each service reserves a GPU through
`deploy.resources.reservations.devices`.

The overlay starts Ollama but does not download a model. Pull the one `LLM_MODEL` names
once after bring-up (the exact command is in `docs/guide/install.md`).

## GPU sharing

Consumer cards like the 3080 have no MIG partitioning, so the four services share the
card without hard isolation. The levers:

1. **TEI working-set caps.** `--max-batch-tokens` bounds each TEI service's batch
   memory.
2. **Scheduling.** LLM work runs only in lifecycle jobs (hourly maturation, nightly
   synthesis/promotion/reflection), never in capture or retrieval.

## Request allocation

| Operation | GPU services called |
|---|---|
| Fast retrieve | dense + sparse |
| Deep retrieve | dense + sparse + reranker |
| Capture | dense + sparse |
| Concept synthesis (nightly) | dense + LLM |
| Curated rendering (promotion) | LLM |
| Maturation enrichment | LLM |

The LLM is **never in the hot path**.

## Startup and health

With the overlay, Core waits for all four services to pass their health checks. TEI
health checks open a TCP connection to port 80 inside the container; Ollama's runs
`ollama list`. First start downloads model weights into the cache volumes, which can
take several minutes.

At runtime, `GET /v1/ops/status` reports each dependency:

```json
{
  "status": "ok",
  "version": "v1.27.12",
  "components": {
    "qdrant":       {"name": "qdrant",       "healthy": true, "detail": ""},
    "tei-dense":    {"name": "tei-dense",    "healthy": true, "detail": ""},
    "tei-sparse":   {"name": "tei-sparse",   "healthy": true, "detail": ""},
    "tei-reranker": {"name": "tei-reranker", "healthy": true, "detail": ""},
    "ollama":       {"name": "ollama",       "healthy": true, "detail": ""}
  }
}
```

`status` is `degraded` when any component is unhealthy. Core probes TEI at `/health`
and Ollama at `/api/tags`. It reports no VRAM figures; use a GPU exporter for those.

## Degradation

### Dense TEI unavailable

- At startup, Core refuses to become ready.
- Capture returns `503 BACKEND_UNAVAILABLE`.
- Retrieval fails with `500 INTERNAL`, counted in
  `musubi_retrieval_errors_total{kind="internal"}`.

### Sparse TEI unavailable

Retrieval falls back to dense-only and returns a `sparse_embedding_failed` warning,
counted in `musubi_retrieval_warnings_total`.

### Reranker unavailable

Deep retrieval falls back to the fused (RRF) order and returns a `reranker_failed`
warning, counted in `musubi_retrieval_warnings_total` and
`musubi_reranker_degradation_causes_total`.

### LLM unavailable

Capture and retrieval are unaffected. Lifecycle jobs that need the LLM fail or skip
their work for that tick; failures show in `musubi_lifecycle_job_errors_total`, and a
failing job tick posts an `ops-alerts` Thought from the lifecycle worker.

## Why local

- BGE-M3 is competitive with hosted embedding APIs on multilingual retrieval.
- SPLADE v3 is a strong learned-sparse model with no hosted equivalent.
- Qwen 3 4B is good enough for the narrow tasks it does (structured extraction, concept
  naming, short renders).

There is no hosted-embedding switch in Core: it calls the TEI-compatible endpoints set
by URL. Importance scoring (maturation) and synthesis can use any OpenAI-compatible
endpoint via `LIFECYCLE_LLM_API`, `LIFECYCLE_LLM_BASE_URL`, `LIFECYCLE_LLM_MODEL` and
`LIFECYCLE_LLM_API_KEY`. Promotion and reflection ignore those settings and always call
`OLLAMA_URL` with `LLM_MODEL`, so an Ollama endpoint is still required.

## Scaling beyond one GPU

1. **Add a second GPU** to the same host: encoders on one card, the LLM on the other.
2. **Move the LLM off-box:** run Ollama on a second host with a bigger GPU and point
   `OLLAMA_URL` at it.
3. **Hosted LLM, partly:** point the `LIFECYCLE_LLM_*` settings at a hosted
   OpenAI-compatible API for maturation and synthesis. Promotion and reflection still
   need `OLLAMA_URL`.

## Observability

Core does not emit GPU, TEI or Ollama metrics. For VRAM and per-service latency, run an
NVIDIA GPU exporter and scrape it alongside Core; Core's own view of the inference path
is `musubi_http_request_duration_ms` and the retrieval warning and error counters. See
[[09-operations/observability]].

## Test Contract

**Module under test:** the GPU overlay (`deploy/docker/compose.local-gpu.yml`).

1. `test_public_compose_local_gpu_mode_has_no_inference_port`
   (`tests/ops/test_public_compose.py`) — four GPU services, digest-pinned, health
   checked, no host port.
2. `test_public_compose_gpu_mode_refuses_unpinned_image` (same file).

Live-host bullets, defined in `tests/test_embedding.py` but **skipped** (they need the
reference GPU host):

3. `test_all_four_services_healthy_within_60s`
4. `test_vram_below_9_5gb_after_cold_start`
5. `test_core_degrades_gracefully_if_ollama_killed`
6. `test_core_503s_if_tei_dense_killed`
