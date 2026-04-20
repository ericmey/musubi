---
title: "ADR 0010: v1 Is Single-Host, No HA"
section: 13-decisions
tags: [adr, deployment, scope, section/decisions, status/accepted, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0010: v1 Is Single-Host, No HA

**Status:** accepted
**Date:** 2026-03-18
**Deciders:** Eric

## Context

The Musubi v1 user base is a household / small team — call it 1 to 5 humans and a handful of agents. Workload estimates ([[09-operations/capacity]]):

- Capture: ~5k memories/day peak.
- Retrievals: ~500/day across agents.
- Lifecycle jobs: hourly maturation (~seconds), daily synthesis (~minutes).

That is trivial for a single modest box. The dedicated host is:

- AMD Ryzen 5 (6 cores), 32GB DDR5 RAM.
- NVIDIA RTX 3080, 10GB VRAM.
- 2TB NVMe SSD for hot data; 4TB SATA SSD for cold blobs + snapshots.
- Ubuntu 24.04 LTS.

Adding HA to this would mean:

- At least two of these boxes.
- A Qdrant cluster (raft-based; requires odd node count for sane quorum → 3 boxes).
- Shared storage or per-node replication.
- A load balancer (Caddy → HAProxy / keepalived / similar).
- Secrets distribution across nodes.
- Failover runbooks.

The cost in ops and hardware is substantial. The benefit — 99.99% vs 99.9% availability — doesn't match the use case (agents tolerate a restart; this is not a life-support system).

## Decision

**v1 ships as a single-host deployment.** No HA, no load balancer in front of Musubi, no Qdrant clustering.

Availability story:

- Systemd restarts on crash.
- Health checks + alert when it's down ([[09-operations/alerts]]).
- Restore-from-snapshot in < 1h ([[09-operations/backup-restore]]).
- Documented planned downtime for updates (minutes).

Clients are built to tolerate this:

- OpenClaw has offline capture queue (IndexedDB).
- MCP clients retry with backoff.
- LiveKit fast-talker has pre-session cache ([[07-interfaces/livekit-adapter]]).

When v1 outgrows single-host ([[11-migration/scaling]]), we re-open this ADR.

## Alternatives

**A. Two-host active-active from day one.** Cost: ~2x hardware + ops. Benefit: tiny for our usage pattern.

**B. Two-host active-passive (warm standby).** Cheaper than A but still ops burden. Reconsidered as a v1.5 addition if uptime becomes a felt need.

**C. Cloud deployment for HA.** Would solve uptime but dump other problems (data residency for household content, cost, network latency for local LLM inference).

**D. Managed Qdrant (Qdrant Cloud) + self-hosted core.** Partial HA for the vector DB but still single-host for Core. Doesn't meaningfully move the needle.

## Consequences

- Deployment story is one host, one Ansible inventory entry, one systemd unit.
- Backups are the main durability story; snapshots + vault git push + blob rsync cover loss.
- Updates are planned downtime (usually <2 minutes with pre-pulled images). Announce in `ops` presence.
- Upgrade to multi-host = a phase of work, planned in [[11-migration/scaling]].

Trade-offs:

- Any single-host outage blocks all users. Mitigated by: agents degrade gracefully (offline queues), restore is fast, alerts land quickly.
- We occasionally say "Musubi is down" in chat. Normalize this.

## Links

- [[08-deployment/index]]
- [[09-operations/capacity]]
- [[09-operations/backup-restore]]
- [[11-migration/scaling]]
