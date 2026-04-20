# `.operator/` — operator-side scaffolding

Process scaffolding used by the human operator (Eric) + the reviewer-role Claude session to drive the multi-agent fleet. None of this is consumed by slice-worker agents during a slice — it's tooling for the layer above the slice work.

## Current contents

```
.operator/
├── README.md                                   ← you are here
├── backlog.md                                  ← scripting targets, prioritised
├── scripts/
│   └── claimable.py                            ← enumerate slices + claim-readiness + brief-block emitter
└── prompts/
    ├── slice-start.md.template                 ← brief sent to an agent to start a new slice
    └── followup-next-slice.md.template         ← after clean merge: docs touch-up + next slice
```

## Why this exists

Across the multi-agent rounds in this session, operator-role work — brief assembly, review verdict narration, next-slice handoffs, cross-slice tracking — was done from scratch each time. Same patterns kept repeating:

1. Brief author looks up Issue number + spec path + owns_paths from memory → gets it wrong → agent catches the mismatch at claim time → operator-side reconcile PR lands to fix it.
2. "Is slice X claimable right now?" is a manual dep-check walk through ~30 slice files.
3. Merge → frontmatter flip → cleanup → next-slice prompt is a five-step ritual per agent handoff.

Codifying these:
- Makes each round's agent prompts consistent (the **"clockwork"** pattern Eric asked for).
- Extracts the slot structure so future agents — or a future orchestrator agent — can render them mechanically.
- Forms the operator-side bridge to `~/Projects/musubi-orchestrator-brief.md`: specifically the Tier 1 "mechanical plumbing" layer.

## The scripts

### `scripts/claimable.py`

Operator's first line of defense against brief-vs-reality mismatches.

```bash
# list all slices with claim-readiness + Issue number:
python3 .operator/scripts/claimable.py

# filter to only slices claimable right now (all deps done):
python3 .operator/scripts/claimable.py list --only-claimable

# emit the slice-specific brief-block for pasting into an agent prompt:
python3 .operator/scripts/claimable.py brief slice-lifecycle-promotion

# sanity-check a slice file against ground truth (Issue match, spec file
# existence, owns_paths sibling-convention drift, depends-on consistency):
python3 .operator/scripts/claimable.py verify slice-retrieval-fast
```

Reads `docs/Musubi/_slices/slice-*.md` + `gh issue list --label slice`. Cross-references to produce a single source of truth for:

- What's claimable right now (strict: deps all `status: done` on v2 or their Issue is CLOSED).
- Issue number for each slice (parsed from GitHub Issue titles matching `slice: slice-xxx`).
- Spec file paths (resolved from frontmatter wikilinks; flagged if missing on disk).
- Owned + forbidden paths (parsed from slice file sections).
- Sibling-convention drift (flags e.g. `fast_path.py` when siblings are `hybrid.py` / `scoring.py` / `rerank.py`).
- Parallel agents (from `status: in-progress` / `in-review` slices).

**Use it every time you're about to write an agent brief.** The `brief` subcommand emits a pre-filled block ready to paste — no hand-authored Issue numbers or path references.

Deps: Python 3.12+ · PyYAML · `gh` CLI on PATH.

## The prompt templates

`prompts/slice-start.md.template` and `prompts/followup-next-slice.md.template` capture the structure operator-side briefs converged to after ~10 rounds of manual iteration. Slot syntax is `{{double-braces}}`; each template ends with a `## Slots` reference section documenting what each slot expects.

Currently hand-filled. Future integration: `claimable.py brief <slice-id>` populates the slot values so the full prompt renders mechanically.

## How this directory is meant to evolve

Per the backlog (`backlog.md`), the roadmap is:

1. **`claimable.py`** (shipped) — claim-readiness + brief-block emitter.
2. **`merge-flow.py`** — post-merge ritual: flip slice frontmatter to `done`, close Issue if still open, clean up orphan branches, update work-log.
3. **`render-prompt.py`** — composes claimable.py + the prompt templates into a fully-formed agent session prompt.
4. **`board.py`** — daily digest / weekly status view.

Each script is operator-side, idempotent, safe to re-run. None modify vault content except `merge-flow.py` (which only makes commits the operator would make anyway).

## Conventions

- **Shebang + chmod +x.** Scripts are runnable as `.operator/scripts/<name>.py` from repo root. `python3` required.
- **No ambient state.** Scripts read the repo + `gh` API; no `~/.musubi-operator/` caches, no database. Everything derivable.
- **Stderr for warnings, stdout for pasteable output.** Makes `brief` output safely redirectable to clipboard / session prompt.
- **Exit codes:** 0 = clean, 1 = policy violation (use for CI gating later), 2 = usage error.

## Not to be confused with

- `.agent-brief.*.local.md` files at repo root — those are the **filled** briefs (gitignored, copied into each agent session). The `.operator/prompts/` templates are what filled briefs are derived from.
- `docs/Musubi/_tools/` — vault-hygiene tooling consumed by agents during a slice (`make agent-check`, `make tc-coverage`). Different audience, different concern.
- `~/Projects/musubi-orchestrator-brief.md` — the "next project" design for a real orchestrator agent. This directory is the manual-precursor: the mechanical operations the future orchestrator will automate.
