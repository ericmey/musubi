---
title: Capture
section: 06-ingestion
tags: [capture, hot-path, ingestion, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
implements: ["src/musubi/api/routers/writes_episodic.py", "src/musubi/ingestion/capture.py", "tests/ingestion/test_capture.py"]
---
# Capture

The hot write path. A client calls `POST /v1/episodic` (or `POST /v1/artifacts`) and Core persists the object while the caller waits. Enrichment happens later, in the lifecycle worker.

The normative wire contract is the root `openapi.yaml`; this page explains the behaviour behind it.

## Endpoint

```
POST /v1/episodic
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: <optional, opaque>

{
  "namespace": "alex/claude-code/episodic",
  "content": "CUDA 13.0 driver installed on the build host; reboot required.",
  "summary": "CUDA 13 driver install",
  "tags": ["cuda", "nvidia", "ops"],
  "importance": 7
}
```

## Contract

`CaptureRequest` (`src/musubi/api/routers/writes_episodic.py`) accepts exactly these fields:

| Field | Required | Notes |
|---|---|---|
| `namespace` | yes | `tenant/presence/episodic`. Must be writable by the token's scopes. |
| `content` | yes | Non-empty. At most **32,768 UTF-8 bytes**; larger content is refused with `422 CONTENT_TOO_LARGE` (use the artifact plane). |
| `summary` | no | When present, the summary (not the content) is what gets embedded. |
| `tags` | no | Accepted as given; normalised later by maturation. `kind:` and `staleness:` tags are validated against known values, and `kind:episode` / `staleness:episodic` are added when absent. |
| `importance` | no | 1–10, default 5. Maturation may re-score it. |
| `created_at` | no | Timezone-aware timestamp for migration/replay. Requires **operator scope** (403 otherwise) and may not be in the future. |

Unknown fields are ignored (the model does not forbid extras). There is no `topics`, `content_type`, `capture_source`, `source_ref` or `ingestion_metadata` field on episodic capture. Core sets `object_id`, `state`, `version` and the server timestamps.

## Response

```
202 Accepted
{
  "object_id": "2W1eP3rZaLlQ4jTuYz0Q9CkZAB1",
  "state": "provisional",
  "dedup": null
}
```

The status is always 202. On a dedup merge the response carries the **existing** row's `object_id`; the `dedup` field exists on the model but the route never populates it, so a caller cannot tell a merge from a fresh insert from the response alone.

## Hot-path steps

```
 1. authenticate + authorize the body namespace (write access)
 2. reject oversize content (32,768 UTF-8 bytes)
 3. idempotency lookup (if Idempotency-Key is present)
 4. embed summary-or-content (dense + sparse via TEI)
 5. dedup probe: dense cosine >= 0.92 within the namespace
    ├─ hit + factually compatible: reinforce the existing row
    └─ miss or incompatible:       insert a new provisional row
 6. return 202
```

"Factually compatible" means the NFKC/case/whitespace/terminal-punctuation normalised content is equal and the complete participants set is equal. A paraphrase, correction or negation is not enough to merge.

No latency budget is enforced by tests; the benchmark bullets below are skipped in the unit suite.

## Step detail

### Embedding

Dense and sparse vectors come from TEI (see [[06-ingestion/embedding-strategy]]). The vector is required to write the point, so an embedding failure fails the request; it is not deferred.

### Dedup

`EpisodicPlane.create` (`src/musubi/planes/episodic/plane.py`) probes for the nearest neighbour in the same namespace with a default threshold of **0.92**. A hit merges only when strict factual compatibility also passes. For a compatible hit:

- Tags merge (set union).
- Content follows `longer-wins`: the strictly longer of existing/new content is kept, so more detail wins. This applies only after compatibility has authorised the merge.
- `updated_at`, `updated_epoch` and `version` are bumped.
- `reinforcement_count` is incremented.
- The existing `object_id` is returned.

Curated memory does not dedup by vector; it is keyed by `vault_path` and owned by the file.

### Write

A fresh row is written with `state="provisional"`, `version=1`, `reinforcement_count=0`. Updates to an existing row go through the attributable mutation path rather than a full-point upsert.

## Artifact capture

```
POST /v1/artifacts
Content-Type: multipart/form-data

namespace=alex/shared/artifact
title=Planning session 2026-04-17
content_type=text/vtt
source_system=session-recorder      (optional, default "api-upload")
chunker=markdown-headings-v1        (optional)
file=<binary>
```

The upload is streamed to disk and refused with `413 CONTENT_TOO_LARGE` past `ARTIFACT_MAX_BYTES` (default 100 MiB). The response is `202` with `{object_id, state, size_bytes, sha256}`, where `state` is the **indexing** state (`indexing`, or `failed` when the lifecycle outbox is at capacity). Chunking and embedding run in the lifecycle worker's artifact indexer; poll `GET /v1/artifacts/{id}`. See [[04-data-model/source-artifact]].

## What capture does not do

- **Does not mature.** The row lands `provisional`; see [[06-ingestion/maturation]].
- **Does not synthesize, promote or reflect.** Those are lifecycle jobs.
- **Does not notify.** No Thought is emitted on write.

Keeping write and enrichment separate is what keeps the hot path short.

## Idempotency

Write endpoints accept an optional `Idempotency-Key` header. The key is bound to the authenticated principal and the operation, and is checked **after** authorization. The same key with the same body replays the stored response (marked `X-Idempotent-Replay: true`); the same key with a different body is a `409 CONFLICT`.

The replay cache (`src/musubi/api/idempotency.py`) is **in-memory and process-local** with a 24h TTL. That is why `API_WORKERS` is pinned to 1. An optional durable receipt ledger (`src/musubi/api/idempotency_receipts.py`, SQLite next to the lifecycle database unless `IDEMPOTENCY_RECEIPT_SQLITE_PATH` is set) records completed responses for the routes that require it.

Without a key, dedup still catches most accidental duplicates, but not the "client retried after a timeout, the server had already committed" case. Idempotency keys close that gap.

## Error paths

| Failure | Response |
|---|---|
| Token missing or invalid | 401 |
| Namespace not in scope, or `created_at` without operator scope | 403 |
| Empty content, bad tags, naive or future `created_at` | 422 |
| Content over 32,768 UTF-8 bytes | 422 `CONTENT_TOO_LARGE` |
| Rate limit exceeded (`capture` bucket, 100/min; 10x for operator tokens) | 429 + `Retry-After` |
| Durable idempotency receipt cannot be written | 503 |

Branch on the error `code`, not the HTTP status: ordinary validation errors are also 422.

## Batched capture

`POST /v1/episodic/batch` takes `{namespace, items: [...]}`, where each item has `content`, `summary`, `tags`, `importance` and optional `created_at`:

- Every item is size-checked before any item is written. One oversize item fails the whole batch.
- Any `created_at` override requires operator scope for the whole batch.
- Items are written one at a time through the same `EpisodicPlane.create` path, so each gets dedup.
- The response is `202 {"object_ids": [...]}`, in input order.
- The route uses the `batch-write` rate-limit bucket (50/min). The request model sets no item-count cap.

## Test Contract

**Module under test:** `src/musubi/api/routers/writes_episodic.py`, `src/musubi/planes/episodic/plane.py`, `src/musubi/ingestion/capture.py`

`tests/ingestion/test_capture.py` exercises `CaptureService` (`src/musubi/ingestion/capture.py`), a service layer with its own per-token idempotency cache, bounded retry and lifecycle-event emission. **The HTTP capture route does not call `CaptureService`**; it calls `EpisodicPlane.create` directly. Route behaviour is covered in `tests/api/test_api_v0_write.py`, `tests/api/test_idem*.py` and `tests/api/test_rate_limits.py`.

Happy path:

1. `test_capture_returns_202_and_object_id`
2. `test_capture_writes_provisional_state`
3. `test_capture_writes_both_vectors`
4. `test_capture_sets_timestamps_server_side`
5. `test_capture_emits_lifecycle_event` (skipped)
6. `test_capture_p95_under_250ms_on_100k_corpus` (benchmark, skipped)

Dedup:

7. `test_dedup_merges_on_high_similarity`
8. `test_dedup_increments_reinforcement_count`
9. `test_dedup_merges_tag_union`
10. `test_dedup_keeps_longer_content`
11. `test_dedup_disabled_on_curated`

Idempotency:

12. `test_idempotency_key_returns_same_object_twice`
13. `test_idempotency_key_expires_after_24h`
14. `test_idempotency_key_scoped_per_token`

Errors:

15. `test_capture_empty_content_returns_400`
16. `test_capture_forbidden_namespace_returns_403` (skipped here; covered at the HTTP layer)
17. `test_capture_tei_down_returns_503`
18. `test_capture_qdrant_retry_logic_succeeds_on_transient_failure`
19. `test_capture_qdrant_permanent_failure_returns_503`

Batch:

20. `test_batch_capture_single_tei_embed_call`
21. `test_batch_capture_single_qdrant_upsert`
22. `test_batch_capture_100_items_under_1s` (benchmark, skipped)

Factual-compatibility dedup (in `tests/planes/test_episodic.py`):

23. `test_semantic_dedup_merges_exact_duplicate`
24. `test_semantic_dedup_merges_normalized_duplicate`
25. `test_semantic_dedup_rejects_correction`
26. `test_semantic_dedup_rejects_negation`
27. `test_semantic_dedup_rejects_participant_change`
28. `test_semantic_dedup_rejects_time_change`
29. `test_semantic_dedup_rejects_conflicting_numbers`
30. `test_semantic_dedup_rejects_ambiguity`
31. `test_semantic_dedup_rejects_language_token_punctuation`
32. `test_semantic_dedup_compares_content_not_summary`
33. `test_semantic_dedup_rejects_paraphrase`
34. `test_semantic_dedup_rejects_participants_change`
