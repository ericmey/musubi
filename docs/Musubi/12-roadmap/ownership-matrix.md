---
title: Ownership Matrix
section: 12-roadmap
tags: [ownership, responsibility, roadmap, section/roadmap, status/complete, type/roadmap]
type: roadmap
status: complete
updated: 2026-04-17
up: "[[12-roadmap/index]]"
reviewed: false
---
# Ownership Matrix

Who owns what. Musubi is (for now) effectively a single-developer project — but "ownership" still matters, because it's a map of where to look when something breaks and how to contribute safely.

## Repos

Musubi is a **single-repo monorepo** per [[13-decisions/0015-monorepo-supersedes-multi-repo]] and [[13-decisions/0016-vault-in-monorepo]]. The 8-repo layout originally proposed in [[13-decisions/0011-canonical-api-and-adapters]] is retired; its interface discipline survives as import-lint rules. The Obsidian architecture vault lives in the same repo at `docs/Musubi/`.

| Repo | Primary owner | Backup | Access | Contents |
|---|---|---|---|---|
| `github.com/ericmey/musubi` | Eric | — | public | Everything: Core, SDK, MCP/Obsidian/CLI adapters, contract tests, compose + Ansible under `deploy/`, *and* the Obsidian architecture vault under `docs/Musubi/`. `main` carries current development; the original POC is archived on the `alpha-archive` branch for history. |

If a second contributor joins, the "backup" column fills in. For now, Eric holds all bus factor.

## Modules within `musubi` (monorepo)

All paths are under `src/musubi/`.

| Module | Role | Notes |
|---|---|---|
| `types/` | Shared pydantic types | Foundation — imported by every other module. `slice-types` lands here first. |
| `api/` | FastAPI routes | Thin; calls into plane modules. The canonical HTTP/gRPC surface. |
| `planes/episodic/` | Episodic | Capture, maturation, demotion. |
| `planes/curated/` | Curated | Mostly reads; vault is the writer. |
| `planes/concept/` | Concept | Synthesis, reinforcement, gates. |
| `planes/artifact/` | Artifacts | Upload, chunking, blob store. |
| `thoughts/` | Thoughts | Send, check, read, history. |
| `retrieve/` | Retrieval | Fast + deep paths, scoring, blending. |
| `lifecycle/` | Lifecycle engine | APScheduler, events, transitions. |
| `vault_sync/` | Vault watcher | Watchdog, echo filter, reconciler. |
| `llm/` | LLM prompts + parsers | Synthesis, rendering, maturation. |
| `embedding/` | TEI / Gemini clients | Dense + sparse encode. |
| `rerank/` | Reranker client | Cross-encoder calls. |
| `collections.py` | Qdrant setup | Collection create, indexes. |
| `auth/` | Auth middleware | Token validation, scope check. |
| `config.py` | Config | Single source of truth for env. |
| `sdk/` | Python client | Imports `types/` only. Never touches storage. |
| `adapters/mcp/` | MCP adapter | Imports `sdk/`. Speaks MCP to agents. |
| `adapters/obsidian/` | Obsidian bridge | Imports `sdk/`. |
| `adapters/cli/` | CLI | Imports `sdk/`. |
| `contract_tests/` | Contract test suite | Black-box API suite; runs in CI against the `api/` impl. |

**Import discipline** (enforced by `ruff`/import-linter in `make check`, not by repo fences):

- `sdk/*` may import `types/*`. Nothing else.
- `adapters/*` may import `sdk/*` and `types/*`. Nothing else.
- `api/*` is the only module allowed to compose `planes/*` + `retrieve/*` + `lifecycle/*`.

Violations fail CI.

## External dependencies

| Dep | Version | Why | Replaceability |
|---|---|---|---|
| Qdrant | 1.15+ | Vector DB | Hard — core to the design. Could swap long-term (Weaviate, Vespa) but a lot of work. |
| FastAPI | latest | API server | Easy — Starlette compatible. |
| pydantic | 2.x | Models | Hard — everywhere. Could move to attrs but pain. |
| APScheduler | latest | Scheduler | Easy — swap for custom/Temporal. |
| watchdog | latest | Filesystem events | Easy — similar libs available. |
| httpx | latest | HTTP client | Easy — requests compatible. |
| TEI | 1.5+ | Embeddings | Medium — could swap to Sentence-Transformers direct. |
| Ollama | 0.4+ | LLM runtime | Easy — vLLM or similar. |
| BGE-M3 | v1 | Dense model | Medium — requires re-embed; see [[11-migration/re-embedding]]. |
| SPLADE++ V3 | v1 | Sparse model | Medium — same. |
| BGE-reranker-v2-m3 | v1 | Reranker | Easy — stateless. |

## What "owner" means here

Owner is:

- First responder for an issue in their module.
- Reviewer of PRs touching it.
- Accountable for its tests, docs, roadmap.
- Can say "no, not yet" on a feature request if it doesn't fit.

Owner is NOT:

- A gate on every line of code.
- Required for an emergency fix at 2am.

For a single-developer shop, ownership is mostly about what I pay attention to this week.

## Adding a contributor

When a second person joins:

1. They pick a module as primary (likely one that needs help).
2. Eric becomes backup on that module; they become backup on one other.
3. Credentials split: both have GitHub admin; 1Password vault shared; ansible-vault password rotated.
4. Code review required on PRs touching each other's primary modules.

## Knowledge transfer materials

When onboarding:

- This vault (sections 01-13).
- A walkthrough session recording (future).
- Pairing on a real task in their primary module.

Don't start them on migration work. Start them on an isolated feature (e.g., a new eval suite entry) to build familiarity.

## Bus-factor mitigation

Today, bus factor = 1. Steps to raise it:

- Record walkthroughs of each module.
- Keep docs current (this vault).
- Write runbooks everyone could follow.
- Pick apprentices or collaborators where it makes sense.
- Keep the architecture simple enough that a well-equipped stranger can onboard.

## Test contract (for this matrix)

This is a living doc, not code. "Tests":

1. Roles are assigned and documented.
2. Every module has an entry.
3. Every external dep has a replaceability note.
4. When a new top-level module under `src/musubi/` is added, this doc is updated in the same PR.
