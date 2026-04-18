---
title: Status
section: 12-roadmap
tags: [progress, roadmap, section/roadmap, status, status/complete, type/roadmap]
type: roadmap
status: complete
updated: 2026-04-17
up: "[[12-roadmap/index]]"
reviewed: false
---
# Status

Snapshot as of **April 2026**. Updated as reality evolves.

## Boards

- [[12-roadmap/slice-board]] — slices in flight.
- [[11-migration/migration-board]] — migration phases.
- [[_inbox/research/research-board]] — research pipeline.

## v1 progress

| Phase | Status | Notes |
|---|---|---|
| 1. Schema | in progress | Pydantic models partially migrated; some dict-passthrough remains. |
| 2. Hybrid search | not started | TEI containers not yet deployed. |
| 3. Reranker | not started | — |
| 4. Planes | not started | Still single-collection POC. |
| 5. Vault | not started | Obsidian vault exists locally; no watcher yet. |
| 6. Lifecycle | not started | No scheduler or events. |
| 7. Adapters | not started | MCP runs in-process with core. |
| 8. Ops | partial | Docker-compose exists; no Ansible, no observability. |

We're at the beginning of the v1 plan. This architecture vault is the blueprint.

## What exists today (POC)

- `musubi-core` single repo, single Qdrant collection (`musubi`), FastMCP-based tools.
- Gemini embeddings (3072-d dense, no sparse, no rerank).
- 4 tools exposed via MCP: `memory_store`, `memory_recall`, `thought_send`, `thought_check`, plus ancillary reflect/forget/read variants.
- Thoughts live in the same collection as memories (content_type flag).
- No vault, no concepts, no artifacts, no lifecycle, no HTTP API, no auth.

See [[02-current-state/index]] for detail.

## What's in flight (April 2026)

- Schema tightening (phase 1).
- Evaluating TEI setup for dense + sparse (pre-phase 2).
- Deciding on Qwen2.5 vs alternatives for local LLM.

## Next up

Finish phase 1 schema work. Then start phase 2 (TEI dense deployment, create `musubi_episodic_v2`, dual-write).

## Recently completed

- (none in the v1 plan yet — architecture doc itself is the recent milestone)

## Blockers

- **None hard.** Moving through phases as time allows.
- **Soft:** dedicated time for phase 6 (lifecycle) given its size.

## Risks

### Time

Single-developer + part-time. Calendar-time estimate in [[11-migration/index#duration-estimate]] is 3 months elapsed; expect 4-6.

### Model drift

BGE-M3 / SPLADE++ V3 may be superseded before v1 ships. Re-embedding path ([[11-migration/re-embedding]]) handles that, but an early swap adds work.

### Obsidian Sync vs Watchdog

Syncthing is solid for vault file sync; watchdog detects local filesystem events. Interaction is: sync writes file → watchdog event → normal indexing. Works today in POC-style testing; edge cases possible at scale (rapid successive syncs).

### Qdrant 1.x evolution

We depend on features in 1.15. Minor upgrades within 1.x should be backward compatible; major jump to 2.x (if/when) will be a phase unto itself.

## Done criteria for v1.0

Checklist:

- [ ] Contract suite canonical passes against Core + each adapter.
- [ ] Smoke suite < 30s.
- [ ] Promotion writes to vault + Qdrant in sync; round-trips cleanly.
- [ ] Restore-from-snapshot drill completes in < 1h.
- [ ] Dashboards populated.
- [ ] All runbooks exercised at least once via chaos drill.
- [ ] Documentation (this vault) committed + reviewed.

When every box is checked, v1.0 is tagged.

## Post-v1 immediate priorities

Once v1.0 ships:

1. Watch for 2 weeks; fix surprises.
2. Start expanding eval suite with real-user queries.
3. Begin v2 proactive-thoughts experimentation.
4. Write post-mortem: what worked, what didn't.

## How to update this page

Every week, scan phases; update status flags. Quarterly, add a dated note to "what's in flight" / "recently completed." Keep the prose short; this is a status board, not a narrative.
