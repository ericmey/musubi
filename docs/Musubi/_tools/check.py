#!/usr/bin/env python3
"""
check.py — Musubi vault + slice + spec validator.

Usage:
  python3 _tools/check.py [vault|specs|wikilinks|all] [--json] [--fix]

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
        choices=["vault", "specs", "wikilinks", "all"],
        default="all",
        nargs="?",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rep = Report()
    if args.command in ("vault", "all"):
        check_vault(rep)
    if args.command in ("specs", "all"):
        check_specs(rep)
    if args.command in ("wikilinks", "all"):
        check_wikilinks(rep)

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
