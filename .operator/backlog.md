# `.operator/` scripting backlog

Ordered by priority. Each entry: what it does, why it's worth scripting, rough scope estimate.

## Shipped

### ✅ `scripts/claimable.py`

Enumerates all slices, cross-references with `gh issue list`, flags claim-readiness, emits pasteable brief-blocks, sanity-checks slice files against ground truth.

**Motivation:** three brief-vs-reality mismatches caught by agents (wrong Issue number, wrong spec path, owns_paths sibling-convention drift) within 24 hours of agent work. Every one of them should have been caught by a mechanical tool, not by an attentive agent pausing at claim time.

**Status:** v1 shipped 2026-04-19. Future enhancements noted inline below.

### ✅ `scripts/handoff-audit.py`

Pre-review audit script. Confirms an agent's "handoff green" claim matches what's actually pushed to the PR branch: owns_paths files exist in git tree, feat commit touches owns_paths, canonical commit shape approximately present, mergeStateStatus CLEAN, frontmatter in-review, PR body `Closes #N`, CI green.

**Motivation:** PR #67 (Hana, slice-retrieval-deep, 2026-04-19) claimed 92% coverage + make check green but `src/musubi/retrieve/deep.py` was never `git add`'d. CI failed with ImportError; her local tools saw the unstaged working-tree file.

**Status:** v1 shipped 2026-04-19 (PR #80). v1.1 accepts either `docs(slice): handoff` or `chore(slice): handoff` prefix (PR #99, 2026-04-19 evening).

### ✅ `scripts/merge-flow.py`

Automates the per-slice post-merge ritual: merge, pull v2, flip slice frontmatter (status + tags + reviewed + updated + inline Status line), commit + push, close tracking Issue if `Closes #N` didn't auto-close it, audit paths-touched against owns_paths, sweep orphan branches on origin.

**Motivation:** operator-time sink. Ran this by hand ~15+ times during the 2026-04-18/19 burst. Each run was 4-6 hand-curated git/gh calls that occasionally slipped. Drift was operator-caught, not tool-caught.

**Status:** v1 shipped 2026-04-19 (PR #TBD — this PR). Supports `--dry-run`, `--skip-merge`, `--no-push`, `--force` modes.

---


### `scripts/render-prompt.py`

Automates prompt composition for the operator by templating slice details directly into a ready-to-paste markdown block. Eliminates typos and misaligned spec paths in agent briefs.

**Motivation:** brief writing was the highest-drift operator task. Templating + slot-filling closes the loop.

**Status:** v1 shipped 2026-04-19 (PR #122).


## Next up



### `scripts/board.py`

**Priority: medium.** Daily/on-demand status report for the whole slice DAG. Replaces my manual "v2 HEAD + open PRs + in-progress slices + claimable slices" investigations that I keep running.

**Output:**

```
=== Musubi Board — 2026-04-19 17:45 UTC ===

v2 HEAD:  1c075e1 docs(deploy): ADR-0019 (PR #66)

Open PRs:    3
  #70 [READY] slice-api-v0-read         | vscode-cc-sonnet47 | 2 commits, CI green
  #13 [DRAFT] slice-lifecycle-promotion | gemini-3-1-pro-nyla | 5 commits, CI green
  #27 [DRAFT] slice-retrieval-deep      | gemini-2-0-flash   | 3 commits, CI pending

Claimable now: 3
  slice-lifecycle-promotion     #13
  slice-retrieval-deep          #27
  slice-ops-ansible             #16

In-flight (not PR'd): 0

Stuck Issues: 0  (none with status:blocked)

Slice DAG health:
  done=16 · in-review=2 · ready=16
  2 slices in `in-review` limbo (no recent activity): slice-config, slice-plane-episodic
```

**Motivation:** operator-facing daily morning check. Also useful as input to a future orchestrator's scheduled polling loop.

**Estimate:** ~120 lines.

---

## Deferred

### `scripts/coverage-diff.py`

Compares per-module coverage on the current PR against the 90% plane/retrieve floor and the 85% general floor. Surfaces "you dropped below 90% on `src/musubi/planes/foo`" as a hard fail, which `make check` currently only partially reports.

**Priority: low.** Annoying papercut; not a bug-prevention tool.

### `scripts/adr-new.py`

`python3 adr-new.py "some decision"` → creates `docs/Musubi/13-decisions/00NN-some-decision.md` with frontmatter + section skeleton + correct NN numbering based on existing ADRs.

**Priority: low.** Fixed-cost per ADR; only ~5 ADRs/year probably.

### Enhancements to `claimable.py`

- `--json` output mode for machine consumption.
- Flag when an agent owns multiple in-progress slices (caught by accident this session).
- Flag in-review limbo (slice is in-review but >48h without commits on its branch).
- Detect phantom Issue claims (label flipped but no matching frontmatter update).
- Cache `gh issue list` for 5 minutes to avoid rate-limit churn on repeat invocations.

---

## Not scripting (manual operator work remains)

These are judgment calls that resist automation:

- Writing reviewer Should-fix vs Nit classifications on PRs.
- Deciding which coaching prompt fits a given agent misstep.
- Choosing the next slice to recommend for each agent given strengths.
- Orchestrator-level rebalancing (if one agent is blocked, move them).

Automating these lives in the orchestrator agent itself (per `~/Projects/musubi-orchestrator-brief.md`), not in `.operator/scripts/`.

---

## How to add a new entry

When you catch yourself doing a task by hand ≥ 3 times, promote it to this backlog. Note the trigger frequency + scope estimate. If it's urgent (blocks current work), move it to "Next up". Otherwise it lives in "Deferred" until another operator uses it and confirms priority.
