---
title: POC Inventory
section: 02-current-state
tags: [current-state, section/current-state, status/complete, type/gap-analysis]
type: gap-analysis
status: complete
updated: 2026-04-17
up: "[[02-current-state/index]]"
reviewed: false
---
# POC Inventory

Snapshot of the current Musubi repo (April 2026), for migration planning. Cross-referenced from [[11-migration/index]].

## Directory layout

```
musubi/                     # python package
├── server.py               # FastMCP tool definitions (thin wrappers)
├── memory.py               # memory_store, memory_recall, memory_recent, memory_forget, memory_reflect
├── thoughts.py             # thought_send, thought_check, thought_read, thought_history
├── collections.py          # ensure_collections, _ensure_indexes
├── embedding.py            # embed_text (Gemini, singleton client, exponential backoff)
├── config.py               # env loading + constants
└── utils.py                # extract_payload helper

tests/
├── conftest.py             # mock_qdrant, mock_embed, FakePoint/FakeQueryResult/etc.
├── test_memory.py          # ~60 tests
├── test_thoughts.py        # ~30 tests
├── test_collections.py
├── test_embedding.py
├── test_integration.py     # multi-step flows
└── test_session_sync.py

scripts/
├── install.sh              # Colima + Qdrant + venv + LaunchAgent (macOS-biased)
├── update.sh
├── uninstall.sh
└── seed_memories.py        # optional import helper

mcp_server.py               # entrypoint (imports from musubi.server)
docker-compose.yml          # single-service: qdrant:latest, ports 6333/6334, volume qdrant_data
pyproject.toml              # python 3.12+, ruff, mypy strict, bandit
Makefile                    # fmt / lint / typecheck / test / check / install / dev
README.md                   # architecture diagram + quick start + tools table
CLAUDE.md                   # architecture rules for coding agents
.env.example
```

## Qdrant schema

### Collection: `musubi_memories`
- Vector: single, 3072-d, COSINE.
- Payload fields: `content`, `type` ∈ {`user`, `feedback`, `project`, `reference`}, `agent`, `tags` (list), `context`, `created_at`, `created_epoch`, `updated_at`, `updated_epoch`, `access_count`, `last_accessed`.
- Indexes (payload): `agent` (KEYWORD), `type` (KEYWORD), `created_at` (KEYWORD), `created_epoch` (FLOAT), `updated_epoch` (FLOAT), `access_count` (INTEGER).

### Collection: `musubi_thoughts`
- Vector: single, 3072-d, COSINE.
- Payload fields: `content`, `from_presence`, `to_presence` (or `"all"`), `read` (bool), `read_by` (list), `created_at`, `created_epoch`.
- Indexes: `from_presence` (KEYWORD), `to_presence` (KEYWORD), `read_by` (KEYWORD), `read` (BOOL), `created_epoch` (FLOAT).

## MCP tool surface

| Tool | Params | Returns |
|---|---|---|
| `memory_store` | `content, type, agent, tags, context` | `{status, id}` |
| `memory_recall` | `query, limit, agent_filter, type_filter, min_score, brief` | `{memories: [...]}` |
| `memory_recent` | `hours, agent_filter, type_filter, limit, brief` | `{memories: [...]}` |
| `memory_forget` | `id` | `{status, id}` |
| `memory_reflect` | `mode` ∈ {summary, stale, frequent} | mode-dependent |
| `thought_send` | `content, from_presence, to_presence` | `{ok, id}` |
| `thought_check` | `my_presence, limit` | `{unread, thoughts: [...]}` |
| `thought_read` | `thought_ids, my_presence` | `{ok, marked}` |
| `thought_history` | `query, limit, presence_filter, min_score, brief` | `{thoughts: [...]}` |
| `session_sync` | `my_presence, hours, thought_limit, memory_limit` | `{thoughts, memories, ...}` |

## Infrastructure defaults

| Resource | Value | Config key |
|---|---|---|
| Qdrant host | `localhost` | `QDRANT_HOST` |
| Qdrant port | `6333` | `QDRANT_PORT` |
| MCP port | `8100` | `BRAIN_PORT` |
| Memory collection | `musubi_memories` | `MEMORY_COLLECTION` |
| Thought collection | `musubi_thoughts` | `THOUGHT_COLLECTION` |
| Embedding model | `gemini-embedding-001` | `EMBEDDING_MODEL` |
| Vector dimension | `3072` | `VECTOR_SIZE` |
| Dedup threshold | `0.92` | `DUPLICATE_THRESHOLD` |

All from `.env` via `config.py` (the single source of truth).

## Tests and tooling

- `make test` — pytest + coverage, 80% target, `server.py` excluded.
- `make lint` — ruff format check + ruff check.
- `make typecheck` — mypy strict.
- `make check` — all three.
- `make dev` — runs streamable-http server locally.
- `make install` — venv setup.
- Mocks: `mock_qdrant` MagicMock with realistic defaults, `mock_embed` patches both `memory.embed_text` and `thoughts.embed_text`.
- Fakes: `FakePoint`, `FakeQueryResult`, `FakeCollectionInfo`.

## Known sharp edges in the POC

These are not bugs but constraints the target design unwinds:

1. **All memory types in one collection** → filtering by `type` works but doesn't scale to new plane semantics.
2. **Single dense vector per point** → no hybrid search, no multi-model support, no Matryoshka.
3. **MCP is the only interface** → can't serve LiveKit or OpenClaw without reimplementing MCP handling.
4. **Dedup is simple cosine threshold** → can miss paraphrased duplicates; no merge semantics beyond content overwrite.
5. **No lifecycle state** → provisional/matured/promoted/etc. all undifferentiated.
6. **No curated/artifact distinction** → everything is "memory."
7. **No auth** → assumes localhost trust.
8. **install.sh is macOS-flavored** (Colima) → needs an Ansible-managed Ubuntu path for the target host profile.
9. **`agent` payload field is untyped** — will become `presence` with a registry in `config/presences.yaml`.
10. **No background jobs** — reflection is synchronous and user-triggered.
