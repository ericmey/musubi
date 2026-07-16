---
title: "0038: Network-Protect Read-Only Ops Endpoints"
section: 13-decisions
tags: [architecture, auth, operations, security, type/adr, status/accepted]
type: adr
status: accepted
date: 2026-07-15
updated: 2026-07-15
deciders: [Eric, Yua]
---

# 0038: Network-Protect Read-Only Ops Endpoints

## Context

`GET /v1/ops/health`, `GET /v1/ops/status`, and `GET /v1/ops/metrics` are
intentionally readable without a bearer token. They are consumed before normal
tenant authentication is available:

- deployment and rollback probes use health/status to decide whether Core is safe;
- the SDK probes status for service-version compatibility;
- the local Prometheus container scrapes metrics over the private Compose bridge.

Adding route-level operator auth would require distributing an operator credential
to readiness probes and Prometheus. That makes liveness depend on the auth stack,
turns a long-lived scrape secret into another production credential, and breaks the
current failure-isolation boundary.

The 2026-07-12 review correctly identified that unauthenticated ops reads are safe
only when the repository makes their network protection explicit and testable.

## Decision

Keep the three **read-only** ops endpoints bearer-unauthenticated and protect them
at the deployment network boundary.

The enforced boundary is:

1. Host ingress is UFW default-deny.
2. While Kong is deferred by [[13-decisions/0024-kong-deferred-for-musubi-v1]],
   Core `:8100` is allowed only from `musubi_vlan_cidr`.
3. When `musubi_kong_ip` is configured, the VLAN rule is disabled and Core `:8100`
   is allowed only from that single Kong address.
4. Prometheus scrapes `core:8100/v1/ops/metrics` on the private Compose network; it
   does not traverse the host ingress rule. Prometheus's own host port is bound to
   `127.0.0.1`.
5. Mutating and debug ops endpoints remain operator-scoped in Core. This exception
   applies only to health, status, and metrics reads.

The endpoints must not return tenant content, namespace inventories, bearer tokens,
signing material, or secret configuration. Status may expose service version,
component names, health booleans, and bounded diagnostic detail. Metrics may expose
the existing bounded-label operational series.

## Negative proof

CI pins the boundary at the source-of-truth deployment files:

- `deploy/ansible/bootstrap.yml` must retain incoming default-deny and mutually
  exclusive Kong-IP / trusted-VLAN allows for the Core port;
- `deploy/ansible/templates/prometheus.yml.j2` must scrape Core by Compose service
  name, not a host/LAN address;
- `deploy/ansible/templates/docker-compose.yml.j2` must keep Prometheus loopback-only;
- the read-only routes must remain distinct from the operator-authenticated debug
  route, so adding a new unauthenticated mutation is not hidden by this decision.

## Blast radius and residual risk

Any host already admitted to the trusted Musubi VLAN can read readiness and metrics
until Kong becomes the sole upstream. That can reveal component availability,
software version, request volume, and latency. It cannot authorize data reads or
mutations. We accept that bounded operational disclosure for the household trusted
VLAN rather than introduce scrape/probe credentials.

These endpoints are not safe for public Internet exposure.

## Owner and review triggers

Owner: Yua / Musubi operations.

Review by 2026-10-15, or immediately if any of these occurs first:

- Core `:8100` is exposed beyond the trusted VLAN or configured Kong address;
- Prometheus scrapes Core across a host or VLAN boundary instead of Compose;
- status or metrics begins including tenant-derived content, unbounded labels, or
  secret-bearing diagnostic values;
- a multi-tenant or untrusted device joins the allowed VLAN;
- deployment changes weaken UFW default-deny or the mutually exclusive allow rules.

At that point, prefer a dedicated least-privilege probe/scrape credential or a
separate internal listener over distributing a full operator token.

## Alternatives rejected

### Require `operator` on status and metrics now

Rejected because it couples readiness and observability to auth availability and
requires a privileged long-lived Prometheus credential.

### Leave the routes unauthenticated without a repository contract

Rejected. An undocumented network assumption silently becomes public exposure when
bind, firewall, or proxy configuration changes.
