# Musubi Docker files

The public stack is the repo-root [`docker-compose.yml`](../../docker-compose.yml)
(Core, the lifecycle worker, Qdrant and a one-shot volume-init). Install and
operate it with [`docs/guide/install.md`](../../docs/guide/install.md) and
[`docs/guide/operate.md`](../../docs/guide/operate.md). This directory holds the
optional extras.

## Files

- `compose.local-gpu.yml`: optional overlay that adds local TEI (dense, sparse,
  reranker) and Ollama. Use it with
  `docker compose -f docker-compose.yml -f deploy/docker/compose.local-gpu.yml ...`.
  The inference services publish no host ports.
- `smoke-health.sh`: brings the stack up (`up -d --wait`), checks every service
  is healthy, then probes `/v1/ops/health` on `127.0.0.1:8100`. It is not
  read-only. Its defaults are an older `/etc/musubi` layout; for the public stack,
  set `PROJECT_DIR`, `COMPOSE_FILE` and `ENV_FILE` to your checkout and `.env`.
- `kong.yml`: an example decK configuration for one operator's gateway. It is not
  part of the stack. Any TLS reverse proxy works; see
  [Exposing Core](../../docs/Musubi/08-deployment/kong.md).

Core publishes only `127.0.0.1:8100` by default. Qdrant, TEI and Ollama are reached
by Compose service name on the stack's default network.
