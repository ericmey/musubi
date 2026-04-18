---
title: "ADR 0015: Monorepo supersedes multi-repo adapter layout"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-17
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr, monorepo, packaging]
updated: 2026-04-18
up: "[[13-decisions/index]]"
reviewed: true
supersedes: "[[13-decisions/0011-canonical-api-and-adapters]] (repo layout only)"
superseded-by: "[[13-decisions/0016-vault-in-monorepo]] (Neutral → vault-stays-separate clause only; repo-layout decision stands)"
---

# ADR 0015: Monorepo supersedes multi-repo adapter layout

**Status:** accepted
**Date:** 2026-04-17
**Deciders:** Eric

## Context

[[13-decisions/0011-canonical-api-and-adapters]] called for an 8-repo split: `musubi-core`, `musubi-client`, `musubi-mcp-adapter`, `musubi-livekit-adapter`, `musubi-openclaw-adapter`, `musubi-contract-tests`, `musubi-infra`, `musubi-vault`. That decision was about **interface discipline** (canonical API, adapters never touch storage directly) and was sound on that front. It additionally prescribed a *repository layout* that, on reflection, doesn't pay for itself at the current scale:

- One operator, one deployment target (the single host in [[08-deployment/host-profile]]).
- Adapters are not released on independent cadences to external consumers; they ship when Musubi ships.
- The "adapter wants a new endpoint" workflow (land in Core, cut a client release, bump the adapter) is high ceremony for a solo operator — it turns every cross-cutting change into three PRs.
- Contract tests and the SDK are easier to keep honest when they live next to the code they test.
- Eight repos × per-repo CI, release, dependency management is real recurring overhead.

The **interface** decision from 0011 stands: one canonical HTTP/gRPC API, adapters talk only to that API via the SDK, adapters never reach into storage. What changes is where the code lives.

## Decision

**Musubi is a single monorepo at `github.com/ericmey/musubi`.** All components live under `src/musubi/` on the `v2` branch (merging to `main` at feature parity):

```
src/musubi/
  types/            shared pydantic types (slice-types)
  planes/           episodic, curated, artifact, concept
  retrieve/         scoring, hybrid, fast/deep path
  lifecycle/        maturation, synthesis, promotion
  api/              FastAPI + OpenAPI/proto (canonical API)
  sdk/              Python client — imports from types/, never storage
  adapters/
    mcp/            MCP adapter — imports sdk/, never touches storage
    obsidian/       Obsidian plugin bridge
    cli/            CLI
  contract_tests/   black-box suite run against any canonical API impl
tests/              unit + integration tests mirror src/musubi/ layout
deploy/             ansible, docker-compose, kong route stubs
```

Infrastructure (`musubi-infra` in 0011) stays in this repo under `deploy/`. The vault (`musubi-vault` in 0011) **was** proposed as separate — see the Neutral section below. That part of this ADR was reversed on 2026-04-18 by [[13-decisions/0016-vault-in-monorepo]], which moves the vault into this repo at `docs/architecture/` for multi-agent ergonomics. The rest of 0015 (single repo for core + SDK + adapters + infra + contract tests, import discipline as lint instead of repo fences) stands unchanged.

**Import discipline remains intact.** The monorepo makes it *easier* to enforce, not harder:

- `musubi.sdk.*` may import `musubi.types.*`; may not import `musubi.planes.*`, `musubi.retrieve.*`, `musubi.lifecycle.*`, `musubi.api.*`.
- `musubi.adapters.*` may import `musubi.sdk.*` and `musubi.types.*`; may not import anything else.
- `musubi.api.*` is the only module allowed to import both `musubi.planes.*` and `musubi.retrieve.*` / `musubi.lifecycle.*`.
- Enforced by `ruff` import-linter rules (or equivalent static check) as part of `make check`.

## Consequences

### Positive

- **One PR, one release.** A cross-cutting change (new endpoint + SDK method + adapter consumer) lands atomically. No three-way version coordination.
- **Contract tests co-located with code.** The suite in [[07-interfaces/contract-tests]] runs against the API in the same CI job, not an external release train.
- **One CI config, one dependency tree.** `uv` manages a single resolved environment for the whole project.
- **Atomic refactors are cheap.** Renaming a type propagates across Core, SDK, and adapters in the same commit.
- **Simpler onboarding.** One clone, one `make install`, the whole system.

### Negative

- **No "reach into core's privates" guardrail from repo boundaries.** Interface discipline now depends on lint rules and review, not on "that code is in another repo so I literally can't." Mitigation: encode the import graph in lint and fail CI on violation.
- **Adapter releases are tied to Core releases.** If a third-party adapter consumer later needs an independent cadence, we revisit (likely extract that adapter to its own repo at that point, not rearchitect the whole layout preemptively).
- **Repo grows.** Not a real problem at this scale; git handles monorepos with many thousands of files without issue.

### Neutral

- **Vault stays separate.** It has human-editor tooling (Obsidian, Breadcrumbs, Bases) that doesn't belong in a Python monorepo.
- **Infra lives in-repo.** `deploy/` holds Ansible roles, compose, and Kong route stubs — easier to keep in sync with code than in a sibling repo.

## Alternatives considered

### A) Keep 0011's 8-repo layout

Rejected. Coordination cost exceeds the benefit of repo-level boundaries at current scale. Interface discipline is better enforced by lint than by repository fences.

### B) Split `musubi-core` + `musubi-sdk` + `musubi-adapters` (3 repos)

Rejected. Still pays two-thirds of the coordination cost for the same interface-discipline benefit we get from lint. If we ever externally publish the SDK on PyPI with independent versioning, we extract it then — not preemptively.

### C) Monorepo with separate installable wheels per component

Considered. Possible future move if we ever need to publish `musubi-sdk` or `musubi-mcp-adapter` as independent PyPI packages. Today the whole thing installs as one `musubi` wheel; split when there's a consumer that needs it.

## References

- [[13-decisions/0011-canonical-api-and-adapters]] — interface decision stands; repo-layout portion is superseded by this ADR.
- [[12-roadmap/ownership-matrix]] — repo column collapses to a single `musubi` entry.
- [[07-interfaces/canonical-api]], [[07-interfaces/contract-tests]] — unchanged by this ADR; interface discipline is identical.
- `github.com/ericmey/musubi` `v2` branch — scaffold committed 2026-04-17 reflects this layout.
