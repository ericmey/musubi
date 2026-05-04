---
title: "ADR 0033: Centralize Observability on Shiori"
section: 13-decisions
tags: [adr, architecture, observability, ops, section/decisions, status/proposed, type/adr]
type: adr
status: proposed
date: 2026-05-03
deciders: [Eric]
updated: 2026-05-03
up: "[[13-decisions/index]]"
reviewed: false
supersedes: []
superseded-by: []
---
# ADR 0033: Centralize Observability on Shiori

**Status:** proposed
**Date:** 2026-05-03
**Deciders:** Eric

## Context

[[_slices/slice-ops-observability]] (closed `done` 2026-04-19) planned a full local observability stack on the musubi workload host: Prometheus, Loki, Tempo, and Grafana, all running alongside the musubi service containers. The slice's `## Owned paths` listed `deploy/grafana/`, `deploy/loki/`, `deploy/prometheus/`, and `deploy/tempo/` as new — to be created together.

Reality on the host as of 2026-05-03:

- **`prometheus` is running** (`musubi-prometheus-1`), scraping musubi-core, the three TEI services, and itself. Working as designed.
- **No grafana, no loki, no tempo, no alertmanager** is deployed. The config directories under `deploy/grafana/`, `deploy/loki/`, `deploy/tempo/` exist as scaffolding, never wired into the production compose template (`deploy/ansible/templates/docker-compose.yml.j2`).
- The `deploy/prometheus/prometheus.yml` header explicitly notes the gap: *"alertmanager + rule_files (no Alertmanager deployed yet; cutting a separate slice once notification channels are decided)"*.

Two pieces of context have changed since the slice was authored:

1. **A dedicated observability host now exists** (shiori, 10.0.20.15 — Ubuntu 26.04, GMKtec NucBox G10 Pro). Its sole role is the LGTM stack (Loki / Grafana / Tempo / Mimir + OTel Collector). It went live 2026-05-03 with shiori host metrics, Grafana self-metrics, and a first dashboard. Multi-host correlation is what it was built for.
2. **The musubi workload host is RAM-tight** (16 GB serving ollama + qdrant + 3 TEI models, all RAM-hungry). Adding ~1.5 GB of local visualization stack to a host that already has the central stack reachable on the same VLAN is paying twice for the same capability.

The "single host v1" framing of [[13-decisions/0010-single-host-v1]] applies to the *musubi service deployment*, not to its observability surface. Shiori as a sibling observability host doesn't violate that ADR — it complements it.

## Decision

**Local observability on musubi keeps only what it must run locally; everything else moves to shiori.**

Concretely:

- **Keep on musubi:** the existing `prometheus` container. It scrapes musubi service internals (musubi-core `/v1/ops/metrics`, the three TEI `/metrics` endpoints, itself) where it has the docker-network advantage. Local scrape stays cheap and reliable.
- **Add on musubi:** `node-exporter` (a documented gap in the existing `prometheus.yml` header) for host CPU/memory/disk/network metrics. Pinned image `prom/node-exporter:v1.11.1`.
- **Add on musubi:** a `remote_write` block in `prometheus.yml` pointing at shiori's Mimir (`http://shiori.mey.house:9009/api/v1/push`). Everything prometheus scrapes locally is mirrored to central in real time.
- **Remove from this repo:** the never-deployed `deploy/grafana/`, `deploy/loki/`, `deploy/tempo/` directories. They documented a future that's no longer the future.
- **Defer central-side artifacts** (musubi-specific dashboards on shiori, alert rules in Mimir/Grafana, log shipping from musubi to Loki, distributed traces) to follow-up slices/PRs scoped to the shiori-side codebase (which lives in the operator's vault, not here).

The visualization, alerting, and trace surfaces are now provided by shiori's central stack. Musubi contributes its metrics; consumes nothing locally beyond the prometheus it already runs.

## Alternatives considered

### Alternative 1: Keep the planned local stack + remote_write

Deploy `deploy/grafana/`, `deploy/loki/`, `deploy/tempo/` on musubi as originally planned, *and* add `remote_write` to shiori. Both surfaces exist; operators can use either.

**Rejected.** Pays the ~1.5 GB RAM cost on a memory-tight host for visualization that the central stack already does better (multi-host correlation, single-source-of-truth). "Where do I look?" having two answers is operational debt — operators rotate, dashboards drift, alert routing forks. The fail-safe argument (local stack survives shiori outage) is weak: shiori is on the same VLAN with sub-millisecond latency; if it's unreachable, the wider problem is bigger than musubi's dashboards.

### Alternative 2: Remove the local prometheus too — central scrapes musubi remotely

Strip prometheus from musubi entirely. Run a central prometheus on shiori that reaches into musubi's docker network somehow (port-forwarding, proxy, exposing internal endpoints).

**Rejected.** Local scrape from inside the musubi docker network is the cheapest, most reliable path — service names resolve on the bridge, no VLAN traversal per scrape, no auth surface to expose. Pulling that across the VLAN would mean either exposing TEI/qdrant/core internal `/metrics` endpoints (auth surface we don't currently have) or running a remote-scrape-via-tunnel mechanism (more infrastructure than we save). `remote_write` from local prometheus → central Mimir is the standard pattern; use it.

### Alternative 3: Replace local prometheus with an OTel Collector

Deploy an OTel Collector container on musubi instead of prometheus. Its `prometheus` receiver scrapes musubi internals; its `hostmetrics` receiver covers the host; everything pushed to shiori via OTLP. No node-exporter needed.

**Rejected for now.** OTel Collector is the right pattern for *new* fleet hosts (it's what shiori uses for its own host metrics). On musubi we already have a working prometheus with tuned scrape configs and 30 days of historical data. Rip-and-replace creates risk for marginal architectural purity. If we later want OTel-native everywhere, that's a follow-up — `remote_write` lands the centralization win today without disturbing the working scraper.

## Consequences

### Good

- **Frees ~1.5 GB RAM on musubi** (no grafana/loki/tempo). Real headroom for ollama + qdrant + TEI on a 16 GB host.
- **Single visualization surface.** All dashboards live on shiori. One place to look, one to maintain, one to teach future operators.
- **Multi-host correlation becomes possible.** Musubi metrics + shiori metrics + future fleet hosts all in one Mimir instance.
- **Closes the documented host-metrics gap** (`prometheus.yml` header noted "no host-level exporter deployed yet"). node-exporter lands as part of this change.
- **Removes scaffolding that documented the wrong future.** `deploy/grafana/`, `deploy/loki/`, `deploy/tempo/` were never built; their continued presence implied a roadmap item that's now superseded.

### Bad

- **Visualization is now a network-dependent capability.** If shiori is unreachable, operators lose the Grafana view. Mitigations: (a) prometheus on musubi still scrapes locally — the `/api/v1/query` endpoint on `127.0.0.1:9090` is direct PromQL access for debugging; (b) shiori is on the same VLAN with sub-millisecond latency — outage is a real failure mode of the wider system, not just observability.
- **No local alerting** until central alerting is wired on shiori. The previously-planned local Alertmanager won't materialize. Acceptable: the slice that was going to deploy local Alertmanager hadn't decided notification channels anyway, and central alerting is strictly better (multi-host alert correlation, one routing config).
- **One-time codebase churn.** Doc updates across 5 files + slice spec adjustment + ADR + this PR.

### Neutral

- **`prometheus` on musubi keeps a 30-day local TSDB** (per `--storage.tsdb.retention.time=30d`). Mimir on shiori will also retain. Brief overlap is fine — local is the source for the next 30 days; central is the longer-horizon and cross-host store.
- **`remote_write` adds outbound network traffic** (~few KB/s of compressed metric samples). Negligible at musubi's scrape rate.
- **The `host: musubi.example.local` placeholder** in current `prometheus.yml` `external_labels` gets fixed to `host: musubi` while we're touching the file.

## Implementation order

1. **This ADR.**
2. **PR `chore/centralize-observability-on-shiori`** — single PR per [[CLAUDE]] cleanup conventions:
   - `deploy/prometheus/prometheus.yml` — add `remote_write`, fix `host` placeholder, add node-exporter scrape, update header comments
   - `deploy/ansible/templates/docker-compose.yml.j2` — add `node-exporter` service
   - `deploy/ansible/group_vars/all.yml` — pin `musubi_node_exporter_image`
   - Delete `deploy/grafana/`, `deploy/loki/loki.yml`, `deploy/tempo/tempo.yml`
   - Doc updates: `_slices/slice-ops-observability` (supersession note + work log entry), `09-operations/CLAUDE.md`, `08-deployment/CLAUDE.md`, `12-roadmap/next-up.md`, `_inbox/cross-slice/slice-ops-observability-slice-lifecycle-job-emit.md`
3. **Apply via `deploy/ansible/update.yml`** with `-e changed_services='["prometheus","node-exporter"]'` per `deploy/runbooks/upgrade.md` — re-renders compose, reloads prometheus config (SIGHUP), starts node-exporter container.
4. **Verify on shiori** that `node_*` (host metrics from node-exporter — e.g. `node_load1`, `node_cpu_seconds_total`, `node_memory_MemAvailable_bytes`) and `musubi_*` (musubi-core API metrics) series appear in Mimir tagged with `cluster=musubi` and `host=musubi` external labels. Note: shiori's own host metrics use the `system_*` naming convention because shiori uses the OTel Collector hostmetrics receiver; musubi's host metrics use `node_*` because musubi uses node-exporter scraped by prometheus. Both are valid; queries that span hosts need to use the appropriate name per source.
5. **(Future, separate PRs, scoped to shiori-side codebase)** central dashboards for musubi service health, central alert rules, log shipping from musubi to Loki on shiori, distributed traces.

## Related

- [[_slices/slice-ops-observability]] — the slice that planned the now-superseded local stack. Updated by this PR.
- [[09-operations/observability]] — observability strategy spec. Updated by this PR.
- [[13-decisions/0010-single-host-v1]] — the single-host ADR for the musubi service deployment. This ADR doesn't conflict; observability host is a sibling, not a violation of single-host v1.
- `deploy/runbooks/upgrade.md` — applies this change via `update.yml` after merge.
