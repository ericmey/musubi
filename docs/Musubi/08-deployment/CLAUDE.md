---
title: "Agent Rules — Deployment (08)"
section: 08-deployment
type: index
status: complete
tags: [section/deployment, status/complete, type/index, agents]
updated: 2026-10-01
up: "[[08-deployment/index]]"
reviewed: true
---

# Agent Rules — Deployment (08)

Local rules for the root `docker-compose.yml`, `deploy/docker/compose.local-gpu.yml`
and `.env.example`. Supplements [[CLAUDE]].

## Must

- **The Compose stack is the install path.** The root `docker-compose.yml` (plus the
  optional GPU overlay) is the source of truth for what runs. Preparing the host
  (Docker, GPU drivers, OS updates, firewall) is the operator's job; Musubi does not
  provision hosts. See [[08-deployment/host-profile]].
- **Health checks on every long-running service.** Startup order uses
  `depends_on` with `service_healthy` / `service_completed_successfully`.
- **Pin every image by digest.** The Core image is pinned once on the
  `x-core-image` anchor; Qdrant is pinned by tag and digest; the GPU overlay
  refuses to start without operator-supplied TEI and Ollama digests.
- **One host.** v1 targets a single host; see [[13-decisions/0010-single-host-v1]].
- **GPU VRAM is a budget.** For the GPU overlay, see
  [[08-deployment/gpu-inference-topology]] before adding or resizing a model.
- **Secrets stay out of the repo.** They go in the operator's private `.env` or
  secret manager. `.env.example` keeps secret values blank.

## Must not

- Introduce Kubernetes, Nomad, Swarm, Podman or any orchestrator other than
  Docker Compose in v1.
- Publish Qdrant, TEI or Ollama on a host port. Only Core publishes a port, and it
  defaults to `127.0.0.1:8100`.
- Add host-provisioning steps (package installs, firewall rules, users) to the
  public stack.

## Reference host

The latency and capacity figures in these docs were measured on one reference host:
AMD Ryzen 5 (6c/12t), 32 GB RAM, one NVIDIA RTX 3080 (10 GB VRAM), NVMe SSD. It is a
point of comparison, not a requirement.

## Container roster

| Service | Image | Role | GPU |
|---|---|---|---|
| `volume-init` | Core image (one-shot) | chowns fresh volumes to UID 999/GID 985 | no |
| `qdrant` | `qdrant/qdrant:v1.17.1@sha256:…` | vector DB | no |
| `core` | `ghcr.io/sourceblender/musubi-core@sha256:…` | API + planes | no |
| `lifecycle-worker` | same Core image, `python -m musubi.lifecycle.runner` | scheduled lifecycle jobs | no |
| `tei-dense` (overlay) | operator-chosen TEI image, by digest | BGE-M3 dense | yes |
| `tei-sparse` (overlay) | same | SPLADE v3 sparse | yes |
| `tei-reranker` (overlay) | same | BGE-reranker-v2-m3 | yes |
| `ollama` (overlay) | operator-chosen Ollama image, by digest | lifecycle LLM (`LLM_MODEL`; `qwen3:4b` in `.env.example`) | yes |
