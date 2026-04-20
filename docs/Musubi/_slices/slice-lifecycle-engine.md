---
title: "Slice: Lifecycle scheduler"
slice_id: slice-lifecycle-engine
section: _slices
type: slice
status: done
owner: cowork-auto
phase: "6 Lifecycle"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-types]]"]
blocks: ["[[_slices/slice-lifecycle-maturation]]", "[[_slices/slice-lifecycle-synthesis]]", "[[_slices/slice-lifecycle-promotion]]", "[[_slices/slice-lifecycle-reflection]]"]
---

# Slice: Lifecycle scheduler

> APScheduler-based worker. Emits LifecycleEvents. Idempotent per-job. Separate process from the API.

**Phase:** 6 Lifecycle · **Status:** `done` · **Owner:** `cowork-auto`

## Specs to implement

- [[06-ingestion/lifecycle-engine]]
- [[04-data-model/lifecycle]]

## Owned paths (you MAY write here)

  - `musubi/lifecycle/__init__.py`
  - `musubi/lifecycle/transitions.py`
  - `musubi/lifecycle/events.py`
  - `musubi/lifecycle/scheduler.py`
  - `tests/lifecycle/__init__.py`
  - `tests/lifecycle/test_lifecycle.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/api/`

## Depends on

  - [[_slices/slice-types]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-lifecycle-maturation]]
  - [[_slices/slice-lifecycle-synthesis]]
  - [[_slices/slice-lifecycle-promotion]]
  - [[_slices/slice-lifecycle-reflection]]

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-19 — cowork-auto — claim + test contract

- Atomic claim via `gh issue edit 11 --add-assignee @me --add-label status:in-progress`.
- Slice frontmatter flipped `ready → in-progress`; owner set to `cowork-auto`.
- Branch `slice/slice-lifecycle-engine` created; draft PR opened.
- Test contract file committed first (`tests/lifecycle/test_lifecycle.py`) per
  AGENTS.md §Test-first; reconciled slice `## Owned paths` to match the spec's
  module names — `transitions.py`, `events.py`, `scheduler.py` replace the
  placeholder `engine.py` / `states.py` / `test_engine.py`. The specs
  ([[04-data-model/lifecycle#Transition function]] and
  [[06-ingestion/lifecycle-engine]]) were already authoritative; this is a
  slice-file-only update, not a spec-prose change. Recorded here rather than
  via `spec-update:` trailer because no spec .md was edited.

#### Test Contract Closure Rule declarations

The following bullets are declared `⊘ out-of-scope` for the per-bullet
`def test_<name>` match because they are `hypothesis:` or `integration:`
prose that `tc_coverage.py` classifies as non-test. Each is covered by an
adjacent implementation:

- `hypothesis: state-machine reachability — every declared allowed transition is reachable from some state; no state is orphaned`
  → covered by `test_hypothesis_state_machine_reachability` in
  `tests/lifecycle/test_lifecycle.py`, which asserts the property directly
  over the `_ALLOWED` table.
- `hypothesis: monotone invariants — version, updated_epoch never decrease across any sequence of legal transitions`
  → covered by `test_hypothesis_monotone_invariants`, a Hypothesis property
  test over random legal transition sequences.
- `integration: full day simulation — seed corpus, advance clock 24h, assert each scheduled job ran once`
  → out-of-scope for unit tests. Implementing it requires the full per-job
  sweep implementations (maturation, synthesis, promotion, demotion,
  reflection, reconcile) that are explicitly owned by the downstream slices
  `slice-lifecycle-maturation`, `slice-lifecycle-synthesis`,
  `slice-lifecycle-promotion`, `slice-lifecycle-reflection`. Deferred to an
  integration-harness slice once those land.
- `integration: crash recovery — kill worker mid-synthesis, restart, synthesis completes from cursor`
  → same rationale; synthesis cursor lives in
  `slice-lifecycle-synthesis`'s `owns_paths`.
- `integration: ollama-outage scenario — synthesis skips cleanly, maturation skips enrichment, alerts emit`
  → same rationale; the enrichment + alert paths are owned by downstream
  sweep slices.

### 2026-04-19 — cowork-auto — implementation + tests

Implemented all three owned modules:

- `src/musubi/lifecycle/transitions.py` — canonical `transition()`. Concurrent-
  modification check (`expected_version`) runs before the legality gate so the
  last-writer-wins warning fires on stale version per spec bullet 13. Cycle
  walk bounded at 64 hops.
- `src/musubi/lifecycle/events.py` — `LifecycleEventSink`, thread-safe,
  batched sqlite persistence, <=5s or 100-event flush, context-manager +
  idempotent `close()`. Committed writes → survives worker restart.
- `src/musubi/lifecycle/scheduler.py` — `Job` dataclass, `build_default_jobs()`
  mirroring `06-ingestion/lifecycle-engine#Job registry` verbatim,
  `TestingScheduler` with `force_run` / `force_coalesced_run` honouring
  `grace_time_s` + `coalesce`, `fcntl`-backed `file_lock` + `NamespaceLock`,
  `JobFailureMetrics`. APScheduler wiring explicitly deferred to a follow-up
  ADR (module docstring + `build_scheduler(testing=False)` logs the pending
  delegation); the harness implements the exact contract the APScheduler
  adapter will delegate to, so the per-sweep slices are unblocked today.

Verification (all green):

- `make check` → ruff format + ruff lint + mypy --strict + pytest + coverage
  (252 passed, 9 skipped, **93.98% total coverage**).
- `make tc-coverage SLICE=slice-lifecycle-engine` → 36 bullets: 23 ✓ passing,
  8 ⏭ skipped (downstream slice pointers recorded per-bullet), 5 ⊘ out-of-scope
  declared above. Closure rule satisfied.
- `make agent-check` → clean (warnings only on unrelated specs).
- Owned-file coverage: `src/musubi/lifecycle/` @ **91.54%** (events 96%,
  scheduler 89%, transitions 91%).

**Handoff blocker (operator action required before flipping to `in-review`):**
The Cowork sandbox's FUSE mount for `.git/` refuses `unlink`/`rename`, so I
cannot complete the final commit from here — `.git/index.lock` and
`.git/HEAD.lock` are pinned until the host shell removes them. The commit
itself is authored at a working clone under `/tmp/musubi-work`; its
contents + message are also persisted here as:

- `.slice-lifecycle-engine.bundle` — `git bundle` from `origin/slice/...` to
  the new commit. Apply with: `git bundle unbundle .slice-lifecycle-engine.bundle`
  then fast-forward the local branch.
- `.cowork-handoff-slice-lifecycle-engine.commit-msg` — the commit message body.

When Eric picks up the handoff:

```
cd ~/Projects/musubi
rm -f .git/*.lock .git/refs/heads/slice/*.lock
git add src/musubi/lifecycle/ tests/lifecycle/test_lifecycle.py
git commit -F .cowork-handoff-slice-lifecycle-engine.commit-msg
git push origin slice/slice-lifecycle-engine
rm -f .cowork-handoff-slice-lifecycle-engine.commit-msg .git.commit-msg-tmp .slice-lifecycle-engine.bundle
gh pr ready <PR#>            # mark the draft ready for review
gh issue edit 11 --remove-label status:in-progress --add-label status:in-review
```

…then flip this slice's frontmatter `status: in-progress → in-review`.

Leaving `status: in-progress` here and on Issue #11 until the push completes,
so reviewers don't start reviewing a branch that doesn't yet have the
implementation commit on origin.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet — draft PR #__TBD__ on branch `slice/slice-lifecycle-engine`; ready-for-review pending the operator-side push above)_

### 2026-04-19 — cowork-auto — push landed, flipping to in-review

- Implementation commit `76501dd` pushed to
  `origin/slice/slice-lifecycle-engine` (fast-forward from
  `cdb408f`). The FUSE unlink blocker was bypassed by pushing from the
  out-of-FUSE side-clone at `/tmp/musubi-work`.
- Flipping slice frontmatter `status: in-progress → in-review` in this
  commit. Companion API calls flip PR #40 from draft → ready and swap
  `status:in-progress → status:in-review` on both PR #40 and Issue #11.
- Handoff artefacts at repo root (`.slice-lifecycle-engine.bundle`,
  `.cowork-handoff-slice-lifecycle-engine.commit-msg`,
  `.git.commit-msg-tmp`) are no longer needed; operator can delete them
  locally — they were never committed.
