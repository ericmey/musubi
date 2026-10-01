---
title: Decisions
section: 13-decisions
tags: [adr, decisions, index, section/decisions, status/complete, type/adr]
type: adr
status: complete
updated: 2026-10-01
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
Deciders: Admin (+ anyone else)
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
- [[13-decisions/0012-local-inference]] — Local inference on dedicated GPU, not hosted APIs. **Partially superseded** by 0019 (model), 0043 (lifecycle LLM placement) and 0045 (TEI ownership).
- [[13-decisions/0013-api-spec-authoring]] — How the canonical API spec is authored.
- [[13-decisions/0014-kong-over-caddy]] — Kong API Gateway as the forward gateway target; implementation deferred by 0024.
- [[13-decisions/0015-monorepo-supersedes-multi-repo]] — Single monorepo for core + SDK + adapters; supersedes 0011's repo split.
- [[13-decisions/0016-vault-in-monorepo]] — Obsidian vault lives in the monorepo at `docs/Musubi/`.
- [[13-decisions/0017-watchdog-for-vault-fs-watcher]] — Use `watchdog` for the vault filesystem watcher.
- [[13-decisions/0018-ruamel-yaml-for-format-preserving-frontmatter]] — Use `ruamel.yaml` for format-preserving frontmatter.
- [[13-decisions/0019-qwen-on-musubi-gpu-phase-1]] — Qwen 3 4B co-located on the Musubi GPU (Phase 1). **Superseded** by 0043.
- [[13-decisions/0020-python-multipart-for-fastapi-uploads]] — Use `python-multipart` for FastAPI `multipart/form-data` uploads.
- [[13-decisions/0021-mcp-server-library]] — Use Anthropic's `mcp` package for the MCP server.
- [[13-decisions/0022-extension-ecosystem-naming]] — Non-Python integrations live in sibling repos; Python integrations in-monorepo. Python SDK and LiveKit location superseded by 0046.
- [[13-decisions/0023-qdrant-version-bump-to-1-17]] — Qdrant pin moves from 1.15 to 1.17.1 to match the pre-staged host install.
- [[13-decisions/0024-kong-deferred-for-musubi-v1]] — Kong integration deferred for v1; first deploy is VLAN-internal only. 0014 remains the forward target.
- [[13-decisions/0025-lifecycle-runner-without-apscheduler]] — Lifecycle worker ships as an asyncio tick-loop instead of pulling in APScheduler; revisit when we need persisted-jobstore or sub-minute cron semantics.
- [[13-decisions/0026-release-please-for-versioning]] — release-please drives version bumps + tag cutting from conventional commits on `main`; tag push triggers the signed GHCR publish.
- [[13-decisions/0027-rate-limit-per-bucket]] — Per-bucket rate limits, not global per-second rates.
- [[13-decisions/0028-retrieve-2seg-namespace-crossplane]] — 2-segment namespace for cross-plane retrieve, strict scope fanout.
- [[13-decisions/0029-plane-aligned-endpoint-paths]] — Plane-aligned endpoint paths for v1.0.
- [[13-decisions/0030-agent-as-tenant]] — Tenant is the agent; presence is the channel (`<agent>/<channel>/<plane>`).
- [[13-decisions/0031-retrieve-wildcard-namespace]] — Wildcard namespace segments for tenant-wide retrieve.
- [[13-decisions/0032-agent-tools-canonical-surface]] — Five-tool canonical agent surface (`musubi_recent`, `musubi_search`, `musubi_get`, `musubi_remember`, `musubi_think`) every adapter implements identically; cross-modal default for recent/search.
- [[13-decisions/0033-centralize-observability-on-shiori]] — Keep only the local Prometheus scrape (plus node-exporter) on the Musubi host; visualization, alerting and traces move to a central observability host.
- [[13-decisions/0034-context-pack-api]] — Add `/v1/context` as the deployed ranked context-pack surface for essence alignment.
- [[13-decisions/0035-additive-api-contract-ret003-wire]] — RET-003 ranked vs recent retrieve wire shape. **Partially superseded** by DATA-001 Phase 2 (corrupt-source rule, ranked reads).
- [[13-decisions/0036-artifact-committed-generation-indexing]] — Committed-generation artifact indexing (C4 / ART-001).
- [[13-decisions/0037-grapheme-safe-truncation-dependency]] — Grapheme-safe truncation dependency.
- [[13-decisions/0038-network-protect-read-only-ops-endpoints]] — Keep health, status, and metrics bearer-unauthenticated behind a testable trusted-network boundary.
- [[13-decisions/0039-durable-client-idempotency-receipts]] — Durable client idempotency receipts.
- [[13-decisions/0040-durable-operation-evidence-and-legacy-resolution]] — Durable operation evidence and legacy resolution. **Proposed**; the §6 receipt-audit endpoint has shipped.
- [[13-decisions/0041-truthful-hybrid-channel-controls]] — Replace dead numeric hybrid weights with explicit channel booleans while preserving unweighted server-side RRF.
- [[13-decisions/0042-escrow-backed-episodic-retraction]] — Escrow exact original episodic bytes as a stored-unindexed artifact before a bounded, evidence-backed, non-reembedding retraction.
- [[13-decisions/0043-lifecycle-llm-openai-compatible-endpoint]] — Lifecycle LLM via an OpenAI-compatible endpoint; supersedes 0019.
- [[13-decisions/0044-additive-reranker-degradation-causes]] — Preserve `reranker_failed` while adding bounded cause detail and separate cause telemetry.
- [[13-decisions/0045-authenticated-shared-inference-services]] — Extract TEI into an independently managed, authenticated shared-inference boundary without publishing raw model ports.
- [[13-decisions/0046-standalone-python-sdk-and-livekit]] — Extract the Python SDK and LiveKit callback adapter into standalone packages while forwarding the old import path.
- [[13-decisions/ADR-auth-boundary-consolidation]] — Consolidated auth boundary: SEC-002/003/004 + IDEM-001.
- [[13-decisions/c6-lifecycle-durability-options]] — C6: lifecycle audit durability, Option A (durable-on-accept).
- [[13-decisions/c6b-lifecycle-atomicity-design]] — C6b: lifecycle Qdrant↔SQLite atomicity via a durable-intent outbox + coordinator.
- [[13-decisions/c6b-phase1-source-cut-plan]] — C6b Phase-1 source cut plan and authoritative Pending contract.
- [[13-decisions/data001-phase2-identity-consumer-inventory]] — DATA-001 Phase 2: identity-consumer inventory (#530).
- [[13-decisions/data001-phase2-immutable-vectors]] — DATA-001 Phase 2: immutable content points + a fenced anchor `live_point` pointer swap as the atomic vector-change commit; reconciliation rides the existing lifecycle coordinator custom-intent seam.
- [[13-decisions/h5-canonical-plane-transition-design]] — H5: canonical plane transition boundary.
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
