---
title: Failure Modes
section: 03-system-design
tags: [architecture, degradation, failure-modes, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[03-system-design/index]]"
reviewed: false
implements: "docs/Musubi/03-system-design/"
---
# Failure Modes

What breaks, what Musubi does about it, and how it degrades. This is the spec; the runbooks in [[09-operations/runbooks]] are the operational response.

> The stack is defined in the root `docker-compose.yml`, with an optional GPU overlay in `deploy/docker/compose.local-gpu.yml`. See [[08-deployment/compose-stack]].

## Classification

Failures fall into three buckets:

- **Data-plane** — Qdrant, vault, artifact blobs. Loss here is loss of memory.
- **Compute-plane** — TEI, Ollama, Core, lifecycle worker. Loss here is loss of service, but data is safe.
- **Edge-plane** — the operator's TLS reverse proxy, network, auth. Loss here is loss of reachability.

Our design principle: degrade **feature-wise** before degrading **correctness-wise**. We'll return fewer / worse results before we return wrong results.

## How degradation is reported

A degraded but successful retrieval returns `200` with a `warnings` list in the response body (`src/musubi/retrieve/warnings.py`). The codes are bounded:

- `sparse_embedding_failed` — the sparse leg failed; results are dense-only.
- `reranker_failed` — the reranker failed; results are unreranked. A cause (`timeout`, `request_rejected`, `unavailable`, `invalid_response`, `unexpected_error`) travels with it.
- `plane_timeout_<plane>` / `plane_error_<plane>` — one plane timed out or failed; the other planes' results are returned.

A healthy response carries `warnings: []`. The streaming endpoint (`POST /v1/retrieve/stream`) carries the same codes in an `X-Musubi-Warnings` response header. There are no other `X-Musubi-*` degradation headers.

Hard failures return the typed error envelope `{"error": {"code", "detail", "hint"}}`.

## Data-plane failures

### Qdrant down

**Detection:** `GET /v1/ops/status` reports `qdrant` unhealthy; the Compose health check on `qdrant` fails.

**Core behavior:**
- Reads and writes fail with an error response.
- No silent acceptance of writes — Core does not buffer writes; if Qdrant is down, the caller is told.

**Adapter behavior:** surface the failure to the user ("I can't access memory right now") and retry with backoff.

**Recovery:** restart the container. Data is durable in the `qdrant-storage` volume. If corrupted: restore from snapshot ([[09-operations/backup-restore]]).

### Qdrant data corruption

**Response:**
1. Operator decides: restore from snapshot (loses recent writes) vs rebuild from canonical sources.
2. For the **curated** collection: rebuild from the vault. The lifecycle worker's `vault_reconcile` job re-syncs the vault into the index.
3. For **artifact** collections: rebuild from the blobs under `ARTIFACT_BLOB_PATH` plus artifact metadata.
4. For **episodic** / **concept** / **thought** collections: these exist only in Qdrant. Restore from the last snapshot. The gap between snapshot and crash is lost data.

This is why episodic memories must be durable in Qdrant snapshots. See [[09-operations/asset-matrix]].

### Vault filesystem unwritable / disk full

**Behavior:** promotion and reflection cannot write vault files, so those jobs fail. Curated reads keep serving from the Qdrant index. All other planes continue to operate.

### Artifact blob storage inaccessible

**Behavior:** artifact uploads and `GET /v1/artifacts/{object_id}/blob` fail. Chunk text is in Qdrant, so chunk retrieval keeps working.

## Compute-plane failures

### Dense TEI down

**Detection:** `GET /v1/ops/status` reports `tei-dense` unhealthy (TEI `/health`).

**Core behavior:**
- Writes: **hard failure** — without an embedding the point would be unqueryable, so Core fails the request and the caller retries.
- Retrieval: fails, except paths that need no query embedding (get by ID, `recent`).

**Recovery:** restart the TEI service or the remote endpoint. Core probes the dense endpoint at startup, with retries, and does not start without it.

### Sparse TEI or reranker down

Retrieval continues with a `sparse_embedding_failed` or `reranker_failed` warning (see above).

### Ollama / lifecycle LLM down

**Detection:** `GET /v1/ops/status` reports `ollama` unhealthy (`/api/tags`).

**Behavior:**
- **No hot-path dependency on the LLM.** All user-facing APIs continue.
- Maturation: importance and topic enrichment for the failed batches is skipped and counted in `musubi_lifecycle_enrichment_batch_failures_total`.
- Synthesis, promotion and reflection make no progress until the LLM is back; they retry on their next scheduled run.

### Core crash

**Detection:** the Compose health check (`/v1/ops/health`) fails.

**Response:**
- `restart: unless-stopped` restarts it.
- The reverse proxy returns an error to clients meanwhile.
- The lifecycle worker keeps running: it imports the same libraries and talks to Qdrant directly, not through Core's HTTP API.

### Lifecycle worker crash

**Detection:** the Compose health check (`musubi_lifecycle_coordinator_ready` on `:8101/metrics`) fails.

**Response:**
- Restart; the next scheduled job window resumes work.
- Pending lifecycle transitions are persisted in the SQLite work store (`LIFECYCLE_SQLITE_PATH`) and re-applied by `lifecycle_reconcile`; see [[06-ingestion/lifecycle-engine]] for the idempotency guarantees.

## Edge-plane failures

### Reverse proxy down or misconfigured

**Response:**
- Clients on the host can still reach Core on `127.0.0.1:8100`.
- Remote clients fail until the operator fixes the proxy.

### Network partition between Core and Qdrant

Corresponds to the Qdrant-down case above from the client's perspective.

### Authentication failure / token expiry

**Response:**
- `401` with the typed error envelope.
- Retrying with the same token cannot recover. Musubi has no token refresh (Core, the SDK and the published adapters all lack it); tokens are credentials the operator issues.
- The operator must supply a valid token. Retry only after the credentials are updated.

## Cross-cutting degradation modes

### Partial plane unavailability

Retrieval across several planes serves the planes it can and reports the rest with `plane_timeout_<plane>` / `plane_error_<plane>` warnings.

### Fast-path response cache

The fast path keeps a 30-second exact-query response cache in the Core process (`src/musubi/retrieve/fast.py`). A cached result is at most 30 seconds old; there is no separate staleness flag.

### Reranker unavailable

Scoring falls back to the hybrid score only, and the response carries `reranker_failed`. Ranking quality drops on hard queries; correctness is unaffected.

## Observability of failures

- Warning codes are counted in Prometheus metrics; see [[09-operations/observability]].
- Alert rules are in [[09-operations/alerts]].
- Runbooks in [[09-operations/runbooks]] list the observable symptoms for each failure.

## Principle: fail loudly to callers, gently to users

Internal logs are verbose. API responses carry warning codes and typed errors. The user-facing message (surfaced by the adapter) is plain: "I'm having trouble remembering right now." We do not dump stack traces into a voice turn.

## Test Contract

This is an architecture-overview spec — no single code path or test file owns it end-to-end. Verification is distributed across the component specs in sections 04–10, each of which carries its own `## Test Contract` section.
