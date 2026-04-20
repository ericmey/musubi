#!/usr/bin/env python3
"""merge-flow.py — automate the per-slice post-merge ritual.

Operator-only tool. Runs the mechanical steps performed on every slice PR
after the handoff audit is green:

    1. Merge the PR (`gh pr merge --squash --admin --delete-branch`).
    2. Check out v2, fast-forward pull.
    3. Flip the slice's frontmatter:
         status: in-review → done
         tags:   status/in-review → status/done
         reviewed: false → true
         updated: <today>
       …and the inline `**Phase:** … · **Status:** `in-review`` line to
       match (if present).
    4. Commit + push to v2.
    5. Close the tracking Issue if the PR body didn't auto-close it via
       `Closes #N`.
    6. Audit: warn if the PR touched files outside the slice's owns_paths
       (tolerated: cross-slice tickets under _inbox/, the shared
       `00-index/work-log.md`, and specs named in a `spec-update:` trailer).
    7. Warn about any orphan remote branches matching `slice/<slice-id>`.

Usage:
    python3 .operator/scripts/merge-flow.py <pr-number>
    python3 .operator/scripts/merge-flow.py <pr-number> --dry-run
    python3 .operator/scripts/merge-flow.py <pr-number> --skip-merge    # PR already merged
    python3 .operator/scripts/merge-flow.py <pr-number> --no-push       # commit, don't push

Exit codes:
    0 — ritual completed without surfacing errors
    1 — one or more steps failed or surfaced operator-level concerns
    2 — usage error, PR not found, working tree unsuitable, or similar

Deps: Python 3.12+ stdlib · PyYAML · `gh` CLI · `git` on PATH.
Library: imports load_slices, Slice, REPO_ROOT from claimable.py.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Share parsers + data classes with claimable.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from claimable import REPO_ROOT, Slice, load_slices

# ---- ANSI helpers (no-op when not a TTY) -----------------------------------


def _color(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def green(s: str) -> str:
    return _color(s, "32")


def red(s: str) -> str:
    return _color(s, "31")


def yellow(s: str) -> str:
    return _color(s, "33")


def dim(s: str) -> str:
    return _color(s, "2")


def bold(s: str) -> str:
    return _color(s, "1")


# ---- Data -------------------------------------------------------------------


@dataclass
class PRInfo:
    number: int
    head_ref: str
    head_sha: str
    base_ref: str
    title: str
    body: str
    merge_state: str
    state: str  # OPEN / MERGED / CLOSED
    files: list[str]


# ---- gh / git wrappers ------------------------------------------------------


def _run(cmd: list[str], check: bool = True) -> str:
    return subprocess.run(cmd, check=check, capture_output=True, text=True).stdout


def load_pr(pr_number: int) -> PRInfo:
    data = json.loads(
        _run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "number,headRefName,headRefOid,baseRefName,title,body,mergeStateStatus,state,files",
            ]
        )
    )
    return PRInfo(
        number=data["number"],
        head_ref=data["headRefName"],
        head_sha=data["headRefOid"],
        base_ref=data["baseRefName"],
        title=data["title"],
        body=data.get("body") or "",
        merge_state=data.get("mergeStateStatus") or "UNKNOWN",
        state=data.get("state") or "OPEN",
        files=[f["path"] for f in data.get("files") or []],
    )


def git(*args: str, check: bool = True) -> str:
    return _run(["git", *args], check=check)


def git_working_tree_clean_enough() -> tuple[bool, str]:
    """Is the working tree safe to operate on?

    Tolerates untracked files + modifications to docs/Musubi/.obsidian/
    (operator's Obsidian UI state, frequently changes without being committed).
    Fails only on modifications to tracked files outside that allowlist.
    """
    out = git("status", "--porcelain")
    bad: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:]
        if code.startswith("??"):  # untracked — ignored
            continue
        if path.startswith("docs/Musubi/.obsidian/"):
            continue
        bad.append(line)
    if bad:
        return False, "\n".join(bad)
    return True, ""


# ---- Slice matching ---------------------------------------------------------


_BRANCH_SLICE_RE = re.compile(r"^slice/(slice-[a-z0-9-]+)$")
_CLOSES_RE = re.compile(r"^Closes\s+#(\d+)", re.M | re.I)


def match_slice_from_branch(head_ref: str, slices: dict[str, Slice]) -> Slice | None:
    m = _BRANCH_SLICE_RE.match(head_ref)
    if not m:
        return None
    return slices.get(m.group(1))


def match_slice_from_closes_issue(
    body: str,
    slices: dict[str, Slice],
) -> tuple[int | None, Slice | None]:
    """Parse `Closes #N` and look up the matching slice by its Issue number."""
    m = _CLOSES_RE.search(body)
    if not m:
        return None, None
    issue_num = int(m.group(1))
    for s in slices.values():
        if s.issue and s.issue.number == issue_num:
            return issue_num, s
    return issue_num, None


# ---- Step 3-4: slice frontmatter flip --------------------------------------


def flip_slice_frontmatter(slice_path: Path, dry_run: bool = False) -> tuple[bool, str]:
    """Flip frontmatter in-review → done + inline Status line.

    Only fires when the slice is currently `status: in-review` — this is the
    implementation-landing case. Slice carve PRs (operator adds a new
    `status: ready` slice file) and non-slice PRs must NOT trigger a flip.
    Returns (changed, preview). Idempotent: re-running on already-done or
    still-ready slices is a no-op.
    """
    original = slice_path.read_text(encoding="utf-8")

    # Gate on current status. `in-review` is the only state where flip is valid.
    # `ready` → a carve PR that hasn't yet been claimed; `in-progress` → agent
    # handed off incorrectly; `done`/`retired` → already flipped.
    status_match = re.search(r"^status:\s*(\S+)\s*$", original, flags=re.M)
    if not status_match:
        return False, "no status field in frontmatter — not a slice file"
    current_status = status_match.group(1)
    if current_status != "in-review":
        return False, (
            f"status is '{current_status}' — flip only fires on 'in-review'. "
            "If this PR did implement the slice and the agent forgot to flip "
            "to in-review before handoff, fix it manually and re-run."
        )

    text = original
    # Frontmatter: status
    text = re.sub(r"^status:\s*in-review\s*$", "status: done", text, count=1, flags=re.M)
    # Frontmatter: tags (may be an inline list with many other tags)
    text = re.sub(r"status/in-review", "status/done", text, count=1)
    # Frontmatter: reviewed
    text = re.sub(r"^reviewed:\s*false\s*$", "reviewed: true", text, count=1, flags=re.M)
    # Frontmatter: updated → today
    today = date.today().isoformat()
    text = re.sub(r"^updated:\s*\S+\s*$", f"updated: {today}", text, count=1, flags=re.M)
    # Inline Status line, e.g. "**Phase:** 4 Planes · **Status:** `in-review` · **Owner:**"
    text = re.sub(
        r"(\*\*Status:\*\*\s+)`in-review`",
        r"\1`done`",
        text,
        count=1,
    )

    if text == original:
        return False, "no changes — slice already done or frontmatter absent"

    if not dry_run:
        slice_path.write_text(text, encoding="utf-8")

    # Tiny diff preview for operator visibility
    diff_lines: list[str] = []
    for a, b in zip(original.splitlines(), text.splitlines(), strict=False):
        if a != b:
            diff_lines.append(f"  - {a}")
            diff_lines.append(f"  + {b}")
    preview = "\n".join(diff_lines[:20])
    return True, preview


# ---- Step 5: Issue close --------------------------------------------------


def issue_is_closed(issue_number: int) -> bool:
    out = _run(["gh", "issue", "view", str(issue_number), "--json", "state"])
    return json.loads(out)["state"] == "CLOSED"


def close_issue(issue_number: int, pr_number: int, dry_run: bool = False) -> None:
    comment = (
        f"Closed by PR #{pr_number} (merged to v2). "
        f"Auto-close didn't fire because the PR body didn't use the `Closes #{issue_number}` "
        f"keyword syntax exactly."
    )
    if dry_run:
        print(dim(f"  would run: gh issue close {issue_number} --comment '<see body>'"))
        return
    _run(["gh", "issue", "close", str(issue_number), "--comment", comment])


# ---- Step 6: path audit ----------------------------------------------------


_TOLERATED_OUTSIDE = {
    "docs/Musubi/00-index/work-log.md",
    "pyproject.toml",
    "uv.lock",
}
_TOLERATED_PREFIX = (
    "docs/Musubi/_inbox/cross-slice/",
    "docs/Musubi/13-decisions/",  # ADR updates via spec-update: trailer
)


def _path_is_under(path: str, owns: list[str]) -> bool:
    path = path.lstrip("./")
    for raw in owns:
        o = raw.strip().lstrip("./")
        if o.endswith((".py", ".yaml", ".yml", ".md")) and (path == o or path.endswith("/" + o)):
            return True
        if o.endswith("/") and path.startswith(o):
            return True
        if not o.endswith("/") and (path == o or path.startswith(o + "/")):
            return True
    return False


def audit_paths(pr: PRInfo, s: Slice) -> list[str]:
    """Return a list of files touched that are NOT under owns_paths."""
    violations: list[str] = []
    for f in pr.files:
        if _path_is_under(f, s.owns_paths):
            continue
        if f in _TOLERATED_OUTSIDE:
            continue
        if f.startswith(_TOLERATED_PREFIX):
            continue
        # Specs updated via a spec-update: trailer in a commit message are
        # explicitly in-scope; a full trailer-scan would be more precise but
        # the spec files live under docs/Musubi/ so allowlist by prefix
        # below is already forgiving enough.
        if f.startswith("docs/Musubi/07-interfaces/") or f.startswith(
            "docs/Musubi/04-data-model/"
        ):
            # reviewer still sees these in the preview
            violations.append(f"{f}  (spec update — likely intentional via spec-update: trailer)")
            continue
        # Slice file itself is always owned
        if f == f"docs/Musubi/_slices/{s.id}.md":
            continue
        violations.append(f)
    return violations


# ---- Step 7: orphan branches -----------------------------------------------


def orphan_branches(slice_id: str) -> list[str]:
    """Query origin for branches matching `slice/<slice-id>*` authoritatively.

    Uses `git ls-remote` rather than `git branch -r --list`, because the local
    remote-tracking cache goes stale the moment GitHub auto-deletes a merged
    branch (via --delete-branch on merge or branch-protection settings).
    `ls-remote` hits the origin directly and returns only references that
    actually exist server-side.
    """
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/slice/{slice_id}*"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return []
    # Each line: "<sha>\trefs/heads/slice/<name>"
    refs: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        ref = parts[1].removeprefix("refs/heads/")
        refs.append(ref)
    return refs


def delete_orphan_branch(branch_name: str) -> tuple[bool, str]:
    """Delete an origin branch; return (ok, message).

    Tolerates the race where the branch was already deleted between the
    orphan sweep and this call (another agent / auto-delete / manual rm).
    Returns ok=True with a soft message in that case; only returns ok=False
    for unexpected failures.
    """
    result = subprocess.run(
        ["git", "push", "origin", "--delete", branch_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, f"deleted origin/{branch_name}"
    stderr = (result.stderr or "").lower()
    # GitHub's stderr when the branch is already gone:
    #   error: unable to delete '<name>': remote ref does not exist
    if "remote ref does not exist" in stderr:
        return True, f"origin/{branch_name} already deleted (race-ok)"
    return False, f"delete failed: {stderr.strip()[:120]}"


# ---- Main -------------------------------------------------------------------


def _print_header(msg: str) -> None:
    print()
    print(bold(msg))


def run(args: argparse.Namespace) -> int:
    dry = bool(args.dry_run)
    if dry:
        print(yellow("DRY RUN — no side effects"))

    # Load PR
    _print_header(f"▶ PR #{args.pr_number}")
    try:
        pr = load_pr(args.pr_number)
    except subprocess.CalledProcessError as e:
        print(red(f"✗ gh pr view failed: {e.stderr or e}"))
        return 2
    print(f"  title: {pr.title}")
    print(f"  head:  {pr.head_ref} @ {pr.head_sha[:10]}")
    print(f"  state: {pr.state}  mergeState: {pr.merge_state}")

    # Match to slice
    _print_header("▶ matching PR to slice")
    slices = load_slices()
    s = match_slice_from_branch(pr.head_ref, slices)
    issue_from_closes, s_from_closes = match_slice_from_closes_issue(pr.body, slices)
    if s is None and s_from_closes is not None:
        s = s_from_closes
    if s is None:
        print(
            yellow(
                "  ⚠ could not match PR to a slice by branch name or `Closes #N`. "
                "Slice flip + audit will be skipped; merge + Issue-close still run."
            )
        )
    else:
        print(f"  matched slice: {s.id}  (status={s.status})")
        if s.status == "done":
            print(dim("  (slice already done — flip step will no-op)"))

    # Working tree gate
    clean, detail = git_working_tree_clean_enough()
    if not clean and not dry:
        print(red("✗ working tree has modifications to tracked files:"))
        print(detail)
        print(dim("  stash or commit them, then re-run"))
        return 2

    # Merge (unless skipped)
    if args.skip_merge:
        print(dim("\n▶ merge SKIPPED (--skip-merge)"))
    elif pr.state == "MERGED":
        _print_header("▶ merge")
        print(dim("  PR already merged; skipping"))
    elif pr.state == "CLOSED":
        _print_header("▶ merge")
        print(red(f"✗ PR state={pr.state}; refusing to merge"))
        return 1
    else:
        _print_header("▶ merge")
        if pr.merge_state != "CLEAN":
            print(
                yellow(
                    f"  ⚠ mergeStateStatus={pr.merge_state}; merging with --admin would succeed "
                    "but usually indicates failing CI. Check `gh pr checks` first."
                )
            )
            if not args.force:
                print(dim("  pass --force to merge anyway"))
                return 1
        if dry:
            print(dim(f"  would run: gh pr merge {pr.number} --squash --admin --delete-branch"))
        else:
            _run(["gh", "pr", "merge", str(pr.number), "--squash", "--admin", "--delete-branch"])
            print(green("  ✓ merged"))

    # Checkout v2 + pull
    _print_header("▶ sync v2")
    if dry:
        print(dim("  would run: git checkout v2 && git pull --ff-only origin v2"))
    else:
        git("checkout", "v2")
        git("pull", "--ff-only", "origin", "v2")
        print(green("  ✓ v2 synced"))

    # Slice flip (if matched)
    if s is not None:
        _print_header(f"▶ flip slice frontmatter: {s.id}")
        changed, preview = flip_slice_frontmatter(s.path, dry_run=dry)
        if changed:
            print(preview)
            if not dry:
                git("add", str(s.path.relative_to(REPO_ROOT)))
                msg = (
                    f"chore(slices): mark {s.id} done\n\n"
                    f"PR #{pr.number} merged. Flip frontmatter in-review -> done\n"
                    f"and the inline Status line."
                )
                git("commit", "-m", msg)
                if not args.no_push:
                    git("push", "origin", "v2")
                    print(green("  ✓ committed + pushed"))
                else:
                    print(yellow("  ⚠ committed, NOT pushed (--no-push)"))
        else:
            print(dim("  no-op — slice already done"))

    # Close Issue if needed
    _print_header("▶ Issue close")
    issue_num = issue_from_closes or (s.issue.number if s and s.issue else None)
    if issue_num is None:
        print(dim("  no Issue linked — nothing to close"))
    elif issue_is_closed(issue_num):
        print(green(f"  ✓ Issue #{issue_num} already closed"))
    else:
        print(yellow(f"  ⚠ Issue #{issue_num} still OPEN — closing with reference to PR"))
        close_issue(issue_num, pr.number, dry_run=dry)
        print(green(f"  ✓ Issue #{issue_num} closed") if not dry else "")

    # Path audit
    if s is not None:
        _print_header("▶ path audit")
        violations = audit_paths(pr, s)
        if not violations:
            print(green("  ✓ all files under owns_paths or tolerated allowlist"))
        else:
            for v in violations:
                print(yellow(f"  ⚠ {v}"))

    # Orphan branches
    if s is not None:
        _print_header("▶ orphan branch sweep")
        orphans = orphan_branches(s.id)
        if not orphans:
            print(green("  ✓ no orphan branches on origin"))
        else:
            for o in orphans:
                print(yellow(f"  ⚠ {o}"))
                # Only delete if it's exactly slice/<slice-id> (not slice-id-followup etc.)
                if not dry and o == f"slice/{s.id}":
                    ok, msg = delete_orphan_branch(o)
                    if ok:
                        print(green(f"    ✓ {msg}"))
                    else:
                        print(yellow(f"    ⚠ {msg}"))

    print()
    print(green(f"✓ merge-flow complete for PR #{pr.number}"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the per-slice post-merge ritual.")
    ap.add_argument("pr_number", type=int, help="PR number to merge + post-process")
    ap.add_argument("--dry-run", action="store_true", help="print plan, no side effects")
    ap.add_argument(
        "--skip-merge", action="store_true", help="PR already merged; just run the flip+close"
    )
    ap.add_argument("--no-push", action="store_true", help="commit the slice flip but don't push")
    ap.add_argument("--force", action="store_true", help="merge even if mergeStateStatus != CLEAN")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
