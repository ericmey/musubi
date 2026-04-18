---
title: Gap Analysis
section: 02-current-state
tags: [current-state, migration, section/current-state, status/complete, type/gap-analysis]
type: gap-analysis
status: complete
updated: 2026-04-17
up: "[[02-current-state/index]]"
reviewed: false
---
# Gap Analysis

What must change, per subsystem, to move from POC to target. Each row has a migration reference.

## Data model

| Area | POC | Target | Migration |
|---|---|---|---|
| Planes | 1 memory collection | 3 planes + concepts | [[11-migration/phase-1-schema]] |
| Vectors | single dense | named dense + sparse | [[11-migration/phase-1-schema]] |
| Lineage | none | `supersedes`, `merged_from`, `promoted_to`, etc. | [[11-migration/phase-1-schema]] |
| Versioning | updated_at only | `version`, `superseded_by` | [[11-migration/phase-1-schema]] |
| IDs | UUID | KSUID (payload `object_id`; point ID still UUID) | [[11-migration/phase-1-schema]] |
| Bitemporal | no | `event_at`, `ingested_at`, `valid_from`, `valid_until` | [[11-migration/phase-1-schema]] |
| Schema version | implicit | `schema_version` field | Phase 1 |

## Retrieval

| Area | POC | Target | Migration |
|---|---|---|---|
| Search | dense cosine | hybrid (dense + sparse RRF fusion) | [[11-migration/phase-2-hybrid-search]] |
| Scoring | similarity only | weighted triad + penalties | [[11-migration/phase-2-hybrid-search]] |
| Reranker | none | local cross-encoder | Phase 3 |
| Fast path | none | dedicated episodic cache | Phase 3 |
| Orchestration | none | cross-plane query planner | Phase 4 |

## Ingestion / lifecycle

| Area | POC | Target | Migration |
|---|---|---|---|
| Capture | sync dedup | sync dedup (kept) + async enrichment | [[11-migration/phase-6-lifecycle]] |
| Maturation | none | hourly job (importance, tagging, dedup pass) | Phase 4 |
| Synthesis | none | daily job (fact extraction, concept creation) | Phase 4 |
| Promotion | none | threshold-gated write to vault | Phase 4 |
| Demotion | `memory_forget` (delete) | soft delete + archive flag | Phase 4 |
| Vault sync | none | watchdog + reindexer | Phase 4 |

## Interfaces

| Area | POC | Target | Migration |
|---|---|---|---|
| Primary API | MCP | HTTP + gRPC (canonical) | [[11-migration/phase-7-adapters]] |
| MCP | in-tree | separate repo `musubi-mcp` depending on SDK | Phase 5 |
| LiveKit | none | separate repo `musubi-livekit` | Phase 6 |
| OpenClaw | none | separate repo `musubi-openclaw` | Phase 6 |
| SDK | none | `musubi-sdk-py` (first), `musubi-sdk-ts` (later) | Phase 5 |
| Backward compat | n/a | legacy MCP tools preserved via shim on top of new planes | Phase 5 |

## Inference

| Area | POC | Target | Migration |
|---|---|---|---|
| Embedding model | Gemini remote | BGE-M3 local (TEI) | [[11-migration/phase-2-hybrid-search]] |
| Sparse embedding | none | SPLADE++ local (TEI) | Phase 7 |
| Reranker | none | BGE-reranker-v2-m3 local (TEI) | Phase 7 |
| Importance / extraction LLM | none | Qwen2.5-7B Q4 local (Ollama) | Phase 7 |
| Gemini | primary | optional fallback for long-context | Phase 7 |

## Deployment / ops

| Area | POC | Target | Migration |
|---|---|---|---|
| Orchestration | scripts (macOS Colima) | Ansible playbooks (Ubuntu) | [[11-migration/phase-8-ops]] |
| Secrets | `.env` | ansible-vault encrypted vars + runtime env injection | Phase 8 |
| Host | dev laptop | dedicated Ubuntu Server (Ryzen 5, 32GB, RTX 3080) | Phase 8 |
| Backup | none (manual) | nightly Qdrant snapshot + vault git + artifact sync | Phase 8 |
| Observability | stdout logs | structured JSON logs + Prometheus metrics | Phase 8 |
| Healthchecks | none | `/healthz` + `/readyz` endpoints on all services | Phase 8 |

## Security

| Area | POC | Target | Migration |
|---|---|---|---|
| Auth | none | bearer tokens scoped to tenant | [[11-migration/phase-8-ops]] |
| Transport | http / stdio | TLS-terminated behind Kong | Phase 8 |
| Redaction | none | regex-based PII redaction at ingestion (opt-in) | Phase 9 (post-v1) |
| Audit log | access_count | full audit of writes, promotions, demotions | Phase 4 |

## Test strategy

| Area | POC | Target | Migration |
|---|---|---|---|
| Unit tests | ~90 | ~300 (new planes + retrieval + lifecycle + vault) | throughout |
| Integration tests | 1 file | `tests/integration/` with docker-compose fixture | Phase 3 |
| Contract tests | none | `tests/contract/` golden API surface | Phase 5 |
| Chaos tests | none | kill-and-recover scenarios | Phase 8 |
| Property tests | none | Hypothesis-based for scoring + lifecycle | Phase 3 |
| Perf tests | none | latency SLOs on reference host | Phase 7 |
