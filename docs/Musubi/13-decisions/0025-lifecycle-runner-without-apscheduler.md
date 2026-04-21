---
title: "ADR 0025: Lifecycle runner without APScheduler (for now)"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-21
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr]
updated: 2026-04-21
up: "[[13-decisions/index]]"
reviewed: false
supersedes: ""
superseded-by: ""
---

# ADR 0025: Lifecycle runner without APScheduler (for now)

**Status:** accepted
**Date:** 2026-04-21
**Deciders:** Eric

## Context

`src/musubi/lifecycle/scheduler.py` defines the :class:`Job` primitive +
`build_default_jobs()` registry but ships no production runner — the
module's docstring states "the production APScheduler wiring is a follow-up
slice." Running the first deploy with no runner left captures stuck in
`state: "provisional"` (see [`retrieve/orchestration.py`](../../src/musubi/retrieve/orchestration.py#L173)
default state filter `{matured, promoted}`), so capture→retrieve never
closed.

Adding APScheduler as a top-level dependency is a prohibited pattern per
[`CLAUDE.md` § Prohibited patterns](../../../CLAUDE.md) without an ADR.
Rather than take that dependency under time pressure, we shipped a minimal
asyncio-based runner that implements the subset of APScheduler semantics
the existing job registry actually needs.

## Decision

Ship [`src/musubi/lifecycle/runner.py`](../../src/musubi/lifecycle/runner.py)
— a tick-driven asyncio scheduler with:

- Minute-resolution `cron` trigger evaluation for `minute` / `hour` /
  `day` / `month` / `day_of_week` kwargs (everything
  `build_default_jobs()` currently emits).
- `interval` trigger support via last-fired tracking. Interval jobs fire
  on runner boot so operators see activity without waiting a full
  cadence.
- Per-minute dedupe so a clock-drift tick inside the same wall-minute
  doesn't double-dispatch.
- Dispatch via `asyncio.to_thread(job.func)` so existing sync wrappers
  (e.g. [`build_maturation_jobs`](../../src/musubi/lifecycle/maturation.py#L942)
  which internally call `asyncio.run()`) work unchanged — each sweep
  runs its own event loop in a worker thread.
- Entrypoint `python -m musubi.lifecycle.runner` wires real deps
  (QdrantClient, LifecycleEventSink, MaturationCursor, OllamaClient)
  and runs until SIGTERM / SIGINT.

Maturation jobs get wired to the real sweep functions via the existing
`build_maturation_jobs()` helper. Synthesis, promotion, demotion,
reflection, and vault reconcile remain placeholder-lambda jobs until
follow-up slices land `build_xxx_jobs()` helpers for each — the registry
still has all nine names so misfires get logged rather than silently
dropped.

We also land [`src/musubi/llm/ollama.py`](../../src/musubi/llm/ollama.py)
— an `httpx.AsyncClient`-backed `OllamaClient` that satisfies the
Protocol in [`musubi.lifecycle.maturation`](../../src/musubi/lifecycle/maturation.py#L144).
Prompts live at `src/musubi/llm/prompts/{importance,topics}/v1.txt` per
the frozen-prompt rule in `docs/Musubi/06-ingestion/CLAUDE.md`.

## When this decision gets revisited

Move to APScheduler when any of the following hit:

- **More than minute-resolution cron** is needed — e.g. a sweep that
  needs sub-minute granularity.
- **Persisted jobstore across worker restarts** becomes load-bearing —
  today misfires simply skip; when we care about catch-up on a post-
  downtime boot, APScheduler's `SQLAlchemyJobStore` is the answer.
- **Distributed coordination** across more than one worker host is
  needed (ADR 0010 is still single-host — revisit when that ADR
  changes).

At that point, write the ADR to add APScheduler, replace
`LifecycleRunner.run` with a `BlockingScheduler` wiring, and delete
the tick loop. The `Job` dataclass stays the same, so
`build_default_jobs()` / `build_maturation_jobs()` don't change.

## Consequences

**Positive:**

- Captures actually mature. Capture→retrieve round-trip is closed — the
  core functional gap from the first deploy. Verified 2026-04-21: 3
  provisional captures matured in one forced sweep; all three are
  retrievable via `/v1/retrieve`.
- No new top-level deps. `asyncio`, `signal`, `httpx` (existing), and
  the stdlib are all we need.
- Production and test code share the same `Job` abstraction.
  `TestingScheduler` (drives unit tests via `force_run`) and
  `LifecycleRunner` (drives production via wall-clock ticks) both
  consume `build_default_jobs()` output.
- The APScheduler swap, when it happens, is a single-file change.

**Negative:**

- One-minute blind spot on job misfires. If the runner is down when a
  cron minute passes, the job doesn't retry — the next natural fire
  time is an hour / day / week away. APScheduler's misfire grace
  handling would catch that; we currently log and skip. Acceptable for
  now because the sweeps are idempotent and the next scheduled fire
  eventually reprocesses the same rows.
- No persistence of `_last_fired`. On restart, interval jobs fire
  immediately (by design) and cron jobs resume wherever the wall clock
  is. This is fine for the current sweep set but is why APScheduler's
  jobstore exists.
- Manual cron evaluation means `trigger_kwargs` keys are hardcoded to
  the five supported fields. New kwargs raise `ValueError` — a
  feature, not a bug: silently ignoring a typo would turn a scheduled
  sweep into a no-op.

## Alternatives considered

- **Add APScheduler now.** Fully correct; adds a top-level dep under
  time pressure without the operational signal to justify it. The
  asyncio runner buys us time to land real builders for the other
  sweeps (synthesis, promotion, …) before we decide whether APScheduler
  or a persistent job queue (Redis + RQ, etc.) is the right long-term
  choice.
- **Run each sweep on a systemd timer on the host.** Breaks the
  "single compose stack" deploy model, leaks host-level state
  (timers + service units) that Ansible would need to manage per
  sweep, and makes rollback ("just restart the container") no longer
  sufficient.
- **Embed the scheduler in the API container.** Couples worker load to
  request latency — a cron minute where maturation runs would starve
  request handling. The separate `lifecycle-worker` container gives
  each concern its own Python process and log stream.

## Related

- [06-ingestion/lifecycle-engine](../06-ingestion/lifecycle-engine.md) §Scheduler
- [06-ingestion/maturation](../06-ingestion/maturation.md) §Failure modes
- `src/musubi/lifecycle/runner.py` (introduced here)
- `src/musubi/llm/ollama.py` (introduced here)
- `deploy/ansible/templates/docker-compose.yml.j2` (adds `lifecycle-worker` service)
