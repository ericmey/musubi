---
title: Qdrant Config
section: 08-deployment
tags: [deployment, qdrant, section/deployment, status/complete, type/spec, vector-db]
type: spec
status: complete
updated: 2026-04-17
up: "[[08-deployment/index]]"
reviewed: false
implements: "src/musubi/store/"
---
# Qdrant Config

How the Qdrant container is configured — storage, collections, quantization, HNSW, snapshots.

## Version

Qdrant **1.15.x** (April 2026 stable). Key features we depend on:

- Named vectors per collection with independent configs.
- Server-side RRF (`FusionQuery`) for hybrid retrieval.
- Built-in INT8 scalar quantization for sparse+dense.
- `batch_update_points` with `SetPayloadOperation`.
- Snapshots (per-collection and full).

## Container

```yaml
qdrant:
  image: qdrant/qdrant:v1.17.1
  ports:
    - "127.0.0.1:6333:6333"   # REST
    - "127.0.0.1:6334:6334"   # gRPC
  volumes:
    - /var/lib/musubi/qdrant:/qdrant/storage
    - /etc/musubi/qdrant-config.yaml:/qdrant/config/production.yaml
  environment:
    - QDRANT__LOG_LEVEL=INFO
    - QDRANT__STORAGE__ON_DISK_PAYLOAD=true
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:6333/healthz"]
    interval: 30s
  restart: unless-stopped
```

## Config file

```yaml
# /etc/musubi/qdrant-config.yaml
service:
  http_port: 6333
  grpc_port: 6334
  enable_cors: false
  api_key: ${QDRANT_API_KEY}        # enforced even on LAN

storage:
  storage_path: /qdrant/storage
  on_disk_payload: true              # payload is mmap'd from disk
  wal:
    wal_capacity_mb: 32
    wal_segments_ahead: 0

  performance:
    max_search_threads: 4             # half the logical cores
    max_optimization_threads: 2
    update_concurrency: 4

  optimizers:
    deleted_threshold: 0.2
    vacuum_min_vector_number: 5000
    default_segment_number: 4

  hnsw_index:
    m: 16
    ef_construct: 128
    full_scan_threshold: 10000
    max_indexing_threads: 2

cluster:
  enabled: false                       # single-node v1
```

## Collections

All collections use **named vectors** so we can add/remove vector models without rebuilding.

### `musubi_episodic`

```python
client.create_collection(
    "musubi_episodic",
    vectors_config={
        "dense_bge_m3_v1": VectorParams(
            size=1024,
            distance=Distance.COSINE,
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(type=ScalarType.INT8, always_ram=False),
            ),
        ),
    },
    sparse_vectors_config={
        "sparse_splade_v1": SparseVectorParams(),
    },
    hnsw_config=HnswConfigDiff(m=16, ef_construct=128),
)
```

Indexes on payload:

- `namespace` (keyword)
- `state` (keyword)
- `importance` (integer, range-queryable)
- `tags` (keyword, multi-valued)
- `topics` (keyword, multi-valued)
- `created_epoch` (integer)
- `updated_epoch` (integer)
- `last_accessed_epoch` (integer)

### `musubi_curated`

- Same named vectors.
- Payload fields include `vault_path`, `title`, `status`, `frontmatter.*` (stored but not all indexed).

### `musubi_concept`

- Same vectors.
- Indexes on `state`, `reinforcement_count`, `attempts`, `created_epoch`.

### `musubi_artifact_chunks`

- Dense vectors only (sparse on chunks is rarely useful; saves index space).
- Payload: `artifact_id`, `chunk_index`, `offset_start`, `offset_end`, `content_type`.
- Indexes on `artifact_id`, `created_epoch`.

### `musubi_thoughts`

- Dense vectors only.
- Payload: `from_presence`, `to_presence`, `channel`, `read`, `read_by`, `created_epoch`.
- Indexes on every filter field (thoughts are filter-heavy).

See [[04-data-model/qdrant-layout]] for the full schema.

## Quantization

All dense vectors use **INT8 scalar quantization**:

- Storage: 4x smaller (3.8 MB per 10k vectors vs 15 MB at FP32).
- Recall loss: < 1% on our eval sets at `ef=256`.
- Query time: modestly faster due to better cache locality.

`always_ram=false` keeps quantized vectors on disk (mmap). With 10k-100k episodic memories + a few thousand curated, the working set fits in page cache easily.

Sparse vectors are not quantized (already compact).

## HNSW params

Defaults (`m=16`, `ef_construct=128`) balanced for our scale:

- `m=16` — good recall/latency tradeoff for < 1M vectors.
- `ef_construct=128` — one-time index build cost; we never rebuild after initial promotion.
- At query time, the SDK sets `params.hnsw_ef=128` for fast path, `256` for deep path.

If scale reaches > 1M vectors per collection, revisit (see [[11-migration/scaling]]).

## Storage layout

```
/var/lib/musubi/qdrant/
  aliases/
  collections/
    musubi_episodic/
    musubi_curated/
    musubi_concept/
    musubi_artifact_chunks/
    musubi_thoughts/
  raft_state.json
  snapshots/               # local snapshots; copied to /mnt/snapshots
```

At v1 scale, expect:

- Episodic: 5-20 GB after a year (5k memories/mo × 8 KB payload + 4 KB quantized vector).
- Curated: ~50 MB (small set, rarely grows fast).
- Concepts: ~200 MB (grows with curated).
- Artifact chunks: 5-30 GB (depends on how much is ingested).
- Thoughts: ~50 MB.

NVMe has 1 TB; multi-year runway.

## Snapshots

Every 6 hours, Ansible cron runs:

```bash
curl -X POST -H "api-key: ${QDRANT_API_KEY}" \
  http://localhost:6333/snapshots
```

This creates a full snapshot in `/var/lib/musubi/qdrant/snapshots/`. The cron then rsyncs to `/mnt/snapshots/qdrant/<ts>/` on the SATA SSD. 90-day retention, oldest pruned.

Per-collection snapshots also available; same pattern.

Recovery: stop Qdrant, wipe `collections/`, copy snapshot back, start Qdrant. See [[09-operations/backup-restore]].

## WAL + durability

WAL capacity 32 MB — plenty for our write rate (a few writes/sec peak). On ungraceful shutdown, Qdrant replays WAL on restart; at most the last ~100ms of writes may be lost, which is why Musubi captures use idempotency keys so callers can retry safely.

## Auth

We run Qdrant with `api_key` set even though it's localhost-only. Defense in depth: if Kong is ever misconfigured and proxies a port it shouldn't, Qdrant refuses.

The key lives in `.env`; Core reads it via `QDRANT_API_KEY`.

## Upgrades

Qdrant patch upgrades (`1.15.0 → 1.15.x`):

1. Trigger a snapshot.
2. `docker compose pull qdrant`.
3. `docker compose up -d qdrant`.
4. Restart Musubi Core (reconnects).

Minor upgrades (`1.15 → 1.16`):

1. Read Qdrant upgrade notes for schema migrations.
2. Snapshot.
3. Bring up 1.16 on a parallel port, run contract suite, compare.
4. Swap if green.

Major upgrades (`1.x → 2.x`): handle like any other breaking dependency — see [[11-migration/index]].

## Observability

Qdrant exposes `/metrics` (Prometheus). We scrape:

- `qdrant_points_count{collection=...}`
- `qdrant_segments_count{collection=...}`
- `qdrant_vector_size_bytes{collection=...}`
- `qdrant_grpc_requests_duration_seconds{endpoint=...}`
- `qdrant_http_requests_duration_seconds{endpoint=...}`

Plus per-collection `optimizer_status` (healthy | indexing | suboptimal).

## Failure recovery

| Failure | Recovery |
|---|---|
| Container crash | Compose auto-restarts; WAL replays on boot. |
| Corrupt segment | Qdrant quarantines and continues; alert fires; restore from snapshot if needed. |
| Disk full | Core switches to read-only; no new writes; ops alert. |
| Network partition (N/A v1) | — |

## Test Contract

**Module under test:** `musubi/collections.py` + this config

1. `test_collections_created_with_named_vectors`
2. `test_payload_indexes_present_on_expected_fields`
3. `test_hnsw_params_match_config`
4. `test_quantization_config_int8_on_dense`
5. `test_snapshot_roundtrip_preserves_point_count` (integration)
6. `test_disk_full_leads_to_readonly_mode_not_crash` (chaos)
7. `test_api_key_enforced_on_localhost`
