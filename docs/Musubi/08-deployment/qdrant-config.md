---
title: Qdrant Config
section: 08-deployment
tags: [deployment, qdrant, section/deployment, status/complete, type/spec, vector-db]
type: spec
status: complete
updated: 2026-10-01
up: "[[08-deployment/index]]"
reviewed: false
implements: "src/musubi/store/"
---
# Qdrant Config

How the Qdrant container is configured: container, collections, quantization, HNSW,
snapshots.

## Version

Qdrant **1.17.1**, pinned by tag and digest in the root `docker-compose.yml` (per
[[13-decisions/0023-qdrant-version-bump-to-1-17]]). Features Musubi depends on:

- Named vectors per collection.
- Server-side fusion (RRF) for hybrid dense + sparse retrieval.
- INT8 scalar quantization on dense vectors.
- Snapshots (per collection and full).

## Container

```yaml
qdrant:
  image: qdrant/qdrant:v1.17.1@sha256:…
  environment:
    QDRANT__SERVICE__API_KEY: ${QDRANT_API_KEY:?Set QDRANT_API_KEY in .env}
  volumes:
    - qdrant-storage:/qdrant/storage
    - qdrant-snapshots:/qdrant/snapshots
  healthcheck:
    test: ["CMD", "bash", "-c", "exec 6<>/dev/tcp/localhost/6333"]
  restart: unless-stopped
```

- **No host ports.** Qdrant is reachable only on the Compose network, as `qdrant:6333`.
- **No config file mount.** Everything not set by environment uses Qdrant's defaults.
- **Health check** opens a TCP connection to port 6333, because the image has no `curl`.
- **API key** is set from `QDRANT_API_KEY` and enforced on every request.

## Collections

Created at Core startup if missing, from the registry in `src/musubi/store/specs.py`
(created by `src/musubi/store/collections.py`; payload indexes by
`src/musubi/store/indexes.py`). Existing collections are never modified.

| Collection | Dense `dense_bge_m3_v1` (1024, cosine) | Sparse `sparse_splade_v1` |
|---|---|---|
| `musubi_episodic` | yes | yes |
| `musubi_curated` | yes | yes |
| `musubi_concept` | yes | yes |
| `musubi_artifact_chunks` | yes | yes |
| `musubi_artifact` (artifact metadata) | yes | no |
| `musubi_thought` | yes | yes |
| `musubi_lifecycle_events` (audit-log mirror) | yes | no |

Every collection gets the universal payload indexes: `namespace`, `identity_family`,
`object_id`, `state`, `schema_version`, `tags`, `topics`, `created_epoch`,
`updated_epoch`, `importance`, `version`. Each also gets its own delta indexes (for
example `vault_path` and `body_hash` on curated, `artifact_id` and `chunk_index` on
chunks, `from_presence` / `to_presence` / `channel` / `read` on thoughts). The full list
is in `src/musubi/store/specs.py`; the schema is in [[04-data-model/qdrant-layout]].

## Quantization

Every dense vector uses **INT8 scalar quantization** (`quantile=0.99`,
`always_ram=true`): the quantized copy stays in RAM, original vectors are stored in
memory too (`on_disk=false`). Sparse vectors are not quantized; their index is kept in
RAM with `full_scan_threshold=5000`.

## HNSW params

Set per dense vector at collection creation: **`m=32`, `ef_construct=256`**. Core does
not set a query-time `hnsw_ef`, so Qdrant's default applies. Changing these values for
an existing collection needs a rebuild; `ensure_collections` will not alter it.

## Storage

Qdrant's data lives in the `qdrant-storage` volume (`/qdrant/storage` in the container)
and its snapshots in the separate `qdrant-snapshots` volume (`/qdrant/snapshots`). Both
are part of the cold backup set; see [[09-operations/backup-restore]].

## Snapshots

The stack does not schedule snapshots. Qdrant's port is not published, so call the
snapshot API from inside the Compose network, for example through the Core container
(which has `curl` and `QDRANT_API_KEY`):

```bash
# One collection
docker compose exec core sh -c \
  'curl -fsS -X POST -H "api-key: $QDRANT_API_KEY" http://qdrant:6333/collections/musubi_episodic/snapshots'

# Full storage snapshot
docker compose exec core sh -c \
  'curl -fsS -X POST -H "api-key: $QDRANT_API_KEY" http://qdrant:6333/snapshots'
```

Snapshots are written under `/qdrant/snapshots` (the `qdrant-snapshots` volume). A
snapshot alone does not cover the vault, artifact blobs or lifecycle state, so it is no
substitute for the cold six-volume backup. It is useful before an upgrade, or to restore
one collection. Restore steps: [[09-operations/backup-restore]].

## WAL and durability

Qdrant uses its default WAL settings. On an unclean shutdown it replays the WAL at
startup. Captures carry idempotency keys, so a client can safely retry a write that may
not have landed.

## Auth

Qdrant runs with an API key even though it is reachable only on the Compose network.
Core reads the same value from `QDRANT_API_KEY` and talks to Qdrant over plain HTTP
inside that network (`MUSUBI_ALLOW_PLAINTEXT=true` in the stack).

## Upgrades

The Qdrant pin moves with Musubi releases. Upgrade Qdrant by upgrading Musubi (see
`docs/guide/operate.md`), and take a cold backup first. For a minor-version jump, read
Qdrant's release notes for storage migrations before you start.

## Observability

Qdrant serves its own Prometheus metrics at `/metrics` on the Compose network (send the
API key). The stack does not scrape it. Core's view of Qdrant is the `qdrant` component
in `GET /v1/ops/status`.

## Failure recovery

| Failure | Recovery |
|---|---|
| Container crash | Compose restarts it; WAL replays on boot. Core's `/v1/ops/status` shows `qdrant` unhealthy meanwhile. |
| Corrupt collection | Restore that collection from a snapshot, or restore the whole cold backup set. |
| Disk full | Writes fail. Free space or grow the volume's filesystem. |

## Test Contract

**Module under test:** `src/musubi/store/specs.py`, `src/musubi/store/collections.py`
and the `qdrant` service in `docker-compose.yml`.

1. `test_public_compose_has_real_matching_core_pins` (`tests/ops/test_public_compose.py`)
   — Qdrant pinned to `v1.17.1` by digest.
2. `test_public_compose_remote_mode_is_host_independent` (same file) — Qdrant publishes
   no port and has a health check.

3. `tests/store/test_specs.py` — registry coverage, dense size, sparse opt-in per
   collection, HNSW `m`, INT8 quantization default, universal and delta indexes.
4. `tests/store/test_collections.py` — first boot creates every collection, later boots
   create nothing, quantization and sparse config as declared, `m=32`.
5. `tests/store/test_indexes.py` — every declared payload index is created.

Not covered by any test: snapshot round-trip.
