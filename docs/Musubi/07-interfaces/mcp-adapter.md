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

We expose the subset of Musubi that makes sense for a coding agent. Not everything.

**Implementation status:** the current adapter (`src/musubi/adapters/mcp/tools.py`) registers `memory_capture` + `memory_recall` — enough for the v1.0 cut. The rest of the tables below is the **designed** MCP surface; unchecked rows are tracked in `[[_slices/slice-adapter-mcp]]` as follow-up work, not v1.0 scope.

### Memory

| Tool | Status | Musubi call | Notes |
|---|---|---|---|
| `memory_capture` | shipped | `client.episodic.capture()` | Capture a new episodic observation. |
| `memory_recall` | shipped | `client.retrieve(mode="fast")` | Retrieve for just-in-time context. |
| `memory_recent` | planned | Filtered retrieve | Recent items in a namespace. |
| `memory_forget` | planned | Raw `DELETE /v1/episodic/{id}` (SDK method TBD) | Soft-archive. No `client.episodic.archive()` on the SDK yet. |
| `memory_reflect` | planned | Filtered retrieve + aggregation | Returns counts by tag/topic (no LLM). |

### Curated

| Tool | Musubi call | Notes |
|---|---|---|
| `curated_search` | Retrieve with `planes=["curated"]` | |
| `curated_get` | `client.curated.get(id)` | Full body on demand. |
| `curated_link_topics` | (future) | Bulk re-tag. Post-v1. |

### Thoughts

| Tool | Musubi call | Notes |
|---|---|---|
| `thought_send` | `client.thoughts.send()` | |
| `thought_check` | `client.thoughts.check()` | |
| `thought_read` | `client.thoughts.read()` | |
| `thought_history` | `client.thoughts.history()` | |

### Artifacts

| Tool | Musubi call | Notes |
|---|---|---|
| `artifact_upload` | `client.artifacts.upload()` | File stays in memory on MCP side; streamed. |
| `artifact_get` | `client.artifacts.get()` | |
| `artifact_chunks` | `client.artifacts.chunks()` | |

### Not exposed via MCP

- Lifecycle transitions (operator-only, too dangerous via an agent).
- Promotion / concept reject (operator-only).
- Ops/health endpoints (use the CLI).
- Contradictions resolve (human-only decision).

This restriction is important — an agent should not be able to delete a curated file, reject a concept, or reconcile the vault. Read + write-new + soft-delete is the appropriate scope.

## Tool definitions (snippet)

```json
{
  "name": "memory_capture",
  "description": "Capture a new episodic memory observation in Musubi.",
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
  },
  "outputSchema": {
    "type": "object",
    "properties": {
      "object_id": {"type": "string"},
      "state": {"type": "string"},
      "dedup": {"type": ["object", "null"]}
    }
  }
}
```

Tool definitions mirror the canonical API shapes — they're auto-generated from the pydantic models (see `musubi-mcp-adapter/codegen/`).

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
