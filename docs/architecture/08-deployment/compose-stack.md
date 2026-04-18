---
title: Compose Stack
section: 08-deployment
tags: [containers, deployment, docker-compose, section/deployment, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-18
up: "[[08-deployment/index]]"
reviewed: false
---
# Compose Stack

The `docker compose` stack. One file captures every container Musubi runs.

**Location on host:** `/etc/musubi/docker-compose.yml` (Ansible-rendered).

> Hostnames and IPs use placeholder tokens (`<kong-gateway>`, `<musubi-ip>`, etc.). Real values in `.agent-context.local.md` (gitignored).

## Services

| Service | Image | Role |
|---|---|---|
| `qdrant` | `qdrant/qdrant:v1.15.0` | Vector DB |
| `tei-dense` | `ghcr.io/huggingface/text-embeddings-inference:1.5-cuda` | Dense embeddings (BGE-M3) |
| `tei-sparse` | same image | Sparse embeddings (SPLADE++ V3) |
| `tei-reranker` | same image | Cross-encoder rerank (BGE-reranker-v2-m3) |
| `ollama` | `ollama/ollama:0.4.0-cuda` | LLM (Qwen2.5-7B Q4) |
| `core` | `ghcr.io/ericmey/musubi-core:v1.0.0` | Musubi Core (FastAPI + lifecycle worker) |

No gateway runs on the Musubi host. **Kong on `<kong-gateway>`** terminates TLS and fronts Musubi Core — covered in [[08-deployment/kong]] and [[13-decisions/0014-kong-over-caddy]].

## Volumes

| Volume | Mounted by | Purpose |
|---|---|---|
| `qdrant-storage` | qdrant | Vector DB storage |
| `tei-models` | tei-* | HF model cache (shared across TEI services) |
| `ollama-models` | ollama | Ollama model weights |
| bind `/var/lib/musubi/vault` | core | Obsidian vault (read/write) |
| bind `/var/lib/musubi/artifact-blobs` | core | Content-addressed artifact blobs |
| bind `/var/lib/musubi/lifecycle-work.sqlite` | core | Write-log + schedule locks |
| bind `/var/log/musubi` | core | Structured log output |

Bind mounts stay on the host (`/var/lib/musubi/...`) for easy backup + external access.

## Networks

One user-defined network `musubi-net`. All services on it; Core reaches peers via service-name DNS (`qdrant:6333`, `tei-dense:80`, etc.).

Kong (on `<kong-gateway>`, `<kong-ip>`) reaches Core via `http://<musubi-ip>:8100`. Core's only host-exposed port.

## Health checks

Every container has a health check. Compose's `depends_on.condition: service_healthy` enforces the start order:

```
qdrant + tei-* + ollama  (parallel, independent)
         │
         ▼
       core   (waits for all above)
```

Examples:

```yaml
qdrant:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:6333/healthz"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 60s

tei-dense:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:80/health"]
    interval: 30s
    start_period: 120s    # model load on first boot

ollama:
  healthcheck:
    test: ["CMD-SHELL", "ollama list | grep -q qwen2.5 || exit 1"]
    interval: 60s
    start_period: 300s    # first pull is ~5 min

core:
  depends_on:
    qdrant: {condition: service_healthy}
    tei-dense: {condition: service_healthy}
    tei-sparse: {condition: service_healthy}
    tei-reranker: {condition: service_healthy}
    ollama: {condition: service_healthy}
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8100/v1/ops/health"]
    interval: 30s
    start_period: 30s
```

## Env

Core reads its config from `/etc/musubi/.env` (managed by Ansible). Sample:

```env
# Qdrant
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_API_KEY=...

# Inference
TEI_DENSE_URL=http://tei-dense
TEI_SPARSE_URL=http://tei-sparse
TEI_RERANKER_URL=http://tei-reranker
OLLAMA_URL=http://ollama:11434
EMBEDDING_MODEL=BAAI/bge-m3
SPARSE_MODEL=naver/splade-v3
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
LLM_MODEL=qwen2.5:7b-instruct-q4_K_M

# Core
BRAIN_PORT=8100
VAULT_PATH=/var/lib/musubi/vault
ARTIFACT_BLOB_PATH=/var/lib/musubi/artifact-blobs
LIFECYCLE_SQLITE_PATH=/var/lib/musubi/lifecycle-work.sqlite
LOG_DIR=/var/log/musubi

# Auth
JWT_SIGNING_KEY=...
OAUTH_AUTHORITY=https://auth.internal.example.com

# Feature flags
MUSUBI_GRPC=false
MUSUBI_ALLOW_PLAINTEXT=false
```

## Resource limits

Compose enforces per-service limits so no runaway container can starve the host:

```yaml
core:
  deploy:
    resources:
      limits: {memory: 4G, cpus: "2.0"}
      reservations: {memory: 1G}

qdrant:
  deploy:
    resources:
      limits: {memory: 8G, cpus: "4.0"}

tei-dense:
  deploy:
    resources:
      limits: {memory: 2G, cpus: "2.0"}
      reservations:
        devices:
          - capabilities: [gpu]
```

GPU is shared (see [[08-deployment/gpu-inference-topology]]). CPU + RAM limits keep the host stable; Qdrant gets the lion's share of RAM because its page cache is critical.

## Logging

All services configured with journald:

```yaml
x-logging: &default-logging
  driver: journald
  options:
    tag: "musubi.{{.Name}}"

services:
  core:
    logging: *default-logging
  ...
```

`journalctl -t musubi.core -f` tails Core logs. Useful even without a log aggregator.

## Restart policy

Everything: `restart: unless-stopped`. Crash → back up. Host reboot → stack comes up. Ansible handles explicit stops (during updates).

## Update procedure

```
$ ansible-playbook playbooks/update.yml
```

Pulls new image digests (must be pinned in the compose file) and recreates only changed services. See [[08-deployment/ansible-layout#playbooks/update.yml]].

## Full compose (trimmed)

```yaml
version: "3.9"

networks:
  musubi-net:

volumes:
  qdrant-storage:
  tei-models:
  ollama-models:

x-gpu: &gpu
  deploy:
    resources:
      reservations:
        devices:
          - capabilities: [gpu]

x-logging: &default-logging
  driver: journald
  options:
    tag: "musubi.{{.Name}}"

services:
  qdrant:
    image: qdrant/qdrant:v1.15.0@sha256:...
    volumes:
      - qdrant-storage:/qdrant/storage
      - /etc/musubi/qdrant-config.yaml:/qdrant/config/production.yaml
    networks: [musubi-net]
    ports:
      - "127.0.0.1:6333:6333"
      - "127.0.0.1:6334:6334"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/healthz"]
      interval: 30s
    restart: unless-stopped
    logging: *default-logging

  tei-dense:
    image: ghcr.io/huggingface/text-embeddings-inference:1.5-cuda@sha256:...
    command: --model-id BAAI/bge-m3 --max-batch-tokens 32768
    volumes:
      - tei-models:/data
    networks: [musubi-net]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/health"]
      start_period: 120s
    <<: *gpu
    restart: unless-stopped
    logging: *default-logging

  tei-sparse:
    image: ghcr.io/huggingface/text-embeddings-inference:1.5-cuda@sha256:...
    command: --model-id naver/splade-v3 --pooling splade --max-batch-tokens 16384
    volumes:
      - tei-models:/data
    networks: [musubi-net]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/health"]
      start_period: 120s
    <<: *gpu
    restart: unless-stopped
    logging: *default-logging

  tei-reranker:
    image: ghcr.io/huggingface/text-embeddings-inference:1.5-cuda@sha256:...
    command: --model-id BAAI/bge-reranker-v2-m3 --pooling rerank
    volumes:
      - tei-models:/data
    networks: [musubi-net]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/health"]
      start_period: 120s
    <<: *gpu
    restart: unless-stopped
    logging: *default-logging

  ollama:
    image: ollama/ollama:0.4.0-cuda@sha256:...
    volumes:
      - ollama-models:/root/.ollama
    networks: [musubi-net]
    environment:
      - OLLAMA_KEEP_ALIVE=24h
      - OLLAMA_NUM_PARALLEL=1
    healthcheck:
      test: ["CMD-SHELL", "ollama list | grep -q qwen2.5 || exit 1"]
      start_period: 300s
    <<: *gpu
    restart: unless-stopped
    logging: *default-logging

  core:
    image: ghcr.io/ericmey/musubi-core:v1.0.0@sha256:...
    env_file: /etc/musubi/.env
    volumes:
      - /var/lib/musubi/vault:/var/lib/musubi/vault
      - /var/lib/musubi/artifact-blobs:/var/lib/musubi/artifact-blobs
      - /var/lib/musubi/lifecycle-work.sqlite:/var/lib/musubi/lifecycle-work.sqlite
      - /var/log/musubi:/var/log/musubi
    networks: [musubi-net]
    ports:
      - "127.0.0.1:8100:8100"
    depends_on:
      qdrant:        {condition: service_healthy}
      tei-dense:     {condition: service_healthy}
      tei-sparse:    {condition: service_healthy}
      tei-reranker:  {condition: service_healthy}
      ollama:        {condition: service_healthy}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8100/v1/ops/health"]
      start_period: 30s
    restart: unless-stopped
    logging: *default-logging
```

Digest pins elided for brevity; Ansible fills them from `inventory/group_vars/all.yml`.

## Test contract

**Module under test:** `/etc/musubi/docker-compose.yml`

1. `test_compose_config_valid` — `docker compose config` exits 0.
2. `test_every_service_has_healthcheck`
3. `test_every_image_pinned_by_digest`
4. `test_core_depends_on_all_dependencies_healthy`
5. `test_only_core_publishes_a_host_port` — only `core` publishes a port (`<musubi-ip>:8100`); every other service is bridge-only. Kong lives on `<kong-gateway>`, not here.
6. `test_gpu_services_list_gpu_reservation`
7. `test_bind_mounts_exist_on_host`
8. `test_compose_up_to_healthy_under_5min_on_warm_cache` (integration)
