---
title: "Slice: Thoughts stream (SSE)"
slice_id: slice-api-thoughts-stream
section: _slices
type: slice
status: in-progress
owner: gemini-3-1-pro-nyla
phase: "7 Adapters"
tags: [section/slices, status/in-progress, type/slice, api, sse, thoughts, realtime]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-plane-thoughts]]", "[[_slices/slice-api-v0-read]]", "[[_slices/slice-api-v0-write]]"]
blocks: []
---

# Slice: Thoughts stream (SSE)

> Real-time thought delivery over SSE. Unblocks the OpenClaw extension (and every future consumer) from tight polling on `POST /thoughts/check`. Same transport pattern as `/retrieve/stream`; same auth model as existing thoughts endpoints.

**Phase:** 7 Adapters · **Status:** `in-progress` · **Owner:** `gemini-3-1-pro-nyla`

## Why this slice exists (2026-04-19 context)

Aoi (Claude Code on Nyla machine) researched the Musubi API against OpenClaw v0.1's needs tonight. One real gap surfaced: **thoughts are poll-only today.** For OpenClaw's "show inbound thought as unread badge in extension popup" UX to feel live without the extension hammering `/thoughts/check` on a tight interval, Musubi needs a push path.

Webhooks were rejected — browser extensions (OpenClaw) and sandboxed agent runtimes (LiveKit worker, future homelab services) cannot receive webhooks (no public URL, only outbound connections). The only transport that works without an extra broker intermediary is **SSE**, matching the shape already in production at `POST /retrieve/stream`.

This slice ships `GET /v1/thoughts/stream` + the in-process pub-sub broker that feeds it, with design notes pre-validated with Aoi for the consumer-side integration.

## Specs to implement

- [[07-interfaces/canonical-api]] (the spec-update trailer from this PR adds §Thoughts stream + §Consumer expectations — see commit message)

## Owned paths (you MAY write here)

- `src/musubi/api/events.py`                        (new module — in-process pub-sub broker)
- `src/musubi/api/routers/thoughts.py`               (parent done — append GET /v1/thoughts/stream)
- `src/musubi/api/routers/writes_thoughts.py`        (parent done — add broker publish hook on send)
- `tests/api/test_thoughts_stream.py`                (new — SSE unit + integration tests)
- `openapi.yaml`                                     (parent done — extend with new endpoint + schemas)
- `docs/architecture/07-interfaces/canonical-api.md` (spec-update: trailer required on feat commit)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/planes/`              (pass through; do not read Qdrant directly)
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/`
- `src/musubi/ingestion/`
- `src/musubi/types/`
- `src/musubi/sdk/`                 (SDK ergonomic addition for `client.thoughts.stream()` is a cross-slice to slice-sdk-py, not this slice)
- `src/musubi/adapters/`            (consumer integration happens in adapter/external repos, not here)
- `proto/`

## Depends on

- [[_slices/slice-plane-thoughts]]   (done — thoughts storage)
- [[_slices/slice-api-v0-read]]      (done — parent of thoughts.py, share authz + error patterns)
- [[_slices/slice-api-v0-write]]     (done — parent of writes_thoughts.py, publish hook on send)

Start this slice only after every upstream slice has `status: done`. ✓ all met.

## Unblocks

- **OpenClaw extension v0.1 integration** — pushed thoughts surface as unread-badge without polling.
- **LiveKit worker live-thought routing** (future enhancement of adapter-livekit).
- `slice-api-lifecycle-stream` / `slice-api-contradictions-stream` (future siblings that reuse the same broker infrastructure built here).

## Endpoint contract (normative)

### `GET /v1/thoughts/stream`

**Request:**

```http
GET /v1/thoughts/stream?namespace=eric/openclaw&include=openclaw,all
Accept: text/event-stream
Authorization: Bearer <token>
Last-Event-ID: <optional — KSUID of last seen thought; triggers replay>
```

Query params:
- `namespace` (required) — presence namespace to subscribe against; must match a namespace the token can read.
- `include` (optional, default `<token-presence>,all`) — comma-separated list of `to_presence` values to filter server-side. Useful for an extension that wants only thoughts addressed directly to itself + broadcasts.

**Auth:**

Uses the **same convention as all other thoughts endpoints**: `AuthRequirement(namespace=<ns>, access="r")`. No new scope keyword is introduced — scope language stays unified with `POST /thoughts/check`, `POST /thoughts/read`, `POST /thoughts/history`. A token that can poll `/thoughts/check` for a namespace can stream `/thoughts/stream` for the same namespace.

(Note: an earlier design draft considered a capability-style scope like `thoughts:check:<presence>`, but the existing codebase uses namespace-scoped `AuthRequirement` across all thought endpoints. This slice matches convention.)

**Response:**

```http
200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

Event frames:

```
event: thought
id: 2iVVRLuCjwsSIxfv8KKaZg3NoXc       # KSUID of the thought (lex-sortable by time)
data: {"object_id":"...","from_presence":"eric/claude-code","to_presence":"openclaw","namespace":"eric/openclaw","content":"...","channel":"default","importance":7,"sent_at":"2026-04-19T23:14:22.104Z"}

event: ping
data: {"at":"2026-04-19T23:14:52.000Z"}

event: close
data: {"reason":"server-shutdown","reconnect_after_ms":5000}
```

Event type semantics:

- `thought` — one per new thought matching subscription filter. The SSE `id:` field is the thought's `object_id` (a 27-char base62 KSUID). Lex-sortable by time creation.
- `ping` — keepalive, emitted every **30 seconds**. Makes the stream VPN/proxy-survivable.
- `close` — Musubi signals graceful shutdown; client should reconnect after the hinted delay. Never emitted on error paths (those just close the TCP connection with a status code).

**Replay on reconnect:**

If the client sends `Last-Event-ID: <ksuid>`, Musubi replays every thought matching the subscription filter where `object_id > <ksuid>` (lexicographic, ascending) before entering live-tail mode. Uses a single bounded range query against the thoughts plane — cheap because KSUIDs sort by time.

Dedup safety net: if replay overlaps with in-memory events already delivered, the client is responsible for idempotent handling via a bounded local set keyed on `object_id` (see §Consumer expectations).

**Subscriber semantics: BROADCAST.**

Two clients subscribed to the same presence receive **the same events**. Example: user has OpenClaw open in two browsers + a LiveKit worker connected for the same `eric/openclaw` presence — all three streams see every thought addressed to `openclaw` or `all`. This is **intentional and normative** — never accidentally regressed into competing-consumer round-robin semantics. Having your second browser tab stay quiet is a worse failure mode than having two browsers buzz simultaneously.

**Backpressure:**

If a client's outbound TCP buffer backs up (slow consumer, flaky network), Musubi drops in-memory events for that connection rather than blocking the broker. On reconnect, replay via `Last-Event-ID` recovers everything — thoughts are durable in Qdrant; no data loss. The broker tracks per-connection buffer depth; drops are metered into an ops counter (`thoughts_stream_dropped_events_total{reason="slow_consumer"}`).

**Connection cap:**

Bounded pool of **100 concurrent SSE streams per API process** (v1.0 single-host scope). New connections above the cap receive `503 Service Unavailable` with `Retry-After: 5` header. Plenty of headroom for a handful of extensions + a worker or two; future multi-process deploys can lift the cap per-process and add a shared-broker fanout layer.

### Publish hook on `POST /v1/thoughts/send`

The send endpoint (in `writes_thoughts.py`) gains a single post-persist call into the new broker. Conceptually:

```python
# after successful Qdrant upsert:
broker.publish(
    namespace=thought.namespace,
    to_presence=thought.to_presence,
    event=thought,
)
```

Publish is fire-and-forget; broker takes a lock-free handoff and returns immediately. If no subscribers for the `(namespace, to_presence)` topic, publish is a no-op — no cost to senders.

## Broker design

Lives at `src/musubi/api/events.py`. In-process pub-sub, FastAPI-lifecycle-aware.

Core interface:

```python
class EventBroker:
    async def subscribe(
        self,
        namespace: str,
        presences: list[str],
        since: str | None = None,   # KSUID for replay
    ) -> AsyncIterator[Thought]: ...

    async def publish(
        self,
        namespace: str,
        to_presence: str,
        event: Thought,
    ) -> None: ...

    @property
    def metrics(self) -> BrokerMetrics: ...
```

Implementation notes:

- **Single-process.** Uses `asyncio.Queue` per subscriber, dispatched by a background task. **No Redis / RabbitMQ / Kafka** — that's a future concern when Musubi goes multi-process, out of scope here.
- **Lifespan-managed.** Broker instance attached to the FastAPI app via `lifespan` context manager. Graceful shutdown drains connections by sending `event: close` then closing the TCP socket.
- **Replay isolation.** Replay reads go through the thoughts plane (`ThoughtsPlane.query`) directly — broker doesn't keep history. Keeps the broker memory bounded regardless of traffic.
- **Metered.** `subscriptions_active`, `publishes_total`, `dropped_events_total{reason}`, `replay_events_total` as OTel gauges/counters. `/v1/ops/metrics` already passes these through to Prometheus.

## Consumer expectations (NORMATIVE — applies to every `/thoughts/stream` consumer)

This section is also rendered into `07-interfaces/canonical-api.md` verbatim via the `spec-update:` trailer. Both OpenClaw and Musubi read from the same contract doc.

1. **Reconnect with exponential backoff + jitter.** On connection drop, wait `min(2^n * 1s + rand(0, 1s), 60s)` before retrying. Cap at 60s. Reset the backoff after a connection has been up for 5 minutes. Don't hammer Musubi when it's down.

2. **Persist `Last-Event-ID` across restarts.** OpenClaw uses `chrome.storage.local` (or IndexedDB). Python consumers write to a file or KV. Lose the ID and you replay the entire thoughts plane on restart (bounded by plane size, still non-trivial).

3. **Bounded local dedup set.** Clients MAY receive the same `object_id` twice if replay overlaps with in-flight events. Maintain a set of the last 1000 `object_id`s seen (or a TTL cache — 1 hour TTL is ample). Skip any event whose ID is already in the set.

4. **Scope-mismatch error handling.** If the token doesn't have namespace-read scope, the server returns `403 Forbidden` on the initial GET (before any SSE frames). This is a client-bug signal: retry with the same token is futile. Consumers MUST NOT reconnect on 403. Bubble the error to the user ("Musubi token expired — please re-authenticate").

5. **Ping-gap timeout.** If no frame arrives within 2× the ping interval (i.e., 60s), assume the connection is dead and close it yourself. Triggers the reconnect path. This catches silent half-open connections where the OS thinks the TCP is fine but no bytes are flowing.

6. **Lexicographic ID comparison.** `object_id` is a KSUID (27-char base62). Compare as strings, not as numbers. `"2iVVRLuCjwsSIxfv8KKaZg3NoXc" > "2iVVRLuAhk..."` is the right dedup-set insertion order.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item below is a passing test in `tests/api/test_thoughts_stream.py`.
- [ ] Branch coverage ≥ 85% on `src/musubi/api/events.py` + the new endpoint code in `src/musubi/api/routers/thoughts.py`.
- [ ] `openapi.yaml` extended with the endpoint + event schemas; `make openapi-diff` (or equivalent) shows only additive changes.
- [ ] `07-interfaces/canonical-api.md` updated with §Thoughts stream + §Consumer expectations; `spec-update:` trailer on the feat commit.
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Issue label status:ready → status:in-progress at claim time (Dual-update rule; post-#93 drift-check is ✗ not ⚠).
- [ ] Lock file removed from `_inbox/locks/`.

## Test Contract

**Endpoint shape:**

1. `test_stream_returns_sse_content_type`
2. `test_stream_emits_ping_every_30s`
3. `test_stream_returns_403_without_read_scope`
4. `test_stream_returns_503_when_connection_cap_exceeded`

**Subscription filtering:**

5. `test_stream_filters_by_namespace`
6. `test_stream_filters_by_include_parameter`
7. `test_stream_defaults_include_to_token_presence_plus_all`
8. `test_stream_never_delivers_cross_namespace_events`

**Fanout semantics (NORMATIVE — broadcast, NOT competing-consumer):**

9. `test_two_subscribers_same_presence_both_receive_every_event`
10. `test_three_subscribers_one_slow_fast_ones_unaffected`

**Publish hook:**

11. `test_send_thought_publishes_to_broker`
12. `test_send_with_no_subscribers_is_noop_not_error`

**Replay:**

13. `test_replay_from_last_event_id_emits_events_after_that_ksuid`
14. `test_replay_with_missing_last_event_id_starts_from_live`
15. `test_replay_is_lexicographic_by_object_id`

**Backpressure:**

16. `test_slow_consumer_events_dropped_and_metered`
17. `test_reconnect_with_last_event_id_recovers_dropped_events`

**Lifecycle:**

18. `test_server_shutdown_sends_close_event`
19. `test_client_disconnect_cleans_up_subscription`

**Hypothesis / property:**

20. `hypothesis: idempotency — client receiving the same object_id N times with local dedup set yields exactly 1 user-visible event`
21. `hypothesis: ordering — for any subscriber, received object_id sequence is monotonically increasing in KSUID order`

**Explicitly out-of-scope (do NOT implement here):**

- `GET /v1/lifecycle/stream` — future sibling slice once infrastructure exists.
- `GET /v1/contradictions/stream` — future sibling slice.
- Multi-process broker fanout (Redis pub-sub etc.) — future multi-process deploy concern.
- SDK ergonomic `client.thoughts.stream()` method — cross-slice to slice-sdk-py, not here.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-20 01:00 — gemini-3-1-pro-nyla — claim

- Claimed slice via `pick-slice` skill. Issue #102, PR #106 (draft).

### 2026-04-19 — operator — slice carved

- Carved in response to OpenClaw v0.1 research by Aoi (Claude Code on Nyla). Three notes from Aoi pre-folded into spec: auth scope alignment (matches existing `AuthRequirement(ns, "r")` convention — no new scope), broadcast fanout semantics declared normative in spec not just tests, and Consumer expectations section added as shared contract.
- Endpoint design cross-checked against `src/musubi/api/routers/thoughts.py` current conventions — matches (namespace-scoped authz, body vs query-string patterns consistent with sibling endpoints).
- KSUID confirmed over ULID per `musubi.types.common.KSUID`; SSE event id maps 1:1 to `object_id`.
- No pre-src-monorepo path drift on the spec file or the target routers — all post-ADR-0015.

## Cross-slice tickets opened by this slice

- _(none yet; may open one to slice-sdk-py to add `AsyncMusubiClient.thoughts.stream()` convenience method after this lands)_

## PR links

- _(none yet)_
