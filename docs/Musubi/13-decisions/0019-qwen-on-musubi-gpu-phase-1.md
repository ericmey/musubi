---
title: "ADR-0019: Qwen 3 4B on musubi.example.local GPU (Phase 1)"
section: 13-decisions
type: adr
status: accepted
tags: [section/decisions, status/accepted, type/adr, llm, gpu, deployment]
updated: 2026-04-19
---

# ADR-0019: Qwen 3 4B on musubi.example.local GPU (Phase 1)

## Status

Accepted (phase-gated).

## Context

Musubi's lifecycle sweeps (maturation, synthesis, reflection, future promotion) and future hot-path retrieval need an LLM. The spec at `08-deployment/gpu-inference-topology.md` originally named Qwen 2.5 7B Q4 co-resident with TEI dense + sparse + reranker on a single GPU.

Two material facts reshape the decision:

1. **Target host is `musubi.example.local`** — purpose-built dedicated bare-metal server with an RTX 3080 (10 GB VRAM), not shared. Dedicated to Musubi + dependent services.
2. **Operator has two other GPU hosts** in the homelab — `photo.example.local` (5070 Ti 16 GB) and `av.example.local` (5090 32 GB) — currently load-balancing image-gen. Photo-Club is a viable fallback LLM host if needed.

The original 7B placement would leave ~0.4 GB headroom on the 10 GB card — workable, but fragile under real load.

## Decision

**Phase 1 (initial deploy):** Run **Qwen 3 4B Q4_K_M** (~2.5 GB) co-resident with the three TEI services on musubi.example.local's 3080. Keep the full stack self-contained on one box.

**Phase 2 (triggered by objective criteria; see below):** If Phase 1 fails on any criterion, move Qwen to Photo-Club (5070 Ti 16 GB) as a dedicated LLM host reachable over the `example.local` VLAN, OR upgrade musubi's GPU.

This is a phase-gated decision, not a permanent choice. The point is to ship the simplest viable configuration, measure, and upgrade on evidence.

## VRAM budget (Phase 1)

| Service | Resident |
|---|---|
| TEI-dense (BGE-M3) | ~2.3 GB |
| TEI-sparse (SPLADE++ V3) | ~0.5 GB |
| TEI-reranker (BGE-reranker-v2-m3) | ~2.0 GB |
| Qwen 3 4B Q4_K_M | ~2.5 GB |
| KV cache + CUDA context (shared) | ~1.5-2.0 GB |
| **Total committed** | **~8.8-9.3 GB** |
| **Headroom** | **0.7-1.2 GB** |

Tight but workable.

## Phase 2 trigger criteria

Phase 2 triggers if **any** of the following happens during normal operation over a 14-day monitoring window starting at the first real capture:

| Criterion | Measurement | Threshold |
|---|---|---|
| VRAM OOM / eviction | Ollama logs + `dmesg` for `nvidia`/CUDA OOMs | >1 per week |
| Sweep wall-time exceeds schedule window | Hourly maturation >15 min; daily synthesis/reflection >2 hours | Any occurrence |
| TEI availability during sweeps | TEI call errors coincident with Ollama activity | >5% error rate in sweep windows |
| Retrieval latency regression | P95 `hybrid_search` latency during sweep windows vs. baseline | >2x regression |
| Synthesis / reflection quality | Operator review of generated concepts + daily digest | 2+ consecutive thumbs-down |
| Qwen 3 4B quality floor | Shallow rationales; missed obvious contradictions | Operator judgment at Day 14 |

Mechanical criteria (first four) trigger immediate Phase 2 consideration. Quality criteria are bounded in time — evaluated no later than Day 14 after first real capture.

## Phase 2 options (when triggered)

Listed in preference order:

1. **Move Qwen to Photo-Club** (repurpose from image gen)
   - Qwen 3 8B or Qwen 2.5 7B Q4 comfortably fits with room for a second (uncensored / Dolphin-class) model alongside
   - All TEI stays on musubi; musubi's 10 GB becomes comfortable (~5 GB headroom)
   - Ollama exposed on `photo.example.local:11434`; musubi reaches it via VLAN
   - Cost: ~50 % loss of parallel image-gen burst throughput (AV-Club alone handles image gen)

2. **Upgrade musubi's GPU** (e.g. 4090 24 GB or similar)
   - Preserves single-box topology
   - Enables larger Qwen (14B Q4 fits)
   - Cost: hardware spend + downtime

3. **External LLM API** (Anthropic / OpenAI / Gemini) for specific sweeps
   - Lowest infra change
   - Cost: per-call pricing (marginal for sweep volume); personal-data privacy trade-off

## Consequences

**Positive:**

- Self-contained initial deploy; no VLAN dependencies for Musubi's core loop.
- Cheapest, fastest path to first smoke test on real hardware.
- Reversible — `Settings.ollama_url` config change + container re-point is the entire Phase 2 code cost.
- Qwen 3 4B quality is benchmarked as "approaching Qwen 2.5 7B" on synthesis-class tasks per public evals; realistic quality floor for the workload.

**Negative:**

- Tight VRAM headroom (~1 GB) leaves no slack for growth (larger TEI batch sizes, retrieval-deep hot-path LLM, multi-model experiments).
- Qwen 3 4B may produce shallower synthesis / reflection prose than Qwen 2.5 7B baseline; quality evaluation is subjective and requires operator review.
- Mechanical failure (OOM, latency spike) forces Phase 2 mid-deployment.

**Neutral:**

- None of this decision blocks any in-flight slice work. `OllamaClient` + `Settings.ollama_url` abstractions make the LLM placement a pure configuration concern.

## Alternatives considered

1. **Qwen 2.5 7B Q4 on 3080 (original spec)** — ~0.4 GB headroom; too fragile for real load. Rejected for Phase 1.
2. **Qwen 2.5 7B on CPU (Ollama CPU mode)** — frees VRAM fully but sweeps become minute-scale; retrieval-deep hot-path would be untenable. Rejected for Phase 1.
3. **Multi-host topology from day one** (Photo-Club as dedicated LLM) — cleanest architecture but premature teardown of image-gen parallelism without evidence it's needed. Deferred to Phase 2.
4. **External LLM API** (Anthropic / OpenAI / Gemini) — sidesteps VRAM entirely but puts personal thought content through third-party APIs. Deferred pending privacy review.
5. **Smaller models (Gemma 3 4B, Phi-3.5-mini)** — similar size to Qwen 3 4B but weaker on synthesis-class tasks per public evals. Qwen 3 4B chosen as the highest-quality option at this VRAM budget.

## Decision owners

- Phase 1 monitoring + evaluation: operator.
- Phase 2 trigger call: operator, advised by reviewer agent (Claude Code reviewer session).

## References

- `08-deployment/gpu-inference-topology.md` — canonical VRAM + service topology spec; updated to match this ADR.
- `.agent-context.local.md` — operator-only, gitignored; contains real hostnames + endpoints.
- ADR-0015 — monorepo; locks the single-repo assumption this decision is scoped to.

## Review trigger

Re-evaluate this ADR at Day 14 after first real capture on musubi.example.local. Document the outcome in the ADR's `status` field:

- `accepted` (current) — Phase 1 is the steady state.
- `superseded` — Phase 2 triggered; link to the superseding ADR.
