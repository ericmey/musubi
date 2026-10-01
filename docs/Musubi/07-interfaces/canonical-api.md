---
title: Canonical API
section: 07-interfaces
tags: [api, http, interfaces, section/interfaces, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[07-interfaces/index]]"
reviewed: false
implements: ["src/musubi/api/", "src/musubi/api/app.py", "src/musubi/api/bootstrap.py", "src/musubi/api/dependencies.py", "src/musubi/api/events.py", "src/musubi/api/routers/thoughts.py", "src/musubi/api/routers/writes_thoughts.py", "src/musubi/api/routers/context.py", "tests/api/", "tests/api/test_bootstrap.py", "tests/api/test_thoughts_stream.py", "tests/api/test_context.py"]
---
# Canonical API

The authoritative interface to Musubi Core. Everything else (the SDK, the MCP adapter, client integrations, the `musubi` CLI) calls it.

**The normative contract is the committed `openapi.yaml` at the repository root.** FastAPI generates the same document at runtime and serves it at `GET /v1/openapi.json` (interactive docs at `/v1/docs`). `tests/api/test_api_v0_read.py::test_runtime_openapi_matches_committed_paths` fails if the committed paths and the runtime paths diverge. This page explains the shape and the rules behind it; where the two disagree, `openapi.yaml` wins.

HTTP/JSON is the only wire protocol. There is no gRPC service. A `MUSUBI_GRPC` setting exists but nothing reads it, and the `.proto` file under `docs/Musubi/proto/` is a design sketch. (Not implemented.)

## Base URL

```
http://127.0.0.1:8100/v1/
```

Core listens on `BRAIN_PORT` (default 8100); the Compose stack binds it to `127.0.0.1` by default. Anything beyond the local host should go through an operator-provided TLS reverse proxy. `MUSUBI_ALLOW_PLAINTEXT=true` only permits non-TLS calls from Core to its own backends (Qdrant, TEI, the LLM endpoint) and is meant for development.

## Auth

```
Authorization: Bearer <jwt>
```

Tokens are JWTs (HS256 with `JWT_SIGNING_KEY`, or RS256), validated against the configured issuer (`OAUTH_AUTHORITY`) and audience. A token's `sub` must equal its `presence` (for example `"sub": "alex/voice"`, `"presence": "alex/voice"`). Each entry in the token's scope list is either a namespace glob with an access suffix or the `operator` meta scope:

- `alex/voice/episodic:rw`: read and write that one namespace.
- `alex/*/episodic:r`: read Alex's episodic memory across every presence.
- `alex/shared/curated:r`: read the shared curated namespace.
- `**:r`: read every namespace at any depth. `**` is special-cased; it is never granted write (`**:rw` is treated as read-only).
- `operator`: the meta scope for operator endpoints.

`*` matches exactly one segment, and a glob must have the **same number of segments** as the namespace it is checked against (`src/musubi/auth/scopes.py`). See [[10-security/auth]] for token issuance.

### Scope by endpoint

| Endpoint | Namespace source | Segments | Required access |
|---|---|---|---|
| `POST /v1/episodic`, `/v1/episodic/batch`, `GET/PATCH/DELETE /v1/episodic/{id}`, `GET /v1/episodic` | body or query | 3 (`<tenant>/<presence>/episodic`) | `r` or `w` |
| `POST /v1/episodic/{id}/retract` | body; the sibling artifact namespace is derived | 3, plus the derived artifact namespace | `w` on both |
| `POST/GET/PATCH/DELETE /v1/curated[/{id}]` | body or query | 3 (`<tenant>/<presence>/curated`) | `r` or `w` |
| `GET /v1/concepts[/{id}]`, `DELETE /v1/concepts/{id}`, `POST /v1/concepts/{id}/reinforce` | query | 3 (`<tenant>/<presence>/concept`) | `r` or `w` |
| `POST /v1/concepts/{id}/promote`, `/reject` | query | n/a | `operator` |
| `POST/GET /v1/artifacts`, `GET /v1/artifacts/{id}[/blob\|/chunks]`, `POST /v1/artifacts/{id}/archive` | form or query | 3 (`<tenant>/<presence>/artifact`) | `r` or `w` |
| `POST /v1/artifacts/{id}/purge` | query | 3 | `w` **and** `operator` |
| `POST /v1/retrieve`, `/v1/retrieve/stream`, `/v1/context` with a **3-segment** namespace | body | 3 | `r` on that namespace |
| same, with a **2-segment** namespace and `planes` | body | 2, then each `<namespace>/<plane>` | `r` on the 2-segment base **and** on every expanded target |
| same, with **no** namespace | body | all authorized namespaces in the caller's identity family, minus exclusions | `r` per namespace |
| `POST /v1/thoughts/send`, `/v1/thoughts/read` | body `namespace` | 3 (`<tenant>/<presence>/thought`) | `w` (`/read` marks thoughts read) |
| `POST /v1/thoughts/check`, `/v1/thoughts/history` | body `namespace` | 3 | `r` |
| `GET /v1/thoughts/stream` | query `namespace` | as given (usually 3) | `r` |
| `POST /v1/lifecycle/transition`, `GET /v1/lifecycle/events[/{id}]` | n/a | n/a | `operator` |
| `GET /v1/contradictions` | optional query `namespace` | 3 when given | `r` on it; `operator` when omitted |
| `GET /v1/namespaces` | n/a | n/a | any valid token (lists the token's own scopes; `operator` gets `["*"]`) |
| `GET /v1/namespaces/{ns}/stats` | path | as given | `r` |
| `POST /v1/ops/debug/trigger-synthesis` | n/a | n/a | `operator` |
| `GET /v1/ops/health`, `/status`, `/metrics` | n/a | n/a | none (network-protected; see Ops) |

**Common mistake:** a token with only `<tenant>/<presence>/*:rw` covers every 3-segment call but gets `403` on `POST /v1/retrieve` with a 2-segment namespace. A presence token that uses cross-plane recall also needs `<tenant>/<presence>:r`.

### Recommended scope set for a per-presence token

```
alex/voice:r              # 2-segment: cross-plane retrieve/context
alex/voice/*:rw           # 3-segment: capture, thoughts, plane-specific retrieve,
                          #            GET/PATCH/DELETE across the presence's planes
alex/shared/curated:r     # read shared curated knowledge
alex/shared/concept:r     # read shared concepts (synthesis writes to <tenant>/shared/concept)
```

Notes:

- There is no separate thoughts scope. Every thoughts endpoint checks the 3-segment `<tenant>/<presence>/thought` namespace and is covered by `<tenant>/<presence>/*:rw`.
- `alex/shared/curated:r` and `alex/shared/concept:r` are the cross-presence read scopes. Omit them if the presence should see only its own output.
- An Admin token for operator endpoints carries `operator`, plus whatever namespace scopes it needs for ordinary reads and writes.

## Content types

- Requests and responses: `application/json`.
- Artifact upload: `multipart/form-data` (`POST /v1/artifacts` only).
- `POST /v1/retrieve/stream`: newline-delimited JSON (`application/x-ndjson`).
- `GET /v1/thoughts/stream`: server-sent events (`text/event-stream`).

## Endpoints

The full list, with request and response schemas, is in `openapi.yaml`. Summary by group:

### 1. Episodic memory

```
POST   /v1/episodic                          # capture (202)
POST   /v1/episodic/batch                    # batch capture (202)
GET    /v1/episodic                          # list (cursor-paginated)
GET    /v1/episodic/{id}                     # fetch one
PATCH  /v1/episodic/{id}                     # tags / importance / summary / content (non-re-embedding)
POST   /v1/episodic/{id}/retract             # escrow-first falsehood retraction (ADR 0042)
DELETE /v1/episodic/{id}                     # soft delete (state=archived)
```

See [[06-ingestion/capture]] for capture semantics. The capture body is `{namespace, content, summary?, tags?, importance?, created_at?}`; `created_at` requires operator scope. Episodic `content` is limited to **32,768 UTF-8 bytes**: exactly 32,768 bytes proceeds, and 32,769 returns `422` with `error.code="CONTENT_TOO_LARGE"`. The check runs after namespace write authorization and before idempotency acquisition or any plane write. Clients branch on the code, never on 422 alone. Batch capture checks every item first: if any item is oversized, no item runs. Batch is not eligible for durable completed-response receipts.

PATCH is namespace-bound, layout-aware, version-fenced and non-re-embedding. `state`, `version`, `object_id` and `namespace` are refused. Tags use replacement semantics and importance is replaced. On a legacy row, `content` and `summary` keep their existing non-re-embedding behaviour. On a v2 anchor, both are embedding-projection fields backed by the immutable content snapshot, so either returns `409 CONFLICT`; tags and importance stay writable on the anchor. A same-version loser also gets 409 rather than a silent retry against a newer observation.

The retraction route implements ADR 0042. Its JSON body carries `namespace`, `expected_version`, `on`, `because`, `truth`, optional `summary` and replacement `tags`. Artifact identity, original-byte accounting, tombstone structure, vector basis and retraction evidence are server-owned. The route requires `Idempotency-Key` and always uses durable completed-response receipts. The server authorizes both the episodic namespace and its derived sibling artifact namespace before touching storage. It then:

1. validates the current row;
2. builds and pre-validates a bounded tombstone;
3. durably escrows the exact original bytes as `stored_unindexed`;
4. applies the supplied version fence;
5. performs one evidence-gated, non-re-embedding mutation.

An escrow failure leaves the episodic physical layout unchanged. A stale version after escrow returns 409 and keeps the verified escrow for an exact retry. V2 retraction changes only the anchor and keeps the immutable original content point and vectors; legacy retraction keeps its existing vector. The 2xx response returns `object_id`, the new `version`, the whole-artifact reference and strict `retraction_evidence`. Exact terminal retries replay the stored response bytes. A tombstone that landed before the receipt committed is adopted only when its principal-bound operation identity, request digest, episodic bindings and exact escrow all verify. A different attempt cannot overwrite the first evidence or start a retraction chain.

### 2. Curated knowledge

```
POST   /v1/curated                 # create a curated row (vault_path + body_hash required)
GET    /v1/curated                 # list (cursor-paginated)
GET    /v1/curated/{id}            # fetch one
PATCH  /v1/curated/{id}            # tags / importance / topics only
DELETE /v1/curated/{id}            # soft delete: state=archived (the vault file is not touched)
```

Curated rows normally come from the vault (see [[06-ingestion/vault-sync]]) or from promotion. `POST /v1/curated` indexes a row; it does not write a vault file.

Curated PATCH accepts only `tags`, `importance` and `topics`, all with replacement semantics. The write is namespace-bound, layout-aware, version-fenced, attributable and non-re-embedding. It targets only the v1 identity row or the v2 anchor. A same-version HTTP loser gets `409 CONFLICT` and is never silently retried against a fresh row; internal plane mutations keep their separate bounded retry contract.

### 3. Concepts

```
GET    /v1/concepts                          # list (cursor-paginated)
GET    /v1/concepts/{id}
POST   /v1/concepts/{id}/reinforce           # explicit reinforcement (rare; usually implicit)
POST   /v1/concepts/{id}/promote             # operator: link to an existing curated row
POST   /v1/concepts/{id}/reject              # operator: record a promotion rejection
DELETE /v1/concepts/{id}                     # soft delete (state=superseded)
```

There is no `POST /v1/concepts` and no `PATCH /v1/concepts/{id}`. Concepts come from synthesis, not from external writes. See [[06-ingestion/promotion#Operator actions]].

### 4. Artifacts

```
POST   /v1/artifacts                         # upload (multipart; 202, indexing runs in the lifecycle worker)
GET    /v1/artifacts                         # list (cursor-paginated)
GET    /v1/artifacts/{id}                    # metadata
GET    /v1/artifacts/{id}/blob               # download bytes
GET    /v1/artifacts/{id}/chunks             # list chunks
POST   /v1/artifacts/{id}/archive            # soft archive
POST   /v1/artifacts/{id}/purge              # operator: hard delete (metadata + blob)
```

There is no single-chunk route.

### 5. Thoughts

```
POST   /v1/thoughts/send                     # {namespace, from_presence, to_presence, content, channel?, importance?}
POST   /v1/thoughts/check                    # {namespace, presence, limit?}: unread for a presence
POST   /v1/thoughts/read                     # {namespace, ids, reader}: mark read
POST   /v1/thoughts/history                  # {namespace, presence, query_text, limit?}: semantic search
GET    /v1/thoughts/stream                   # SSE real-time delivery
```

`namespace` is **required** on send. It is the 3-segment thought namespace of the sender's presence (for example `alex/voice/thought`), and the token needs `w` on it. See [[04-data-model/thoughts]].

#### Thoughts stream (SSE)

Push delivery for consumers that cannot poll cheaply: voice workers, browser extensions, any service that wants cross-presence notifications.

```http
GET /v1/thoughts/stream?namespace=alex/voice/thought&include=voice,all
Accept: text/event-stream
Authorization: Bearer <token>
Last-Event-ID: <optional: KSUID of the last seen thought; triggers replay>
```

- `namespace` (required): the thought namespace to subscribe to. The token needs `r` on it.
- `include` (optional, default `<token presence>,all`): comma-separated `to_presence` values to receive. `all` is a literal `to_presence` value, not a wildcard; a client that narrows `include` without `all` stops receiving broadcasts.

Frames:

```
event: thought
id: 2iVVRLuCjwsSIxfv8KKaZg3NoXc
data: {"object_id":"...","from_presence":"alex/claude-code","to_presence":"voice","namespace":"alex/voice/thought","content":"...","channel":"default","importance":7,...}

event: ping
data: {"at":"2026-04-19T23:14:52.000Z"}

event: close
data: {"reason":"server-shutdown","reconnect_after_ms":5000}
```

- `thought`: one per new matching thought. The SSE `id:` is the thought's `object_id` (27-character base62 KSUID, lexicographically sortable by time).
- `ping`: keepalive every **30 seconds**.
- `close`: graceful-shutdown signal. Error paths just close the connection.

**Replay on reconnect.** `Last-Event-ID: <ksuid>` replays every matching thought with `object_id > <ksuid>` (ascending) before live tail begins. The server scrolls the thoughts plane filtered by `created_epoch >= <ksuid timestamp>`, **exhausts that filtered slice**, then sorts by `object_id` and applies the cap. It must not stop early once `cap + 1` candidates are collected: scroll order is by point id, not KSUID, so an early stop could return later events and skip earlier ones. Reconnect cost therefore scales with the number of matching thoughts since the anchor. There is deliberately no scan cap, because one would reintroduce event loss.

Replay is capped at **500 events**. If more matched, the response carries `X-Musubi-Replay-Truncated: true`, and the client should backfill with `POST /v1/thoughts/history` rather than silently lose events.

**Fanout is broadcast (normative).** Every subscriber matching the namespace and `include` filter receives every event: two browsers and a voice worker subscribed to the same presence all see the same thoughts. This must not regress to competing-consumer delivery.

**Backpressure.** If a subscriber's in-memory queue holds more than 1,000 events, new events for that connection are dropped and logged. Thoughts are durable in Qdrant, so reconnecting with `Last-Event-ID` recovers them.

**Connection cap.** 100 concurrent SSE subscriptions per API process. Over the cap, the server returns `503` with `Retry-After: 5`.

#### Consumer expectations (any `/thoughts/stream` subscriber)

These are shared contract, not suggestions:

1. **Reconnect with exponential backoff and jitter:** `min(2^n * 1s + rand(0, 1s), 60s)`, reset after 5 minutes of stable connection.
2. **Persist `Last-Event-ID` across restarts** (browser storage, a file, a KV store). Losing it means replaying from the start.
3. **Keep a bounded dedup set** (the last 1,000 `object_id`s or a 1-hour TTL). Replay and live delivery can overlap.
4. **Do not reconnect on 403.** It is a token-scope problem; surface it as "re-authenticate".
5. **Ping-gap timeout:** no frame for 60 s (twice the ping interval) means the connection is dead; close it and reconnect.
6. **Compare ids as strings.** KSUIDs sort lexicographically, not numerically.

### 6. Retrieval

```
POST   /v1/retrieve                          # body = RetrieveQuery
POST   /v1/retrieve/stream                   # same query, NDJSON response
```

One entry point for the retrieval pipeline, with the mode chosen by `mode`. See [[05-retrieval/orchestration]].

Request body (`RetrieveQuery` in `src/musubi/api/routers/retrieve.py`):

| Field | Default | Notes |
|---|---|---|
| `namespace` | omitted | 3-segment, 2-segment (with `planes`) or wildcard. Omit to recall across every authorized namespace in the caller's identity family, minus configured exclusions (ADR 0031, AUTH-001). |
| `query_text` | `""` | Required for `fast`, `deep`, `blended`. |
| `mode` | `fast` | `fast`, `deep`, `blended`, `recent`. |
| `limit` | 10 | |
| `planes` | omitted | Required with a 2-segment namespace. |
| `include_archived` | false | In `fast` mode, adds `demoted`, `archived`, `superseded`. Ignored by `deep` and `blended`. |
| `state_filter` | omitted | Ranked modes default to `matured, promoted`; `recent` defaults to `provisional, matured, promoted`. |
| `since` | omitted | `recent` only: inclusive epoch-seconds floor on `created_epoch`. |
| `tags` | omitted | `recent` only: AND filter. |
| `include_lineage` | true | Strict bool. Set false to skip lineage hydration. |

| `mode` | What it does | Default states |
| --- | --- | --- |
| `fast` | Latency-budgeted hybrid search with recency and reinforcement scoring (whole-call budget `RETRIEVAL_FAST_WHOLE_TIMEOUT_S`, default 0.4 s). | `matured, promoted` |
| `deep` | Hybrid search, cross-encoder rerank and lineage hydration. | `matured, promoted` |
| `blended` | Deep without the reranker. | `matured, promoted` |
| `recent` | Time-ordered scroll, newest first, no ranking. | `provisional, matured, promoted` |

#### Namespace shapes

- **3-segment** (`<tenant>/<presence>/<plane>`): one plane; `planes` is ignored.
- **2-segment** (`<tenant>/<presence>`): cross-plane. Each entry in `planes` expands to `<namespace>/<plane>`, the planes are searched in parallel, and results are merged by score.
- **Wildcard** (`*` in any segment): `alex/*/episodic` reads Alex's episodic memory across every presence; `*/voice/curated` reads every tenant's voice curated. Wildcards expand server-side against live data. **Writes always reject `*`**, so every row keeps its presence-level provenance.

See [ADR-0028](../13-decisions/0028-retrieve-2seg-namespace-crossplane.md) and [ADR-0031](../13-decisions/0031-retrieve-wildcard-namespace.md).

Prefer one 2-segment cross-plane call over client-side fanout. Example:

```http
POST /v1/retrieve
Authorization: Bearer <token with alex/voice:r and per-plane read scope>

{
  "namespace": "alex/voice",
  "query_text": "how do I restart the voice agent",
  "mode": "fast",
  "limit": 5,
  "planes": ["curated", "concept", "episodic"]
}
```

Response (abbreviated):

```json
{
  "mode": "fast",
  "limit": 5,
  "warnings": [],
  "results": [
    {
      "object_id": "2iVV…",
      "namespace": "alex/voice/curated",
      "plane": "curated",
      "title": "Voice agent restart runbook",
      "content": "Restart the agent service on the voice host …",
      "content_truncated": false,
      "content_length": null,
      "state": "matured",
      "importance": 8,
      "score": 0.91,
      "score_kind": "ranked_combined",
      "extra": {
        "score_components": {"relevance": 0.93, "recency": 0.41, "importance": 0.8, "provenance": 1.0, "reinforcement": 0.12},
        "lineage": {}
      }
    }
  ]
}
```

`recent` responses use `score_kind: "created_epoch"`, an empty `score_components`, and an extra `provenance_score` field. The response is a discriminated union on `mode`; there is no pagination cursor on retrieve.

**Scope check:** a 2-segment call needs read access to the base (`alex/voice:r` or broader) **and** to every expanded target. One missing target scope fails the whole request with `403`; partial results would be misleading.

**Degradation:** a degraded `200` lists bounded codes in the body's `warnings` array (for example `reranker_failed`, plus at most one cause code per degraded rerank: `reranker_failed_timeout`, `reranker_failed_request_rejected`, `reranker_failed_unavailable`, `reranker_failed_invalid_response`, `reranker_failed_unexpected_error`). A healthy response carries `[]`. See [[13-decisions/0044-additive-reranker-degradation-causes]].

#### NDJSON streaming

`POST /v1/retrieve/stream` takes the same body and returns one JSON row per line. The result set is fully computed before the first line is sent, so streaming gives clients a line-by-line parser, not lower time-to-first-result. The response headers carry `X-Musubi-Mode`, `X-Musubi-Limit` and `X-Musubi-Warnings` (comma-separated warning codes).

### 7. Context packs

```
POST   /v1/context                           # body = ContextQuery
```

Builds a small, grouped startup context pack from server-side retrieval. Clients send the current task as `query_text`, and Musubi returns a character-capped pack grouped by operational purpose.

```json
{
  "namespace": "alex/claude-code",
  "query_text": "billing service release checklist",
  "mode": "startup",
  "planes": ["episodic", "curated", "concept"],
  "candidate_limit": 30,
  "max_items": 8,
  "max_chars": 1200,
  "include_history": false
}
```

`namespace` is optional, with the same shapes as retrieve. Response:

```json
{
  "mode": "startup",
  "query_text": "...",
  "groups": [
    {
      "title": "Current-Project",
      "items": [
        {
          "object_id": "...",
          "namespace": "alex/claude-code/episodic",
          "plane": "episodic",
          "kind": "project-stance",
          "staleness": "durable",
          "content": "...",
          "evidence_handle": "alex/claude-code/episodic/<object_id>",
          "why_surfaced": "durable project-stance; BM25 lexical match",
          "score": 0.71
        }
      ]
    }
  ],
  "max_chars": 1200,
  "used_chars": 248,
  "suppressed": {"superseded": 1},
  "warnings": []
}
```

Typed metadata uses tags such as `kind:project-stance` and `staleness:durable`. Unknown typed values are rejected on episodic capture or patch. Legacy rows with no `kind:` tag are treated as `episode`. Superseded and history records are suppressed unless `include_history=true`.

`POST /v1/episodic` and `/v1/episodic/batch` fill in missing typed metadata at the API boundary: a missing `kind:*` becomes `kind:episode` and a missing `staleness:*` becomes `staleness:episodic`, while caller-supplied typed tags are kept. The default `kind:episode` is a broad classification, not evidence that the caller chose that kind.

The `musubi context` CLI and the `musubi-context` script call this endpoint. See [[05-retrieval/context-pack]] for the ranking contract.

### 8. Lifecycle

```
POST   /v1/lifecycle/transition              # operator: {object_id, to_state, actor, reason, superseded_by?}
GET    /v1/lifecycle/events                  # operator: list events
GET    /v1/lifecycle/events/{object_id}      # operator: events for one object
```

A transition may answer `202` with a durably pending body when the lifecycle coordinator defers it. There is no reconcile endpoint; the lifecycle worker reconciles continuously ([[06-ingestion/lifecycle-engine]]).

### 9. Contradictions

```
GET    /v1/contradictions                    # list active contradictions
```

Read-only. There is no resolve endpoint; resolve a contradiction by transitioning or editing the concepts involved.

### 10. Idempotency receipts

```
POST   /v1/idempotency/receipts/lookup
POST   /v1/idempotency/receipts/audit
```

See Idempotency below.

### 11. Ops

```
GET    /v1/ops/health                        # liveness; network-protected, no bearer
GET    /v1/ops/status                        # readiness; network-protected, no bearer
GET    /v1/ops/metrics                       # Prometheus; private scrape, no bearer
POST   /v1/ops/debug/trigger-synthesis       # operator: integration-test hook (offline mode only)
```

The three read-only endpoints are protected by the network boundary (loopback bind, firewall or Compose network), not by bearer scope. See [[13-decisions/0038-network-protect-read-only-ops-endpoints]]. The synthesis hook only runs with `simulate_ollama_offline: true` and returns `501` otherwise. There is no reindex endpoint.

### 12. Namespaces

```
GET    /v1/namespaces                        # namespaces named by the token's scopes
GET    /v1/namespaces/{ns}/stats             # counts, sizes, last activity
```

## Errors

Every error uses one envelope:

```json
{
  "error": {
    "code": "FORBIDDEN",
    "detail": "namespace 'alex/other-presence/episodic' not in token scope",
    "hint": "request a token with scope including this namespace"
  }
}
```

| Code | HTTP | Meaning |
|---|---|---|
| `BAD_REQUEST` | 400 or 422 | Validation failed (body validation is 422) |
| `CONTENT_TOO_LARGE` | 422 (episodic), 413 (artifact upload) | Size limit exceeded; no mutation started |
| `UNAUTHORIZED` | 401 | Missing or invalid token |
| `FORBIDDEN` | 403 | Valid token, insufficient scope |
| `NOT_FOUND` | 404 | Unknown object |
| `CONFLICT` | 409 | Version fence lost, or idempotency key reused with a different body or in flight |
| `RATE_LIMITED` | 429 | Bucket exhausted (`Retry-After` set) |
| `BACKEND_UNAVAILABLE` | 503 | Qdrant, TEI, receipt store or another backend unavailable |
| `INTERNAL` | 500 | Unexpected; the detail is redacted, so look up the request id in the logs |

Branch on `error.code`, not on the HTTP status alone.

## Rate limits

Enforced **in Core**, per token and bucket, over a rolling 60-second window (`src/musubi/api/rate_limit.py`, ADR 0027). Only write methods (POST, PATCH, PUT, DELETE) are counted, which includes `POST /v1/retrieve` and `POST /v1/context`. GET requests are not limited.

| Bucket | Per minute | Routes |
|---|---|---|
| `capture` | 100 | writes under `/v1/episodic` (except batch) and `/v1/curated` |
| `batch-write` | 50 | `POST /v1/episodic/batch` |
| `artifact-upload` | 20 | `/v1/artifacts` writes |
| `thought` | 100 | `POST /v1/thoughts/send` |
| `transition` | 50 | `POST /v1/lifecycle/transition` |
| `default` | 200 | everything else, including retrieve and context |

Operator-scoped tokens get 10x. Every limited response carries `X-RateLimit-Limit` and `X-RateLimit-Remaining`; a 429 also carries `Retry-After`. Limits are not configurable through settings, and the counters are in-memory per process.

## Pagination

List endpoints (`GET /v1/episodic`, `/v1/curated`, `/v1/concepts`, `/v1/artifacts`) take `namespace`, `limit` (1–500, default 50) and an opaque `cursor`:

```
GET /v1/episodic?namespace=alex/voice/episodic&limit=50&cursor=<opaque>
```

The response is `{"items": [...], "next_cursor": "<opaque>" | null}`. `null` means the list is exhausted. Treat cursors as opaque.

## Idempotency

`Idempotency-Key: <opaque>` on a write makes it replay-safe for 24 hours. The key is bound to the authenticated principal and the operation, and is checked **after** authorization. The same key with the same body replays the stored response byte-for-byte (`X-Idempotent-Replay: true`). The same key with a different body, or while the first request is still in flight, is `409 CONFLICT`. The replay cache is in-memory and process-local, which is why Core refuses to start with more than one API worker. See [[06-ingestion/capture#Idempotency]].

### Durable completed-response receipts

An external durable client must not assume that a missing POST response means the mutation did not happen. An eligible idempotent capture opts in with `Idempotency-Receipt: durable`. Musubi then commits an authorization-bound receipt and publishes the ordinary replay entry before releasing a successful response. If either step fails, the client gets a typed 503, the process-local lease stays held (fail-closed), and the client recovers through receipt lookup. Ordinary POST replay without that header keeps its 24-hour TTL. The receipt ledger is independent and survives an API restart. Durable mode requires `Idempotency-Key` and is deliberately explicit, so ordinary callers keep the existing key-reuse contract.

Durable mode applies only to single-object episodic and curated capture, plus retraction, which always uses it. Batch capture is rejected before mutation, because a multi-object response cannot satisfy the single-`object_id` receipt contract.

**Lookup.** `POST /v1/idempotency/receipts/lookup` takes the authorized namespace, the `POST` method, an eligible operation id, the idempotency key, and the request digest: exactly 64 ASCII hexadecimal characters, with whitespace rejected rather than normalized. Authentication and namespace authorization run before the receipt store is even resolved. The response status is one of:

- `found`: includes the accepted `object_id`, namespace, operation id, original response status and response-body SHA-256;
- `absent`;
- `conflict`: the key exists with another digest (owning principal only);
- `in_flight`: a live process-local lease exists (owning principal only).

Lookup deliberately requires write authority: it is a recovery operation for the principal that could retry the mutation, not a general namespace read.

**Audit.** `POST /v1/idempotency/receipts/audit` is the separate two-seat confirmation surface. It requires both `operator` scope and read authority on the namespace, before the receipt store is resolved. The caller names one exact target: issuer, subject, presence, method, eligible operation, namespace, key and digest. There is no enumeration, prefix or list form. The response never echoes the raw key. It records `observer_attestation="server_attested"`, the observer's identity and effective scopes, a timestamp, the requested namespace/operation/digest, and an opaque target identity hash. A matching receipt also returns its object id, commit time, response status and response-body SHA-256.

For a cross-principal auditor, an existing identity with the wrong digest collapses to `absent`; only the owning principal may receive `conflict`. That stops a loop over guessable keys from becoming an existence oracle. The audit endpoint confirms committed receipts only and never exposes in-flight leases. Its safety assumes a single trusted operator domain: an operator with namespace read authority may confirm that a named principal captured a named digest at a named time. Do not expose it to a broader trust domain without a new security decision.

Audit `absent` means no committed durable receipt is visible to this caller. It does not mean no operation exists: it also covers non-durable operations and cross-principal digest conflicts. And `absent` is never permission to re-POST after an ambiguous server failure. Durable multi-worker leases and orphaned-operation reconciliation are tracked separately (issue #558); v1 keeps a single API worker.

### Staged operation-evidence boundary (IDEM-004)

Issue #603 and [[13-decisions/0040-durable-operation-evidence-and-legacy-resolution]] define the next recovery contract. The durable operation journal may exist internally before object-side evidence does, but it adds no public lookup states and authorizes no new client behaviour. States such as `reserved`, `rejected` or `orphaned` join this API only in the slice that also lands mutation-coupled evidence and exact reconciliation. Until then, the four statuses above are the complete wire contract, and `absent` stays fail-closed.

Lookup still requires namespace write authority. Read-only second-seat auditing of receipt status remains an open security-policy question; ADR 0040 does not widen receipt visibility.

## Versioning

Path-prefixed (`/v1/…`). Within a major version, changes are additive only: new endpoints, new optional request fields, new optional response fields, new optional enum values. A breaking change needs a new prefix (`/v2/…`), and both versions run side by side through a deprecation window. Changes to `src/musubi/api/` or `openapi.yaml` need an ADR (additive) or a version bump (breaking).

## Observability headers

- `X-Request-Id`: read from the request if present, otherwise generated, and echoed on every response and in logs. Clients should propagate it for cross-system tracing. The SDK sends one per call.
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`: on write responses (see Rate limits).
- `X-Musubi-Warnings`, `X-Musubi-Mode`, `X-Musubi-Limit`: on `POST /v1/retrieve/stream` only. Non-streaming responses carry warnings in the body.
- `X-Musubi-Replay-Truncated`: on `GET /v1/thoughts/stream` when replay hit its cap.
- `X-Idempotent-Replay: true`: on a replayed idempotent response.

## Test Contract

**Module under test:** `src/musubi/api/`, root `openapi.yaml`

Coverage lives in `tests/api/`. There is no `tests/contract/` package. Key files:

Shape and routing:

1. `tests/api/test_api_v0_read.py::test_committed_openapi_yaml_includes_read_paths`
2. `tests/api/test_api_v0_read.py::test_runtime_openapi_matches_committed_paths`
3. `tests/api/test_api_v0_write.py::test_committed_openapi_yaml_includes_write_paths`
4. `tests/api/sec003_route_inventory.py`, `tests/api/test_sec003_namespace_scope.py` (every route's namespace authorization)

Auth:

5. `tests/api/test_auth001_token_scope.py`
6. `tests/api/test_req7_token_identity_invariant.py`
7. `tests/api/test_req8_public_invalid_protected_bearer.py`

Idempotency:

8. `tests/api/test_idempotency_contract.py`, `test_idem001_replay_and_race.py`
9. `tests/api/test_idem003_durable_receipts.py`, `test_idem006_receipt_audit.py`
10. `tests/api/test_req10_single_worker_fail_closed.py`

Rate limits:

11. `tests/api/test_rate_limits.py`

Retrieval and streaming:

12. `tests/api/test_retrieve_ret003_wire.py`, `test_retrieve_wildcards.py`, `test_retrieve_recent.py`
13. `tests/api/test_retrieve_stream.py`, `test_ret007_http_warnings.py`
14. `tests/api/test_thoughts_stream.py`

Context:

15. `tests/api/test_context.py`

A shared cross-adapter contract suite is future work; see [[07-interfaces/contract-tests]].
