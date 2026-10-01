---
title: Compose Stack
section: 08-deployment
tags: [containers, deployment, docker-compose, section/deployment, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[08-deployment/index]]"
reviewed: false
implements: "docker-compose.yml"
---
# Compose Stack

The root `docker-compose.yml` is the public application stack. It runs Musubi
Core, its lifecycle worker and Qdrant. The CPU quickstart in
`quickstart/docker-compose.yml` remains a separate demo with a public test key.
An optional `deploy/docker/compose.local-gpu.yml` adds local TEI and Ollama
services; operators choose digest-pinned TEI and Ollama images compatible
with their GPU.
Neither path
installs packages, drivers, users or firewall rules on the host.

## Required inputs

Copy `.env.example` to a private `.env` and set a random `JWT_SIGNING_KEY`, a
`QDRANT_API_KEY`, and reachable dense, sparse, reranker and Ollama URLs. Core
loads model IDs and the OAuth issuer from the same file. Compose refuses
missing keys or endpoint URLs before creating containers. Optional TEI Basic
auth and lifecycle LLM settings are passed through to Core when supplied.

Core publishes only `MUSUBI_CORE_BIND:MUSUBI_CORE_PORT`, defaulting to
`127.0.0.1:8100`. To reach it over a network, the operator must explicitly
change the bind, configure TLS/auth at the edge and manage their own firewall.
Qdrant, the lifecycle worker and optional inference services publish no host
ports. Qdrant uses HTTP only on the Compose network with its API key; Core's
`MUSUBI_ALLOW_PLAINTEXT=true` applies to that internal Qdrant link.

## Data and startup

Named volumes persist Qdrant storage and snapshots, vault files, artifact
blobs, lifecycle state and logs. The optional GPU stack adds model-cache
volumes. The published Core image runs as UID 999/GID 985, so a one-shot
`volume-init` service grants that account the roots of fresh application
volumes. Core and the worker stay non-root. Core waits for Qdrant health and
volume initialization; the worker waits for Core health. Core itself probes
Qdrant and the dense TEI endpoint at startup and refuses readiness if either
is unreachable. Operators verify the remaining inference paths with a canary.

The Core, worker and volume-init services use one image pin via the
`x-core-image` anchor. Its digest must match the Core image in the CPU
quickstart; the release pin PR updates both files. The source repository
does not deploy to any host. Operators review and apply pins themselves.

## Backups

All named volumes must be backed up and restored as one consistent set.
The previous `/var/lib/musubi` backup script is for the private Ansible
layout and is not this stack's backup procedure. The public guide describes
a cold backup with the stack stopped. The operator supplies the storage and
retention system.

## Test Contract

**Module under test:** root `docker-compose.yml`, optional GPU override and
`.env.example`.

1. `test_public_compose_has_real_matching_core_pins`
2. `test_public_compose_remote_mode_is_host_independent`
3. `test_public_compose_local_gpu_mode_has_no_inference_port`
4. `test_public_compose_gpu_mode_refuses_unpinned_image`
5. `test_public_compose_example_requires_operator_secrets_and_endpoints`
6. `test_public_compose_refuses_missing_key_or_endpoint`
7. `test_public_compose_allows_explicit_lan_bind`
