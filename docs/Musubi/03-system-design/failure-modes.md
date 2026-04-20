---
title: Failure Modes
section: 03-system-design
tags: [architecture, degradation, failure-modes, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[03-system-design/index]]"
reviewed: false
implements: "docs/Musubi/03-system-design/"
---
# Failure Modes

What breaks, what Musubi does about it, and how it degrades gracefully. This is the spec; the runbooks in [[09-operations/runbooks]] are the operational response.

## Classification

Failures fall into three buckets:

- **Data-plane** — Qdrant, vault, artifact store. Loss here is loss of memory.
- **Compute-plane** — TEI, Ollama, Core, Worker. Loss here is loss of service, but data is safe.
- **Edge-plane** — Kong, network, auth. Loss here is loss of reachability.

Our design principle: degrade **feature-wise** before degrading **correctness-wise**. We'll return fewer / worse results before we return wrong results.

## Data-plane failures

### Qdrant down

**Detection:** healthcheck at `/readyz` fails; Core's internal `qdrant_healthy` gauge goes 0.

**Core behavior:**
- All write endpoints return `503` with `X-Musubi-Degradation: qdrant-down`.
- All read endpoints return `503` with the same header.
- No silent acceptance of writes — we do not buffer writes; if Qdrant is down, the caller is told.

**Adapter behavior (MCP, LiveKit, etc.):**
- Surfaces the degradation to the user ("I can't access memory right now").
- Retries with exponential backoff for up to 30s.

**Recovery:** typically < 30s for a restart. Data is durable in the Qdrant volume. If corruption: restore from last snapshot ([[09-operations/backup-restore]]).

### Qdrant data corruption

**Detection:** checksum mismatch on read, or `get_collection` reports anomaly.

**Response:**
1. Alarm fires. Core remains healthy for read-only on non-corrupted collections.
2. Operator decides: restore from snapshot (loses recent writes) vs rebuild from canonical sources.
3. For **curated** collection: rebuild from vault via `musubi-cli vault reindex --full`. ~30min.
4. For **artifact** collection: rebuild from `artifacts/` + metadata table. ~1hr per 10k artifacts.
5. For **episodic** / **concept** / **thought** collections: these are the only canonical Qdrant-only assets. Restore from last snapshot. Gap between snapshot and crash is lost data.

This is why episodic memories must be durable in Qdrant snapshots. See [[09-operations/asset-matrix]].

### Vault filesystem unmounted / disk full

**Detection:** watcher `/readyz` fails. Core `vault_writable` gauge goes 0.

**Core behavior:**
- Write endpoints for curated plane return `503 X-Musubi-Degradation: vault-unwritable`.
- Read endpoints for curated plane fall back to **Qdrant-only** mode — they serve from the indexed copy but flag `stale: true` in the response.
- All other planes continue to operate.

### Artifact store inaccessible

**Detection:** object-store ping fails.

**Core behavior:**
- Write endpoints for artifacts return `503`.
- Read endpoints return chunks from Qdrant (which has the text) but omit the blob URL and set `X-Musubi-Degradation: artifact-blobs-unavailable`.

## Compute-plane failures

### TEI down

**Detection:** `/health` fails.

**Core behavior:**
- Write endpoints: **hard failure** — we cannot embed, cannot store. Return `503`.
- Read endpoints: **degradation** — use a cached query embedding if available, otherwise fail. For query-by-ID paths (no embedding needed), continue normally.
- Fast-path cache: continues to serve cached results; new queries fail.

**Why hard failure on writes:** writing without an embedding corrupts the collection (the point becomes unqueryable). Better to 503 and let the caller retry.

**Recovery:** typical < 10s for TEI to reload models. If a model refuses to load (OOM, GPU fault): restart the container; fall back to a Gemini-cloud path per [[13-decisions/0006-pluggable-embeddings]] by flipping a config flag.

### Ollama down

**Detection:** `/api/tags` fails.

**Core behavior:**
- **No hot-path dependency on Ollama.** All user-facing APIs continue.
- Lifecycle Worker: synthesis + reflection jobs skip with a logged warning. They retry next scheduled run.
- Ingestion importance scoring: falls back to a default score of `5` (neutral). A backfill job re-scores on Ollama recovery.

### Core crash

**Detection:** Docker restart loop, healthcheck fails.

**Response:**
- `on-failure:5` restart policy. After 5 crashes in a short window, the container stays stopped.
- Kong returns `503` to clients.
- Lifecycle Worker and Vault Watcher continue (they don't depend on Core's HTTP API; they import the same libs and access Qdrant directly).

### Lifecycle Worker crash

**Detection:** healthcheck fails.

**Response:**
- Restart; next scheduled job window it resumes.
- If a job was mid-flight: job state is persisted in `lifecycle-state.db`; re-runs are idempotent (see [[06-ingestion/lifecycle-engine]] for idempotency guarantees).

### Vault Watcher crash

**Detection:** healthcheck fails.

**Response:**
- Restart.
- On startup: do a hash-based reconcile pass — scan the vault, compare file hashes to the last-indexed hash recorded per file, reindex anything that changed while the watcher was down.

## Edge-plane failures

### Kong down / misconfigured

**Detection:** `:443` unreachable from clients.

**Response:**
- Internal adapter-to-Core traffic continues if adapters are co-located (they usually are).
- External clients (e.g., a LiveKit agent on another host) 503.
- Human operator fixes Kong config; Docker restart.

### Network partition between Core and Qdrant

**Detection:** Qdrant calls start failing inside Core.

**Response:**
- Circuit breaker in Core opens after 5 failures in 10s; fail fast for 30s, then half-open trial.
- Corresponds to the Qdrant-down case above from the client's perspective.

### Authentication failure / token expiry

**Detection:** Core auth middleware rejects.

**Response:**
- `401` with `WWW-Authenticate` header.
- Adapter should refresh token and retry.
- If refresh fails: alert the operator. This is a misconfiguration, not a transient fault.

## Cross-cutting degradation modes

### Partial plane unavailability

The query API accepts a `planes` parameter. If one of the requested planes is unavailable (e.g., curated Qdrant collection corrupted), we serve the planes we can and flag via `X-Musubi-Partial: {plane-name}` response header. The response body's `meta` includes `unavailable_planes: [...]`.

### Stale fast-path cache

If Core can't verify the cache is fresh (e.g., Qdrant is down momentarily), it serves stale results with `X-Musubi-Cache-Stale: true`. LiveKit adapter treats this as acceptable; other adapters may choose to retry.

### Reranker unavailable

If the reranker endpoint fails, scoring falls back to **hybrid score only** (no rerank). Response flags `X-Musubi-Rerank: false`. Correctness-wise fine; quality drops ~10–15% on hard queries.

### Slow Ollama (synthesis backlog)

If synthesis runs are backing up (queue > 1000 pending), Worker starts dropping oldest pending synthesis candidates and logs warnings. The synthesis watermark advances so we don't loop forever. Dropped candidates are still in Qdrant as matured episodic memories; they just didn't get synthesized this pass. Next pass tries again.

## Observability of failures

- Every degradation header is counted in a Prometheus metric.
- Every failure mode has a named alert in [[09-operations/alerts]].
- Every runbook in [[09-operations/runbooks]] begins with "Observable symptoms" listing the metrics + log patterns.

## Principle: fail loudly to callers, silently to users

Internal logs are verbose. Adapter responses carry diagnostic headers. The user-facing message (surfaced by the adapter) is plain: "I'm having trouble remembering right now." We do not dump stack traces into a voice turn.

## Test Contract

This is an architecture-overview spec — no single code path or test file owns it end-to-end. Verification is distributed across the per-component slices listed in the sibling specs under this section, each of which carries its own `## Test Contract` section bound to an owning slice.
