---
title: Scaling
section: 11-migration
tags: [ha, migration, multi-host, scaling, section/migration, status/complete, type/migration-phase]
type: migration-phase
status: complete
updated: 2026-04-17
up: "[[11-migration/index]]"
reviewed: false
---
# Scaling

Beyond one box. What to do when v1 outgrows the dedicated Ubuntu host.

This isn't v1 work — v1 is explicitly single-host. This doc sketches the playbook for when the time comes.

## Signals to watch

From [[09-operations/capacity]]:

- Disk > 75% on `/var/lib/musubi`.
- VRAM > 9.5 GB sustained.
- Fast retrieve p95 > 500ms sustained.
- Ingest backlog > 1000 items provisional-older-than-7d.
- Two-GPU requirement for concurrent LLM + encoders.

Any of these sustained → scaling time.

## Scale dimensions

Musubi has three resource dimensions that can scale independently:

1. **Inference (GPU)** — encoders + LLM.
2. **Vector DB (Qdrant)** — RAM for page cache + disk for storage.
3. **Core (FastAPI)** — CPU + memory for requests, scheduler, watcher.

Rarely do all three max out at once. Scale the bottleneck.

## Step 1: Bigger GPU

Upgrade the 3080 (10 GB) → 4090 (24 GB) or 5090. One box, one GPU.

Pros:

- No distributed complexity.
- Fits all models loose (LLM + encoders + room for concurrent LLM).

Cons:

- Single point of failure still.
- Cost (~$1.5-2k).

Ansible-trivial to swap; no schema changes.

## Step 2: Second GPU

Add a second card; split workload:

- GPU 0: encoders (dense, sparse, reranker).
- GPU 1: LLM.

```yaml
# compose
ollama:
  environment:
    - CUDA_VISIBLE_DEVICES=1
tei-dense:
  environment:
    - CUDA_VISIBLE_DEVICES=0
```

Still one host; stack unchanged. Twice the VRAM.

## Step 3: Off-box LLM

Move Ollama to a second host with a larger GPU. Core calls over LAN.

Changes:

- `.env`: `OLLAMA_URL=http://musubi-llm:11434`.
- Add TLS (see [[10-security/data-handling#data-in-transit]]).
- Monitor cross-host latency; usually < 1ms LAN.

Pros: unblocks LLM-heavy workloads (longer contexts, bigger models).

Cons: another host to operate.

## Step 4: Off-box Qdrant

Qdrant on a separate host with more RAM (say 128 GB). Core calls over gRPC.

Rarely needed below ~10M vectors. Vector size + quantization keeps memory modest.

## Step 5: Qdrant cluster

Qdrant 1.x supports sharding + replication across nodes. Enable by moving from single-node config to cluster mode.

At our scale (< 50M vectors) a 3-node cluster (2 shards + 1 replica each) is sufficient. Operational cost is meaningfully higher.

Only go here if sustained workload exceeds a single large-RAM node.

## Step 6: Multi-Core

More than one Core process, typically:

- Multiple FastAPI workers behind Kong (load balance).
- One Lifecycle Worker (singleton via the file-lock pattern; doesn't scale horizontally by design).
- One Vault Watcher (single-writer discipline).

The singletons must remain singletons — don't horizontally scale them without reworking the write-log + lifecycle-event ordering guarantees.

## High availability

v1 has no HA. Single-box loss = outage until restore. Post-v1 options:

### Warm standby

Second box mirrors the first:

- Qdrant: snapshot-restore nightly; or use Qdrant cluster replication.
- Vault: git-based replication (clone on the standby; pull nightly).
- Artifact blobs: rsync.
- Sqlite: streaming replication (Litestream or similar).

On primary failure, DNS fails over; reconcile.

RTO: < 1h. RPO: 5min - 1h depending on replication interval.

### Active-active

Not feasible for v1-style design (the lifecycle worker's singleton assumption would need unwinding). Deferred indefinitely.

## Multi-tenant

v1 is single-tenant in practice (one household). Namespace scopes give logical separation, but we don't harden against adversarial co-tenants.

For multi-tenant (household → multi-household, or commercial):

- Separate Qdrant collections per tenant (or per-tenant filter in shared collections).
- Per-tenant rate limits.
- Per-tenant export / delete.
- Per-tenant encryption (tenant-specific keys for restic).
- Per-tenant cost attribution.

Not a small amount of work. Re-evaluate if use case demands it.

## Federation

Across households (e.g., shared team memory with colleagues):

- Each household runs its own Musubi.
- Federation is via selective Thought passing + optional shared curated repo (git-synced across hosts).
- No write-synced shared data plane — too complex for the benefit.

This is a roadmap topic; explored in [[12-roadmap/phased-plan]].

## Horizontal scaling principles

If/when we scale out:

- **Keep canonical stores on one box per tenant.** Replicate, don't shard, at v1+1 scale.
- **Make singletons obvious.** Scheduler, watcher — document who holds the lock.
- **Use the write-log for echo prevention.** Not clocks. Never clocks.
- **Version everything.** Schema, config, model versions. Drift will happen; version makes it legible.

## Cost model

At v1 (one box), marginal cost is electricity (see [[09-operations/capacity#cost-of-running]]).

At v1+1 (second box), double the electricity + second box purchase + more ops time.

At v1+cluster, marginal cost trends toward SaaS. If you find yourself there, reconsider whether the self-hosted thesis still applies.

## Test contract

**Module under test:** the scaling plans (operational)

1. `test_cross_host_musubi_core_to_qdrant_passes_contract_suite` (when scaled)
2. `test_warm_standby_rto_under_1h` (quarterly drill when HA)
3. `test_lifecycle_worker_singleton_enforced_across_hosts` (when scaled)
4. `test_shared_curated_federation_converges` (if/when federation)
