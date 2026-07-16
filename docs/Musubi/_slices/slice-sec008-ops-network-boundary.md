---
title: "Slice: SEC-008 — Ops Endpoint Network Boundary"
slice_id: slice-sec008-ops-network-boundary
status: done
owner: yua
phase: "8-ops"
section: _slices
type: slice
tags: [section/slices, status/done, type/slice]
updated: 2026-07-15
reviewed: true
depends-on: []
blocks: []
---

# Slice: SEC-008 — Ops Endpoint Network Boundary

Tracks #557.

## What

Makes the intentional network protection for unauthenticated read-only operational
endpoints explicit and testable. It does not weaken normal bearer/scope enforcement
and does not authorize any mutating or debug endpoint.

## Specs to implement

- [[10-security/auth]]
- [[07-interfaces/canonical-api]]
- [[13-decisions/0038-network-protect-read-only-ops-endpoints]]

## Files

- `owns_paths`:
  - `docs/Musubi/13-decisions/0038-network-protect-read-only-ops-endpoints.md`
  - `docs/Musubi/13-decisions/index.md`
  - `docs/Musubi/10-security/auth.md`
  - `docs/Musubi/07-interfaces/canonical-api.md`
  - `tests/ops/test_sec008_ops_network_boundary.py`
  - `docs/Musubi/_slices/slice-sec008-ops-network-boundary.md`
  - `docs/Musubi/_inbox/locks/slice-sec008-ops-network-boundary.lock`

## Test Contract

1. `test_read_only_ops_exception_stays_bounded`
2. `test_core_ingress_is_default_deny_and_source_restricted`
3. `test_prometheus_scrapes_core_privately_and_stays_loopback_only`
4. `test_sec008_adr_names_owner_blast_radius_and_review_triggers`

## Work log

- Verified the production Ansible firewall, Compose, Prometheus, SDK, and ops-router
  consumers before selecting network-policy acceptance over route authentication.
- Recorded the accepted exposure, negative proof, owner, blast radius, and triggers
  in ADR 0038.
- Added structural CI contracts over the actual deployment sources of truth.
- Full repository gate: 2399 passed, 195 skipped, 5 xfailed; ruff, mypy,
  coverage, diff check, agent-check, and Closure Rule are green.
- Tama independently reviewed exact head `debbd92`, verified all five contract
  points against the router and deployed boundary, and proved each of the four
  structural tests fails when its protected invariant is weakened. Verdict:
  **APPROVE**.
