---
title: "ADR 0027: Per-bucket rate limits, not global per-second rates"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-23
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr]
updated: 2026-04-23
up: "[[13-decisions/index]]"
reviewed: false
supersedes: ""
superseded-by: ""
---

# ADR 0027: Per-bucket rate limits, not global per-second rates

**Status:** accepted
**Date:** 2026-04-23
**Deciders:** Eric
**Closes:** #193

## Context

Two places in the codebase looked like they controlled rate limits:

1. `src/musubi/settings.py` — global per-second floats
   (`rate_limit_capture: 10.0`, `rate_limit_retrieve: 20.0`,
   `rate_limit_thought: 5.0`).
2. `src/musubi/api/rate_limit.py` — a `DEFAULT_BUCKETS` dict with
   per-bucket, per-minute capacities plus a 10× operator multiplier
   applied at `allow()` time.

The `RateLimiter.__init__` method read the settings values into
`self._capture_rate` / `_retrieve_rate` / `_thought_rate` and then
**never referenced them anywhere**. `allow()` exclusively consults
`bucket.capacity_per_min`. The settings fields were dead code —
tunable knobs that did nothing.

Issue #193 surfaced this: a serial seed of 10k episodic rows started
getting 429s around row 1,600 against an operator-scoped token.
That's consistent with the real cap being `100/min × 10 = 1000/min`
for the `capture` bucket — the bucket cap was working, but the
`settings.rate_limit_capture=10.0` (= 600/min) was never in effect.
Two knobs, one of them inert.

## Decision

Keep the **per-bucket, per-minute** model. Delete the global
per-second settings fields. Document that the buckets are the only
rate-limit surface.

Why per-bucket beats per-second:

- Different endpoints need different ceilings. `artifact-upload` at
  20/min and `capture` at 100/min live together because the cost
  profile is different (large multipart body vs. small JSON).
- Per-minute windows tolerate short bursts naturally. A voice turn
  that fires 4 captures in 3 seconds doesn't trip the cap; a
  misbehaving client firing 4/sec steady-state does.
- The bucket name is already in each route's `operation_id`, so the
  mapping is declarative and visible at the endpoint definition.
- A single `rate_limit_capture: 10/s` knob gives operators no way to
  distinguish batch-write throughput from singleton capture; the
  bucket model does.

## Rate values (re-confirmed)

These stand for v1; revisit if Gate-2 re-run on v0.4.0 surfaces a
real bottleneck.

| Bucket | Base | ×10 operator |
|--------|------|--------------|
| `capture` | 100/min | 1000/min |
| `batch-write` | 50/min | 500/min |
| `thought` | 100/min | 1000/min |
| `transition` | 50/min | 500/min |
| `artifact-upload` | 20/min | 200/min |
| `default` | 200/min | 2000/min |

Realistic per-user steady-state math (captured here so we don't
re-derive later):

- Voice turn: ~4 captures per turn (open + 2-3 tool calls)
- Browser supplements: ~2 captures/min
- Capture mirror: ~1/min
- Steady: ~15 captures/min/user
- Burst (mid-voice-turn): 4 captures in ~5s, spreads under the window

At 1000/min operator cap the system comfortably supports ~60
concurrent active users before capture becomes the bottleneck —
well past the "small team fleet" target.

**Seed / backfill is not normal traffic.** A 10k serial seed will
trip 1000/min by design. The seed harness (`scripts/perf/seed_corpus.py`)
honors `Retry-After` and backs off — that's the correct protocol for
bulk loads against a production cap.

## Consequences

- `settings.rate_limit_capture` / `_retrieve` / `_thought` are
  removed; nothing reads them. `RateLimiter.__init__` no longer
  imports settings.
- `DEFAULT_BUCKETS` is now the single source of truth for rate
  ceilings. Changing one is a code change + commit, not an env var
  flip — the trade-off is a bit more friction for a lot more
  visibility.
- When `slice-api-rate-limit-distributed` lands (moving state to
  Redis / Kong), it inherits the per-bucket model and the 10×
  operator multiplier unchanged.

## Deferred

The operator multiplier is still a single constant (`_OPERATOR_MULTIPLIER = 10`).
If we ever want per-bucket multipliers (e.g. operator gets 20× on
`capture` for migrations, only 5× on `artifact-upload`) the bucket
spec grows a field — not today.
