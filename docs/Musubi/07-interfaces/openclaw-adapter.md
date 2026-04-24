---
title: OpenClaw Adapter
section: 07-interfaces
tags: [adapter, interfaces, openclaw, plugin, section/interfaces, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-23
up: "[[07-interfaces/index]]"
reviewed: false
implements: "github.com/ericmey/openclaw-musubi"
---
# OpenClaw Adapter

Integrates Musubi into the OpenClaw agent runtime. Mirrors agent episodic output into Musubi, serves curated + concept recall as prompt supplements, and delivers cross-presence thoughts over SSE.

**Implementation lives in a sibling repo:** `github.com/ericmey/openclaw-musubi` (TypeScript OpenClaw plugin, Node.js 20+). Per [ADR-0022](../13-decisions/0022-extension-ecosystem-naming.md), non-Python integrations live in external `<system>-musubi` repos so their toolchain (pnpm, tsc, vitest) and release cadence stay separate from Musubi's Python monorepo.

This spec is the **contract** the plugin implements against Musubi's canonical API. The contract lives here; the implementation lives in `openclaw-musubi`. TypeScript types are generated from `openapi.yaml` (in this repo) via `openapi-typescript`, giving the plugin compile-time safety against the canonical API.

## What OpenClaw is

OpenClaw is a local agent-orchestration runtime (April 2026 onward) that runs one or more identity-distinct agents ("presences") against a user's local context. Each presence has its own working memory, personality, and system-prompt — and needs persistent, cross-presence memory plus the ability to notify sibling presences of events.

## Capabilities

### Capture mirror

Every episodic event emitted by an OpenClaw agent — user messages, agent responses, tool calls, agent_end markers — is mirrored into Musubi as an episodic memory under the agent's presence namespace. The OpenClaw-side memory stays authoritative for in-session recall; Musubi becomes the durable cross-session + cross-presence store.

### Prompt + corpus supplement

On each agent turn, the plugin asks Musubi for curated + concept hits relevant to the in-flight context and blends them into the prompt as an authority-labeled supplement ("from your durable memory…"). Corpus search is available as a tool (`musubi_recall`) for explicit lookup.

### Thoughts

Presences send and receive thoughts across OpenClaw instances (e.g. Aoi says "I restarted the LiveKit agent" → Rin's next turn sees it). Delivery is push-over-SSE from Musubi's `/v1/thoughts/stream` endpoint; send uses `/v1/thoughts/send`.

## Auth

**Static bearer tokens.** Not OAuth — the plugin is a trusted local-host integration, not a third-party consumer that needs delegated user consent.

Two token configurations:

- **Single-token mode** — `core.token` covers every presence the plugin represents. Token scope must be broad enough for all of them.
- **Per-agent tokens mode** — `core.perAgentTokens: {<agent-id>: <token-or-${ENV_VAR}>}` maps OpenClaw agent ids to dedicated Musubi bearer tokens. Each agent's operations use its own token, so identities stay cryptographically isolated at the wire. Env-var substitution (`${MUSUBI_TOKEN_AOI}`) is supported so tokens never sit in config files.

Strict mode (`core.strictPerAgent: true`) rejects any operation where an agent has a presence mapping but no matching token entry — fails loud instead of silently falling back to `core.token`.

### Scope requirements

The plugin issues calls at two different namespace segment counts. Tokens must carry scopes for both. See [[07-interfaces/canonical-api#scope-by-endpoint]] for the canonical table; the plugin-relevant subset is:

| Plugin operation | Endpoint | Namespace | Token scope pattern |
|---|---|---|---|
| Capture mirror | `POST /v1/memories` | `<owner>/<presence>/episodic` | `<owner>/<presence>/episodic:w` |
| Recall (cross-plane) | `POST /v1/retrieve` | `<owner>/<presence>` (2-seg) | `<owner>/<presence>:r` |
| Thought send | `POST /v1/thoughts/send` | `<owner>/<presence>/thought` | `<owner>/<presence>/thought:w` |
| Thought stream | `GET /v1/thoughts/stream` | `<owner>/<presence>` (2-seg) | `<owner>/<presence>:r` |

A presence token typically needs `<owner>/<presence>:r` (covers 2-seg reads — retrieve, stream) plus `<owner>/<presence>/episodic:w` and `<owner>/<presence>/thought:w` for the write paths. Issuing only 3-segment scopes will 403 the stream; issuing only 2-segment will 403 the writes.

## Presence mapping

Conventional shape — OpenClaw presence id → Musubi namespace:

| OpenClaw config | Namespace convention |
|---|---|
| `presence.defaultId: "eric/openclaw"` | episodic: `eric/openclaw/episodic`, thought: `eric/openclaw/thought` |
| `presence.perAgent: {"aoi": "eric/aoi"}` | episodic: `eric/aoi/episodic`, thought: `eric/aoi/thought` |

The plugin never writes to `_shared` namespaces directly; cross-presence curated is populated by the Musubi Lifecycle Worker's synthesis/promotion pipeline, not by OpenClaw.

## Retrieve pattern

The plugin uses Musubi's 2-segment cross-plane retrieve (see [ADR-0028](../13-decisions/0028-retrieve-2seg-namespace-crossplane.md)). One call per recall or prompt-supplement refresh, not one per plane:

```http
POST /v1/retrieve
Content-Type: application/json
Authorization: Bearer <presence-token>

{
  "namespace": "eric/openclaw",
  "query_text": "how do I restart the livekit agent",
  "mode": "fast",
  "limit": 5,
  "planes": ["curated", "concept", "episodic"]
}
```

Server expands `namespace + planes` into `<namespace>/<plane>` targets, fans out in parallel, merges by score, and returns a single sorted result set. Strict per-plane scope check — if the token lacks read scope for any expanded plane, the whole request is 403. (This is the correct failure mode: consumers want explicit "your scope is wrong" over silent partial results.)

## Thoughts stream

The plugin runs one SSE subscription per active presence. Each subscription:

- Persists `Last-Event-ID` to local storage across process restarts.
- Honors `Retry-After` on `503`; reconnects with exponential backoff + jitter (cap 60s) on other errors.
- Does **not** reconnect on `403` — bubbles up as a re-auth signal.

On reconnect, Musubi replays thoughts emitted during the gap (see [[07-interfaces/canonical-api#thoughts-stream]]); the plugin does not need to poll `/v1/thoughts/check` as a backfill mechanism.

## Capture payload shape

Episodic mirror from an OpenClaw `agent_end` event:

```json
POST /v1/memories
{
  "namespace": "eric/aoi/episodic",
  "content": "<agent response text>",
  "tags": ["openclaw-mirror", "agent_end"],
  "topics": [],
  "importance": 5,
  "content_type": "observation",
  "capture_source": "openclaw-agent-end",
  "source_ref": "openclaw-session:<session-id>:<turn-ksuid>",
  "ingestion_metadata": {
    "agent_id": "aoi",
    "session_id": "...",
    "turn_id": "..."
  }
}
```

Notes:
- `importance` must be in `[1, 10]` per the server schema. The plugin-side default is 5.
- `agent_id` must be extracted from the event payload so per-agent attribution survives the mirror. Falling back to `presence.defaultId` for every capture loses the isolation that per-agent tokens were supposed to provide.

## Thought-send payload shape

```json
POST /v1/thoughts/send
Idempotency-Key: <uuid>

{
  "from_presence": "eric/aoi",
  "to_presence": "eric/rin",
  "channel": "default",
  "content": "Heads up: LiveKit agent restarted at 09:03 UTC.",
  "importance": 5
}
```

Idempotency is **header-only** — `Idempotency-Key`. There is no `client_id` body field.

## Lifecycle

- **Plugin start** → validate config → resolve presences → open one SSE subscription per presence → register tools (`musubi_recall`, `musubi_remember`, `musubi_think`).
- **Plugin unload** → close SSE subscriptions → drain in-flight captures → release tokens. Leaking the SSE listener or the capture retry loop will keep the Node.js process alive past unload.
- **Error taxonomy** — `401` → token invalid (re-auth); `403` → scope insufficient (surface to user, don't retry); `422` → payload invalid (bug in plugin, log + drop); `503` → backend down, honor `Retry-After`.

## Observability

The plugin emits OpenClaw telemetry events for its own operations (`openclaw.memory.capture.sent`, `openclaw.recall.request.latency_ms`, `openclaw.thought.received`, etc.). Forwarding into Musubi core metrics is out of scope — metrics on the Musubi side are already exposed at `/v1/ops/metrics`.

## Test Contract

**Module under test:** the Node.js plugin (`github.com/ericmey/openclaw-musubi`). Contract-level tests that the plugin must pass against a running Musubi:

Capture mirror:

1. `test_agent_end_capture_uses_agent_specific_token_when_configured`
2. `test_agent_end_capture_namespace_derived_from_agent_presence_mapping`
3. `test_capture_payload_importance_clamped_to_server_range_1_10`

Retrieve:

4. `test_recall_issues_single_2seg_cross_plane_call_not_per_plane_fanout`
5. `test_recall_403_on_insufficient_scope_surfaces_as_error_not_partial_results`
6. `test_recall_surfaces_top_level_title_field_for_curated_rows`

Thoughts:

7. `test_stream_reconnect_with_last_event_id_receives_replayed_thoughts`
8. `test_stream_honors_retry_after_on_503_rather_than_exponential_backoff`
9. `test_thought_send_idempotency_via_header_not_body_field`

Auth + presence:

10. `test_strict_mode_rejects_agent_missing_token_entry`
11. `test_env_var_substitution_resolves_tokens_from_process_env`
12. `test_single_token_mode_falls_back_when_no_per_agent_config_present`

Lifecycle:

13. `test_plugin_unload_closes_all_sse_subscriptions`
14. `test_plugin_unload_drains_in_flight_capture_retries`

Shared contract:

15. `integration: canonical contract suite via adapter` (subset from [[07-interfaces/contract-tests]])
