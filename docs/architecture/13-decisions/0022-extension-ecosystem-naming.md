---
title: "ADR 0022: Extension ecosystem — non-Python integrations live in sibling `<system>-musubi` repos; Python integrations live in-monorepo as workspace subpackages"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-19
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr, monorepo, ecosystem, adapters, extensions, packaging]
updated: 2026-04-19
up: "[[13-decisions/index]]"
reviewed: false
extends: "[[13-decisions/0015-monorepo-supersedes-multi-repo]]"
---

# ADR 0022: Extension ecosystem — non-Python integrations live in sibling `<system>-musubi` repos; Python integrations live in-monorepo as workspace subpackages

**Status:** accepted
**Date:** 2026-04-19
**Deciders:** Eric

## Goal

Musubi should have **as many official interfaces as tools that might use it**, each one Musubi-sanctioned and consuming the canonical API. The more official surfaces exist, the less temptation there is for any external service to bypass the API and reach into Qdrant (or any other storage) directly. Interface proliferation is a feature; the *locations* of those interfaces need a rule that scales.

## Context

[[13-decisions/0015-monorepo-supersedes-multi-repo]] established that Musubi is a single Python monorepo at `github.com/ericmey/musubi` with all components under `src/musubi/`. ADR-0015 did not take a position on components whose **implementation language is not Python** — an OpenClaw browser extension in TypeScript, a future Obsidian plugin in TS/Electron, a future VS Code extension in Node, a hypothetical Rust TUI, etc. The question surfaced 2026-04-19 when `slice-adapter-openclaw` needed its owns_paths reconciled to post-monorepo layout and it became obvious that TypeScript source cannot live inside a Python package at `src/musubi/adapters/openclaw/`.

A related but separable concern surfaced in parallel: **how do Python integrations whose runtime is not the Musubi deploy target get distributed?** Example: the LiveKit adapter is Python but runs inside a LiveKit Agents worker process deployed on a different machine. Copying adapter source into every consumer's repo is a DX disaster; installing the full Musubi server wheel into a LiveKit worker pulls ~150 MB of deps it never touches.

An earlier draft proposed a **runtime criterion** (external-runtime components move external regardless of language) to unify the two concerns. That criterion was rejected after weighing distribution mechanics — **uv workspace subpackages** solve the Python distribution problem cleanly *without* moving source out of the monorepo. The language criterion is sharper and correctly partitions the real pain point: TypeScript-in-a-Python-monorepo is genuinely broken (different toolchain, different package manager, different CI needs); Python-in-a-Python-monorepo-that-happens-to-be-installed-elsewhere is not broken.

## Decision

### Rule: source location is determined by implementation language.

**If the implementation is Python,** source lives in the Musubi monorepo under `src/musubi/adapters/<name>/` (or future workspace subpackage `packages/musubi-<name>/`). When the component is installed into a runtime other than Musubi's deploy target (e.g., a LiveKit Agents worker), it's published as its own wheel — see §Distribution below.

**If the implementation is not Python,** source lives in a separate sibling repository named:

```
github.com/ericmey/<system>-musubi
```

where `<system>` names the host system the component targets.

### Worked examples

| Component | Language | Source location | Install into consumer |
|---|---|---|---|
| Musubi server | Python | `src/musubi/` → future `packages/musubi-server/` | Operator deploys on `musubi.mey.house` |
| Musubi Python SDK | Python | `src/musubi/sdk/` → future `packages/musubi-client/` | `pip install musubi-client` |
| MCP server (remote HTTP + SSE) | Python | `src/musubi/adapters/mcp/` → future `packages/musubi-mcp/` | Deploys with Musubi; Kong-fronts it |
| MCP local stdio plugin *(if ever built)* | Python | `packages/musubi-mcp-stdio/` *(future subpackage)* | `pip install musubi-mcp-stdio` on agent host |
| LiveKit adapter | Python | `src/musubi/adapters/livekit/` → future `packages/musubi-livekit/` | `pip install musubi-livekit` into LiveKit worker |
| OpenClaw browser extension | TypeScript | **`github.com/ericmey/openclaw-musubi`** | Chrome Web Store / Firefox Add-ons / sideload |
| Obsidian plugin *(future, if built)* | TypeScript | **`github.com/ericmey/obsidian-musubi`** | Obsidian Community Plugins |
| VS Code extension *(future, if built)* | TypeScript | **`github.com/ericmey/vscode-musubi`** | VS Code Marketplace |

### Naming rationale for external repos

The `<system>-musubi` order reads as "the Musubi integration for `<system>`," framing the component as a citizen of the host system's ecosystem first, a Musubi consumer second. That reflects the reality — the component belongs to the host system's packaging, release, and review process (browser-extension store review, Obsidian Community Plugins process, VS Code Marketplace) — and makes the separate-cadence expectation legible from the name alone. It inverts ADR-0011's `musubi-<system>-adapter` naming, which framed these as Musubi pieces that knew about hosts; that framing stopped making sense once the component ships on a host-system schedule independent of Musubi's.

## Distribution: uv workspace subpackages for Python

Today Musubi is a **single-wheel** repo: one `pyproject.toml` at the root publishes one wheel (`musubi`). Installing `musubi` into a LiveKit worker pulls the full server — ~150 MB of deps the worker never touches.

To let external Python consumers install only what they need, Musubi will restructure into a **uv workspace** publishing multiple wheels from the same repo:

```
musubi/                                     ← repo root
├── pyproject.toml                          ← workspace root (publishes nothing)
├── packages/
│   ├── musubi-server/                      ← the server Musubi deploys
│   │   ├── pyproject.toml                  ← publishes "musubi" wheel
│   │   └── src/musubi/                     ← api, planes, retrieve, lifecycle, ingestion
│   ├── musubi-client/                      ← the SDK
│   │   ├── pyproject.toml                  ← publishes "musubi-client" wheel
│   │   └── src/musubi/sdk/
│   ├── musubi-livekit/                     ← LiveKit adapter
│   │   ├── pyproject.toml                  ← publishes "musubi-livekit" wheel
│   │   └── src/musubi/adapters/livekit/
│   └── musubi-mcp/                         ← MCP server(s)
│       ├── pyproject.toml                  ← publishes "musubi-mcp" wheel
│       └── src/musubi/adapters/mcp/
```

Each `pyproject.toml` declares its own version, deps, and target. Python's **PEP 420 implicit namespace packages** let multiple wheels contribute to the same `musubi.*` namespace — imports like `from musubi.adapters.livekit import SlowThinker` work unchanged regardless of whether the consumer has one wheel installed or many.

### Consumer install path (LiveKit worker example)

```toml
# In the LiveKit worker's pyproject.toml:
[project]
dependencies = [
  "livekit-agents>=1.0",
  "musubi-livekit>=0.1.0",
]
```

```bash
uv add musubi-livekit   # from PyPI once published
# or, pre-PyPI:
uv add "git+https://github.com/ericmey/musubi.git@musubi-livekit-v0.1.0#subdirectory=packages/musubi-livekit"
```

Either path installs **only** `musubi-livekit` + its transitive deps (`musubi-client`, `httpx`, `pydantic`, plus `livekit-agents` that the worker requested). The full Musubi server wheel never lands on the worker machine. Total install ~15 MB.

### Versioning

Each subpackage releases on its own cadence:

- `musubi` (server) versions independently.
- `musubi-client` (SDK) versions when the canonical API changes.
- `musubi-livekit`, `musubi-mcp` pin `musubi-client>=X.Y,<X.(Y+1)`; release when their own code changes or when the SDK requires it.

Git tags scope per-subpackage: `musubi-v1.2.0`, `musubi-livekit-v0.1.0`, `musubi-mcp-v0.3.2`. CI builds and publishes the specific wheel whose tag was pushed.

### Timing

**The restructure is not part of this ADR.** It's queued as [[_slices/slice-ops-workspace-packaging]] for when demand materialises — LiveKit worker wants a thin `pip install musubi-livekit`, or the SDK wants independent PyPI publishing, or similar. Until then, consumers can use the git-URL-with-subdirectory install pattern against the current `src/musubi/adapters/<name>/` tree. The ADR establishes the destination; the slice executes the move.

## Consequences

### Positive

- **Language-appropriate tooling per repo.** Musubi's `make check` stays Python-only. TS repos keep their own pnpm/tsc/vitest stack.
- **Host-system-appropriate release cadence.** TS repos ship on browser-store / marketplace schedules. Python subpackages ship on per-package semver.
- **Clean DX for external Python consumers.** LiveKit-worker dev runs `uv add musubi-livekit`, imports `from musubi.adapters.livekit`. No Musubi source in their repo. Identical to consuming any pip-installable library.
- **Clean DX for external TS consumers.** OpenClaw dev clones `openclaw-musubi`, uses pnpm, publishes to the browser store. No Python toolchain touches them.
- **Atomic cross-cutting changes for Python.** Changing a plane type + SDK + LiveKit adapter lands in one Musubi PR. The monorepo wins where it wins; external repos handle what must be external.
- **Interface proliferation stays sanctioned.** Every external consumer, regardless of repo location, goes through the canonical API. No direct Qdrant access. ADR-0011's interface-discipline decision is preserved.
- **Ecosystem is visible at the namespace level.** `github.com/ericmey/*-musubi` enumerates the non-Python surfaces at a glance.

### Negative

- **Cross-repo changes for non-Python integrations are multi-PR.** Breaking an API field forces an OpenClaw repo update. Accepted because (a) host-release cadence is independent anyway, (b) most API changes are additive.
- **Workspace restructure is a real migration.** Moving `src/musubi/` into `packages/musubi-server/src/musubi/` + carving subpackages is real operator work. Deferred to `slice-ops-workspace-packaging`. Current monolithic layout works fine until first consumer demands thin install.
- **Naming-convention discipline depends on the operator.** No automation stops someone from creating `musubi-openclaw` instead of `openclaw-musubi`. Mitigation: this ADR is the canonical record.
- **Adapter specs stay in Musubi even for non-Python components.** `docs/architecture/07-interfaces/openclaw-adapter.md` is Musubi's spec of the contract; implementation in the external repo consumes that spec. Readers need to know to look at two places. Mitigation: each spec names the implementing repo in its opening paragraph.

### Neutral

- **ADR-0011 naming (`musubi-<system>-adapter`) is superseded on repo-name axis only** — the interface-discipline decisions stand.
- **Transport choice (HTTP/SSE, gRPC, h2c, unix sockets) for in-VLAN consumers** is orthogonal — tracked as a future backlog ADR when the first concrete in-VLAN Python consumer materialises.
- **`slice-adapter-livekit` (done 2026-04-19, PR #96) and `slice-adapter-mcp` (done 2026-04-19, PR #95)** stay in-monorepo at their current paths. Distribution to external runtimes is handled by the future workspace restructure, not by moving source.

## Alternatives considered

### A) Broad framing — runtime criterion (rejected earlier today)

External repos for every component whose runtime is not Musubi's deploy target, Python or otherwise. Under this rule, LiveKit adapter moves to `livekit-musubi`, stdio MCP moves to `mcp-musubi`, only the HTTP MCP server stays in-monorepo.

**Rejected.** Forces an avoidable repo split for Python components whose distribution problem is cleanly solved by uv workspace subpackages. The extra bootstrap cost (multiple external repos each with its own slice machinery + CI + release pipeline) outweighs the coordination cost of keeping Python unified. Also: PR #96 (slice-adapter-livekit) landed in-monorepo 2026-04-19; retiring it immediately after merging would discard working code, and the argument for retirement (distribution to external runtimes) turned out to be solvable in-place.

### B) Polyglot monorepo

Top-level `extensions/` directory in Musubi for TS extensions; pnpm workspace for TS co-existing with uv workspace for Python.

**Rejected.** Pays polyglot-tooling cost on every PR (bigger CI matrix, two package managers, unrelated dep surfaces) for a benefit that's mostly illusory — TS components ship on host-system-store schedules that make atomic cross-language merges meaningless. Blurs the "Musubi is a Python service" mental model for operators and agents.

### C) Python SDK on npm (first-party TS SDK)

Publish `@musubi/client` alongside `musubi-client` so TS consumers get an official client instead of generating types from `openapi.yaml`.

**Deferred.** Worth doing if/when 2+ active TS consumers exist. Premature for one (`openclaw-musubi`). Until then, `openapi-typescript` against `openapi.yaml` gives strongly-typed HTTP calls without maintaining a second SDK surface.

### D) Each external repo as a git submodule inside Musubi

Check out `openclaw-musubi` as a submodule under `external/openclaw/` so `git clone --recurse-submodules` gets the whole ecosystem.

**Rejected.** Submodules are a well-known DX footgun; recursive-clone behaviour is easy to get wrong; submodule commits are harder to review than merge commits.

## Mechanics triggered by this ADR (this PR)

1. **`slice-adapter-openclaw` (Issue #5) is retired.** TypeScript browser extension moves to `github.com/ericmey/openclaw-musubi` (future operator action outside this monorepo). Slice file flips to `status: retired` with a superseded-by pointer. Issue #5 closed with a comment linking this ADR.
2. **No change to `slice-adapter-livekit` (done) or `slice-adapter-mcp` (done).** Both stay in-monorepo at their current paths.
3. **`07-interfaces/openclaw-adapter.md` updated** — repo name `musubi-openclaw-adapter` → `openclaw-musubi`; opening paragraph names the implementing repo. Commit carries `spec-update:` trailer.
4. **`_tools/check.py`** gains `"retired"` as a valid slice status so retired slices don't trip vault hygiene.
5. **`slice-ops-workspace-packaging` stubbed** — new slice file in `ready` status tracking the future uv-workspace restructure. Not claimable until a concrete consumer needs thin installs.
6. **Backlog Issue filed** — future ADR for gRPC / h2c / Unix-socket transport. Carries `area:api`, `priority:low`, `type:followup` labels.

## References

- [[13-decisions/0011-canonical-api-and-adapters]] — original 8-repo interface-discipline ADR; superseded on repo-layout by 0015 and this ADR.
- [[13-decisions/0015-monorepo-supersedes-multi-repo]] — the Python-monorepo decision this ADR extends (language criterion is the extension).
- [[13-decisions/0016-vault-in-monorepo]] — precedent for "extend 0015, don't supersede".
- [[13-decisions/0021-mcp-server-library]] — Nyla's sibling ADR adopting Anthropic's `mcp` package (unrelated to this ADR; shares nothing but the session date).
- [[07-interfaces/mcp-adapter]] — MCP spec; implementation in-monorepo.
- [[07-interfaces/livekit-adapter]] — LiveKit spec; implementation in-monorepo.
- [[07-interfaces/openclaw-adapter]] — OpenClaw spec (contract only; implementation in `openclaw-musubi`).
- [[_slices/slice-adapter-openclaw]] — retired by this ADR.
- [[_slices/slice-adapter-mcp]] — done; stays in-monorepo.
- [[_slices/slice-adapter-livekit]] — done; stays in-monorepo.
- [[_slices/slice-ops-workspace-packaging]] — stubbed by this ADR.
