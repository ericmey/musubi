---
title: Capture
section: 06-ingestion
tags: [capture, hot-path, ingestion, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
implements: ["src/musubi/ingestion/capture.py", "tests/ingestion/test_capture.py"]
---
# Capture

The hot write path. An adapter calls `POST /v1/episodic` (or `POST /v1/artifacts`) and we persist the object. This happens while a user (or agent) is waiting — budget is tight.

## Endpoint

```
POST /v1/episodic
Authorization: Bearer <token>
Content-Type: application/json

{
  "namespace": "eric/claude-code/episodic",
  "content": "CUDA 13.0 driver 575 installed on musubi host; reboot required.",
  "tags": ["cuda", "nvidia", "ops"],
  "topics": ["infrastructure/gpu"],
  "importance": 7,
  "content_type": "observation",
  "capture_source": "claude-code-session-log",
  "source_ref": "session:ksuid-....",
  "ingestion_metadata": {...}
}
```

## Contract

| Field | Required | Notes |
|---|---|---|
| `namespace` | ✓ | Must match token scope. |
| `content` | ✓ | 1..16000 chars. |
| `tags` | optional | Normalized at maturation; accepted as-is now. |
| `topics` | optional | Checked against a topic dictionary (warned, not rejected, if unknown). |
| `importance` | optional | Default 5; LLM may re-score at maturation. |
| `content_type` | optional | Default `observation`. |
| `capture_source` | optional | For provenance. |
| `source_ref` | optional | Free-form opaque back-ref. |
| `ingestion_metadata` | optional | dict[str, Any], preserved, not indexed. |

The adapter does not choose the `object_id`, `state`, or `version` — Core sets those. `created_at`, `updated_at` are also server-set.

## Response

```
202 Accepted
{
  "object_id": "2W1eP3rZaLlQ4jTuYz0Q9CkZAB1",
  "namespace": "eric/claude-code/episodic",
  "state": "provisional",
  "version": 1,
  "created_at": "2026-04-17T09:00:00Z",
  "dedup": null
}
```

If a near-duplicate was found (dedup triggered, see below), the response is 200 with `dedup.action: "merged"` and the existing `object_id`.

## Hot-path steps

```
 1. authn + authz                                      ~1ms
 2. pydantic validate                                  ~1ms
 3. embed content (dense + sparse, parallel)           ~40ms warm
 4. dedup probe (cosine ≥ 0.92 within namespace)       ~20ms
   ├─ hit:  update existing (merge tags, refresh)
   └─ miss: upsert new point
 5. upsert to Qdrant                                    ~20ms
 6. emit LifecycleEvent(provisional → created)         <1ms (batched)
 7. return 202
                                                      total: ~80-100ms
```

Budget: p50 ≤ 100ms, p95 ≤ 250ms.

## Step detail

### Step 3 — Embedding

Parallel call to TEI for dense + sparse. See [[06-ingestion/embedding-strategy]]. Failures here can't be deferred — we need the vector to write the point. On TEI down, capture returns 503 with `retry-after: 5`.

### Step 4 — Dedup

Same as POC: query Qdrant for cosine similarity ≥ `DUPLICATE_THRESHOLD` (0.92 default) within the same `namespace`. If hit:

- Merge tags (set union).
- Update content if the new content is strictly longer (more detail wins).
- Bump `updated_at`, `updated_epoch`, `version`.
- Increment `reinforcement_count`.
- Emit LifecycleEvent(dedup-merged).
- Return the existing `object_id`.

Dedup threshold is per-plane tunable:

- `episodic`: 0.92
- `curated`: dedup disabled (humans own the file)
- `artifact_chunks`: 0.98 (very high — near-identical chunks only)

### Step 5 — Upsert

Single Qdrant `upsert` call with the named vectors + full payload. We use `wait=True` so the write is durable before responding. Latency cost: 10–30ms depending on collection size.

### Step 6 — LifecycleEvent

Batched to the event writer. Not blocking the response. See [[04-data-model/lifecycle]].

## Artifact capture

```
POST /v1/artifacts
Content-Type: multipart/form-data
```

```
{
  "namespace": "eric/_shared/artifact",
  "title": "LiveKit session 2026-04-17",
  "content_type": "text/vtt",
  "source_system": "livekit-session",
  "source_ref": "session:abc-123",
  "file": <binary>
}
```

Response is 202 with `artifact_state: "indexing"`; the chunking+embedding run as a background task. Caller polls `GET /v1/artifacts/{id}` for the state transition. See [[04-data-model/source-artifact]] for details.

## What capture does NOT do

- **Does not mature.** The object lands in `provisional` state; maturation happens in the background (see [[06-ingestion/maturation]]).
- **Does not synthesize.** Concepts come from the synthesis job, not from individual writes.
- **Does not promote.** Promotion is LLM-assisted and slow; never on the hot path.
- **Does not reflect.** Reflection is a daily job.
- **Does not notify.** No cross-presence Thought emitted on write (unless explicitly requested via an optional flag).

Clean separation of write vs. enrichment is what keeps the hot path under budget.

## Idempotency

The API accepts an optional `Idempotency-Key` header (UUID). If seen within the last 24h for the same token + namespace, we return the previously-written `object_id` instead of writing a duplicate.

Idempotency keys live in a small sqlite at `/srv/musubi/idempotency.db` with a 24h TTL.

Without idempotency keys, dedup (step 4) catches most accidental duplicates but doesn't help for "client retried after timeout, server committed but client didn't see" — the classical exactly-once problem. Idempotency keys close that gap.

## Error paths

| Failure | Response |
|---|---|
| Token missing/invalid | 401 |
| Namespace not in scope | 403 |
| Content empty / too long | 400 + details |
| TEI down | 503 + retry-after |
| Qdrant upsert fails | 503 + retry-after (preferred: the Core catches and retries 3x with backoff before returning 503) |
| Dedup race (two concurrent same-content writes) | Both get IDs; the maturation sweep consolidates on next pass |

## Batched capture

`POST /v1/episodic/batch` accepts up to 100 items at once:

- Embeds them in a single TEI batch (more efficient).
- Dedups against the index but not against each other (within the batch).
- Upserts in a single Qdrant call.
- Returns 202 with a list of `(object_id, state, dedup)` triples.

Used by Claude Code's session-end "flush these captured observations" flow.

## Test Contract

**Module under test:** `src/musubi/planes/episodic/` (capture entry points) + `src/musubi/api/routers/writes_episodic.py` + `src/musubi/api/routers/episodic.py`

Happy path:

1. `test_capture_returns_202_and_object_id`
2. `test_capture_writes_provisional_state`
3. `test_capture_writes_both_vectors`
4. `test_capture_sets_timestamps_server_side`
5. `test_capture_emits_lifecycle_event`
6. `test_capture_p95_under_250ms_on_100k_corpus` (benchmark)

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
16. `test_capture_forbidden_namespace_returns_403`
17. `test_capture_tei_down_returns_503`
18. `test_capture_qdrant_retry_logic_succeeds_on_transient_failure`
19. `test_capture_qdrant_permanent_failure_returns_503`

Batch:

20. `test_batch_capture_single_tei_embed_call` (instrumented)
21. `test_batch_capture_single_qdrant_upsert` (instrumented)
22. `test_batch_capture_100_items_under_1s` (benchmark)
