---
name: spec-check
description: Run the Musubi vault hygiene gates — frontmatter, slice DAG, Test Contract presence, broken wikilinks, stale status. Use before opening a PR that touches `docs/Musubi/` or as a quick health check of the vault.
---

# Skill: spec-check

Run every vault-hygiene check Musubi ships and produce a one-screen report. Designed to be fast (< 15 s on the current vault) and to be runnable from any agent (not Codex specific).

## When to invoke

- Before opening a PR that touches `docs/Musubi/`.
- When the user says: "check the vault", "anything stale?", `/spec-check`.
- After pulling an upstream change that touched specs, to sanity-check your working copy.
- **Before handoff, to generate the Test Contract coverage matrix** that the PR template requires. Run `make tc-coverage SLICE=<slice-id>` and paste the output into the PR description. Anything marked `✗ missing` blocks merge — either write the test, add `@pytest.mark.skip(reason=...)`, or declare out-of-scope in the slice's work log, then re-run.

## Do not invoke when

- The user asked for a code-only check → use `make check` instead.
- You're mid-edit and the tree isn't in a reviewable state.

## Instructions

### 1. Run the vault-state gates

```bash
cd ~/Projects/musubi  # or wherever the repo is
make agent-check
```

`agent-check` covers:

- Frontmatter schema (every note has `title`, `section`, `type`, `status`, `tags`, `updated`).
- Slice DAG (no cycles, every `depends-on` resolves to a real slice file, `blocks:` is the inverse of `depends-on` across the set).
- `owns_paths` / `forbidden_paths` do not conflict across slices (two slices can't claim the same path).
- Every `slice-*.md` has a Definition-of-Done section and a Work-log section.
- Every `_slices/slice-<id>.md` has a matching `status:` value (`ready | in-progress | in-review | blocked | done`).

If `make agent-check` fails, stop — the PR that introduces vault state violating the gates is not mergeable.

### 2. Check for stale status

Print to stdout (agent-readable) in a table:

```bash
echo "=== slice status counts ==="
for s in in-progress in-review blocked ready done; do
  n=$(grep -l "^status: $s" docs/Musubi/_slices/slice-*.md 2>/dev/null | wc -l | tr -d ' ')
  printf "  %-12s %s\n" "$s" "$n"
done
```

Flag anything surprising:

- `in-progress` slices with no commit on any `slice/<id>` branch in the last 7 days → likely abandoned; suggest the user run `pick-slice` to re-home them or mark `blocked`.
- `in-review` slices with a PR that's been open > 7 days → suggest nudge.
- `blocked` slices: list them with the cross-slice ticket file they reference, so the user can check if the blocker is resolved.

### 3. Lock file sanity

```bash
ls docs/Musubi/_inbox/locks/ 2>/dev/null
```

Every lock file must map to:

- An `in-progress` slice with matching frontmatter, and
- An open GitHub Issue with `status:in-progress` label.

Orphan locks (file exists, no matching Issue or frontmatter) → suggest `git rm` — they're stale.

### 4. Wikilink health (lightweight)

Walk every `.md` file in `docs/Musubi/` and grep for `[[...]]` references. For each, check:

- Target file exists (allow optional `#section` suffix).
- Anchor resolves to a heading (skip if target has no `.md`).

Output just the broken ones. Do NOT auto-fix — the user reviews the list and decides.

```bash
python3 - <<'PY'
import pathlib, re, sys
root = pathlib.Path("docs/Musubi")
pattern = re.compile(r"\[\[([^\]|#]+)")
errors = []
md_files = {p.relative_to(root).with_suffix("") for p in root.rglob("*.md")}
for md in root.rglob("*.md"):
    for m in pattern.finditer(md.read_text(errors="ignore")):
        target = pathlib.PurePosixPath(m.group(1))
        if target not in md_files and target.with_suffix("") not in md_files:
            errors.append((md.relative_to(root), str(target)))
for src, tgt in errors[:40]:
    print(f"  {src}: [[{tgt}]]")
print(f"total broken wikilinks: {len(errors)}")
PY
```

### 5. Print the verdict

```
SPEC-CHECK REPORT  (YYYY-MM-DD HH:MM)

  slices: {in-progress: n, in-review: m, blocked: k, ready: r, done: d}
  locks: {active: a, orphan: 0}
  broken wikilinks: 0
  stale in-progress (>7d no commit): 0
  stale in-review (>7d no review): 0

  make agent-check: PASS

All clear to open/merge PRs touching the vault.
```

If anything is non-zero, list the specific items, not a summary count.

## Output contract

This skill prints to stdout in plain text. It does **not** modify any files. Other agents pipe this report into PR bodies or paste it into issue comments.

## If you find real problems

- Report them. Do not fix them as part of this skill.
- Suggest the right next skill or agent (`pick-slice` to re-home a stale slice, `musubi-spec-author` to revise a broken ADR, etc.).
