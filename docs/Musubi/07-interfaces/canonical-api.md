---
title: Canonical API
section: 07-interfaces
tags: [api, grpc, http, interfaces, section/interfaces, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[07-interfaces/index]]"
reviewed: false
implements: ["src/musubi/api/", "src/musubi/api/app.py", "src/musubi/api/bootstrap.py", "src/musubi/api/dependencies.py", "src/musubi/api/events.py", "src/musubi/api/routers/thoughts.py", "src/musubi/api/routers/writes_thoughts.py", "tests/api/", "tests/api/test_bootstrap.py", "tests/api/test_thoughts_stream.py"]
---
# Canonical API

The authoritative interface to Musubi Core. Everything else — SDK, adapters, CLI — calls this. The spec lives in the `musubi-core` repo under `api/openapi.yaml` (generated from pydantic) and `api/musubi.proto` (for gRPC).

This document describes the **shape** of the API; the generated OpenAPI/proto files are the normative source.

## Base URL

```
https://musubi.example.local.<tld>/v1/
```

HTTPS is mandatory. TLS via Kong reverse-proxy in front of Uvicorn. Local dev uses Kong with a self-signed cert.

For LAN-only deploys where TLS is overkill, explicit opt-out via `MUSUBI_ALLOW_PLAINTEXT=true` — not recommended for anything cross-host.

## Auth

```
Authorization: Bearer <jwt-or-opaque-token>
```

Tokens carry a scope list; each scope is a namespace glob:

- `eric/claude-code/episodic:rw` — read+write to that specific namespace.
- `eric/*/episodic:r` — read any of Eric's episodic (rare; operator scope).
- `eric/_shared/curated:rw` — shared curated.
- `operator` — meta scope for admin endpoints.

See [[10-security/auth]] for token issuance and validation.

### Scope by endpoint

The scope matcher (see [[10-security/auth]] and `src/musubi/auth/scopes.py`) requires **exact segment-count match** between the scope pattern and the endpoint's namespace. A scope like `eric/openclaw/*:rw` matches any 3-segment namespace under `eric/openclaw/` but does **not** match the 2-segment namespace `eric/openclaw`. Endpoints check scope at different segment counts, so a single presence typically needs multiple scope entries:

| Endpoint | Namespace source | Segments | Required access |
|---|---|---|---|
| `POST /v1/episodic`, `/v1/episodic/batch`, `GET/PATCH/DELETE /v1/episodic/{id}` | request body / query | 3 (`<tenant>/<presence>/episodic`) | `r` or `w` |
| `POST/GET/PATCH/DELETE /v1/curated[/{id}]` | request body / query | 3 (`<tenant>/<presence>/curated`) | `r` or `w` |
| `GET/PATCH /v1/concepts/{id}`, `POST /v1/concepts/{id}/reinforce\|promote\|reject` | query | 3 (`<tenant>/<presence>/concept`) | `r` or `w` |
| `POST/GET /v1/artifacts`, `GET /v1/artifacts/{id}/blob\|chunks` | body / query | 3 (`<tenant>/<presence>/artifact`) | `r` or `w` |
| `POST /v1/retrieve` with **3-segment** namespace | body `namespace` | 3 | `r` on that one namespace |
| `POST /v1/retrieve` with **2-segment** namespace + `planes` array | body `namespace` + `planes` | 2 (base) + strict per-plane check on each expansion | `r` on the 2-seg base **and** `r` on every `<namespace>/<plane>` target |
| `POST /v1/thoughts/send` | body (derives `<tenant>/<presence>/thought`) | 3 | `w` |
| `POST /v1/thoughts/check`, `/read`, `/history` | body `namespace` | 3 (`<tenant>/<presence>/thought`) | `r` |
| `GET /v1/thoughts/stream` | query `namespace` | **2** (`<tenant>/<presence>`) | `r` on the 2-segment namespace |
| `POST /v1/lifecycle/*`, `POST /v1/contradictions/resolve`, `POST /v1/ops/reindex` | n/a | operator scope | `operator` |
| `GET /v1/namespaces[/{ns}/stats]` | n/a | operator scope for listing; own-namespace read otherwise | varies |

**Common mistake:** issuing a token with only `<tenant>/<presence>/*:rw` covers every 3-segment endpoint (captures, sends, retrieve with 3-seg namespace) but returns `403` on `GET /v1/thoughts/stream` and on `POST /v1/retrieve` with a 2-segment namespace. A presence-level token typically needs both `<tenant>/<presence>:r` (2-seg reads) and `<tenant>/<presence>/*:rw` (3-seg reads+writes).

### Recommended scope set for a per-presence token

A typical presence token (e.g. one issued to a specific OpenClaw agent or the LiveKit voice worker) carries the following scopes. Tune per deployment, but this is the baseline that covers every common operation without over-granting:

```
eric/aoi:r                 # 2-seg: retrieve (cross-plane), thoughts/stream
eric/aoi/*:rw              # 3-seg wildcard: captures, thoughts/send, plane-specific retrieve,
                           #                 GET by id, PATCH, DELETE across episodic / thought / concept / curated / artifact
eric/_shared/curated:r     # read shared curated (lifecycle-promoted knowledge)
eric/_shared/concept:r     # read shared concepts
```

Notes:

- There is **no separate `thoughts:check:<presence>` scope**. Every thoughts endpoint checks the standard `<tenant>/<presence>/thought` 3-segment namespace — `/check`, `/read`, `/history` need `:r`; `/send` needs `:w`. Both are covered by the `<tenant>/<presence>/*:rw` wildcard.
- `<tenant>/_shared/curated:r` + `<tenant>/_shared/concept:r` are the cross-presence read scopes — required if the presence wants to see knowledge the Lifecycle Worker has promoted from any other presence in the same tenant. Omit them if the presence should only see its own curated/concept output.
- For an **operator** token (admin UI, ops scripts), replace the presence-scoped entries with `operator` — that covers admin endpoints (`/v1/lifecycle/*`, `/v1/contradictions/resolve`, `/v1/ops/reindex`) and implicit broad read.

## Content types

- Request: `application/json` (REST) or protobuf (gRPC).
- Response: same.
- File uploads: `multipart/form-data` (only for `/v1/artifacts`).
- Streaming: newline-delimited JSON (NDJSON) for large result sets (retrieval over 100 items).

## Endpoints

### 1. Episodic memory

```
POST   /v1/episodic                          # capture
POST   /v1/episodic/batch                    # batch capture
GET    /v1/episodic/{id}                     # fetch one
PATCH  /v1/episodic/{id}                     # update tags/importance (limited fields)
DELETE /v1/episodic/{id}                     # soft delete (state=archived)
```

See [[06-ingestion/capture]] for semantics. Common errors: 400 (validation), 401/403, 503 (backend down).

### 2. Curated knowledge

```
POST   /v1/curated                 # rare — usually via vault edit
GET    /v1/curated/{id}?include=body
PATCH  /v1/curated/{id}            # metadata only; body edits via vault
DELETE /v1/curated/{id}            # soft-delete, archives vault file
GET    /v1/curated                 # list with filters
```

Body of a curated doc is normally edited via the vault; POST exists for programmatic creation (e.g., Lifecycle Worker's promotion goes through `curated_create_from_concept` internally — but clients may POST too if they want to create a file through the API).

### 3. Concepts

```
GET    /v1/concepts/{id}
PATCH  /v1/concepts/{id}                     # operator only; mostly for contradiction resolution
POST   /v1/concepts/{id}/reinforce           # explicit reinforcement (rare; usually implicit)
POST   /v1/concepts/{id}/promote             # operator: force promotion
POST   /v1/concepts/{id}/reject              # operator: permanent reject
GET    /v1/concepts                          # list with filters
```

No POST `/v1/concepts` — concepts come from synthesis, not external writes.

### 4. Artifacts

```
POST   /v1/artifacts                         # upload raw file
GET    /v1/artifacts/{id}                    # metadata
GET    /v1/artifacts/{id}/blob               # download bytes
GET    /v1/artifacts/{id}/chunks             # list chunks
GET    /v1/artifacts/{id}/chunks/{chunk_id}  # single chunk
POST   /v1/artifacts/{id}/archive            # soft archive
POST   /v1/artifacts/{id}/purge              # operator: hard delete (blob + chunks + metadata)
GET    /v1/artifacts                         # list with filters
```

### 5. Thoughts

```
POST   /v1/thoughts/send
POST   /v1/thoughts/check                    # unread for a presence
POST   /v1/thoughts/read                     # mark read
POST   /v1/thoughts/history                  # semantic search
GET    /v1/thoughts/stream                   # SSE — real-time thought delivery
```

Shape preserved from POC (see [[04-data-model/thoughts]]), just under the `/v1/thoughts/...` path. Real-time delivery over SSE is the push counterpart to the poll-only `/check` endpoint; see §Thoughts stream below.

#### Thoughts stream (SSE)

Real-time thought delivery for consumers that need push semantics without polling — browser extensions (can't receive webhooks), voice-agent workers (low-latency context updates), any future homelab service subscribing to cross-presence notifications.

**Request:**

```http
GET /v1/thoughts/stream?namespace=eric/openclaw&include=openclaw,all
Accept: text/event-stream
Authorization: Bearer <token>
Last-Event-ID: <optional — KSUID of last seen thought; triggers replay>
```

Query params:
- `namespace` (required) — presence namespace to subscribe against; must match a namespace the token can read.
- `include` (optional, default `<token-presence>,all`) — comma-separated `to_presence` filter applied server-side.

**Auth:** same convention as the other thoughts endpoints — `AuthRequirement(namespace=<ns>, access="r")`. A token that can call `/thoughts/check` for a namespace can stream `/thoughts/stream` for the same namespace. No new scope keyword.

**Response frames:**

```
event: thought
id: 2iVVRLuCjwsSIxfv8KKaZg3NoXc
data: {"object_id":"...","from_presence":"eric/claude-code","to_presence":"openclaw","namespace":"eric/openclaw","content":"...","channel":"default","importance":7,"sent_at":"2026-04-19T23:14:22.104Z"}

event: ping
data: {"at":"2026-04-19T23:14:52.000Z"}

event: close
data: {"reason":"server-shutdown","reconnect_after_ms":5000}
```

Event semantics:
- `thought` — one per new thought matching the subscription filter. The SSE `id:` field IS the thought's `object_id` (27-char base62 KSUID; lex-sortable by time).
- `ping` — keepalive, every **30 seconds**; VPN/proxy-survivable.
- `close` — graceful-shutdown signal; client reconnects after the hinted delay. Error paths just close the TCP connection.

**Replay on reconnect:** `Last-Event-ID: <ksuid>` triggers replay of every thought matching the subscription where `object_id > <ksuid>` (lexicographic, ascending) before entering live-tail mode. Single bounded range query against the thoughts plane — cheap because KSUIDs sort by time.

Replay is capped at **500 events per reconnect** (the window that covers typical disconnect gaps without blowing up the range query). If more events matched, the response carries the header `X-Musubi-Replay-Truncated: true` so the client can backfill the missing span via `POST /v1/thoughts/history` (which supports pagination) rather than silently losing events.

**Fanout semantics — BROADCAST (NORMATIVE).** Two clients subscribed to the same presence receive **the same events**. Example: user has OpenClaw open in two browsers + a LiveKit worker connected for `eric/openclaw` — all three streams see every thought addressed to `openclaw` or `all`. This is intentional and MUST NOT be regressed to competing-consumer round-robin under any "scaling" justification.

**Backpressure:** slow-consumer events drop in-memory for that connection (metered via `thoughts_stream_dropped_events_total{reason="slow_consumer"}`); reconnect + `Last-Event-ID` recovers them because thoughts are durable in Qdrant.

**Connection cap:** 100 concurrent SSE streams per API process (v1.0 single-host scope). Over-cap connections receive `503 Service Unavailable` with `Retry-After: 5`.

#### Consumer expectations (for any `/thoughts/stream` subscriber)

These are shared contract, not implementation suggestions. OpenClaw, LiveKit worker, and any future Python homelab consumer all build to these:

1. **Reconnect with exponential backoff + jitter** on drop. `min(2^n * 1s + rand(0, 1s), 60s)`. Reset after 5 minutes of stable connection. Don't hammer Musubi when it's down.
2. **Persist `Last-Event-ID` across restarts.** OpenClaw uses `chrome.storage.local` / IndexedDB; Python consumers a file or KV. Lose the ID and you replay the entire plane on restart.
3. **Bounded local dedup set** — last 1000 `object_id`s or 1h TTL cache. Skip any event already in the set (replay + in-flight can overlap).
4. **Scope-mismatch handling.** `403 Forbidden` on initial GET is a token-scope problem; do NOT reconnect on 403. Bubble to user as "re-authenticate."
5. **Ping-gap timeout.** No frame in 2× ping interval (60s) → connection dead; close client-side to trigger reconnect. Catches silent half-open TCPs.
6. **Lexicographic ID comparison.** `object_id` is KSUID (27-char base62). String compare, not numeric. `"2iVVRLuCj..." > "2iVVRLuAh..."` is the correct dedup-set insertion order.

### 6. Retrieval

```
POST   /v1/retrieve                          # body = RetrievalQuery
POST   /v1/retrieve/stream                   # NDJSON stream for large K
```

Single entry point for fast and deep path. Mode selected by `query.mode`. See [[05-retrieval/orchestration]].

#### Cross-plane retrieve: one call, not N

The `namespace` field accepts two shapes, distinguished by segment count:

- **3-segment** (`<tenant>/<presence>/<plane>`) — single-plane query. `planes` is ignored; results come from that one plane.
- **2-segment** (`<tenant>/<presence>`) — cross-plane query. Each entry in `planes` is expanded to `<namespace>/<plane>` server-side, the pipeline fans out in parallel, and results are merged by score into a single sorted response.

See [ADR-0028](../13-decisions/0028-retrieve-2seg-namespace-crossplane.md) for the design decision.

**Consumers should prefer 2-segment cross-plane calls** for any multi-plane query (prompt supplements, corpus recall). One HTTP request, server-side scoring merge, strict per-plane scope check. Do not reinvent fanout client-side.

Worked example — pull top-5 matches across curated + concept + episodic for the `eric/openclaw` presence:

```http
POST /v1/retrieve
Content-Type: application/json
Authorization: Bearer <token-with-eric/openclaw:r-and-per-plane-scope>

{
  "namespace": "eric/openclaw",
  "query_text": "how do I restart the livekit agent",
  "mode": "fast",
  "limit": 5,
  "planes": ["curated", "concept", "episodic"]
}
```

Response (abbreviated):

```json
{
  "rows": [
    {
      "object_id": "2iVV…",
      "namespace": "eric/openclaw/curated",
      "plane": "curated",
      "title": "LiveKit Agent Restart Runbook",
      "score": 0.91,
      "snippet": "Restart with `systemctl restart livekit-agent` on the GPU host …",
      "extra": { "topics": ["infrastructure/livekit"] }
    },
    {
      "object_id": "2iVW…",
      "namespace": "eric/openclaw/episodic",
      "plane": "episodic",
      "title": null,
      "score": 0.78,
      "snippet": "Yesterday Aoi restarted the LiveKit agent via the control panel …",
      "extra": { "capture_source": "openclaw-agent-end" }
    }
  ],
  "next_cursor": null
}
```

**Scope check semantics:** the token must carry read access to the **2-segment base** (`eric/openclaw:r` or broader) **and** to every **expanded target** (`eric/openclaw/curated:r`, `eric/openclaw/concept:r`, `eric/openclaw/episodic:r`). Missing any one of the expansion scopes → `403` for the entire request. This is deliberate: partial results would be misleading, and the consumer can see exactly which scope is missing from the error detail.

**For single-plane queries**, pass a 3-segment namespace and omit `planes`:

```json
{
  "namespace": "eric/openclaw/curated",
  "query_text": "...",
  "mode": "fast",
  "limit": 5
}
```

### 7. Lifecycle

```
POST   /v1/lifecycle/transition              # operator: explicit transition
GET    /v1/lifecycle/events                  # list events
GET    /v1/lifecycle/events/{object_id}      # events for a specific object
POST   /v1/lifecycle/reconcile               # operator: trigger reconciler
```

### 8. Contradictions

```
GET    /v1/contradictions                    # list active
POST   /v1/contradictions/resolve            # operator: pick winner, set reason
```

### 9. Ops

```
GET    /v1/ops/health                        # liveness + readiness
GET    /v1/ops/status                        # per-component status
GET    /v1/ops/metrics                       # Prometheus format (internal only; protected)
POST   /v1/ops/reindex                       # operator: full reindex
```

### 10. Namespaces

```
GET    /v1/namespaces                        # list namespaces in scope
GET    /v1/namespaces/{ns}/stats             # counts, sizes, last activity
```

## Request shapes

All request bodies are pydantic models. Examples:

### Capture

```json
POST /v1/episodic
{
  "namespace": "eric/claude-code/episodic",
  "content": "CUDA 13 driver 575 installed; reboot required.",
  "tags": ["cuda", "ops"],
  "topics": ["infrastructure/gpu"],
  "importance": 7,
  "content_type": "observation",
  "capture_source": "claude-code-session",
  "source_ref": "session:ksuid-...",
  "ingestion_metadata": { ... }
}
```

### Retrieve

2-segment cross-plane (see §6 for the full semantics + scope rules):

```json
POST /v1/retrieve
{
  "namespace": "eric/openclaw",
  "query_text": "how do I restart the livekit agent",
  "mode": "fast",
  "limit": 5,
  "planes": ["curated", "concept", "episodic"],
  "filters": {
    "tags_any": null,
    "min_importance": null,
    "since": null
  },
  "include_archived": false
}
```

### Thought send

```json
POST /v1/thoughts/send
{
  "from_presence": "claude-code",
  "to_presence": "livekit-voice",
  "channel": "default",
  "content": "Heads up: I restarted the LiveKit agent at 09:03 UTC.",
  "importance": 5
}
```

## Response shapes

All responses are pydantic models. Errors follow the same shape:

```json
{
  "error": {
    "code": "FORBIDDEN",
    "detail": "namespace 'eric/other-presence/episodic' not in token scope",
    "hint": "request a token with scope including this namespace"
  }
}
```

Common error codes:

| Code | HTTP | Meaning |
|---|---|---|
| `BAD_REQUEST` | 400 | Validation failed |
| `UNAUTHORIZED` | 401 | Missing / invalid token |
| `FORBIDDEN` | 403 | Token valid, scope insufficient |
| `NOT_FOUND` | 404 | Unknown object_id |
| `CONFLICT` | 409 | Version mismatch or idempotency collision |
| `RATE_LIMITED` | 429 | Too many requests |
| `BACKEND_UNAVAILABLE` | 503 | Qdrant / TEI / Ollama down |
| `INTERNAL` | 500 | Unexpected |

## Rate limits

Per-token:

- 100 captures / minute
- 500 retrievals / minute
- 20 artifact uploads / minute
- 50 batch writes / minute

Returned via `X-RateLimit-*` headers. Operator-scoped tokens have 10x limits.

Rate limits live in Kong (simpler) or in the Core (more precise). v1: Kong. Post-v1: move to Core for namespace-scoped limits.

## Pagination

List endpoints use cursor pagination:

```
GET /v1/episodic?namespace=eric/...&limit=50&cursor=<opaque>
```

Response includes `next_cursor` if more results exist. `null` when exhausted.

Cursors are opaque ksuid+epoch packed strings; rotating behind the scenes without breaking clients.

## Idempotency

`Idempotency-Key: <uuid>` header on any POST. 24h TTL. See [[06-ingestion/capture#idempotency]].

## Versioning

Path-prefixed (`/v1/…`). Breaking changes → `/v2/…`. Both run side-by-side for 180 days.

Non-breaking additions (new optional fields, new optional enum values) stay in `/v1/…`.

## gRPC

Generated from the pydantic models via `protoc`. Same endpoints, same shapes, same errors. Used where low latency + streaming matters — primarily the LiveKit adapter.

Endpoint map:

```proto
service Musubi {
  rpc Capture(CaptureRequest) returns (CaptureResponse);
  rpc Retrieve(RetrieveRequest) returns (RetrieveResponse);
  rpc RetrieveStream(RetrieveRequest) returns (stream RetrievalResult);
  rpc ThoughtSend(ThoughtSendRequest) returns (ThoughtSendResponse);
  // ... one rpc per HTTP endpoint
}
```

gRPC is optional in v1 (build flag `MUSUBI_GRPC=true` at container build). Default off for simplicity.

## NDJSON streaming

For `POST /v1/retrieve/stream`, response is newline-delimited JSON:

```
{"object_id": "...", "score": 0.82, ...}
{"object_id": "...", "score": 0.79, ...}
...
```

Client parses line-by-line; useful for large `limit` queries to start showing results before the whole batch lands.

## Observability headers

Requests receive:

- `X-Request-Id` (UUID; echoed in all logs).
- `X-Musubi-Duration-Ms` on responses.
- `X-Musubi-Warnings` (comma-separated codes) on responses with non-fatal issues.

Clients can propagate `X-Request-Id` through their own logs for cross-system tracing.

## Test Contract

**Module under test:** `musubi/api/` + OpenAPI + `tests/contract/`

Shape:

1. `test_openapi_generated_matches_pydantic`
2. `test_all_documented_endpoints_routable`
3. `test_error_shape_consistent_across_endpoints`

Auth:

4. `test_missing_token_returns_401`
5. `test_out_of_scope_returns_403`
6. `test_operator_scope_accesses_admin_endpoints`

Content negotiation:

7. `test_json_default`
8. `test_protobuf_via_grpc_matches_rest_semantics`
9. `test_multipart_upload_for_artifacts`

Idempotency:

10. `test_idempotency_key_roundtrip`
11. `test_idempotency_key_expires_after_24h`

Versioning:

12. `test_v1_path_lives_alongside_v2_when_present`

Rate limits:

13. `test_rate_limit_enforces_token_bucket`
14. `test_rate_limit_operator_scope_10x_limit`

Streaming:

15. `test_ndjson_retrieve_stream_yields_per_result`

Pagination:

16. `test_cursor_roundtrip_exhausts_list`
17. `test_cursor_opaque_to_client`

Contract:

18. `test_contract_suite_runs_end_to_end` (subset of [[07-interfaces/contract-tests]])
