---
title: MCP Adapter
section: 07-interfaces
tags: [adapter, interfaces, mcp, section/interfaces, status/complete, type/spec]
type: spec
status: complete
implements: src/musubi/adapters/mcp/
updated: 2026-04-19
up: "[[07-interfaces/index]]"
reviewed: false
---
# MCP Adapter

Maps Musubi to the Model Context Protocol. Coding agents (Claude Code, Cursor, others) speak MCP; this adapter exposes Musubi's capabilities as MCP tools.

Integrated as a module in the monorepo at `src/musubi/adapters/mcp/` (import path `musubi.adapters.mcp`). Ships as a container + stdio binary. Uses the `musubi.sdk` client.

## MCP spec version

Targets the **June 2025 MCP spec** (OAuth 2.1 auth, `tools/`, `resources/`, `prompts/`, structured content types). As of April 2026, this spec is the stable base; newer amendments are backward-compatible.

## Transports

Two modes:

1. **stdio** — for local coding agents (Claude Code) to spawn as a subprocess.
2. **HTTP (SSE + OAuth 2.1)** — for remote use; Kong serves the adapter alongside Musubi Core.

Same core logic; different MCP server bootstrap.

## Tools exposed

The MCP adapter exposes the **canonical agent-tools surface** ([[07-interfaces/agent-tools]]) — five tools, identical names + parameter shapes across every adapter, backed by [[13-decisions/0032-agent-tools-canonical-surface]].

| Canonical tool | Status | Musubi call |
|---|---|---|
| `musubi_recent` | tracked in [[_slices/slice-mcp-canonical-tools]] | `client.retrieve(mode="recent")` (depends on [[_slices/slice-retrieve-recent]]) |
| `musubi_search` | tracked in [[_slices/slice-mcp-canonical-tools]] | `client.retrieve(mode="deep")` |
| `musubi_get` | tracked in [[_slices/slice-mcp-canonical-tools]] | `client.{plane}.get()` |
| `musubi_remember` | tracked in [[_slices/slice-mcp-canonical-tools]] | `client.episodic.capture()` |
| `musubi_think` | tracked in [[_slices/slice-mcp-canonical-tools]] | `client.thoughts.send()` |

**Implementation status (April 2026):** the current adapter (`src/musubi/adapters/mcp/tools.py`) registers only `memory_capture` + `memory_recall` — pre-canonical names from the v1.0 cut. ADR 0032 supersedes those; the canonical surface lands via [[_slices/slice-mcp-canonical-tools]], which keeps `memory_capture` + `memory_recall` as deprecated aliases for one minor release before removal.

### Granular plane tools (optional, not required by the canonical surface)

The MCP adapter MAY expose finer-grained per-plane tools for power-use scenarios that the agent-level surface doesn't cover. These are **optional** and tracked in [[_slices/slice-adapter-mcp]] as follow-up work — not v1.0 scope, not required for canonical conformance.

| Tool | Musubi call | Notes |
|---|---|---|
| `curated_link_topics` | (future) | Bulk re-tag. Post-v1. |
| `thought_check` | `client.thoughts.check()` | Inbox poll, agent-side polling pattern. |
| `thought_history` | `client.thoughts.history()` | Backfill on `X-Musubi-Replay-Truncated`. |
| `artifact_upload` | `client.artifacts.upload()` | File stays in memory on MCP side; streamed. |
| `artifact_chunks` | `client.artifacts.chunks()` | Direct chunk access for large artifacts. |
| `memory_forget` | Raw `DELETE /v1/episodic/{id}` (SDK method TBD) | Soft-archive. Power-use only. |
| `memory_reflect` | Filtered retrieve + aggregation | Returns counts by tag/topic (no LLM). |

`musubi_get` covers single-object retrieval across every plane (curated, concept, episodic, artifact) — no separate `curated_get` / `artifact_get` needed at the agent layer.

### Not exposed via MCP

- Lifecycle transitions (operator-only, too dangerous via an agent).
- Promotion / concept reject (operator-only).
- Ops/health endpoints (use the CLI).
- Contradictions resolve (human-only decision).

This restriction is important — an agent should not be able to delete a curated file, reject a concept, or reconcile the vault. Read + write-new + soft-delete is the appropriate scope.

## Tool definitions

Canonical tool input/output schemas live in [[07-interfaces/agent-tools]]. The MCP adapter renders each schema into MCP `inputSchema` form — JSON-schema-flavored — at registration time. Tool descriptions adapt the canonical text for an MCP audience but never rename the tool or change its parameter names.

The legacy snippet below is the v1.0 `memory_capture` shape, kept here only as a reference for the deprecation alias path. Once aliases drop after one minor release, this block goes too.

```json
{
  "name": "memory_capture",
  "description": "[DEPRECATED] Use musubi_remember. Capture a new episodic memory observation in Musubi.",
  "inputSchema": {
    "type": "object",
    "required": ["content", "namespace"],
    "properties": {
      "namespace": {"type": "string", "description": "Must match OAuth scope."},
      "content": {"type": "string", "minLength": 1, "maxLength": 16000},
      "tags": {"type": "array", "items": {"type": "string"}},
      "topics": {"type": "array", "items": {"type": "string"}},
      "importance": {"type": "integer", "minimum": 1, "maximum": 10}
    }
  }
}
```

## Resources

MCP resources = URI-addressable read-only surfaces. We expose:

```
musubi://memory/{object_id}
musubi://curated/{object_id}
musubi://concept/{object_id}
musubi://artifact/{object_id}
musubi://artifact-chunk/{chunk_id}
musubi://thought/{object_id}
```

Each returns a structured content block (pydantic model serialized). Agents can cite them directly — MCP clients render citations as clickable.

## Prompts

Optional MCP prompts. Pre-canned queries to reduce boilerplate:

- `recall_today` — "what did I capture today in namespace X?"
- `find_runbook` — curated search with topic filter `infrastructure/runbook`.
- `weekly_reflection_summary` — reads the last 7 reflection files.

Prompts are syntactic sugar; nothing non-composable happens in them.

## Auth

OAuth 2.1 with PKCE for HTTP transport; static token for stdio transport.

Token scope = Musubi namespace scope. Flow:

1. User configures MCP client with OAuth discovery URL.
2. User logs in via the Musubi auth endpoint, approves namespace scopes.
3. Token issued for the MCP client; bearer-passed to adapter.
4. Adapter forwards to Musubi Core as the bearer token.

stdio transport: token loaded from `MUSUBI_TOKEN` env var or `~/.musubi/token`.

See [[10-security/auth]] for the full auth spec.

## Namespace presence mapping

Each MCP client identifies itself via a **presence name**, mapped to a namespace triple:

- Claude Code → `eric/claude-code`
- Cursor → `eric/cursor`
- Generic CLI user → `eric/cli`

The adapter reads this from the OAuth client ID or from config. Every `memory_capture` call then defaults `namespace` to `<presence>/episodic` unless the caller overrides. The caller's override is validated against the token scope.

## Error mapping

Musubi error → MCP error:

| Musubi | MCP error code | User-visible |
|---|---|---|
| `FORBIDDEN` | `-32602` (InvalidParams) | "Namespace not in your scope." |
| `BAD_REQUEST` | `-32602` | Message includes the pydantic error. |
| `NOT_FOUND` | `-32602` | "No object with that ID." |
| `CONFLICT` | `-32603` (InternalError) | "Version mismatch; retry." |
| `RATE_LIMITED` | `-32002` (custom) | Honor `Retry-After`. |
| `BACKEND_UNAVAILABLE` | `-32603` | "Musubi is degraded; try again." |

Errors include structured data (`error.data`) so agent clients can parse them.

## Streaming

`memory_recall` with a large `limit` can stream progress. MCP's newer streaming content type is used; chunked results flow to the client as they land.

Default behavior: non-streaming. Opt-in via `stream=true` in the input.

## Observability

The adapter emits:

- `mcp.tool.invoked{tool=...}` counter.
- `mcp.tool.latency_ms{tool=...}` histogram.
- `mcp.auth.failures` counter.

Logs include the MCP `request_id` and forward to Musubi Core as `X-Request-Id`.

## Config

```yaml
# musubi-mcp-adapter.yaml
musubi_url: https://musubi.example.local.example.com/v1
oauth:
  authority: https://auth.internal.example.com
  client_id: musubi-mcp
presence_default: eric/claude-code
tool_allowlist: [memory_capture, memory_recall, thought_send, ...]
tool_denylist: [memory_reflect]
```

Allow/denylist lets the operator customize per deployment.

## Test Contract

**Module under test:** `musubi-mcp-adapter/src/*`

Tool definitions:

1. `test_all_tool_definitions_match_pydantic`
2. `test_tool_input_schemas_valid_json_schema`
3. `test_tool_output_schemas_match_response_shape`

Tool invocation:

4. `test_memory_capture_invokes_sdk_with_mapped_args`
5. `test_memory_recall_invokes_retrieve_fast_mode`
6. `test_thought_send_invokes_sdk`
7. `test_artifact_upload_streams_bytes`
8. `test_prompts_are_tool_compositions` (no hidden logic)

Scope enforcement:

9. `test_out_of_scope_capture_returns_mcp_error_not_exception`
10. `test_namespace_override_validated_against_token_scope`
11. `test_operator_only_tools_not_exposed` (lifecycle, reconcile, promote)

Auth:

12. `test_oauth_pkce_flow_integration`
13. `test_stdio_transport_uses_env_token`

Errors:

14. `test_musubi_errors_mapped_to_mcp_codes_consistently`

Resources:

15. `test_musubi_uri_resolves_to_structured_content`
16. `test_missing_object_returns_mcp_not_found`

Streaming:

17. `test_streaming_memory_recall_yields_partial_results`

Integration (shared contract suite):

18. `integration: runs canonical contract suite against adapter + live Musubi container`
19. `integration: claude-code spawns adapter via stdio, captures + recalls — round trip < 500ms`
