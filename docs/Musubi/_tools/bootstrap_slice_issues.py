#!/usr/bin/env python3
"""Bootstrap GitHub Issues from the slice registry.

One-shot script. Reads every ``docs/Musubi/_slices/slice-*.md``, builds
an Issue body that links back to the slice note + its specs, sets labels
(``slice``, ``phase:*``, ``status:*``), and calls ``gh issue create``.

Safe to re-run: before opening, it queries existing Issues with the ``slice``
label and skips any slice whose Issue already exists (matched by title
``slice: <slice-id>``).

Invocation:

    python3 docs/Musubi/_tools/bootstrap_slice_issues.py              # dry run
    python3 docs/Musubi/_tools/bootstrap_slice_issues.py --apply      # actually create

Output is a table summarising what would be / was created, so you can eyeball
before committing to the mutation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required; install via `pip install pyyaml`.")


ROOT = Path(__file__).resolve().parents[3]
SLICE_DIR = ROOT / "docs" / "Musubi" / "_slices"

# Mapping from the slice's `phase: "1 Schema"` form to the label name.
PHASE_TO_LABEL = {
    "1 Schema": "phase:1-schema",
    "2 Hybrid": "phase:2-hybrid",
    "3 Reranker": "phase:3-reranker",
    "4 Planes": "phase:4-planes",
    "5 Vault": "phase:5-vault",
    "6 Lifecycle": "phase:6-lifecycle",
    "7 Adapters": "phase:7-adapters",
    "8 Ops": "phase:8-ops",
}


@dataclass
class Slice:
    id: str
    path: Path
    frontmatter: dict
    body: str
    status: str = ""
    phase: str = ""
    depends_on: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    specs: list[str] = field(default_factory=list)


def parse_slice(path: Path) -> Slice:
    text = path.read_text()
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing frontmatter")
    end = text.index("---", 3)
    fm = yaml.safe_load(text[3:end]) or {}
    body = text[end + 3 :].strip()

    def extract_ids(value) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            value = [value]
        return [m.group() for v in value for m in [re.search(r"slice-[\w-]+", str(v))] if m]

    # Pull spec wikilinks from the "Specs to implement" section.
    specs: list[str] = []
    spec_section = re.search(r"## Specs? to implement\s*\n((?:-\s*\[\[.*?\]\]\s*\n)+)", body)
    if spec_section:
        specs = re.findall(r"\[\[([^\]]+)\]\]", spec_section.group(1))

    return Slice(
        id=fm.get("slice_id") or path.stem,
        path=path,
        frontmatter=fm,
        body=body,
        status=str(fm.get("status", "")),
        phase=str(fm.get("phase", "")),
        depends_on=extract_ids(fm.get("depends-on")),
        blocks=extract_ids(fm.get("blocks")),
        specs=specs,
    )


def status_label(status: str) -> str:
    return (
        f"status:{status}"
        if status in {"ready", "in-progress", "in-review", "blocked", "done"}
        else "status:ready"
    )


def render_body(s: Slice, all_slices: dict[str, Slice]) -> str:
    """Compose the GitHub Issue body for slice ``s``."""
    rel_path = f"docs/Musubi/_slices/{s.path.name}"

    lines: list[str] = []
    lines.append(f"Tracks implementation of [`{rel_path}`]({rel_path}).")
    lines.append("")
    lines.append(f"- **Phase:** {s.phase or '—'}")
    owner = s.frontmatter.get("owner")
    if owner and owner != "unassigned":
        lines.append(f"- **Owner (vault):** `{owner}`")
    lines.append("")

    if s.specs:
        lines.append("## Specs this slice implements")
        for spec in s.specs:
            lines.append(f"- [`docs/Musubi/{spec}.md`](docs/Musubi/{spec}.md)")
        lines.append("")

    if s.depends_on:
        lines.append("## Depends on")
        for dep in s.depends_on:
            lines.append(f"- `{dep}` (vault: `docs/Musubi/_slices/{dep}.md`)")
        lines.append("")
        lines.append(
            "Start this slice only after every listed dep is `status:done` "
            "(or has a first cut merged that you're comfortable depending on)."
        )
        lines.append("")

    if s.blocks:
        lines.append("## Unblocks")
        for blk in s.blocks:
            lines.append(f"- `{blk}`")
        lines.append("")

    lines.append("## Test Contract")
    lines.append("")
    lines.append(
        "See the **Test Contract** section in the linked spec(s) above. "
        "At handoff, every bullet must be in one of the three states "
        "([Closure Rule](docs/Musubi/00-index/agent-guardrails.md#test-contract-closure-rule))."
    )
    lines.append("")

    lines.append("## How to claim")
    lines.append("")
    lines.append("```")
    lines.append(
        f"gh issue edit $(gh issue list --search 'slice: {s.id}' --json number --jq '.[0].number') \\"
    )
    lines.append("  --add-assignee @me \\")
    lines.append("  --add-label 'status:in-progress' --remove-label 'status:ready'")
    lines.append("```")
    lines.append("")
    lines.append(
        "Then update the slice note's frontmatter (`status: ready → in-progress`, "
        "`owner:` set) in the same PR — per the Dual-update rule in "
        "[`agent-guardrails.md`](docs/Musubi/00-index/agent-guardrails.md)."
    )

    return "\n".join(lines)


def existing_slice_issues() -> dict[str, int]:
    """Map ``slice-id`` → Issue number for any pre-existing slice Issues."""
    result = subprocess.run(
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
            "number,title",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    out: dict[str, int] = {}
    for item in json.loads(result.stdout):
        title = item.get("title", "")
        m = re.match(r"slice: (slice-[\w-]+)", title)
        if m:
            out[m.group(1)] = item["number"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply", action="store_true", help="Actually create Issues; default is dry-run."
    )
    args = ap.parse_args()

    slices: dict[str, Slice] = {}
    for p in sorted(SLICE_DIR.glob("slice-*.md")):
        try:
            s = parse_slice(p)
        except Exception as e:
            print(f"  ✗ {p.name}: {e}", file=sys.stderr)
            continue
        slices[s.id] = s

    existing = existing_slice_issues()

    created = 0
    skipped = 0
    print(f"{'slice-id':<42}  {'status':<14}  {'phase':<20}  action")
    print("-" * 100)
    for sid in sorted(slices):
        s = slices[sid]
        phase_lbl = PHASE_TO_LABEL.get(s.phase, "")
        status_lbl = status_label(s.status)
        if sid in existing:
            action = f"skip (exists: #{existing[sid]})"
            skipped += 1
        elif not args.apply:
            action = f"would create ({status_lbl}, {phase_lbl or '—'})"
        else:
            body = render_body(s, slices)
            cmd = [
                "gh",
                "issue",
                "create",
                "--title",
                f"slice: {sid}",
                "--body",
                body,
                "--label",
                "slice",
                "--label",
                status_lbl,
            ]
            if phase_lbl:
                cmd += ["--label", phase_lbl]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                action = f"ERROR: {r.stderr.strip()[:60]}"
            else:
                url = r.stdout.strip()
                action = f"created {url.split('/')[-1]}"
                created += 1
        print(f"{sid:<42}  {s.status:<14}  {s.phase:<20}  {action}")

    print()
    print(
        f"Summary: {created} created, {skipped} already existed, {len(slices) - created - skipped} unprocessed."
    )
    if not args.apply:
        print("(dry run — pass --apply to actually create)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
