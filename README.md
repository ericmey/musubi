# Musubi (結び) — v2

Shared memory + knowledge plane for a small-team AI agent fleet. Three planes:
**episodic**, **curated**, **source-artifact**; a bridge layer of **synthesised
concepts**; a lifecycle engine; a canonical HTTP/gRPC API.

The authoritative design lives in the Obsidian vault at
`~/Vaults/musubi/`. Start at `00-index/reading-tour.md` or the
`_slices/` registry.

This repo is being rebuilt slice-by-slice per that design. The `main` branch
still holds v1 (the FastMCP + Gemini POC); v2 development happens on this `v2`
branch and will merge to `main` when it reaches feature parity.

**Monorepo.** Core, SDK, adapters (MCP, Obsidian plugin, CLI), and lifecycle
worker all live in this one repo under `src/musubi/`. No per-component repo
split. See ADR 0015 in the vault.

## Status

| Slice | Status |
|---|---|
| `slice-types` (pydantic foundation) | in progress — first cut landed 2026-04-17 (MusubiObject/MemoryObject, the 5 concrete object types + ArtifactRef, LifecycleEvent + transition table, Result[T, E]; 110 tests passing) |
| everything downstream | waiting for slice-types to mark `done` |

## Dev setup

Requires **Python 3.12** and [**uv**](https://docs.astral.sh/uv/).

```bash
make install        # uv sync --extra dev
make check          # fmt + lint + typecheck + test
```

## Layout

```
src/musubi/               importable package — all slices land here
  types/                  shared pydantic types (slice-types)
  planes/                 episodic, curated, artifact, concept (later slices)
  retrieve/               scoring, hybrid, fast/deep path (later slices)
  lifecycle/              maturation, synthesis, promotion (later slices)
  api/                    FastAPI + OpenAPI/proto (later slice)
  sdk/                    Python client (later slice)
  adapters/               mcp/, obsidian/, cli/ (later slices)

tests/                    tests mirror src/musubi/ layout exactly
```

## Slice discipline

Every PR realises one slice (or a clean part of one). Each slice has a **Test
Contract** in its spec. Write tests first; code follows. See
`00-index/agent-guardrails.md` in the vault.

## Why v2

v1 was a single-plane FastMCP server backed by Gemini and a single Qdrant
collection. v2 is the full three-plane architecture with local inference,
named-vector hybrid retrieval, a proper lifecycle engine, and the Obsidian
vault as the curated-plane store of record. See
`13-decisions/` ADRs in the vault for the load-bearing choices.
