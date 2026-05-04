---
title: Decisions
section: 13-decisions
tags: [adr, decisions, index, section/decisions, status/complete, type/adr]
type: adr
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Decisions

Architecture Decision Records for Musubi. Each ADR captures a specific decision, the context, alternatives considered, and consequences.

## Format

Lightweight ADR:

```
Title
Status: [proposed|accepted|superseded|deprecated]
Date: YYYY-MM-DD
Deciders: Eric (+ anyone else)
---
Context
Decision
Alternatives
Consequences
```

We keep them short. The goal is to remember *why* later, not to re-litigate.

## ADRs by status (live)

```dataview
TABLE WITHOUT ID
  file.link AS "ADR",
  status AS "Status",
  date AS "Date",
  supersedes AS "Supersedes",
  superseded-by AS "Superseded by"
FROM "13-decisions"
WHERE type = "adr"
SORT file.name ASC
```

## Static index

- [[13-decisions/0001-three-plane-architecture]] — Three planes (episodic / curated / concept) + artifacts.
- [[13-decisions/0002-planes-not-tiers]] — Planes are orthogonal, not a hierarchy.
- [[13-decisions/0003-obsidian-as-sor]] — Obsidian vault is source of truth for curated knowledge.
- [[13-decisions/0004-no-knowledge-graph-v1]] — No knowledge graph in v1. Reconsider later.
- [[13-decisions/0005-hybrid-search]] — Hybrid dense + sparse + reranker from day one.
- [[13-decisions/0006-pluggable-embeddings]] — Named vectors + embedding provider abstraction.
- [[13-decisions/0007-no-silent-mutation]] — Every state change emits a LifecycleEvent.
- [[13-decisions/0008-no-relational-store]] — Qdrant + sqlite only; no Postgres.
- [[13-decisions/0009-artifact-metadata-in-qdrant]] — Artifact metadata lives in Qdrant, not a separate store.
- [[13-decisions/0010-single-host-v1]] — v1 is single-host, no HA.
- [[13-decisions/0011-canonical-api-and-adapters]] — Canonical API + independent adapter repos. **Partially superseded** by 0015 on the repo-layout portion; interface discipline stands.
- [[13-decisions/0012-local-inference]] — Local inference on dedicated GPU, not hosted APIs.
- [[13-decisions/0013-api-spec-authoring]] — How the canonical API spec is authored.
- [[13-decisions/0014-kong-over-caddy]] — Kong API Gateway on a dedicated VM replaces Caddy on the Musubi host.
- [[13-decisions/0015-monorepo-supersedes-multi-repo]] — Single monorepo for core + SDK + adapters; supersedes 0011's repo split.
- [[13-decisions/0023-qdrant-version-bump-to-1-17]] — Qdrant pin moves from 1.15 to 1.17.1 to match the pre-staged host install.
- [[13-decisions/0024-kong-deferred-for-musubi-v1]] — Kong integration deferred for v1; first deploy is VLAN-internal only. 0014 remains the forward target.
- [[13-decisions/0025-lifecycle-runner-without-apscheduler]] — Lifecycle worker ships as an asyncio tick-loop instead of pulling in APScheduler; revisit when we need persisted-jobstore or sub-minute cron semantics.
- [[13-decisions/0026-release-please-for-versioning]] — release-please drives version bumps + tag cutting from conventional commits on `v2`; tag push triggers the signed GHCR publish.
- [[13-decisions/0032-agent-tools-canonical-surface]] — Five-tool canonical agent surface (`musubi_recent`, `musubi_search`, `musubi_get`, `musubi_remember`, `musubi_think`) every adapter implements identically; cross-modal default for recent/search.
- [[13-decisions/sources]] — Public sources that informed these decisions.
- [[13-decisions/template-weights-change]] — Template ADR for retrieval scoring weight changes.

## How to add an ADR

1. Copy an existing file; name it `NNNN-short-slug.md` where NNNN is next sequential.
2. Write it in a single sitting — if you can't, you haven't decided yet.
3. Link from this index.
4. Commit + push.

## Superseding

If a decision changes:

- Don't delete the old ADR.
- Mark its `Status: superseded` + link to the new one.
- New ADR references the old in "context."

Decisions are a record of reasoning, not a style guide. Old reasoning matters.
