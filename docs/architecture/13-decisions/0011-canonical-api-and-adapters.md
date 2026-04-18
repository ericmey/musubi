---
title: "ADR 0011: Canonical API + Independent Adapter Repos"
section: 13-decisions
tags: [adapters, adr, api, architecture, section/decisions, status/partially-superseded, type/adr]
type: adr
status: partially-superseded
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: true
superseded-by: "[[13-decisions/0015-monorepo-supersedes-multi-repo]] (repo-layout portion only)"
---
# ADR 0011: Canonical API + Independent Adapter Repos

**Status:** partially superseded — interface discipline stands; repo layout superseded by [[13-decisions/0015-monorepo-supersedes-multi-repo]] on 2026-04-17.
**Date:** 2026-03-19
**Deciders:** Eric

> **Note (2026-04-17):** The **interface decision** (canonical HTTP/gRPC API, adapters talk only to that API via the SDK, adapters never touch storage directly) remains in force. The **repository-layout prescription** below (8 separate repos) is **superseded** by [[13-decisions/0015-monorepo-supersedes-multi-repo]] — all components now live in a single monorepo with import-lint enforcing the same discipline. Read this ADR for the *why* of the interface; read 0015 for the current repo layout.

## Context

The POC is one process: a FastMCP server that both implements MCP tools and mutates Qdrant directly. Easy to ship, but wrong long-term:

- MCP isn't the only surface. LiveKit has its own agent tool interface; OpenClaw uses HTTP; future integrations may speak gRPC or something else entirely.
- Adapters evolve on different cadences (MCP spec changes with Anthropic; LiveKit with LiveKit; OpenClaw with us). Cohabiting them in one repo makes every release a coordination problem.
- Business logic deserves to be testable independent of any transport.

The answer is a *core* that owns the domain and a set of *adapters* that translate to external protocols. This is not a new insight; it's how ports-and-adapters / hexagonal architecture has been taught for a decade.

## Decision

**Musubi exposes a single canonical HTTP/gRPC API. Every external protocol (MCP, LiveKit, OpenClaw) is an independent adapter repo that speaks to that canonical API.**

Layout:

```
musubi-core          # FastAPI + gRPC. The SoR for business logic.
musubi-client        # Python SDK that wraps HTTP/gRPC.
musubi-mcp-adapter   # MCP server that imports musubi-client + exposes MCP tools.
musubi-livekit-adapter   # LiveKit agent factory that imports musubi-client.
musubi-openclaw-adapter  # OpenClaw-facing HTTP bridge that imports musubi-client.
musubi-contract-tests    # Black-box suite that exercises any canonical API implementation.
musubi-infra         # Ansible / compose / ops.
musubi-vault         # The vault (Obsidian files, git).
```

Rules:

- Adapters **never** touch Qdrant, sqlite, or the vault directly. They go through `musubi-client` which hits the canonical API.
- Any behavior an adapter wants must first be a canonical API endpoint. If the adapter needs something new, we add it to Core, publish a client release, then consume it in the adapter.
- Every adapter runs the contract test suite against its deployment in CI.
- Adapters can own *their own* concerns (MCP OAuth negotiation, LiveKit session bookkeeping) that Core doesn't care about.

## Alternatives

**A. Monorepo with a single release.** Simpler to start; painful at scale. Adapters evolve on different cadences. Rejected.

**B. Core + adapters in one repo but separate packages.** A middle ground. Rejected because it encourages tight coupling ("I'll just reach into core's private module").

**C. No adapters; adapters are built-in to Core.** Rejected — Core becomes a grab-bag of protocol code; releases get heavy.

**D. RPC-only (gRPC) canonical API, HTTP is an adapter itself.** Considered. Rejected because HTTP is the closest to "universal" and most clients speak it natively; gRPC is a first-class alternative but not the only one.

## Consequences

- Eight repos to manage. Python packaging (`musubi-client` as a wheel) mandatory.
- Contract test suite ([[07-interfaces/contract-tests]]) becomes the shared quality gate. Changes to the API propagate through the suite, which fails adapters until they update.
- Versioning is per-repo with compatibility commitments ([[11-migration/schema-evolution]]).
- Release cadence: Core + client most frequently; adapters at their own pace, with a pinned client range.
- Onboarding a new adapter is a well-trodden path: clone adapter template, depend on `musubi-client`, implement the protocol translation, wire up contract tests.

Trade-offs:

- More repos, more releases, more coordination than a monorepo. Worth it because the interfaces are stable boundaries.
- Small features that touch Core + an adapter require two releases. Almost always acceptable.

## Links

- [[07-interfaces/canonical-api]]
- [[07-interfaces/index]]
- [[07-interfaces/contract-tests]]
- [[12-roadmap/ownership-matrix]]
