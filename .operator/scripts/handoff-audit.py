#!/usr/bin/env python3
"""handoff-audit.py — verify an agent's handoff matches reality.

Operator-only tool. Run this BEFORE reviewing a PR an agent has flipped
to `in-review` + claimed "all handoff checks green". Catches the class
of bugs where the agent's local tools reported green but the pushed
commit graph doesn't actually contain what they wrote.

Surfaced by PR #67 (Hana, slice-retrieval-deep, 2026-04-19): agent
claimed 92% coverage on `src/musubi/retrieve/deep.py` + make check
passed locally, but `deep.py` was never `git add`'d and the pushed
branch didn't contain it. CI failed with ImportError; her local tools
were happily seeing the unstaged working-tree file.

Usage:
    python3 .operator/scripts/handoff-audit.py <pr-number>

Exit codes:
    0 — audit clean, PR ready to review
    1 — one or more audit checks failed (see stdout for specifics)
    2 — usage error or PR not found

Checks performed:
    1. owns_paths: every path declared in the slice's `## Owned paths`
       section exists in the PR branch's git tree (`git ls-tree` on
       the head SHA — not the working tree)
    2. feat commit: commit graph between v2..HEAD contains at least one
       `feat(...)` commit, and that commit touches a file under one of
       the owns_paths directories
    3. canonical 7-commit shape: approximately chore(take) → chore(flip)
       → chore(lock) → test → feat → docs(handoff) → chore(lock-release)
       present. Deviations WARN (non-blocking); missing feat or
       docs(handoff) are HARD failures
    4. mergeStateStatus: `gh pr view --json mergeStateStatus` == CLEAN
       (not UNSTABLE, DIRTY, or UNKNOWN)
    5. frontmatter: slice file on branch tip has `status: in-review`
       (not still `in-progress` or already `done`)
    6. PR body: first line matches `^Closes #<issue>\\.` where <issue>
       matches the Issue derived from the slice-id
    7. CI status: `gh pr checks` shows all required checks `pass`

Deps: Python 3.12+ stdlib · PyYAML · `gh` CLI on PATH.
Library: imports load_slices, load_issues, Slice from
.operator/scripts/claimable.py.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Share the parsers from claimable.py in the same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
from claimable import REPO_ROOT, Slice, load_issues, load_slices

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


# ---- Data --------------------------------------------------------------------


@dataclass
class PRInfo:
    number: int
    head_ref: str
    head_sha: str
    base_ref: str
    title: str
    body: str
    merge_state: str  # CLEAN / UNSTABLE / DIRTY / UNKNOWN
    is_draft: bool
    state: str  # OPEN / MERGED / CLOSED
    checks_pass: bool
    checks_detail: str


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


# ---- gh wrappers -------------------------------------------------------------


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def load_pr(pr_number: int) -> PRInfo:
    data = json.loads(
        _run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "number,headRefName,headRefOid,baseRefName,title,body,mergeStateStatus,isDraft,state",
            ]
        )
    )
    # Collect check statuses
    checks_out = subprocess.run(
        ["gh", "pr", "checks", str(pr_number)],
        capture_output=True,
        text=True,
    ).stdout
    # gh pr checks prints tab-separated rows: name\tstate\tduration\turl
    rows = [line.split("\t") for line in checks_out.strip().splitlines() if line.strip()]
    all_pass = bool(rows) and all(len(r) >= 2 and r[1] == "pass" for r in rows)
    detail = "\n".join(f"  {r[0]}: {r[1] if len(r) > 1 else '?'}" for r in rows)

    return PRInfo(
        number=data["number"],
        head_ref=data["headRefName"],
        head_sha=data["headRefOid"],
        base_ref=data["baseRefName"],
        title=data["title"],
        body=data.get("body") or "",
        merge_state=data.get("mergeStateStatus") or "UNKNOWN",
        is_draft=data["isDraft"],
        state=data.get("state") or "OPEN",
        checks_pass=all_pass,
        checks_detail=detail,
    )


def ensure_branch_fetched(head_ref: str) -> None:
    """Make sure origin/<head_ref> is up to date locally."""
    subprocess.run(
        ["git", "fetch", "origin", head_ref],
        check=False,
        capture_output=True,
    )


def git_ls_tree(ref: str, path: str = "") -> set[str]:
    """Return the set of tracked file paths in `ref` under `path`."""
    args = ["git", "ls-tree", "-r", "--name-only", ref]
    if path:
        args.append(path)
    try:
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def git_commits_between(base_ref: str, head_ref: str) -> list[dict]:
    """Return commits reachable from head but not base, as dicts."""
    try:
        out = subprocess.run(
            [
                "git",
                "log",
                "--format=%H%x09%s",
                f"origin/{base_ref}..origin/{head_ref}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return []
    commits: list[dict] = []
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        sha, subject = line.split("\t", 1)
        commits.append({"sha": sha, "subject": subject})
    return commits


def git_commit_touches_owns(sha: str, owns_paths: list[str]) -> bool:
    """Does commit `sha` modify any file under any of `owns_paths`?"""
    try:
        out = subprocess.run(
            ["git", "show", "--format=", "--name-only", sha],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return False
    touched = {line.strip() for line in out.splitlines() if line.strip()}
    return any(_path_is_under(t, owns_paths) for t in touched)


def _path_is_under(path: str, owns_paths: list[str]) -> bool:
    """Is `path` under any of the owns_paths entries?"""
    path = path.lstrip("./")
    for raw in owns_paths:
        # normalize: strip leading "./", accept both "src/musubi/x" and "musubi/x"
        o = raw.strip().lstrip("./")
        # If owns entry ends with .py or is a specific file: exact or prefix match
        if o.endswith((".py", ".yaml", ".yml")) and (path == o or path.endswith("/" + o)):
            return True
        # If owns entry is a directory: prefix match
        if o.endswith("/") and (path.startswith(o) or path.startswith("src/" + o)):
            return True
        if not o.endswith("/"):
            # Treat as prefix regardless
            if path.startswith(o + "/") or path.startswith("src/" + o + "/"):
                return True
            # And loose exact match
            if path == o or path == "src/" + o:
                return True
    return False


# ---- Checks ------------------------------------------------------------------


def check_owns_paths_exist(pr: PRInfo, s: Slice, branch_tree: set[str]) -> CheckResult:
    """Every owns_path file or dir is present in the branch's git tree."""
    missing: list[str] = []
    for raw in s.owns_paths:
        o = raw.strip().lstrip("./")
        # For specific files: check exact or with src/ prefix
        candidates = [o, f"src/{o}"] if not o.startswith("src/") else [o]
        if any(o.endswith(ext) for ext in (".py", ".yaml", ".yml")):
            if not any(c in branch_tree for c in candidates):
                missing.append(o)
            continue
        # Directory: check if ANY file under it exists
        if o.endswith("/"):
            o = o.rstrip("/")
        prefixes = [f"{o}/", f"src/{o}/"]
        if not any(any(t.startswith(pre) for t in branch_tree) for pre in prefixes):
            # No file under this directory → might be a new directory whose
            # creation the agent forgot to commit. Flag as missing.
            missing.append(f"{o}/ (directory; no files under it)")
    if missing:
        return CheckResult(
            "owns_paths_exist",
            False,
            f"{len(missing)} missing from branch tree:\n"
            + "\n".join(f"    - {m}" for m in missing),
        )
    return CheckResult("owns_paths_exist", True)


def check_feat_commit_present(pr: PRInfo, s: Slice, commits: list[dict]) -> CheckResult:
    """At least one feat(...) commit exists AND touches an owns_path."""
    feat_commits = [c for c in commits if c["subject"].startswith("feat(")]
    if not feat_commits:
        return CheckResult(
            "feat_commit_present",
            False,
            "no feat(...) commit found in the PR's commit history",
        )
    # At least one of them must touch an owns_path
    for c in feat_commits:
        if git_commit_touches_owns(c["sha"], s.owns_paths):
            return CheckResult(
                "feat_commit_present",
                True,
                f"feat commit {c['sha'][:10]} touches owns_paths: {c['subject']}",
            )
    return CheckResult(
        "feat_commit_present",
        False,
        f"{len(feat_commits)} feat commit(s) exist but none touch owns_paths. "
        f"Commits: {', '.join(c['sha'][:10] for c in feat_commits)}. "
        f"owns_paths: {s.owns_paths}",
    )


_CANONICAL_KINDS = ["chore(slice)", "chore(lock)", "test", "feat", "docs(slice)"]

# The handoff commit (frontmatter flip to in-review + work-log entry) can be
# either `docs(slice): handoff ...` or `chore(slice): handoff ...` — both are
# in active use this session (Nyla uses chore(slice), VS Code uses docs(slice)).
# The check recognises either form by subject-substring match so the canonical-
# shape check doesn't false-fail on a valid handoff prefix.
_HANDOFF_PREFIXES = ("docs(slice): handoff", "chore(slice): handoff")


def _has_handoff_commit(subjects: list[str]) -> bool:
    return any(s.startswith(_HANDOFF_PREFIXES) for s in subjects)


def check_canonical_shape(pr: PRInfo, commits: list[dict]) -> CheckResult:
    """Approximate canonical 7-commit shape present. Warn on deviation."""
    subjects = [c["subject"] for c in commits]
    kinds_found: dict[str, int] = {}
    for s in subjects:
        for k in _CANONICAL_KINDS:
            if s.startswith(k):
                kinds_found[k] = kinds_found.get(k, 0) + 1
                break
    missing = [k for k in _CANONICAL_KINDS if k not in kinds_found]
    if "feat" in missing:
        return CheckResult(
            "canonical_shape",
            False,
            "missing feat commit — hard fail (tested separately in feat_commit_present)",
        )
    # Handoff commit check: accept either `docs(slice): handoff ...` or
    # `chore(slice): handoff ...`. The existence of the handoff commit is what
    # matters; its conventional-commit type prefix is operator style.
    handoff_present = _has_handoff_commit(subjects)
    if not handoff_present:
        return CheckResult(
            "canonical_shape",
            False,
            "missing handoff commit — required for frontmatter flip + "
            "coverage-matrix work-log entry. Expected a commit whose subject "
            "starts with `docs(slice): handoff` or `chore(slice): handoff`. "
            "Agent likely rolled the flip into feat or something else.",
        )
    # `docs(slice)` / `chore(slice)` missing from the canonical-kinds report is
    # fine once we've confirmed the handoff commit exists above — it just means
    # the agent used the alternate prefix. Don't warn on either in that case.
    soft_missing = [k for k in missing if k not in ("docs(slice)", "chore(slice)")]
    if soft_missing:
        return CheckResult(
            "canonical_shape",
            True,  # warn, don't fail
            f"canonical-7-commit-shape deviation (soft): missing {soft_missing}. "
            f"Commits present: {', '.join(sorted(kinds_found.keys()))}",
        )
    return CheckResult("canonical_shape", True)


def check_merge_state(pr: PRInfo) -> CheckResult:
    # Already-merged PRs: mergeStateStatus becomes UNKNOWN post-merge. Skip cleanly.
    if pr.state == "MERGED":
        return CheckResult("merge_state", True, "PR already merged — check skipped")
    if pr.state == "CLOSED":
        return CheckResult(
            "merge_state",
            False,
            f"PR state={pr.state} — closed without merging; audit not applicable",
        )
    if pr.merge_state == "CLEAN":
        return CheckResult("merge_state", True)
    if pr.merge_state == "UNSTABLE":
        return CheckResult(
            "merge_state",
            False,
            "mergeStateStatus=UNSTABLE — typically means failing CI on the branch. "
            "Check `gh pr checks` for specifics.",
        )
    if pr.merge_state == "DIRTY":
        return CheckResult(
            "merge_state",
            False,
            "mergeStateStatus=DIRTY — merge conflict with base. Rebase or merge "
            "base into branch before re-claiming green.",
        )
    return CheckResult(
        "merge_state",
        False,
        f"mergeStateStatus={pr.merge_state} — unexpected; investigate.",
    )


def check_frontmatter_in_review(pr: PRInfo, s: Slice, branch_tree: set[str]) -> CheckResult:
    """Slice frontmatter on branch tip should be `status: in-review`."""
    slice_rel = str(s.path.relative_to(REPO_ROOT))
    if slice_rel not in branch_tree:
        return CheckResult(
            "frontmatter_in_review",
            False,
            f"slice file not in branch tree: {slice_rel}",
        )
    try:
        content = _run(["git", "show", f"origin/{pr.head_ref}:{slice_rel}"])
    except subprocess.CalledProcessError:
        return CheckResult(
            "frontmatter_in_review",
            False,
            f"git show failed for {slice_rel}",
        )
    # Extract status frontmatter line
    m = re.search(r"^status:\s*(\S+)", content, re.M)
    if not m:
        return CheckResult(
            "frontmatter_in_review",
            False,
            "no `status:` frontmatter key found",
        )
    status = m.group(1)
    if status != "in-review":
        return CheckResult(
            "frontmatter_in_review",
            False,
            f"frontmatter status='{status}'; expected 'in-review' at handoff",
        )
    return CheckResult("frontmatter_in_review", True)


def check_pr_body_closes(pr: PRInfo, s: Slice) -> CheckResult:
    """PR body first line must be `Closes #N.` where N matches the slice Issue."""
    first_line = pr.body.splitlines()[0].strip() if pr.body else ""
    if not s.issue:
        return CheckResult(
            "pr_body_closes",
            False,
            "no matching Issue found for slice; cannot verify Closes #N",
        )
    expected = f"Closes #{s.issue.number}."
    if first_line == expected:
        return CheckResult("pr_body_closes", True)
    # Fallback: also accept Closes #N on first line without trailing period
    if re.match(rf"^Closes #{s.issue.number}\b", first_line):
        return CheckResult(
            "pr_body_closes",
            True,
            f"accepted: first line is `{first_line}` (trailing period optional)",
        )
    return CheckResult(
        "pr_body_closes",
        False,
        f"first line is `{first_line}`; expected `{expected}`",
    )


def check_ci_pass(pr: PRInfo) -> CheckResult:
    if pr.checks_pass:
        return CheckResult("ci_pass", True)
    return CheckResult(
        "ci_pass",
        False,
        f"not all checks passing:\n{pr.checks_detail}",
    )


# ---- Main --------------------------------------------------------------------


def find_slice_for_pr(pr: PRInfo, slices: dict[str, Slice]) -> Slice | None:
    """Match a PR to its slice by head branch name `slice/<slice-id>` or by Issue."""
    # Primary: branch name matches slice/<id>
    if pr.head_ref.startswith("slice/"):
        sid = pr.head_ref[len("slice/") :]
        if sid in slices:
            return slices[sid]
    # Fallback: search for a slice whose Issue number matches "Closes #N" in PR body
    m = re.search(r"^Closes #(\d+)", pr.body, re.M)
    if m:
        want = int(m.group(1))
        for s in slices.values():
            if s.issue and s.issue.number == want:
                return s
    return None


def run_audit(pr_number: int) -> int:
    try:
        pr = load_pr(pr_number)
    except subprocess.CalledProcessError as e:
        print(f"error: could not load PR #{pr_number}: {e}", file=sys.stderr)
        return 2

    print(bold(f"PR #{pr.number} — {pr.title}"))
    print(f"  head: origin/{pr.head_ref} @ {pr.head_sha[:10]}")
    print(f"  base: origin/{pr.base_ref}")
    print(f"  draft: {pr.is_draft}  mergeState: {pr.merge_state}")
    print()

    ensure_branch_fetched(pr.head_ref)
    slices = load_slices()
    for sid, s in slices.items():
        s.issue = load_issues().get(sid) if s.issue is None else s.issue

    # Actually just load issues once
    issues = load_issues()
    for sid, s in slices.items():
        s.issue = issues.get(sid)

    s = find_slice_for_pr(pr, slices)
    if s is None:
        print(
            red(
                "✗ audit skipped — could not match PR to a slice by head branch "
                "or `Closes #N` in body. Manual review required."
            ),
            file=sys.stderr,
        )
        return 2

    print(f"  matched slice: {s.id}")
    print(f"  owns_paths: {s.owns_paths}")
    print()

    # Branch tree + commits
    branch_tree = git_ls_tree(f"origin/{pr.head_ref}")
    commits = git_commits_between(pr.base_ref, pr.head_ref)

    results: list[CheckResult] = [
        check_owns_paths_exist(pr, s, branch_tree),
        check_feat_commit_present(pr, s, commits),
        check_canonical_shape(pr, commits),
        check_merge_state(pr),
        check_frontmatter_in_review(pr, s, branch_tree),
        check_pr_body_closes(pr, s),
        check_ci_pass(pr),
    ]

    failed = [r for r in results if not r.ok]
    warned = [r for r in results if r.ok and r.detail]  # soft warnings carry detail

    for r in results:
        mark = green("✓") if r.ok else red("✗")
        print(f"  {mark} {r.name}")
        if r.detail:
            # Indent multi-line detail
            indent = "      "
            for line in r.detail.splitlines():
                print(f"{indent}{line}")

    print()
    if not failed:
        print(
            green(f"✓ audit PASS — PR #{pr.number} ready to review")
            + (f"  ({len(warned)} soft warning(s))" if warned else "")
        )
        return 0
    print(red(f"✗ audit FAIL — {len(failed)} check(s) failed; do not accept handoff as green"))
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="handoff-audit.py",
        description="Verify an agent's handoff matches what's actually pushed. "
        "Run before reviewing a PR flipped to in-review. "
        "See top-of-file docstring for the seven audit checks.",
    )
    parser.add_argument("pr_number", type=int, help="GitHub PR number")
    args = parser.parse_args(argv)
    return run_audit(args.pr_number)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
