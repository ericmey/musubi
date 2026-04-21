---
title: Status
section: 12-roadmap
tags: [progress, roadmap, section/roadmap, status, status/complete, type/roadmap]
type: roadmap
status: complete
updated: 2026-04-20
up: "[[12-roadmap/index]]"
reviewed: false
---
# Status

Snapshot as of **2026-04-20**. Updated as reality evolves.

## Boards

- [[12-roadmap/slice-board]] — slices in flight.
- [[11-migration/migration-board]] — migration phases.
- [[_inbox/research/research-board]] — research pipeline.

## v1 progress

Slice-level status drives these phase rollups (see [[_slices/completed-work]]
for the live per-slice view). As of 2026-04-20, 42 of 48 slices are `done`,
1 `blocked-on-demand` (`slice-ops-workspace-packaging`, deferred until an
external consumer needs thin wheels), 1 retired.

| Phase | Status | Notes |
|---|---|---|
| 1. Schema | **done** | Pydantic models via `slice-types` and followups. |
| 2. Hybrid search | **done** | TEI dense + sparse running live on the host per [[08-deployment/host-profile]]. |
| 3. Reranker | **done** | BGE-reranker-v2-m3 live; scoring wired via `slice-retrieval-rerank`. |
| 4. Planes | **done** | All five planes (episodic, artifact, curated, concept, thoughts) shipped. |
| 5. Vault | **done** | Watcher + vault-sync shipped via `slice-vault-sync`. |
| 6. Lifecycle | **done** | Engine, maturation, synthesis, promotion, reflection all shipped. |
| 7. Adapters | **done** | MCP adapter shipped; LiveKit + OpenClaw (OpenClaw retired) landed. |
| 8. Ops | **done (first deploy)** | Ansible + Compose + observability + backup + hardening + first-deploy runbook shipped AND executed: Musubi is live on `musubi.example.local` as of 2026-04-20. See [[00-index/work-log]]. |

Phase 8's "first deploy" doesn't mean Ops is perfect forever — it means the
stack was brought up end-to-end on the reference host, the health endpoint
returns 200, and the playbooks handle a greenfield host. Hardening, image-
digest pinning, GHCR publish, and smoke-test automation are named follow-ups.

## What exists today (POC)

- `musubi-core` single repo, single Qdrant collection (`musubi`), FastMCP-based tools.
- Gemini embeddings (3072-d dense, no sparse, no rerank).
- 4 tools exposed via MCP: `memory_store`, `memory_recall`, `thought_send`, `thought_check`, plus ancillary reflect/forget/read variants.
- Thoughts live in the same collection as memories (content_type flag).
- No vault, no concepts, no artifacts, no lifecycle, no HTTP API, no auth.

See [[02-current-state/index]] for detail.

## What's in flight (2026-04-20)

- First end-to-end smoke test against the live deploy
  (`deploy/smoke/verify.sh` → capture → retrieve round-trip). Queued.
- POC → v1 data migration execution (`slice-poc-data-migration`) against
  the now-live target.
- Harden / automate the operator-only steps that happened manually during
  tonight's first deploy (see [[_slices/slice-ops-first-deploy|first-deploy
  slice]]'s post-mortem section for the list).

## Next up

1. Smoke-test the live deploy, wire failures (if any) as ordinary slices.
2. Publish Musubi Core to GHCR + pin external image digests, replacing the
   `docker save | ssh | docker load` transfer the first deploy used.
3. Automate the HF-cache rsync step in `bootstrap.yml` so a fresh host
   matches the current one without manual intervention.
4. Resolve the `[R]` findings in [[_inbox/operator-notes]] (Ollama model
   drift — closed in tonight's deploy; health-URL contradiction — still
   open, `health.yml` probes Qdrant/Ollama on localhost but compose makes
   them bridge-only).

## Recently completed

- **2026-04-20** — First real deploy of Musubi stack on `musubi.example.local`.
  All six services healthy. See [[00-index/work-log]] for the full entry.
- **2026-04-20** — ADR 0023 (Qdrant 1.15 → 1.17 pin bump) + ADR 0024 (Kong
  deferred for v1) + runbook reconciliation.
- **2026-04-20** — `docs/architecture/` → `docs/Musubi/` vault rename
  landed atomically with history preserved.

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
