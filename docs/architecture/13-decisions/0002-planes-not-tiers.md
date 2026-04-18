---
title: "ADR 0002: Planes, Not Tiers"
section: 13-decisions
tags: [adr, architecture, planes, section/decisions, status/accepted, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0002: Planes, Not Tiers

**Status:** accepted
**Date:** 2026-03-14
**Deciders:** Eric

## Context

Having decided on multiple collections ([[13-decisions/0001-three-plane-architecture]]), the next question: are the planes a hierarchy or orthogonal?

A "tier" reading would say: episodic is hot / recent, curated is warm / important, concept is cold / derived. Data flows one direction: episodic → concept → curated, and old data ages down a pipe.

A "plane" reading says: each collection is a different *kind* of memory, not a different *age* of memory. Data in one plane doesn't replace data in another. An episodic observation and a curated doc can both exist about the same thing, because they serve different purposes.

The tier framing is tempting because it maps to familiar storage hierarchies (L1/L2/L3, hot/warm/cold). It suggests clean pipelines.

The plane framing is more honest about what we're doing: we're separating memory *semantics*, not memory *age*.

## Decision

Treat episodic / curated / concept as **orthogonal planes**, not a hierarchy.

Implications:

- Retention is per-plane, not a cascade. An episodic memory doesn't "become" a curated doc; a curated doc gets *authored* (possibly informed by episodic observations).
- A concept doesn't have to reach curated — many stay in concept forever, or get rejected.
- An observation can stay in episodic forever if it's useful (no forced eviction to "cold storage").
- Retrieval can target a plane or span planes, with no implicit hierarchy of trust.
- Promotion is a *creation*, not a *move*. Promoting a concept to curated writes a new vault file — it does not delete the concept point.

## Alternatives

**A. Strict hierarchy with aging pipeline.** Episodic ages out after N days, concepts that survive become curated, old curated archives to cold storage. Clean mental model but wrong for our use case: we don't want to lose raw episodic observations just because they're old, and promotion isn't about age.

**B. Everything in one plane with severity flags.** Described in [[13-decisions/0001-three-plane-architecture]] as the rejected starting point.

**C. Two planes (observation + derived).** Collapses curated and concept. Works until you realize curated is human-authored and concept is machine-hypothesis; conflating them loses the distinction that makes promotion gating meaningful.

## Consequences

- Scoring model treats planes as independent sources ([[05-retrieval/scoring-model]]).
- Lifecycle jobs don't "move" data across planes in the destructive sense; they create new rows and link back.
- Documentation language: we say "plane" consistently, never "tier." Small thing, but it shapes thinking.
- Simpler retention: each plane has its own policy. No cross-plane TTL cascade.

Trade-offs:

- Users occasionally expect "where does old data go?" We answer: it stays. We rely on vector search and recency scoring to make the store self-organizing without requiring physical movement.

## Links

- [[13-decisions/0001-three-plane-architecture]]
- [[01-overview/three-planes]]
- [[06-ingestion/maturation]]
