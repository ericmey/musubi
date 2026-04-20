# Musubi · Integration Test Environment

The compose stack the integration suite (`tests/integration/`) exercises against. Used by:

- `make test-integration` (local dev — on demand).
- `.github/workflows/integration.yml` (CI — PR-trigger path-filter + nightly cron with a 3-run matrix for flake characterization).

## Shape

5 dependency containers; **musubi-core itself is NOT in this stack** — the harness boots an in-process uvicorn against this dependency pool so the integration suite exercises the same `create_app()` code path as the unit suite, and CI doesn't have to build a `musubi-core:test` image on every run.

| Service | Image | Purpose |
|---|---|---|
| `qdrant` | `qdrant/qdrant:v1.15.0` | Vector store |
| `tei-dense` | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.5` | Dense embeddings (BGE-small-en-v1.5, ~130MB) |
| `tei-sparse` | same image | Sparse embeddings (SPLADE++ EN v1) |
| `tei-reranker` | same image | Cross-encoder rerank (BGE-reranker-base) |
| `ollama` | `ollama/ollama:0.4.0` | LLM (qwen2.5:0.5b, pulled by the `ollama-pull` side-car) |

Models are intentionally small so the stack runs on a stock GitHub Actions runner without a GPU. The production compose ([deploy/docker/](../docker/)) uses larger BGE-M3 + qwen2.5:7b on GPU.

## Local run

```bash
# 1. Boot the dependency stack (~3 min cold cache; ~30s warm).
docker compose -f deploy/test-env/docker-compose.test.yml up -d --wait

# 2. Run the integration suite (boots an in-process uvicorn, runs scenarios, cleans up).
make test-integration

# 3. Tear down + drop volumes (no orphans).
docker compose -f deploy/test-env/docker-compose.test.yml down -v
```

`make test-integration` runs the boot + tear-down for you on each invocation.

### Port collisions

Defaults: Qdrant=6333, TEI dense=8081, TEI sparse=8082, TEI reranker=8083, Ollama=11434. Override any of them via env:

```bash
MUSUBI_TEST_QDRANT_PORT=16333 \
MUSUBI_TEST_TEI_DENSE_PORT=18081 \
... \
make test-integration
```

The `.env.test` file in this directory is the one the harness sources by default; tweak it locally if you have port conflicts.

### Performance budgets

Bullets 13 + 14 (`retrieve_deep_under_5s_on_10k_corpus` / `retrieve_fast_under_200ms_on_10k_corpus`) reference the production-stack budgets. On the CPU test stack those budgets aren't realistic — the bullets skip when running against this stack unless `MUSUBI_TEST_PERF_BUDGETS=strict` is set. Operator runs with `=strict` against the production GPU host nightly to enforce the spec budgets.

## Resetting state between runs

The `qdrant-storage`, `tei-models`, and `ollama-models` volumes persist data + models across `up`/`down` cycles to avoid re-downloading on each iteration. Use `down -v` to wipe.

The harness also calls `bootstrap()` against Qdrant on each test-session start, so collections + indexes always match the slice-types schema.
