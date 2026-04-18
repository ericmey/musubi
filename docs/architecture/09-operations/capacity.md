---
title: Capacity Planning
section: 09-operations
tags: [capacity, operations, scale, section/operations, status/draft, type/runbook]
type: runbook
status: draft
updated: 2026-04-17
up: "[[09-operations/index]]"
reviewed: false
---
# Capacity Planning

What size is Musubi? When does it outgrow one box? What's the signal to look at before it does?

## v1 scope

Household / small-team:

- **Users:** 1-5 humans, 3-10 agent presences.
- **Captures:** 100-5,000 / day.
- **Retrievals:** 500-10,000 / day (voice sessions are retrieval-heavy; coding sessions bursty).
- **Curated docs:** ~500-5,000 long-term.
- **Concepts:** ~500 active at steady state.
- **Artifacts:** ~1,000-10,000 / year.

Comfortably within one box. Not even close to saturating the 10 GB GPU or 32 GB RAM.

## Resource footprint

Measured on a representative workload (5 presences, ~2k captures/day, ~5k retrievals/day):

| Resource | Idle | Typical | Peak |
|---|---|---|---|
| CPU | 5% | 15% | 60% (synthesis batch) |
| RAM | 8 GB | 14 GB | 22 GB |
| VRAM | 8.5 GB | 9.0 GB | 9.5 GB |
| Disk (write) | negligible | few MB/min | 50 MB/min (sync batch) |
| Network (LAN) | < 1 KB/s | 100 KB/s | 5 MB/s (sync) |

## Growth model

Storage growth per year (steady state):

| Store | Rate | Year 1 | Year 3 | Year 5 |
|---|---|---|---|---|
| Episodic (Qdrant) | 2 GB/yr | 2 GB | 6 GB | 10 GB |
| Curated (vault) | 50 MB/yr | 50 MB | 150 MB | 250 MB |
| Concepts (Qdrant) | 100 MB/yr | 100 MB | 300 MB | 500 MB |
| Artifact blobs | 10-30 GB/yr | 20 GB | 60 GB | 100 GB |
| Artifact chunks (Qdrant) | 5 GB/yr | 5 GB | 15 GB | 25 GB |
| sqlite events | 500 MB/yr | 500 MB | 1 GB | 2 GB |
| Snapshots (90d on SATA) | — | ~30 GB held | ~30 GB | ~30 GB |

Year-5 totals: ~180 GB. NVMe is 1 TB. Runway: 15+ years before NVMe pressure.

Artifact blobs dominate growth. The 4 TB SATA absorbs them if NVMe hits headroom.

## Retrieval throughput

Measured:

- **Fast path:** ~150 req/s sustained (GPU encoder is the limit).
- **Deep path:** ~10 req/s sustained (reranker + LLM-none; rerank is batched).

If both paths are loaded heavily at once, fast path is bounded by VRAM contention. We don't get close to this in practice.

## Capture throughput

- Single capture: ~50ms p95 (dense+sparse encode + Qdrant write + dedup probe).
- Sustained: ~50 captures/s.
- Batch: up to 100 captures / POST → ~200/s sustained.

v1 workload: ~2k/day = 0.02/s avg. Orders of magnitude of headroom.

## Scale signals — when to worry

Alert fires when:

- `node_filesystem_avail_bytes{mountpoint="/var/lib/musubi"} / total < 0.25` (75% full).
- `gpu_vram_used_mb > 9500` sustained for 10 min.
- `musubi_retrieve_duration_ms{mode="fast"}` p95 > 500ms sustained.
- Ingest backlog (provisional > 7d) > 1000 items.

Any of these ⇒ time to think about scaling.

## Scaling options (in order of effort)

### 1. Optimize current box

- Tune HNSW params (see [[08-deployment/qdrant-config]]).
- Raise `max_batch_tokens` on TEI if GPU has headroom.
- Enable Qdrant on-disk payload compression (post-1.15 feature).
- Prune old episodic memories past demotion rules.

Typical gain: 2-3x headroom without new hardware.

### 2. Add a second GPU

Install a second card (or upgrade to a 24 GB card). Rehost the LLM on the bigger GPU; encoders stay on the 3080. VRAM pressure goes away.

Cost: ~$1-2k for a 4090 or similar.

### 3. Move LLM off-box

Run Ollama on a second host with a larger GPU. Core calls over LAN. Encoders stay co-located with Qdrant (latency-critical).

Cost: parts of a second box.

### 4. Move Qdrant to a separate host

Qdrant on a big-RAM box, Core + inference on another. Networking ~1ms on LAN.

At this scale, revisit the single-host-only assumption throughout the stack. See [[11-migration/scaling]].

### 5. Cluster Qdrant

Qdrant 1.x supports sharding + replication. Multi-node.

Only needed if we approach 100M+ vectors. Not expected in v1 scope.

## LLM capacity

Qwen2.5-7B Q4 on a 3080 generates ~30-50 tokens/s. That's fine for:

- Synthesis: ~500 tokens per concept candidate × ~50 candidates/day = ~25k tokens/day = ~10 min of LLM time.
- Rendering: ~1200 tokens per promotion × ~5/day = ~6k tokens/day = ~3 min.
- Maturation: optional, batched, flexible.

Total LLM load: ~15 min/day of GPU time. Plenty of headroom.

If we start using LLM in the hot path (not planned), revisit.

## Request rate limits vs capacity

Kong rate limits are coarse (300/min/IP). Core per-token limits from [[07-interfaces/canonical-api#rate-limits]]:

- 100 captures/min/token.
- 500 retrievals/min/token.

At current GPU capacity, we could saturate with ~3 simultaneous clients at full throttle. The rate limits are intentionally lower than physical capacity — they're there to protect against runaway clients, not to manage capacity per se.

## Cost of running

v1 is self-hosted on owned hardware. Marginal cost:

- Electricity: ~$15-30/month for the box.
- Internet: shared with household; negligible increment.
- Backup storage (off-site): optional; $5-10/month if using B2.
- Domain + cert: ~$15/year.

Total: < $50/month. Versus a SaaS equivalent (vector DB + LLM API + storage) at similar throughput: $200-500/month. Economics favor self-hosting here.

## Forecasting

Use the dashboard's growth curves. If disk growth is tracking above projection by a factor of 2 for > 2 weeks → alert → investigate. Common causes:

- Chatty presence capturing too aggressively.
- Chunker output blown up (misconfigured `max_tokens`).
- Artifacts too large (enforce per-upload cap).

## Test contract

**Module under test:** capacity math + thresholds

1. `test_storage_growth_rate_projection_matches_observed` (ongoing)
2. `test_retrieve_p95_stays_under_400ms_at_150rps` (load test)
3. `test_capture_p95_stays_under_300ms_at_50rps` (load test)
4. `test_synthesis_completes_under_1h_on_50_candidates` (perf)
5. `test_gpu_vram_alert_fires_at_9500mb`
