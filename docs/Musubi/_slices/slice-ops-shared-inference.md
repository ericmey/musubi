---
title: "Slice: Authenticated shared inference services"
slice_id: slice-ops-shared-inference
status: in-progress
owner: codex-yua
phase: "8 Ops"
section: _slices
type: slice
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-09-19
reviewed: false
depends-on: []
blocks: []
---

# Slice: Authenticated shared inference services

Tracks #742.

## What

Extracts the dense, sparse, and reranker TEI services from the Musubi
application deployment into a separately managed shared-inference deployment.
Raw TEI listeners remain private. A thin authenticated ingress provides stable
host endpoints to Musubi and other explicitly provisioned consumers.

This slice supersedes the closed, unmerged #741 IP-only exposure. It carries
forward that work's fail-closed policy staging and endpoint migration attacks,
while adding application authentication and per-consumer credentials.

## Specs to implement

- [[13-decisions/0045-authenticated-shared-inference-services]]
- [[08-deployment/compose-stack]]
- [[08-deployment/gpu-inference-topology]]

## Owned paths

- `deploy/ansible/`
- `deploy/runbooks/`
- `docs/Musubi/08-deployment/`
- `docs/Musubi/13-decisions/0045-authenticated-shared-inference-services.md`
- `docs/Musubi/13-decisions/index.md`
- `tests/ops/test_shared_inference.py`
- `tests/ops/iptables_stub.py`
- `docs/Musubi/_slices/slice-ops-shared-inference.md`
- `docs/Musubi/_inbox/locks/slice-ops-shared-inference.lock`

## Forbidden paths

- `src/musubi/api/`
- `openapi.yaml`
- `proto/`

## Test Contract

1. `test_shared_inference_is_owned_by_a_separate_deployment_unit`
2. `test_tei_backends_are_not_host_published`
3. `test_every_shared_endpoint_requires_authentication`
4. `test_consumers_receive_distinct_runtime_credentials`
5. `test_normal_app_deploy_does_not_restart_shared_inference`
6. `test_failed_cutover_keeps_the_old_authenticated_endpoint_protected`
7. `test_musubi_can_cut_over_and_roll_back_by_configuration`
8. `test_live_values_do_not_enter_public_sources`

## Work log

### 2026-09-19 — codex-yua — claimed

- Owner approved extracting the model services for reuse by Musubi and Chord.
- Live/source inspection confirmed three Compose-internal TEI services and no
  host-published backend listeners.
- Rejected raw unauthenticated VLAN exposure; the shared door requires
  authentication and keeps the model backends private.

## PR links

- [PR #743](https://github.com/ericmey/musubi/pull/743)
