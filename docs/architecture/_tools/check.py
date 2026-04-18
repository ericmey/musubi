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

    def merge(self, other: "Report") -> None:
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
        if exclude_infra:
            # Skip any note whose immediate parent-chain hits an infra folder.
            if any(
                str(rel).startswith(f + "/") or str(rel).startswith(f + os.sep)
                for f in INFRA_FOLDERS
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
        # Section field matches parent folder (for foldered notes)
        parent = p.parent.name
        if "/" in rel and parent and parent != "_inbox":
            if fm.get("section") and fm["section"] != parent and not parent.startswith("_"):
                rep.warn(rel, f"section '{fm.get('section')}' != folder '{parent}'")
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

    # 2. owns_paths uniqueness — scope to the ## Owned paths section only
    claims: dict[str, str] = {}
    for sid, s in slices.items():
        body = s["path"].read_text()
        m = re.search(r"## Owned paths.*?\n(.*?)\n## ", body, re.S)
        if not m:
            continue
        owned_section = m.group(1)
        for pm in re.finditer(r"^\s*-\s+`([^`]+)`\s*$", owned_section, re.M):
            path = pm.group(1).strip()
            if path in claims and claims[path] != sid:
                rep.err(
                    str(s["path"].relative_to(VAULT)),
                    f"owns_paths conflict: '{path}' also claimed by '{claims[path]}'",
                )
            claims[path] = sid

    # 3. status transitions
    for sid, s in slices.items():
        status = s["fm"].get("status")
        if status not in {"ready", "in-progress", "in-review", "blocked", "done"}:
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
    if isinstance(val, str):
        items = [val]
    else:
        items = list(val)
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
        "command", choices=["vault", "slices", "specs", "all"], default="all", nargs="?"
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
