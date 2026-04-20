---
title: "Agent Rules — Deployment (08)"
section: 08-deployment
type: index
status: complete
tags: [section/deployment, status/complete, type/index, agents]
updated: 2026-04-17
up: "[[08-deployment/index]]"
reviewed: true
---

# Agent Rules — Deployment (08)

Local rules for `deploy/ansible/`, `deploy/docker/`, `docker-compose.yml`, `Kong route config`, `deploy/grafana/`. Supplements [[CLAUDE]].

## Must

- **Ansible is the source of truth.** Every step to stand up a host is in a playbook. "Run this one command and then…" is a bug.
- **Docker Compose for service topology.** Single file at repo root or `deploy/docker/docker-compose.yml`. Health checks on every service. Startup order via `depends_on: condition: service_healthy`.
- **One host profile at a time.** v1 targets the reference host in [[08-deployment/host-profile]]. Multi-host is explicitly out of scope; see [[13-decisions/0010-single-host-v1]].
- **GPU VRAM is a hard budget.** See [[08-deployment/gpu-inference-topology]]. Adding a model requires updating the budget table. No silent co-residency.
- **Secrets via `ansible-vault encrypt_string`.** Never commit plaintext secrets. `.env` files are templated.

## Must not

- Introduce Kubernetes, Nomad, Swarm, systemd-nspawn, Podman, or any orchestrator other than Docker Compose in v1.
- Mount the Obsidian vault writable by more than one process. One serialized writer.
- Pin a model version outside [[08-deployment/gpu-inference-topology]] — the table is the budget.

## Host profile (v1 target)

- Ryzen 5, 32 GB RAM, single NVIDIA RTX 3080 (10 GB VRAM), NVMe SSD.
- Ubuntu Server LTS.
- Docker + NVIDIA Container Toolkit.
- Public network via Kong reverse proxy + Tailscale for remote.

## Container roster

| Container        | Image                       | Role                      | GPU |
|------------------|-----------------------------|---------------------------|-----|
| `qdrant`         | `qdrant/qdrant:1.15+`       | vector DB                 | no  |
| `tei-dense`      | `ghcr.io/huggingface/text-embeddings-inference:*` | BGE-M3 dense | yes |
| `tei-sparse`     | `ghcr.io/huggingface/text-embeddings-inference:*` | SPLADE++ sparse | yes |
| `tei-rerank`     | `ghcr.io/huggingface/text-embeddings-inference:*` | BGE-reranker | yes |
| `ollama`         | `ollama/ollama:*`           | Qwen2.5-7B Q4             | yes |
| `musubi-core`    | built in repo               | API + planes              | no  |
| `musubi-lifecycle` | built in repo             | scheduler + jobs          | no  |
| `kong`          | `kong (managed outside this repo)`                   | TLS + reverse proxy       | no  |

## Related slices

- [[_slices/slice-ops-ansible]], [[_slices/slice-ops-compose]].
