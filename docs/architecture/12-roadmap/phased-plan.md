---
title: Phased Plan
section: 12-roadmap
tags: [plan, roadmap, section/roadmap, status/draft, type/roadmap, v1, v2, v3]
type: roadmap
status: draft
updated: 2026-04-17
up: "[[12-roadmap/index]]"
reviewed: false
---
# Phased Plan

The Musubi trajectory in phases, with rough targets and unknowns called out.

## v1: Solid Base (ships ~Q3 2026)

Scope covered in [[11-migration/index]]. Recap:

- Three-plane architecture (episodic / curated / concept) + artifacts.
- Obsidian vault as curated source of truth.
- Local inference (BGE-M3, SPLADE++ V3, BGE-reranker-v2-m3, Qwen2.5-7B) on a dedicated box.
- Canonical HTTP/gRPC API + Python SDK.
- MCP, LiveKit, OpenClaw adapters.
- Lifecycle engine (maturation, synthesis, promotion, demotion, reflection).
- Ops: Ansible, backups, observability, alerts.

**Target metrics at v1.0:**

- Capture p95 < 300ms.
- Retrieve fast p95 < 400ms.
- Retrieve deep p95 < 5s.
- Concept promotion → curated in vault, reviewable by human in < 24h of reinforcement threshold.
- Restore-from-snapshot RTO < 1h.
- No single-point-of-data-loss outside vault git + Qdrant snapshots.

## v1.1 - v1.5: Polish

Incremental improvements after v1.0 ships. Typical items:

- Smarter topic extraction (fewer bad auto-tags).
- Better promotion-gate heuristics.
- Richer contradictions UI (currently just operator-resolve).
- CLI `musubi-cli reflect` on demand.
- Per-adapter quality-of-life.
- Evals suite expansion.
- Performance tuning (HNSW `ef`, TEI batch sizes).

Each item is a small, reversible PR.

## v2.0: Proactivity (late 2026)

Musubi begins to originate messages.

### Proactive thoughts

Currently thoughts are send-from-one-presence-to-another; humans don't get proactive input. v2 adds a **pattern detector** job:

- "You mentioned this topic X times in the last 7 days; want a concept?"
- "This curated doc is stale (30d no referenced retrieval); want to archive?"
- "Three agents have different versions of X; want me to reconcile?"

Proactive thoughts go to an `ops` or `home` presence; user sees them in their presence inbox.

Requires:

- Pattern detector lifecycle job.
- Rate-limit proactive messages (one per day max).
- Mute / unsubscribe per category.

### Policy layer

Some categories of concept auto-promote without operator approval:

- Tag normalization (merge `#docker` and `#Docker`).
- Terminology upgrades (`gpu` → `GPU` house style).
- Small stylistic rewrites.

Larger changes (new concept documents, contradictions) still require human approval.

Config-driven; easy to tune.

### Expanded reflections

- **Daily** (already in v1): summary of yesterday.
- **Weekly**: trends, unresolved contradictions, top patterns. `vault/reflections/weekly/YYYY-Www.md`.
- **Monthly**: ~1000-word narrative of the month.
- **Topic-specific**: "All reflection lines touching `projects/livekit` this quarter".

Richer reflection prompts + longer context.

## v2.5: Mobile

Capture from mobile. Options:

- **iOS Shortcut** that hits the capture API with a voice snippet → transcribed + captured.
- **iMessage adapter**: text `musubi` contact; capture.
- **Share sheet** to an adapter app that forwards to the API.

No persistent agent on the phone (battery, network, OS constraints). Just capture surfaces.

## v3.0: Federation (2027+, speculative)

Two Musubi hosts share selected namespaces. E.g.:

- Eric's Musubi + spouse's Musubi share `household/projects/vacation-2026/curated`.
- Both read + write; changes sync.

Mechanism:

- Git-based replication on shared namespaces (vault directories sync via git).
- Thought passing between tenants across hosts (via pull-based inbox).
- Consistent presence naming.

Open questions:

- Conflict resolution (both edited same curated doc)?
- Discovery (how does my presence know another Musubi exists)?
- Trust (what prevents a malicious peer from reading the wrong namespaces)?

Not enough signal yet to commit.

## v3.5: Offline-first replica

Laptop holds a lightweight read-mostly copy of Musubi. Works offline; syncs opportunistically.

Stack:

- Small Qdrant embed (sqlite-based vector index — we'd pick from options available in 2027).
- Vault git clone with pull on reconnect.
- Capture queue in IndexedDB (like OpenClaw today).

Rare but essential for airplane / intermittent-network scenarios.

## v4.0: Multi-modal

Native storage of images and audio beyond transcripts:

- Image embeddings (CLIP-style) for photos.
- Audio embeddings for recordings.
- Multi-modal search: "find the photo of the broken charger I took last month" returns by visual similarity.

Requires GPU upgrade (multimodal models are larger).

## Unknowns

Items we haven't decided:

- **LLM choice for v2+.** Qwen2.5-7B is good for v1; as models improve, we'll evaluate. Could move to Claude/GPT hosted for some paths if quality justifies network hop.
- **Scheduler choice.** APScheduler may not scale to multi-host. Could move to Temporal or a simpler custom scheduler.
- **Storage choice for long artifacts.** Blob storage on SATA SSD is fine for now; at scale, move to object storage.
- **How much web presence.** Zero today. Could add a read-only web view of curated docs (like Obsidian Publish).

## What might cause a re-plan

- A dramatically better embedding model that shifts the whole retrieval stack.
- Qdrant becoming incompatible with a feature we depend on.
- LiveKit or OpenClaw direction changing (vendor risk).
- Household scale stops applying — if this grows to a team or product, re-think.

## Annotations we'll add to this doc

- Dates things actually shipped vs. planned.
- Feature flags added/removed.
- Major post-mortems that shaped the plan.
