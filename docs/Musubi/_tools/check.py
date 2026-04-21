#!/usr/bin/env python3
"""
check.py — Musubi vault + slice + spec validator.

Usage:
  python3 _tools/check.py [vault|slices|specs|all] [--json] [--fix]

Exit code is nonzero if any error is reported. Warnings are informational.

Designed to run from the vault root with only stdlib + PyYAML. No Obsidian
dependency. Drop this script into the musubi code repo's `tools/` folder once
the repo exists; the behaviour is identical either place.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception:
    yaml = None  # graceful degrade; we use a tiny fallback parser

VAULT = Path(__file__).resolve().parent.parent
INFRA_FOLDERS = {
    "_templates",
    "_attachments",
    "_bases",
    "_inbox",
    "_tools",
    "_slices",
    "proto",
    "07-interfaces/openapi",
}

# ---------- Frontmatter parsing ------------------------------------------------

FM_START = re.compile(r"\A---\s*\n")
FM_END = re.compile(r"\n---\s*\n")


def read_frontmatter(path: Path) -> tuple[dict, str]:
    """Return (fm_dict, body). Empty dict if no frontmatter."""
    text = path.read_text(encoding="utf-8")
    if not FM_START.match(text):
        return {}, text
    m = FM_END.search(text, 4)
    if not m:
        return {}, text
    block = text[4 : m.start()]
    body = text[m.end() :]
    if yaml is not None:
        try:
            data = yaml.safe_load(block) or {}
            return data, body
        except Exception:
            pass
    # Fallback: key: value and key: [a, b] only.
    out: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.strip().startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if val.startswith("[") and val.endswith("]"):
            out[key] = [s.strip().strip('"').strip("'") for s in val[1:-1].split(",") if s.strip()]
        else:
            out[key] = val
    return out, body


# ---------- Report types -------------------------------------------------------


@dataclass
class Report:
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, msg)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    def err(self, path: str, msg: str) -> None:
        self.errors.append((path, msg))

    def warn(self, path: str, msg: str) -> None:
        self.warnings.append((path, msg))

    def merge(self, other: Report) -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    def ok(self) -> bool:
        return not self.errors


# ---------- Helpers ------------------------------------------------------------


def iter_notes(root: Path, exclude_infra: bool = True) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(VAULT)
        if any(part.startswith(".") for part in rel.parts):
            continue
        # Skip any note whose immediate parent-chain hits an infra folder.
        if exclude_infra and any(
            str(rel).startswith(f + "/") or str(rel).startswith(f + os.sep) for f in INFRA_FOLDERS
        ):
            continue
        out.append(p)
    return out


def all_note_paths() -> set[str]:
    return {
        str(p.relative_to(VAULT)).rsplit(".md", 1)[0]
        for p in VAULT.rglob("*.md")
        if not any(part.startswith(".") for part in p.parts)
    }


# ---------- Check: vault -------------------------------------------------------

REQUIRED_FIELDS = {"title", "section", "type", "status", "tags", "updated"}

VAULT_ROOT_FILES = {"README.md", "CLAUDE.md"}
SKIP_FRONTMATTER_PREFIXES = ("_templates/", "_attachments/", "proto/")


def check_vault(rep: Report) -> None:
    notes = iter_notes(VAULT, exclude_infra=False)
    for p in notes:
        rel = str(p.relative_to(VAULT))
        if any(rel.startswith(prefix) for prefix in SKIP_FRONTMATTER_PREFIXES):
            continue
        fm, body = read_frontmatter(p)
        if not fm:
            rep.err(rel, "no frontmatter block")
            continue
        # Vault-root meta-docs (README, CLAUDE) don't need `section:`.
        required = (
            REQUIRED_FIELDS if rel not in VAULT_ROOT_FILES else (REQUIRED_FIELDS - {"section"})
        )
        missing = required - fm.keys()
        if missing:
            rep.err(rel, f"missing required fields: {sorted(missing)}")
        # H1 matches title
        h1 = next((line.strip() for line in body.lstrip().splitlines() if line.strip()), "")
        if h1 and h1.startswith("# ") and fm.get("title"):
            title_only = h1[2:].strip().strip('"')
            if title_only != str(fm.get("title")).strip().strip('"'):
                rep.warn(rel, f"H1 '{title_only}' != frontmatter title '{fm.get('title')}'")
        # Section field matches parent folder (for foldered notes).
        #
        # Accepted forms:
        #   - immediate parent name (e.g. `04-data-model` for files under
        #     `04-data-model/*.md`)
        #   - full relative path from the vault root (e.g. `_inbox/cross-slice`
        #     for `_inbox/cross-slice/foo.md`). The full-path form is the
        #     convention used by files under `_inbox/` subfolders and by
        #     `07-interfaces/openapi/README.md` — it disambiguates sub-sections
        #     that share a base name across the tree.
        parent = p.parent.name
        parent_path = str(p.parent.relative_to(VAULT))
        section_val = fm.get("section")
        if (
            "/" in rel
            and parent
            and parent != "_inbox"
            and section_val
            and section_val != parent
            and section_val != parent_path
            and not parent.startswith("_")
        ):
            rep.warn(rel, f"section '{section_val}' != folder '{parent}'")
        # Tags include canonical namespaces
        tags = fm.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        joined = " ".join(tags)
        if fm.get("status") and f"status/{fm['status']}" not in joined:
            rep.warn(rel, f"tag status/{fm['status']} missing")
        if fm.get("type") and f"type/{fm['type']}" not in joined:
            rep.warn(rel, f"tag type/{fm['type']} missing")


# ---------- Check: slices ------------------------------------------------------


def check_slices(rep: Report) -> None:
    sroot = VAULT / "_slices"
    if not sroot.exists():
        rep.err("_slices", "folder missing")
        return
    slice_files = sorted(sroot.glob("slice-*.md"))
    slices: dict[str, dict] = {}
    for p in slice_files:
        fm, _ = read_frontmatter(p)
        if fm.get("type") != "slice":
            continue
        sid = fm.get("slice_id") or p.stem
        slices[sid] = {"path": p, "fm": fm}

    if not slices:
        rep.err("_slices", "no slice files found")
        return

    # 1. depends-on / blocks link consistency
    for sid, s in slices.items():
        fm = s["fm"]
        deps = list_wiki_targets(fm.get("depends-on"))
        blks = list_wiki_targets(fm.get("blocks"))
        for dep in deps:
            dep_sid = dep.rsplit("/", 1)[-1]
            if dep_sid not in slices:
                rep.err(str(s["path"].relative_to(VAULT)), f"depends-on target missing: {dep_sid}")
                continue
            other_blocks = list_wiki_targets(slices[dep_sid]["fm"].get("blocks"))
            if not any(o.endswith("/" + sid) or o == sid for o in other_blocks):
                rep.warn(
                    str(s["path"].relative_to(VAULT)),
                    f"slice '{sid}' depends-on '{dep_sid}' but '{dep_sid}' does not list it in blocks",
                )
        for b in blks:
            b_sid = b.rsplit("/", 1)[-1]
            if b_sid not in slices:
                rep.err(str(s["path"].relative_to(VAULT)), f"blocks target missing: {b_sid}")

    # 2. owns_paths uniqueness — scope to the ## Owned paths section only.
    #
    # The purpose of ``owns_paths`` is coordination during *active* work:
    # two in-flight slices claiming the same file is a merge-conflict
    # hazard. Once a slice reaches ``done``, its claim is a historical
    # record of what that slice built — overlapping with another
    # ``done`` slice's claim is expected (parent + ``<parent>-followup``
    # pairs, or sibling slices that each extended a shared area at
    # different times like ``slice-api-v0-read`` + ``slice-api-v0-write``).
    # Only warn when at least one claimant is still active.
    claims: dict[str, str] = {}
    for sid, s in slices.items():
        body = s["path"].read_text()
        m = re.search(r"## Owned paths.*?\n(.*?)\n## ", body, re.S)
        if not m:
            continue
        owned_section = m.group(1)
        for pm in re.finditer(r"^\s*-\s+`([^`]+)`.*$", owned_section, re.M):
            path = pm.group(1).strip()
            if path in claims and claims[path] != sid:
                status_cur = s["fm"].get("status")
                status_prev = slices[claims[path]]["fm"].get("status")
                other_sid = claims[path]
                both_done = status_cur == "done" and status_prev == "done"
                if both_done:
                    # Historical overlap — two shipped slices that both
                    # legitimately touched this path. Not a signal.
                    pass
                else:
                    msg = f"owns_paths conflict: '{path}' also claimed by '{other_sid}'"
                    if status_cur == "done" or status_prev == "done":
                        rep.warn(str(s["path"].relative_to(VAULT)), msg)
                    else:
                        rep.err(str(s["path"].relative_to(VAULT)), msg)
            claims[path] = sid

    # 3. status transitions
    for _sid, s in slices.items():
        status = s["fm"].get("status")
        if status not in {"ready", "in-progress", "in-review", "blocked", "done", "retired"}:
            rep.err(str(s["path"].relative_to(VAULT)), f"invalid slice status '{status}'")

    # 4. locks correspond to in-progress slices (and vice versa)
    locks = set()
    lroot = VAULT / "_inbox" / "locks"
    if lroot.exists():
        for lp in lroot.glob("*.lock"):
            locks.add(lp.stem)
    for sid, s in slices.items():
        status = s["fm"].get("status")
        if status == "in-progress" and sid not in locks:
            rep.warn(
                str(s["path"].relative_to(VAULT)),
                f"status=in-progress but no lock at _inbox/locks/{sid}.lock",
            )
        if status not in {"in-progress", "in-review"} and sid in locks:
            rep.warn(f"_inbox/locks/{sid}.lock", f"lock exists but slice status is '{status}'")

    # 5. stale locks (> 4h since mtime)
    for lp in (VAULT / "_inbox" / "locks").glob("*.lock") if lroot.exists() else []:
        age = time.time() - lp.stat().st_mtime
        if age > 4 * 3600:
            rep.warn(str(lp.relative_to(VAULT)), f"stale lock (age {int(age / 3600)}h)")


# ---------- Check: issue drift (vault slice ↔ GH Issue) -----------------------


_ISSUE_TITLE_RE = re.compile(r"^slice:\s+(slice-[\w-]+)\s*$")


def _fetch_slice_issues() -> list[dict] | None:
    """Shell out to ``gh`` for all Issues labeled ``slice``.

    Returns a list of dicts (``number``, ``title``, ``labels``, ``state``,
    ``assignees``), or ``None`` if the ``gh`` CLI is unavailable / errors
    out — in which case the caller skips this check with a warning rather
    than blocking. This is deliberate: many CI runners don't provision
    ``gh`` by default, and the drift check is advisory, not a gate.
    """
    import subprocess

    try:
        r = subprocess.run(
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
                "number,title,labels,state,assignees",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def check_issues(rep: Report) -> None:
    """Cross-reference slice frontmatter with GitHub Issues.

    Enforces the Dual-update rule (see agent-guardrails.md) by reporting
    drift between a slice's ``status:`` frontmatter and its Issue's
    ``status:*`` label. Acceptable drift is the handoff race window:
    frontmatter ``in-review`` + Issue ``status:in-progress`` (label not
    yet flipped by the handoff skill) is fine for one PR cycle; everything
    else is a warning.
    """
    issues = _fetch_slice_issues()
    if issues is None:
        rep.warn(
            "_tools/check.py",
            "gh CLI unavailable; skipping issue drift check "
            "(install gh + authenticate, or run this check outside CI)",
        )
        return

    # Build: slice-id -> issue dict.
    by_slice: dict[str, dict] = {}
    for it in issues:
        m = _ISSUE_TITLE_RE.match(it.get("title", ""))
        if not m:
            continue
        by_slice[m.group(1)] = it

    # Read slices from the vault.
    sroot = VAULT / "_slices"
    slice_fms: dict[str, dict] = {}
    for p in sorted(sroot.glob("slice-*.md")):
        fm, _ = read_frontmatter(p)
        if fm.get("type") != "slice":
            continue
        sid = fm.get("slice_id") or p.stem
        slice_fms[sid] = fm

    # 1. Every slice has an Issue.
    for sid in slice_fms:
        if sid not in by_slice:
            rep.warn(
                f"_slices/{sid}.md",
                f"no GH Issue titled 'slice: {sid}' — run "
                "`python3 docs/Musubi/_tools/bootstrap_slice_issues.py --apply`",
            )

    # 2. Every Issue labeled `slice` has a matching slice file.
    #
    # Skip closed Issues — the slice file could have been renamed or retired,
    # and the closed Issue is historical record (not a live claim). The
    # warning only fires for OPEN Issues whose slice files vanished, which is
    # the case we actually want to catch.
    for sid, issue in by_slice.items():
        if sid not in slice_fms:
            if issue.get("state") == "CLOSED":
                continue
            rep.warn(
                f"gh-issue#{issue['number']}",
                f"Issue 'slice: {sid}' has no matching slice file — either slice was "
                "renamed/retired (close the Issue), or slice file is missing",
            )

    # 3. status drift: frontmatter `status:` ↔ Issue `status:*` label.
    #    Acceptable drift (handoff race window):
    #      frontmatter `in-review` + Issue `status:in-progress` — handoff in flight
    #      frontmatter `done` + Issue state `closed` — expected
    ACCEPTABLE = {
        ("in-review", "status:in-progress"),  # handoff flipped frontmatter but not label yet
        (
            "in-progress",
            "status:ready",
        ),  # claim flipped label but not frontmatter yet (rare; flipped order)
    }
    for sid, fm in slice_fms.items():
        if sid not in by_slice:
            continue
        issue = by_slice[sid]
        vault_status = str(fm.get("status", ""))
        labels = [lab.get("name", "") for lab in issue.get("labels", [])]
        issue_status_labels = [lab for lab in labels if lab.startswith("status:")]

        # Issue closed state = slice should be done OR retired. Both are valid
        # closure reasons: `done` means shipped, `retired` means the slice was
        # superseded by an ADR (see e.g. ADR-0022 retiring slice-adapter-openclaw).
        if issue.get("state") == "CLOSED":
            if vault_status not in ("done", "retired"):
                rep.err(
                    f"_slices/{sid}.md",
                    f"Issue #{issue['number']} is closed but frontmatter status is '{vault_status}' "
                    "(expected 'done' or 'retired')",
                )
            continue

        # Open Issue: label should be present + match frontmatter
        if not issue_status_labels:
            rep.warn(
                f"gh-issue#{issue['number']}",
                f"'slice: {sid}' has no status:* label; expected 'status:{vault_status}'",
            )
            continue
        if len(issue_status_labels) > 1:
            rep.warn(
                f"gh-issue#{issue['number']}",
                f"'slice: {sid}' has multiple status:* labels {issue_status_labels}; only one allowed",
            )
            continue
        issue_label = issue_status_labels[0]
        expected = f"status:{vault_status}"
        if issue_label != expected:
            if (vault_status, issue_label) in ACCEPTABLE:
                continue

            if issue_label == "status:in-review" and vault_status not in ("in-review", "done"):
                rep.err(
                    f"_slices/{sid}.md",
                    f"drift: frontmatter status='{vault_status}' but Issue #{issue['number']} "
                    f"label='{issue_label}' (expected 'in-review' or 'done')",
                )
            else:
                rep.warn(
                    f"_slices/{sid}.md",
                    f"drift: frontmatter status='{vault_status}' but Issue #{issue['number']} "
                    f"label='{issue_label}' (expected '{expected}')",
                )


# GitHub only auto-closes on "Closes/Fixes/Resolves #N" — see
# https://docs.github.com/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
_CLOSE_KEYWORD_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*#(\d+)\b",
    re.IGNORECASE,
)


def check_pr_closing_slices(rep: Report) -> None:
    """Catch the post-merge vault-check race at PR time.

    When a PR carries ``Closes #N`` for a ``slice``-labelled Issue, the
    merge will auto-close the Issue. Post-merge, ``check_issues``
    requires the slice frontmatter to be ``done``/``retired``. That
    transition can't happen at merge-time without a human/agent step,
    and without this check the only signal is a red Vault check on the
    main branch AFTER the merge has shipped.

    Rule: if ``$PR_BODY`` contains a close-keyword referencing an Issue
    labelled ``slice``, that slice's frontmatter must be ``status: done``
    (or ``status: retired`` for slices superseded by an ADR). Pairs with
    the ACCEPTABLE exception at line ~458 which already permits
    ``slice=done + issue_label=status:in-review``.

    Runs only on pull_request events (keyed on ``$PR_BODY``). Push
    events skip this check silently.
    """
    pr_body = os.environ.get("PR_BODY")
    if not pr_body:
        return

    # GitHub itself ignores Closes-keywords inside code blocks / inline code
    # when deciding what to auto-close; mirror that so docs PRs that mention
    # `Closes #N` inside a code example don't trip the check. The two
    # regexes are re-used from the wikilink scanner below.
    stripped = _FENCED_CODE_RE.sub("", pr_body)
    stripped = _INLINE_CODE_RE.sub("", stripped)

    matches = _CLOSE_KEYWORD_RE.findall(stripped)
    if not matches:
        return

    issues = _fetch_slice_issues()
    if issues is None:
        rep.warn(
            "pr-check",
            "gh CLI unavailable — skipping PR-closing-slice check",
        )
        return
    by_number = {str(i["number"]): i for i in issues}

    # Build slice-id → frontmatter map (subset of what check_slices does,
    # kept local to avoid re-reading the whole tree).
    sroot = VAULT / "_slices"
    slice_fms: dict[str, dict] = {}
    for p in sroot.glob("slice-*.md"):
        fm, _ = read_frontmatter(p)
        # Filter to true slice notes, matching check_slices' behaviour.
        # A `slice-*.md` file without `type: slice` is a template or
        # README-style note and should not be indexed as a slice.
        if fm.get("type") != "slice":
            continue
        sid = fm.get("slice_id") or p.stem
        slice_fms[sid] = fm

    for issue_number in matches:
        issue = by_number.get(issue_number)
        if issue is None:
            # PR closes a non-slice issue — not this check's concern.
            continue
        # Recover the slice-id from the Issue title convention "slice: <id>".
        title = str(issue.get("title", ""))
        if not title.startswith("slice: "):
            continue
        sid = title[len("slice: ") :].strip()
        fm = slice_fms.get(sid)
        if fm is None:
            rep.err(
                "pr-check",
                f"PR closes Issue #{issue_number} ('slice: {sid}') but no slice file "
                f"at _slices/{sid}.md",
            )
            continue
        vault_status = str(fm.get("status", ""))
        if vault_status not in ("done", "retired"):
            rep.err(
                f"_slices/{sid}.md",
                f"PR body has 'Closes #{issue_number}' which will auto-close the "
                f"slice's tracking Issue on merge; post-merge the Vault check "
                f"requires frontmatter status='done' (or 'retired') but this PR "
                f"still has status='{vault_status}'. Flip to 'done' in this PR.",
            )


# ---------- Check: wikilinks --------------------------------------------------

# Wikilink targets may be .md (notes), .canvas (Obsidian Canvas), or .base
# (Obsidian Bases). We build a resolver that accepts any of the three.
_LINKABLE_EXTENSIONS = {".md", ".canvas", ".base"}

# Wikilink-scan uses these to strip content that looks like a wikilink but
# isn't meant to resolve.
_FENCED_CODE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")

# Paths (relative to VAULT) whose contents intentionally contain placeholder
# wikilinks (Templater variables, example syntax). Wikilink scanning skips
# anything under these prefixes.
_WIKILINK_SKIP_PREFIXES = ("_templates/",)

# Root-level external targets that are valid wikilinks even though the file
# they point at lives outside docs/Musubi/.
_EXTERNAL_TARGETS = {
    "CLAUDE",
    "README",
}


def _build_linkable_index(root: Path) -> set[str]:
    """Return every vault-internal path an Obsidian wikilink can resolve to.

    Each entry is the path relative to the vault root, WITHOUT extension.
    Obsidian matches wikilinks against file stems, so `[[_bases/adrs]]` and
    `[[_bases/adrs.base]]` are both valid references to ``_bases/adrs.base``.
    """
    index: set[str] = set()
    for ext in _LINKABLE_EXTENSIONS:
        for p in root.rglob(f"*{ext}"):
            rel = p.relative_to(root).with_suffix("")
            index.add(str(rel))
            # Also register with the extension, for explicit `[[x.canvas]]` style.
            index.add(str(rel) + ext)
    return index


def _strip_code(text: str) -> str:
    """Remove fenced blocks and inline code spans before wikilink scanning.

    This is how we skip "documentation example" wikilinks — docs like
    ``conventions.md`` use `` `[[path/file]]` `` to show wikilink syntax;
    stripping the backticks before scanning prevents false-positive reports.
    """
    # Order matters: fenced first (greedy multiline), then inline.
    text = _FENCED_CODE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


def check_wikilinks(rep: Report) -> None:
    """Every ``[[wikilink]]`` target resolves to a real file in the vault.

    Skips:
    - Matches inside fenced code blocks and inline code spans.
    - Files under ``_templates/`` (Templater placeholders are intentional).
    - External link-outs like ``[[CLAUDE]]`` and ``[[README]]`` that target
      root-level files outside the vault.

    Reports are errors, not warnings — the author of a note linking to a
    nonexistent target is making a real mistake most of the time, and the
    filters above strip the known-safe exceptions.
    """
    index = _build_linkable_index(VAULT)

    for md in VAULT.rglob("*.md"):
        rel = str(md.relative_to(VAULT))
        if any(rel.startswith(pfx) for pfx in _WIKILINK_SKIP_PREFIXES):
            continue

        text = _strip_code(md.read_text(errors="ignore"))
        for m in _WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            if target in _EXTERNAL_TARGETS:
                continue
            # Strip trailing slashes or spaces (rare, but safe).
            target = target.rstrip("/ ")
            if target in index:
                continue
            # Accept explicit extensions too (e.g. `[[_bases/foo.base]]`).
            if target.endswith(tuple(_LINKABLE_EXTENSIONS)) and target in index:
                continue
            rep.err(rel, f"broken wikilink: [[{target}]]")


# ---------- Check: specs -------------------------------------------------------

SPEC_SECTIONS = (
    "03-system-design",
    "04-data-model",
    "05-retrieval",
    "06-ingestion",
    "07-interfaces",
    "08-deployment",
    "10-security",
)


def check_specs(rep: Report) -> None:
    for section in SPEC_SECTIONS:
        root = VAULT / section
        if not root.exists():
            continue
        for p in root.glob("*.md"):
            if p.name in {"index.md", "CLAUDE.md"}:
                continue
            fm, body = read_frontmatter(p)
            rel = str(p.relative_to(VAULT))
            if fm.get("type") != "spec":
                continue
            # Test Contract section required for non-stub specs
            if fm.get("status") in {"complete", "draft"} and "Test Contract" not in body:
                rep.warn(rel, "spec has no 'Test Contract' section")
            # Implements hint
            if fm.get("status") == "complete" and "implements" not in fm:
                rep.warn(rel, "complete spec has no `implements:` field pointing at the code path")


# ---------- Utilities ----------------------------------------------------------


def list_wiki_targets(val) -> list[str]:
    """Extract paths from a list-of-wikilinks frontmatter value."""
    if val is None:
        return []
    items = [val] if isinstance(val, str) else list(val)
    out = []
    for s in items:
        m = re.match(r"\[\[([^|\]]+)(?:\|[^\]]+)?\]\]", s.strip().strip('"').strip("'"))
        if m:
            out.append(m.group(1))
        elif s:
            out.append(s.strip().strip('"').strip("'"))
    return out


# ---------- Main ---------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "command",
        choices=["vault", "slices", "specs", "issues", "wikilinks", "pr", "all"],
        default="all",
        nargs="?",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rep = Report()
    if args.command in ("vault", "all"):
        check_vault(rep)
    if args.command in ("slices", "all"):
        check_slices(rep)
    if args.command in ("specs", "all"):
        check_specs(rep)
    if args.command in ("issues", "all"):
        check_issues(rep)
    if args.command in ("wikilinks", "all"):
        check_wikilinks(rep)
    if args.command in ("pr", "all"):
        check_pr_closing_slices(rep)

    if args.json:
        print(
            json.dumps(
                {
                    "errors": [{"path": p, "message": m} for (p, m) in rep.errors],
                    "warnings": [{"path": p, "message": m} for (p, m) in rep.warnings],
                },
                indent=2,
            )
        )
    else:
        if rep.errors:
            print(f"\x1b[31m{len(rep.errors)} error(s):\x1b[0m")
            for p, m in rep.errors:
                print(f"  ✗ {p}: {m}")
        if rep.warnings:
            print(f"\x1b[33m{len(rep.warnings)} warning(s):\x1b[0m")
            for p, m in rep.warnings:
                print(f"  ⚠ {p}: {m}")
        if rep.ok() and not rep.warnings:
            print("\x1b[32mOK\x1b[0m")
        elif rep.ok():
            print(f"\x1b[32m{args.command}: clean (warnings only)\x1b[0m")

    return 0 if rep.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
