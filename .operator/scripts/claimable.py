#!/usr/bin/env python3
"""claimable.py — operator-side slice claim-readiness enumerator.

Operator-only tool (not consumed by slice-worker agents during slice work).
Purpose: eliminate brief-vs-reality mismatches when authoring agent prompts.

Reads every `docs/architecture/_slices/slice-*.md`, cross-references with
GitHub Issues (via `gh`), and outputs a mechanical view of what's
actually claimable right now. Replaces the hand-authored slice references
in agent briefs that have been caught as wrong three times in the last
two sessions (wrong Issue number, wrong spec path, owns_paths sibling-
convention drift, unmet depends-on).

Usage:
    # list all slices with claimable flag + Issue number:
    python3 .operator/scripts/claimable.py
    python3 .operator/scripts/claimable.py list --only-claimable

    # emit the slice-specific brief block (paste into an agent prompt):
    python3 .operator/scripts/claimable.py brief slice-retrieval-fast

    # sanity-check a slice file against ground truth:
    python3 .operator/scripts/claimable.py verify slice-retrieval-fast

Deps: Python 3.12+ stdlib · PyYAML · `gh` CLI on PATH.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:
    print("error: requires PyYAML (uv add pyyaml / pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


# Resolve repo root: this script lives at <repo>/.operator/scripts/claimable.py
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SLICES_DIR = REPO_ROOT / "docs" / "architecture" / "_slices"
VAULT_ROOT = REPO_ROOT / "docs" / "architecture"


# ---------------------------------------------------------------------------
# Tiny ANSI helpers (no-op when not a TTY)
# ---------------------------------------------------------------------------


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


def _strip_ansi(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    number: int
    title: str
    state: str  # "OPEN" | "CLOSED"
    labels: list[str]
    assignees: list[str]


@dataclass
class Slice:
    id: str
    path: Path
    title: str
    status: str
    owner: str
    phase: str
    depends_on: list[str]
    blocks: list[str]
    owns_paths: list[str]
    forbidden_paths: list[str]
    specs: list[str]  # relative file paths; suffix "(MISSING)" if not on disk
    issue: Issue | None = None
    claimable: bool | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_BULLET_BACKTICK = re.compile(r"^\s*[-*]\s+`([^`]+)`", re.M)


def _extract_wikilinks(text: str) -> list[str]:
    return _WIKILINK.findall(text)


def _slice_id_from_wikilink(link: str) -> str | None:
    """`_slices/slice-xxx` → `slice-xxx`; returns None if not a slice link."""
    link = link.strip()
    if link.startswith("_slices/"):
        return link[len("_slices/") :]
    return None


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, text[end + 5 :]


def _slice_ids_from_yaml_list(lst: list | None) -> list[str]:
    """Extract slice-ids from a YAML list of quoted wikilinks like '[[_slices/foo]]'."""
    out: list[str] = []
    for item in lst or []:
        for link in _extract_wikilinks(item) if isinstance(item, str) else []:
            sid = _slice_id_from_wikilink(link)
            if sid:
                out.append(sid)
    return out


def _paths_under_heading(body: str, heading: str) -> list[str]:
    """Extract backtick-wrapped paths from bullet items under a given ## heading."""
    m = re.search(
        rf"^##+\s+{re.escape(heading)}.*?\n(.*?)(?=^##+\s|\Z)",
        body,
        re.M | re.S,
    )
    return _BULLET_BACKTICK.findall(m.group(1)) if m else []


def load_slices() -> dict[str, Slice]:
    """Parse every slice-*.md under `_slices/`."""
    slices: dict[str, Slice] = {}
    for p in sorted(SLICES_DIR.glob("slice-*.md")):
        text = p.read_text()
        fm, body = _parse_frontmatter(text)
        if fm.get("type") != "slice":
            continue

        sid = fm.get("slice_id") or p.stem
        depends_on = _slice_ids_from_yaml_list(fm.get("depends-on"))
        blocks = _slice_ids_from_yaml_list(fm.get("blocks"))

        # Specs: wikilinks under "## Specs to implement"
        spec_links: list[str] = []
        m = re.search(r"^##\s+Specs to implement\s*\n(.*?)(?=^##\s|\Z)", body, re.M | re.S)
        if m:
            spec_links = _extract_wikilinks(m.group(1))

        specs: list[str] = []
        for link in spec_links:
            candidate = VAULT_ROOT / f"{link}.md"
            if candidate.exists():
                specs.append(str(candidate.relative_to(REPO_ROOT)))
            else:
                specs.append(f"docs/architecture/{link}.md (MISSING)")

        slices[sid] = Slice(
            id=sid,
            path=p,
            title=str(fm.get("title", "")).strip('"').strip(),
            status=str(fm.get("status", "")).strip(),
            owner=str(fm.get("owner", "")).strip(),
            phase=str(fm.get("phase", "")).strip('"').strip(),
            depends_on=depends_on,
            blocks=blocks,
            owns_paths=_paths_under_heading(body, "Owned paths"),
            forbidden_paths=_paths_under_heading(body, "Forbidden paths"),
            specs=specs,
        )
    return slices


def load_issues() -> dict[str, Issue]:
    """Fetch GitHub issues labeled 'slice', match to slice-ids by title."""
    try:
        out = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--label",
                "slice",
                "--state",
                "all",
                "--limit",
                "200",
                "--json",
                "number,title,state,labels,assignees",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(
            f"warning: gh issue list unavailable; Issue column will be blank ({exc})",
            file=sys.stderr,
        )
        return {}

    by_slice: dict[str, Issue] = {}
    for row in json.loads(out):
        title: str = row["title"]
        # Canonical title pattern: "slice: slice-xxx" or "slice: slice-xxx — <suffix>"
        m = re.match(r"slice:\s+(slice-[a-z0-9-]+)", title)
        if not m:
            continue
        sid = m.group(1)
        # Prefer the FIRST OPEN match, otherwise last closed (handles split-slice history)
        issue = Issue(
            number=row["number"],
            title=title,
            state=row["state"],
            labels=[lab["name"] for lab in row.get("labels", [])],
            assignees=[a["login"] for a in row.get("assignees", [])],
        )
        existing = by_slice.get(sid)
        if existing is None or (existing.state != "OPEN" and issue.state == "OPEN"):
            by_slice[sid] = issue
    return by_slice


def compute_claimability(slices: dict[str, Slice]) -> None:
    """Annotate each slice with `claimable` + reason."""
    for s in slices.values():
        if s.status != "ready":
            s.claimable = False
            s.reason = f"status={s.status}"
            continue
        if s.issue is None:
            s.claimable = False
            s.reason = "no matching Issue"
            continue
        if s.issue.state != "OPEN":
            s.claimable = False
            s.reason = f"Issue #{s.issue.number} is CLOSED"
            continue

        unmet: list[str] = []
        for dep_id in s.depends_on:
            dep = slices.get(dep_id)
            if dep is None:
                unmet.append(f"{dep_id}(missing)")
                continue
            if dep.status == "done":
                continue
            # Tolerate the race where Issue is closed but vault frontmatter
            # hasn't been flipped to "done" yet (operator follows up within hours).
            if dep.status == "in-review" and dep.issue is not None and dep.issue.state == "CLOSED":
                continue
            unmet.append(f"{dep_id}({dep.status})")

        if unmet:
            s.claimable = False
            s.reason = "deps: " + ", ".join(unmet)
        else:
            s.claimable = True
            s.reason = ""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(slices: dict[str, Slice], only_claimable: bool = False) -> int:
    headers = ("slice-id", "issue", "status", "claim", "owner", "reason")
    rows: list[tuple[str, ...]] = []
    for sid, s in sorted(slices.items(), key=lambda kv: (kv[1].phase, kv[0])):
        if only_claimable and not s.claimable:
            continue
        issue_ref = f"#{s.issue.number}" if s.issue else "—"
        color_map = {
            "ready": green if s.claimable else yellow,
            "in-progress": yellow,
            "in-review": yellow,
            "done": dim,
            "blocked": red,
        }
        status_colored = color_map.get(s.status, lambda x: x)(s.status)
        if s.claimable:
            claim_mark = green("✓")
        elif s.status == "ready":
            claim_mark = red("✗")
        else:
            claim_mark = dim("—")
        rows.append(
            (
                sid,
                issue_ref,
                status_colored,
                claim_mark,
                s.owner or "—",
                s.reason,
            )
        )

    widths = [
        max(
            (len(_strip_ansi(r[i])) for r in rows),
            default=0,
        )
        for i in range(len(headers))
    ]
    widths = [max(widths[i], len(headers[i])) for i in range(len(headers))]

    def fmt(cols: tuple[str, ...]) -> str:
        out: list[str] = []
        for i, c in enumerate(cols):
            pad = widths[i] - len(_strip_ansi(c))
            out.append(c + " " * pad)
        return "  ".join(out)

    print(bold(fmt(headers)))
    print(bold(fmt(tuple("-" * w for w in widths))))
    for r in rows:
        print(fmt(r))

    total = len(slices)
    claimable_count = sum(1 for s in slices.values() if s.claimable)
    by_status: dict[str, int] = {}
    for s in slices.values():
        by_status[s.status] = by_status.get(s.status, 0) + 1
    print()
    print(f"{bold('total')}: {total}   {bold('claimable-now')}: {green(str(claimable_count))}")
    print("by status: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    return 0


def cmd_brief(slices: dict[str, Slice], slice_id: str) -> int:
    s = slices.get(slice_id)
    if s is None:
        print(f"error: unknown slice '{slice_id}'", file=sys.stderr)
        print(
            "available: " + ", ".join(sorted(slices.keys())),
            file=sys.stderr,
        )
        return 2

    # Surface sanity issues upfront, to stderr so brief body stays pasteable
    for spec in s.specs:
        if "(MISSING)" in spec:
            print(f"warning: {spec}", file=sys.stderr)
    if not s.issue:
        print(
            f"warning: no matching GitHub Issue found for '{slice_id}'",
            file=sys.stderr,
        )

    issue_num = s.issue.number if s.issue else "???"

    in_flight = [
        (s2.id, s2.owner, s2.owns_paths[0] if s2.owns_paths else "(no owns_paths)")
        for s2 in slices.values()
        if s2.status in ("in-progress", "in-review") and s2.id != slice_id
    ]

    # Print as a plain text block ready to paste. No color here.
    print(f"Slice: {s.id} (#{issue_num}).")
    print(f"Slice file: {s.path.relative_to(REPO_ROOT)}")
    print()
    if s.specs:
        print("Spec" + ("s" if len(s.specs) > 1 else "") + ":")
        for spec in s.specs:
            print(f"  - {spec}")
        print()
    if s.owns_paths:
        print("Owned paths (you MAY write here):")
        for p in s.owns_paths:
            print(f"  - {p}")
        print()
    if s.forbidden_paths:
        print("Forbidden paths (you MUST NOT write here):")
        for p in s.forbidden_paths:
            print(f"  - {p}")
        print()
    if s.depends_on:
        print("Depends on:")
        for dep_id in s.depends_on:
            dep = slices.get(dep_id)
            if dep is None:
                print(f"  - {dep_id} (UNKNOWN slice)")
            else:
                note = dep.status
                if (
                    dep.status == "in-review"
                    and dep.issue is not None
                    and dep.issue.state == "CLOSED"
                ):
                    note = "done (Issue closed; frontmatter-flip race)"
                print(f"  - {dep_id} ({note})")
        print()
    if s.blocks:
        print("Unblocks:")
        for b in s.blocks:
            print(f"  - {b}")
        print()
    print("Parallel agents right now:")
    if in_flight:
        for pid, powner, ppath in in_flight:
            print(f"  - {pid} — {powner or 'unclaimed'} — {ppath}")
    else:
        print("  (none in-progress or in-review)")
    print()
    if s.claimable:
        print(f"✓ CLAIMABLE NOW — Issue #{issue_num} is OPEN and deps are met.")
    else:
        print(f"✗ NOT CLAIMABLE: {s.reason}")
    return 0 if s.claimable else 1


def cmd_verify(slices: dict[str, Slice], slice_id: str) -> int:
    """Sanity-check a slice file against ground truth."""
    s = slices.get(slice_id)
    if s is None:
        print(f"error: unknown slice '{slice_id}'", file=sys.stderr)
        return 2

    problems: list[str] = []

    if s.issue is None:
        problems.append(f"no GitHub Issue with title 'slice: {slice_id}'")

    for spec in s.specs:
        if "(MISSING)" in spec:
            problems.append(f"spec file missing: {spec}")

    for dep in s.depends_on:
        if dep not in slices:
            problems.append(f"depends-on references unknown slice: {dep}")
    for b in s.blocks:
        if b not in slices:
            problems.append(f"blocks references unknown slice: {b}")

    # Sibling-convention check on owns_paths
    for p in s.owns_paths:
        if p.startswith("src/musubi/") and p.endswith(".py"):
            parts = p.split("/")
            dirname = "/".join(parts[:-1])
            fname = parts[-1]
            abs_dir = REPO_ROOT / dirname
            if not abs_dir.exists():
                continue
            siblings = [
                f.name for f in abs_dir.glob("*.py") if f.name != "__init__.py" and f.name != fname
            ]
            if siblings and "_" in fname:
                sibling_has_underscore = any("_" in sb for sb in siblings)
                if not sibling_has_underscore:
                    problems.append(
                        f"sibling-convention drift: {fname} has underscore, "
                        f"but siblings in {dirname}/ ({', '.join(siblings)}) are single-noun"
                    )

    if not problems:
        print(green(f"✓ {slice_id}: no issues detected"))
        return 0
    print(red(f"✗ {slice_id}: {len(problems)} issue(s)"))
    for p in problems:
        print(f"  - {p}")
    return 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="claimable.py",
        description=(
            "Enumerate Musubi slices and their claim-readiness. "
            "Operator-only tool. "
            "See .operator/scripts/claimable.py top-of-file docstring for full usage."
        ),
    )
    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="list all slices (default)")
    p_list.add_argument(
        "--only-claimable",
        action="store_true",
        help="filter to slices claimable right now",
    )

    p_brief = sub.add_parser("brief", help="emit pasteable brief-block for a slice")
    p_brief.add_argument("slice_id")

    p_verify = sub.add_parser("verify", help="sanity-check a slice file against ground truth")
    p_verify.add_argument("slice_id")

    args = parser.parse_args(argv)

    slices = load_slices()
    issues = load_issues()
    for sid, s in slices.items():
        s.issue = issues.get(sid)
    compute_claimability(slices)

    cmd = args.cmd or "list"
    if cmd == "list":
        return cmd_list(slices, only_claimable=getattr(args, "only_claimable", False))
    if cmd == "brief":
        return cmd_brief(slices, args.slice_id)
    if cmd == "verify":
        return cmd_verify(slices, args.slice_id)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
