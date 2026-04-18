---
title: GPU Inference Topology
section: 08-deployment
tags: [deployment, gpu, inference, ollama, section/deployment, status/complete, tei, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[08-deployment/index]]"
reviewed: false
---
# GPU Inference Topology

How 10 GB of VRAM hosts BGE-M3, SPLADE++ V3, BGE-reranker-v2-m3, and Qwen2.5-7B-Instruct Q4 simultaneously — without OOMs.

## The constraint

RTX 3080 has **10 GB VRAM**. We need:

| Model | Role | VRAM (loaded, FP16 or Q4) |
|---|---|---|
| BGE-M3 (dense encoder, 560M) | Embedding | ~1.4 GB |
| SPLADE++ V3 (~110M) | Sparse encoder | ~0.5 GB |
| BGE-reranker-v2-m3 (~560M) | Cross-encoder rerank | ~1.4 GB |
| Qwen2.5-7B-Instruct Q4_K_M | LLM for synthesis / rendering | ~4.8 GB |
| CUDA context + KV cache headroom | — | ~1.5 GB |
| **Total** | | **~9.6 GB** |

Tight. Leaves ~0.4 GB overhead. Acceptable, but we need discipline — model reloads, large batch queries, or multi-request LLM usage can push it over.

## Service layout

Four GPU services, all co-resident:

### TEI dense (BGE-M3)

- Container: `text-embeddings-inference:1.5-cuda`
- Args: `--model-id BAAI/bge-m3 --max-batch-requests 64 --max-client-batch-size 16`
- Port: 8010
- Target throughput: ~500 req/s on small batches, ~150 req/s on 16-doc batches.

### TEI sparse (SPLADE++ V3)

- Container: same image.
- Args: `--model-id naver/splade-v3 --pooling splade --max-batch-requests 32`
- Port: 8011
- Throughput: ~200 req/s.

### TEI reranker (BGE-reranker-v2-m3)

- Container: same image, reranker mode (`--model-id BAAI/bge-reranker-v2-m3 --pooling rerank`).
- Port: 8012
- Batch cross-encoder: ~40 pairs/batch at ~100ms.

### Ollama (Qwen2.5-7B-Instruct Q4)

- Container: `ollama/ollama:0.4.0-cuda`
- Model: `qwen2.5:7b-instruct-q4_K_M`
- Port: 11434
- Use: concept synthesis, curated rendering, maturation enrichment.
- Always-loaded (long cold-start otherwise).

## GPU sharing strategy

**Primary:** MIG is not available on the 3080 (consumer GPU). We rely on NVIDIA's default MPS-style sharing.

Key levers we control:

1. **`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`** — not strictly enforced on the 3080; documented but effectively advisory.
2. **Model memory reservation** — TEI uses `--max-batch-tokens` to cap working-set VRAM. Ollama uses `--num-gpu-layers` (set to all layers fit).
3. **Scheduling** — LLM calls are batched (synthesis runs once per night; we never invoke the LLM during fast retrieval).

We tune Ollama's context window to 8192 tokens (synthesis needs more; retrieval rarely needs LLM).

## Request allocation

| Operation | GPU services called |
|---|---|
| Fast retrieve | dense (1 encode) + sparse (1 encode) |
| Deep retrieve | dense + sparse + reranker |
| Capture / ingestion | dense + sparse (1 encode each) |
| Concept synthesis (nightly) | dense (many encodes, batched) + Ollama |
| Curated rendering (promotion) | Ollama |
| Maturation LLM pass | Ollama |

The LLM is **never in the hot path**. Fast retrieval and capture use only the encoder services — well within budget.

## Loading order

At compose up:

1. TEI dense boots. Downloads weights on first run (~1.3 GB).
2. TEI sparse boots in parallel.
3. TEI reranker boots next.
4. Ollama boots last and pulls Qwen2.5-7B Q4 on first run (~4.7 GB).

Each service has a `/health` endpoint; Compose waits for healthy before Core comes up.

**Cold start time** (first boot, weights cached): ~45 seconds total. First boot ever: 5-10 minutes for downloads.

## Health probes

TEI exposes `/health` (200 OK when model loaded). Ollama exposes `/api/tags` (200 when at least one model is loaded).

Musubi Core's `/v1/ops/status` reports:

```json
{
  "tei_dense": "ok",
  "tei_sparse": "ok",
  "tei_reranker": "ok",
  "ollama": "ok",
  "vram_used_mb": 9612,
  "vram_total_mb": 10240
}
```

Monitored; alert on sustained > 95% VRAM.

## Fallbacks

### TEI dense unavailable

Capture + retrieve both degrade:

- Capture: queue in sqlite, retry every 30s. Fails the caller only after 3 min of sustained unavailability.
- Retrieve: return `503 BACKEND_UNAVAILABLE` with `Retry-After`.

### TEI sparse unavailable

Dense-only retrieval is the fallback. Quality drops but functional. Logged as a warning; ops alert if > 5 min.

### TEI reranker unavailable

Deep path falls through to fast-path result ordering (no rerank). Quality drops on long-tail queries. Logged.

### Ollama unavailable

All LLM-dependent jobs (synthesis, rendering, maturation enrichment) **pause** and retry later. No user-facing impact — captures and retrieval still work. Ops alert if > 30 min.

## Why local (not Gemini)

Given April 2026 model quality:

- BGE-M3 beats or matches Gemini text-embedding-004 on multilingual retrieval benchmarks.
- SPLADE++ V3 is state of the art for learned sparse (no Gemini equivalent).
- Qwen2.5-7B Q4 is good enough for the specific tasks we use it for (structured extraction, concept naming, brief renders). We're not trying to do open-ended reasoning with it.

Trade-off: latency. Local calls are 10-50ms for encoding, ~2s for LLM inference on Q4. A hosted API would be similar for encoding, faster for LLM — but with network and privacy costs.

We keep Gemini as an **optional** alternate embedding path — set `EMBEDDING_PROVIDER=gemini` in `.env`. Useful for a backup or for comparing quality.

## Compose snippet

```yaml
# (excerpt from /etc/musubi/docker-compose.yml)
services:
  tei-dense:
    image: ghcr.io/huggingface/text-embeddings-inference:1.5-cuda
    command: --model-id BAAI/bge-m3 --max-batch-tokens 32768
    deploy:
      resources:
        reservations:
          devices:
            - capabilities: [gpu]
    volumes:
      - tei-models:/data
    ports: ["127.0.0.1:8010:80"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/health"]
      interval: 30s

  tei-sparse:
    image: ghcr.io/huggingface/text-embeddings-inference:1.5-cuda
    command: --model-id naver/splade-v3 --pooling splade --max-batch-tokens 16384
    ...

  tei-reranker:
    image: ghcr.io/huggingface/text-embeddings-inference:1.5-cuda
    command: --model-id BAAI/bge-reranker-v2-m3 --pooling rerank
    ...

  ollama:
    image: ollama/ollama:0.4.0-cuda
    volumes:
      - ollama-models:/root/.ollama
    ports: ["127.0.0.1:11434:11434"]
    environment:
      - OLLAMA_KEEP_ALIVE=24h
      - OLLAMA_NUM_PARALLEL=1
    deploy:
      resources:
        reservations:
          devices:
            - capabilities: [gpu]
    healthcheck:
      test: ["CMD-SHELL", "ollama list | grep qwen || exit 1"]
```

`OLLAMA_NUM_PARALLEL=1` is important — concurrent LLM calls on a 10 GB card would OOM.

## Scaling beyond one GPU

If/when we outgrow 10 GB:

1. **Add a second GPU** to the same host — split encoders on GPU 0, LLM on GPU 1. Near-term option.
2. **Move LLM offbox** — run Ollama on a second host with a bigger GPU; Musubi Core calls over LAN.
3. **Hosted LLM** — switch synthesis calls to a hosted Claude or GPT. Costs money; gains quality.

None of these is needed for v1 scope. Documented for the roadmap.

## Observability

- `vram_used_mb` gauge (via `nvidia-smi` scraper sidecar).
- `tei_dense_request_duration_ms` histogram.
- `tei_sparse_request_duration_ms` histogram.
- `tei_reranker_request_duration_ms` histogram.
- `ollama_generation_ms` histogram.
- `ollama_queue_depth` gauge.

All emitted to the local metrics collector ([[09-operations/observability]]).

## Test contract

**Module under test:** this topology (integration, not a code module per se)

1. `test_all_four_services_healthy_within_60s` — bring up Compose, all four pass health within budget.
2. `test_vram_below_9_5gb_after_cold_start` — poll `nvidia-smi` after all services loaded.
3. `test_tei_dense_encode_latency_p95_lt_50ms`
4. `test_tei_sparse_encode_latency_p95_lt_80ms`
5. `test_reranker_40pair_batch_p95_lt_200ms`
6. `test_ollama_qwen25_generation_p50_lt_4s_for_200_token_output`
7. `test_core_degrades_gracefully_if_ollama_killed` — retrieval still works.
8. `test_core_503s_if_tei_dense_killed` — retrieval returns structured error, doesn't crash.
