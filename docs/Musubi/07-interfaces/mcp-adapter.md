---
title: MCP Adapter
section: 07-interfaces
tags: [adapter, interfaces, mcp, section/interfaces, status/complete, type/spec]
type: spec
status: complete
implements: src/musubi/adapters/mcp/
updated: 2026-10-01
up: "[[07-interfaces/index]]"
reviewed: false
---
# MCP Adapter

Exposes Musubi to Model Context Protocol clients as MCP tools. It lives in this repository at `src/musubi/adapters/mcp/` (`musubi.adapters.mcp`) and talks to Core through the in-repo async SDK (`musubi.sdk.AsyncMusubiClient`, see [[07-interfaces/sdk]]).

It is a small, generic server. Per-host integrations with their own lifecycle hooks, durable outbox and identity handling live in sibling repositories; for Claude Code, use [`musubi-claude`](https://github.com/sourceblender/musubi-claude). See [Connect](../../guide/connect.md) for the full list.

## Running it

```bash
python -m musubi.adapters.mcp.server        # stdio (default)
python -m musubi.adapters.mcp.server sse    # MCP over SSE
```

Configuration comes from the Core settings model (`musubi.config.get_settings()`):

- `MUSUBI_API_URL`: Core API base, default `http://localhost:8100/v1`;
- `MUSUBI_TOKEN`: bearer token sent on every call. With no token configured the server logs a warning and Core answers 401.

Because it loads the full settings model, the process needs the same required environment as Core (Qdrant, TEI and so on), even though it only calls the HTTP API. There is no separate adapter config file, tool allowlist or default presence.

`mcp` (FastMCP) ships as a dependency of the `musubi` package. There is no adapter container image.

## Transports

1. **stdio**, for a local MCP client that spawns the server as a subprocess. The token comes from `MUSUBI_TOKEN`.
2. **SSE**, via FastMCP's SSE transport. The adapter does **no** authentication of its own on this transport. Put it behind an operator-provided reverse proxy that handles TLS and client authentication (for example OAuth 2.1). The adapter always calls Core with its single configured `MUSUBI_TOKEN`; it does not forward per-client tokens.

## Tools exposed

The five canonical agent tools from [[07-interfaces/agent-tools]] ([[13-decisions/0032-agent-tools-canonical-surface]]), all implemented in `src/musubi/adapters/mcp/tools.py`:

| Tool | Parameters | Musubi call |
|---|---|---|
| `musubi_search` | `namespace`, `query`, `limit=5`, `planes=None` | `client.retrieve(mode="deep")` |
| `musubi_recent` | `namespace`, `limit=10`, `tags=None` | `client.retrieve(mode="recent")` |
| `musubi_get` | `plane`, `namespace`, `object_id` | `client.{episodic,curated,concepts,artifacts}.get()` |
| `musubi_remember` | `namespace`, `content`, `importance=7`, `topics=None` | `client.episodic.capture()` |
| `musubi_think` | `namespace`, `from_presence`, `to_presence`, `content`, `channel="default"`, `importance=5` | `client.thoughts.send()` |

Behaviour worth knowing:

- **`namespace` is always an explicit argument.** The adapter does not infer it from the client or the token. Core checks it against the token's scopes.
- `musubi_get` accepts the 2-segment presence root plus `plane` (it composes `<root>/<plane>`) or a full 3-segment namespace.
- `musubi_remember` sends `topics` as tags and adds `kind:episode`, `staleness:episodic` (when absent) and the modality tag `src:mcp-agent-remember`.
- `musubi_search` surfaces retrieval degradation to the model as a leading `[SYSTEM: Retrieval degraded: ...]` line, and both search and recent mark truncated content with its original length.
- Content follows the API limit of 32,768 UTF-8 bytes per memory.

### Deprecated aliases

Still registered and advertised, with `[DEPRECATED]` in their descriptions. Each logs a warning per call:

| Alias | Forwards to | Notes |
|---|---|---|
| `memory_capture` | the `musubi_remember` body | Keeps its old default importance of 5 and takes `tags`. |
| `memory_recall` | the `musubi_search` body | Default `limit=10`, no `planes`. |

### Not exposed

There are no tools for lifecycle transitions, promotion, rejection, purge, ops endpoints, uploads, thought inbox reads, or curated writes. An agent should not be able to archive curated knowledge, reject a concept or reconcile the vault; read, write-new and send are the right scope.

## Output and errors

Every tool returns a plain text string formatted for a model to read. Errors are **not** raised as MCP protocol errors: a failed call returns a string starting with `Error: ` that carries the SDK's exception text (for example a 403 from Core). There is no Musubi-to-JSON-RPC error-code mapping.

## Not implemented

The following were in the original design and do not exist:

- MCP **resources** (`musubi://...` URIs) and MCP **prompts**.
- Built-in OAuth 2.1 / PKCE, or a presence derived from the OAuth client id.
- Streaming tool results.
- Adapter metrics (`mcp.tool.*`) and a config file with tool allow/deny lists.
- Granular per-plane tools (topic linking, artifact upload or chunk access, forget, reflect).

## Test Contract

**Module under test:** `src/musubi/adapters/mcp/`

Canonical tools (`tests/adapters/test_mcp_canonical_tools.py`):

1. `test_attach_tools_registers_all_canonical_plus_aliases`
2. `test_search_invokes_retrieve_deep_mode`
3. `test_search_passes_planes_filter`
4. `test_search_no_results_returns_clear_message`
5. `test_search_backend_error_returns_tool_error_string`
6. `test_search_formats_truncation_metadata`
7. `test_recent_invokes_retrieve_recent_mode_without_query`
8. `test_recent_passes_tags_filter`
9. `test_get_routes_each_plane_to_its_stub`
10. `test_get_accepts_two_part_root_and_composes_plane`
11. `test_get_unknown_id_returns_tool_error_with_id_and_namespace`
12. `test_remember_writes_to_episodic_with_modality_tag`
13. `test_remember_default_importance_is_seven`
14. `test_think_sends_thought_to_recipient_presence`
15. `test_memory_capture_alias_forwards_to_remember_and_logs_deprecation`
16. `test_memory_recall_alias_forwards_to_search_and_logs_deprecation`
17. `test_plane_attr_map_targets_match_real_sdk_accessors`

Warnings (`tests/adapters/test_ret007_adapter_warnings.py`):

18. `test_mcp_adapter_surfaces_warnings`
19. `test_mcp_preserves_reranker_cause_detail`

`tests/adapters/test_mcp.py` holds the original design's contract bullets (resources, prompts, OAuth, error-code mapping, streaming, integration); every one of them is skipped.
