---
title: Next up — rolling 2-week plan
section: 12-roadmap
tags: [roadmap, section/roadmap, type/roadmap, status/in-progress, plan]
type: roadmap
status: in-progress
updated: 2026-04-25
up: "[[12-roadmap/index]]"
reviewed: false
---

# Next up — rolling 2-week plan

> What's worth building next, in what order, and why. Updated as items
> ship or get bumped. Not a release plan — Musubi v1.x is shipping
> hands-off via release-please. This is the **direction** and the
> **sequence** for building on top of v1.2.

**Last updated:** 2026-04-25 — post-v1.2 (state_filter API), post-livekit prompt sweep (thought-partner mode + greeting filter + `musubi_remember` rename).

## What this doc is

- A rolling forward plan, ~2 weeks at a time.
- Complements [[12-roadmap/status]] (point-in-time snapshot of v1 phases) and [[12-roadmap/phased-plan]] (the long-arc v1→v2→v3 direction).
- Items here cross repos: `musubi/`, `openclaw-livekit/`, `openclaw-musubi/`. Each item names its target repo(s).
- Each item has a one-line **DoD** so "shipped" is unambiguous.
- Once an item ships, mark it `[x]` and move it to the **Recently shipped** section at the bottom; once that section gets long, age out to `phased-plan` or a per-month archive.

## Sequencing logic

A few items have prerequisites; ignoring them costs rework:

- **L1 (verify lifecycle)** is sneaky-load-bearing. Synthesis, promotion, demotion, reflection all *exist* in code but we haven't confirmed they're producing output in production. Without that confirmed, every item below that depends on richer data (scoring, profiles, EQ) builds on uncertain ground.
- **W1.2 (pre-rank scoring)** is the foundation for **PL.3 (EQ layer / Eric profile)** — emotional/preference signals need somewhere to live; richer at-capture scoring is where you put them.
- **PL.3 (EQ)** also benefits from synthesis (L1) producing concept-plane data over time.
- **W2.2 (dashboard)** + **W2.3 (Rin watchdog)** pair naturally — visualise + alert.
- **PL.6 (wiki)** is the prerequisite for **PL.7 (thought leadership posts)** — public-facing comms need a landing page.

So the order roughly is:

```
L1 (verify lifecycle)
  → W1.2 (pre-rank) → PL.3 (EQ)
  → W1.3 (dynamic greetings)
  → W2.1 (lifecycle viewer) → W2.2 (dashboard) → W2.3 (rin alerts)
  → PL.4 (callbacks), PL.5 (household prompt sweep)
  → PL.6 (wiki) → PL.7 (thought leadership)
```

---

## Week 1 — 2026-04-25 → 2026-05-02

### W1.1 — Verify the lifecycle is actually cycling

**Repo:** `musubi/`. **Estimate:** an evening of investigation. **Depends on:** nothing.

Maturation we know works (verified via the cocoa-pods test). Synthesis (03:00 UTC), promotion (04:00), demotion (05:00), reflection (06:00) — all coded, all on cron, none verified. Read the lifecycle journal SQLite for the last 48h, see which sweeps fired, what they produced. If reflection hasn't written a daily digest into the vault, that's a feature you've paid for that isn't running.

**Probable failure modes:** Ollama LLM unreachable, vault-write path misconfigured, cron job mis-scoped, dwell windows too long for current row counts.

**DoD:** A short writeup appended to this doc under each sweep: ✓ ran, here's what it produced — OR ✗ failed because X, here's the fix.

### W1.2 — Pre-rank scoring at intake

**Repo:** `openclaw-livekit/` + `openclaw-musubi/` (server stays untouched). **Estimate:** 1-2 days. **Depends on:** Ollama healthy on the box (verified by W1.1).

The thing we band-aided last night: `state_filter=[provisional, matured, promoted]` lets fresh saves surface, but ranks them on (relevance, recency, reinforcement) only — no signal that *this provisional row is the cocoa-pods prank Eric just told me to remember* vs. ambient noise.

Fix: when `musubi_remember` is called, run a quick Ollama pass *before* the capture lands. The model returns:

- `importance: 1-10` (calibrated, not hardcoded `7`)
- `topics: [string, ...]` (extracted, no LLM-side guessing on the model that called the tool)
- `callback_worthy: bool` ("Eric is going to expect to find this on the next call")
- `valence: "positive" | "neutral" | "concerned" | "frustrated" | "excited"` (foundation for PL.3)

These ride into the existing `tags` field as structured tags (`importance:9`, `valence:concerned`, `callback:true`) plus the topic strings — no Musubi server-side change needed in v0. v1 of this could promote the fields out of tags into structured payload columns once we know what we're using.

Latency budget: <500 ms. Eric's voice fillers ("got it, stored") cover it.

**DoD:** Save a memory via voice → inspect the row in canonical Qdrant → see structured tags. Retrieve with `state_filter=[provisional,...]` returns rows ordered with high-importance/callback-worthy first.

### W1.3 — Dynamic / contextual greetings

**Repo:** `openclaw-livekit/`. **Estimate:** half a day. **Depends on:** nothing.

`on_enter` checks time since the last `<agent>-voice` tagged memory. Branches:

- **<30 min since last call:** instructions tell the agent to greet as a continuation. *"Eric — hey, didn't expect to hear from you again so soon."* Pull last call's recent context into the greeting hint.
- **Last call ended without a clean-wrap memory** (no `topic:wrap-up` tag, or memory_store was the last action and content suggests in-progress work): *"Calling back to finish that thread on X?"* — pre-populated from the most recent musubi_search hit.
- **Otherwise:** current behaviour — warm hello, optional callout from recent context.

LLM-side change, not new hardcoded greeting strings. The instruction-template just gets richer based on what `on_enter` discovers.

**DoD:** Short call about the dentist → hang up → call back 5 min later → Nyla opens with "Hey Eric — back already? Did you grab the dentist confirmation?"

---

## Week 2 — 2026-05-02 → 2026-05-09

### W2.1 — Lifecycle journal viewer

**Repo:** `musubi/` (likely a new `tools/` or operator script). **Estimate:** 1-2 hours. **Depends on:** W1.1 (otherwise you're staring at empty output).

50-line CLI or static HTML render that reads the lifecycle event SQLite and shows: last N sweeps per type, when they fired, what they processed, what they emitted. Pure read-side. No new schema.

**DoD:** One command (`musubi-cli lifecycle journal --since 48h` or similar) that gives you a chronological view of overnight sweeps without opening sqlite3.

### W2.2 — Ops dashboard (Grafana on existing Prometheus)

**Repo:** `musubi/` (probably under `deploy/grafana/` template). **Estimate:** 1 day. **Depends on:** Prometheus scraping (already deployed).

Grafana stack pointed at `musubi-prometheus`. First-pass panels:

- Lifecycle sweep durations + last-fired-at per sweep
- Qdrant collection point counts (per plane)
- Retrieve p95/p99 (overall + by mode)
- Error rates per endpoint
- Embedding + LLM latency (TEI dense / sparse / reranker / Ollama)
- Disk + RSS on the workload host

**DoD:** A single Grafana dashboard URL bookmarkable, gives you "is the system healthy" at a glance.

### W2.3 — Rin-as-watchdog

**Repo:** `openclaw-control/` (or wherever cron + Discord lives). **Estimate:** 1 day. **Depends on:** W2.2 (you want the same metrics surfaced in alerts, not duplicated).

Scheduled health probe. If anything fails → Discord DM via Rin's account (her literal persona is *"Operations. Discipline. Ops reports, health checks."* — currently more vibe than function). Optional companion: 8am daily ops summary in #ops Discord channel.

Trigger candidates: Musubi `/v1/ops/health` non-200, Qdrant unreachable, Ollama unreachable, last lifecycle sweep > 90 min stale, disk > 85%.

**DoD:** Kill TEI dense container for 2 minutes → Rin DMs you within 5.

---

## Parking lot (deferred — bigger, external, or downstream of week 1-2)

### PL.1 — Hana-machine voice agents — proper smoke + integration

**Repo:** `openclaw-livekit/`. Eric installed but didn't fully test. Need: register the 5 agents (mizuki / shiori / reika / yua / nana) as workers, mint per-agent tokens with their `<agent>/voice` presence, end-to-end a test call to one, confirm cross-machine household_status works. Probably an evening once we get to it.

### PL.2 — Callbacks (timer-based outbound calls)

**Repo:** new infra spanning `openclaw-livekit/` + Twilio. The "callbacks aren't wired up yet" disclaimer is in all three voice prompts; closing it unlocks normal phone-call patterns ("call me back at 3pm", "remind me in 20 minutes"). Components: a small scheduler (could ride existing `cron` infra), a Twilio outbound trigger, a tool the agent invokes during a call.

### PL.3 — EQ layer / Eric Profile as curated row

**Repo:** `musubi/` (synthesis sweep enhancement) + `openclaw-livekit/` (consume profile in `on_enter` instructions). **Depends on:** W1.1 (synthesis cycling) + W1.2 (valence + preference signals at intake).

Single curated row at `<agent>/_shared/curated` that's the *Eric Profile*, synthesised continuously by a daily sweep — preferences, dislikes, recurring themes, recent emotional weather. Surfaced into every agent's instructions on call-start. The big architectural commitment but it makes EQ *a thing in the system* rather than a feature on each agent.

### PL.4 — Household prompt sweep — all 14 agents

**Repo:** `openclaw-livekit/` (the 6 voice agents we haven't reviewed) + `openclaw-control/` (the text-side personas). **Depends on:** PL.3 so we sweep with EQ-aware prompts, not pre-EQ ones.

Per agent:

- Are they empowered to make decisions in their domain (Momo with email triage, Rin with ops tradeoffs)?
- Are tools wired correctly?
- Is persona distinct or muddy?
- Are agent-as-tenant + canonical Musubi references current?

### PL.5 — Public wiki / installation docs

**Repo:** `musubi/`. Expanded backlog lives at [[12-roadmap/public-docs-wiki]].

The README is a good pitch, and the vault has strong architecture material, but the project still lacks a true public documentation product for evaluators, self-hosters, adapter builders, and contributors. The goal is a complete user-facing docs surface: clear landing pages, install guides, tutorials, API / SDK reference, troubleshooting, and trust / contributor pages.

**Recommended publishing model:** repo-owned markdown as the source of truth, GitHub Wiki as the published surface, generated API reference from `openapi.yaml`, architecture notes in the vault linked as deeper background rather than copied wholesale.

**DoD:** the backlog in [[12-roadmap/public-docs-wiki]] is seeded, linked from the roadmap, and ready to execute phase-by-phase.

### PL.6 — Thought leadership posts

**Repo:** external (blog, X/LinkedIn). **Depends on:** PL.5 — public posts need somewhere to land people.

Topics that pull:

- "Why I built shared memory for an AI agent fleet"
- "Three planes: episodic, concept, curated — a memory model that has opinions"
- "ADR-0030: agent-as-tenant — a different way to think about identity"
- "Cross-channel recall: my voice agent remembers what I told my desktop agent"
- "Building a lifecycle: memories that mature, synthesise, promote, demote"

### PL.7 — Vault doc sweep — agent-as-tenant migration of stale `eric/...` examples

Per the README's status section: pure cleanup, no risk, 1-2 hours. Slot in as a "I want a low-stakes commit tonight" task.

---

## Recently shipped (2026-04-24 → 2026-04-25)

- ✓ **Multimodality end-to-end** — wildcard namespaces (ADR 0031, v1.1.0), state_filter API ([#271](https://github.com/ericmey/musubi/pull/271), v1.2.0), `musubi_search` tool on voice agents, `<owner>/*` retrieve in openclaw-musubi plugin. Verified via real call: phone-Nyla recalled an Openclaw save.
- ✓ **Voice agent thought-partner mode** — Eric ends calls, not the agent. Length matches conversation density. Applied across Nyla / Aoi / Party.
- ✓ **Greeting filter + recency-based recent** — `fetch_recent_context` filters to agent-tagged rows, drops the time-window. Party's "I was just thinking about..." hook now strips speaker prefix and length-checks, falls back to plain hello when nothing reads naturally.
- ✓ **Tool name standardisation** — voice `memory_store` renamed to `musubi_remember` to match the openclaw-musubi plugin. `tags` → `topics`. Optional `importance`.
- ✓ **Aoi delegation cleanup** — Momo dropped from her allowlist (inbox conversations belong with Nyla).
- ✓ **Party identity rewrite** — prompt now says "you are Nyla on the Harem World line", not a separate persona.
- ✓ **Legacy house-brain Qdrant volume archived + dropped** — 168 episodic + 89 thoughts dumped to `~/.openclaw/musubi.archived-v1.0-*/qdrant-dump/` then volume removed.
