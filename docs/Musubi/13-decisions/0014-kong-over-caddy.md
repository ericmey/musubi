---
title: "ADR 0014: Kong API Gateway replaces Caddy on the Musubi host"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-17
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr]
updated: 2026-04-20
up: "[[13-decisions/index]]"
reviewed: true
supersedes: ""
superseded-by: ""
---

# ADR 0014: Kong API Gateway replaces Caddy on the Musubi host

**Status:** accepted (implementation deferred — see [[13-decisions/0024-kong-deferred-for-musubi-v1]])
**Date:** 2026-04-17
**Deciders:** Eric

> **2026-04-20 update:** This ADR remains the forward architectural target.
> Implementation is deferred for Musubi v1 because Kong currently routes only
> `<external-domain>` traffic while Musubi lives on `<homelab-domain>` with no
> external address. Full rationale and re-enablement triggers in [[13-decisions/0024-kong-deferred-for-musubi-v1]].

> Concrete hostnames and IPs use placeholder tokens (`<kong-gateway>`, `<musubi-host>`, `<homelab-domain>`, etc.). Real values live in `.agent-context.local.md` at the repo root, gitignored.

## Context

The original deployment spec (pre-ADR) assumed a self-contained single-host stack with Caddy as the reverse proxy / TLS terminus running as a systemd unit on the Musubi host. The spec was written before the actual operating environment was known.

The operator already runs **Kong API Gateway** on a dedicated VM (`<kong-gateway>`, `<kong-ip>`) as the VLAN-wide API gateway for multiple services. Kong is installed, operational, and handles auth, rate-limiting, TLS, and access logging for other homelab services.

Running Caddy on the Musubi host in parallel would:

- duplicate gateway infrastructure for a single workload,
- bifurcate the cert-management story (Kong has its own; Caddy would add ACME on a second node),
- produce two audit-log surfaces instead of one,
- and force clients to know which hostname reaches which gateway.

Kong already does everything the spec expected Caddy to do, with richer plugin ecosystem for auth (OAuth, JWT, mTLS) and observability.

## Decision

- **Kong is the gateway of record** for Musubi's external API surface. The Musubi host does not run Caddy.
- The Musubi host **publishes Musubi Core on LAN-bound HTTP** at `<musubi-ip>:8100` (plain HTTP, no TLS on the Musubi host). This is Kong's only upstream target.
- TLS terminates at Kong (`<kong-gateway>`, `<kong-ip>`). Clients reach Musubi as `https://<musubi-host>/v1/*`; DNS resolves `<musubi-host>` to Kong, which routes by Host header to the Musubi upstream.
- Auth, rate-limiting, access-log, and CORS rules live in Kong's route config. Musubi Core enforces tenant/namespace scope and per-token quotas internally (not Kong's job).
- The internal inference stack (Qdrant, TEI dense/sparse/rerank, Ollama) remains **bridge-only** inside Docker Compose. No host ports, no LAN access. Musubi Core reaches them via compose service DNS.
- Ollama, if desired for non-Musubi general LLM use, gets its own Kong route (e.g. `https://ollama.<homelab-domain>/*`) with its own auth and rate-limit policy — side-by-side with Musubi's route, not inside the Musubi stack.

## Consequences

### Positive

- **One gateway, one control plane.** Ops, auth, and audit for Musubi live alongside other homelab services.
- **No cert story on the Musubi host.** The spec's ACME / DNS-01 dance is Kong's job, already solved.
- **Lighter Musubi stack.** One fewer container / systemd unit, one fewer failure mode during deploy.
- **Plugin ecosystem upgrade** — Kong's Enterprise/OSS plugins exceed Caddy's built-ins for rate-limiting, JWT validation, mTLS, and request transforms.

### Negative

- **Gateway HA is not free.** If the Kong VM (`<kong-gateway>`) is down, Musubi is unreachable from clients even if the Musubi host itself is healthy. Mitigation: the Kong VM is a small VM on `<pve-node-1>` and cheap to rebuild from the same Ansible VM-upgrade pattern used elsewhere in the homelab; a future ADR could add a standby Caddy on the Musubi host that activates on Kong-down (not pursued in v1).
- **Dependency on the network hop.** Musubi → client latency now includes one extra LAN hop through `<kong-gateway>`. For the fast-path <400ms budget this is ~1-2ms overhead; negligible.
- **Spec mismatch to fix.** Multiple spec notes reference Caddy; this ADR triggers a sweep. See [[00-index/work-log]] for the dated update.

### Neutral

- The Musubi host's `:8100` is exposed on the VLAN for Kong to reach. With the VLAN being private and firewalled upstream (Ubiquiti), this is acceptable. If a tighter posture is later required (mTLS between Kong and Musubi), we revisit in a separate ADR.

## Alternatives considered

### A) Keep Caddy as the primary gateway on the Musubi host

Rejected. Duplicates gateway infrastructure, splits the operator's mental model between "services fronted by Kong" and "Musubi fronted by Caddy", costs more to operate.

### B) Caddy as a dormant failover next to Kong

Rejected for v1 on the grounds of complexity. If Kong becomes a reliability concern in practice we can add this in a follow-up ADR. The single-node Kong VM has acceptable reliability for the homelab fail-tolerance profile.

### C) Expose Musubi Core directly on the LAN with its own TLS

Rejected. Musubi Core should not embed TLS or gateway concerns; the canonical API is plain HTTP to a gateway, always. This also keeps the Core container small and its failure modes scoped to business logic.

## References

- [[08-deployment/kong]] — Kong route + plugin config for Musubi (replaces the retired `caddy.md`).
- [[08-deployment/host-profile]] — removed Caddy systemd unit; added "Kong is the gateway" language.
- [[08-deployment/compose-stack]] — Musubi Core publishes `<musubi-ip>:8100`; no host-bound Caddy service.
- [[08-deployment/ansible-layout]] — `roles/caddy/` removed; Kong route config lives in the Kong-admin repo, not Musubi's.
- [[07-interfaces/canonical-api]] — base URL examples now use `https://<musubi-host>/v1/*`.
- Operator environment: Kong runs on a dedicated VM on the homelab VLAN as the VLAN-wide API gateway. Concrete hostname / IP in `.agent-context.local.md`.
