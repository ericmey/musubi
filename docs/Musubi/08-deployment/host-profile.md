---
title: Host Profile
section: 08-deployment
tags: [deployment, host, section/deployment, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[08-deployment/index]]"
reviewed: false
implements: "docker-compose.yml"
---
# Host Profile

What a host needs before you run the Musubi Compose stack, and the reference host the
docs' numbers were measured on. Musubi does not provision hosts: installing Docker and
GPU drivers, OS updates, users and firewall rules are the operator's job.

## What the operator provides

- **Linux with Docker Engine and Docker Compose v2.**
- **For the optional GPU overlay** (`deploy/docker/compose.local-gpu.yml`): an NVIDIA
  GPU with a working driver and the NVIDIA Container Toolkit configured for Docker.
  Without it, point `TEI_*_URL` and `OLLAMA_URL` at endpoints you run elsewhere.
- **Disk** for the named volumes (Qdrant storage and snapshots, vault, artifact blobs,
  lifecycle state) and, with the overlay, the model caches.
- **If agents connect from other machines:** a TLS reverse proxy in front of Core and
  your own firewall rules. See [[08-deployment/kong]].

Check that containers can see the GPU before starting the overlay:

```
docker run --rm --gpus all ubuntu:24.04 nvidia-smi
```

## What the stack assumes about the host

- Only Core publishes a host port: `127.0.0.1:8100` by default (`MUSUBI_CORE_BIND`,
  `MUSUBI_CORE_PORT`).
- Core and the lifecycle worker run as UID 999 / GID 985 inside the container. The
  one-shot `volume-init` service chowns the fresh volumes, so no host user or directory
  setup is needed.
- Data lives in named Docker volumes, not host paths. See [[08-deployment/compose-stack]].

## Reference host

The hardware the sizing and latency figures in these docs were measured on. Any
comparable host works; use it to judge what it takes to run Musubi.

| Component | Spec |
|---|---|
| CPU | AMD Ryzen 5 (6c/12t) |
| RAM | 32 GB |
| GPU | NVIDIA RTX 3080, 10 GB VRAM |
| Storage | NVMe SSD |
| Network | 1 GbE |
| Role | Dedicated; no shared workloads |

The 10 GB of VRAM is the constraint that shapes [[08-deployment/gpu-inference-topology]].

## Test Contract

**Module under test:** root `docker-compose.yml` and the GPU overlay
(`tests/ops/test_public_compose.py`).

1. `test_public_compose_remote_mode_is_host_independent`
2. `test_public_compose_local_gpu_mode_has_no_inference_port`
3. `test_public_compose_allows_explicit_lan_bind`
