#!/usr/bin/env python3
"""Generate the Test Contract coverage matrix for one slice.

Mechanically audits the [Test Contract Closure
Rule](../../architecture/00-index/agent-guardrails.md#Test-Contract-Closure-Rule)
for a slice at handoff time.

Reads ``docs/architecture/_slices/<slice-id>.md``, finds the specs it
``implements:`` (or links from ``## Specs to implement``), parses each spec's
``## Test Contract`` section into bullets, then classifies each bullet:

  - ``✓ passing``          — a matching ``def test_<name>`` exists in tests/
                              and is not decorated with skip/xfail.
  - ``⏭ skipped``          — function exists but is ``@pytest.mark.skip`` or
                              ``@pytest.mark.xfail`` — reason is captured.
  - ``⊘ out-of-scope``     — bullet text appears in the slice's ``## Work
                              log`` section as a deferral declaration.
  - ``⊘ non-test``         — bullet doesn't start with ``test_``
                              (``hypothesis:``, ``integration:``, prose) —
                              almost always declared out-of-scope for unit
                              tests; flagged for the author to confirm.
  - ``✗ missing``          — no test, no work-log mention. **Review-blocker.**

Output is either a markdown table (default — paste directly into the PR
template's Test Contract coverage matrix) or JSON.

Usage:

    python3 docs/architecture/_tools/tc_coverage.py slice-plane-episodic
    python3 docs/architecture/_tools/tc_coverage.py slice-plane-episodic --json
    make tc-coverage SLICE=slice-plane-episodic
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VAULT = ROOT / "docs" / "architecture"
TESTS_DIR = ROOT / "tests"

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_TEST_CONTRACT_HEADING_RE = re.compile(r"^##\s+Test [Cc]ontract.*$", re.M)
# Use [ \t] (not \s) so the note capture can't bleed across newlines into
# the next bullet. Earlier bug: \s+(.*?)$ consumed \n then matched the next
# line as the "note" — fixed by restricting horizontal whitespace only.
_BULLET_RE = re.compile(r"^\d+\.[ \t]+`([^`]+)`[ \t]*(.*)$", re.M)
_FUNCTION_DEF_RE = re.compile(r"^(?:async\s+)?def\s+(\w+)\b", re.M)
_SKIP_DECORATOR_RE = re.compile(
    r"@pytest\.mark\.(skip|xfail)\s*\(\s*reason\s*=\s*([\"'])(.+?)\2"
)


@dataclass
class Bullet:
    """One parsed Test Contract bullet."""
    spec: str
    index: int
    name: str
    note: str = ""
    state: str = "✗ missing"
    evidence: str = ""


def _section_after_heading(text: str, heading_re: re.Pattern[str]) -> str:
    """Return the text between ``heading_re`` match and the next ``## `` heading."""
    m = heading_re.search(text)
    if not m:
        return ""
    start = m.end()
    next_hdr = re.search(r"^## ", text[start:], re.M)
    end = start + next_hdr.start() if next_hdr else len(text)
    return text[start:end]


def _read_slice(slice_id: str) -> tuple[Path, str]:
    path = VAULT / "_slices" / f"{slice_id}.md"
    if not path.exists():
        raise FileNotFoundError(f"Slice file not found: {path}")
    return path, path.read_text()


def _extract_specs(slice_text: str) -> list[Path]:
    """Find the specs the slice implements from its ``## Specs to implement`` section."""
    section = _section_after_heading(slice_text, re.compile(r"^##\s+Specs?\s+to\s+implement\s*$", re.M))
    if not section:
        return []
    paths: list[Path] = []
    for link in _WIKILINK_RE.findall(section):
        target = link.strip().rstrip("|")
        if target.startswith("docs/architecture/"):
            target = target[len("docs/architecture/"):]
        p = VAULT / f"{target}.md"
        if p.exists():
            paths.append(p)
    return paths


def _extract_work_log(slice_text: str) -> str:
    """Pull the slice's ``## Work log`` section as plain text for out-of-scope detection."""
    return _section_after_heading(slice_text, re.compile(r"^##\s+Work\s+log\s*$", re.M))


def _parse_bullets(spec_text: str, spec_rel: str) -> list[Bullet]:
    """Parse a spec's Test Contract section into bullets, preserving order."""
    section = _section_after_heading(spec_text, _TEST_CONTRACT_HEADING_RE)
    if not section:
        return []
    out: list[Bullet] = []
    for i, m in enumerate(_BULLET_RE.finditer(section), start=1):
        name = m.group(1).strip()
        note = (m.group(2) or "").strip()
        out.append(Bullet(spec=spec_rel, index=i, name=name, note=note))
    return out


def _find_test_definition(func_name: str) -> tuple[Path, int, str] | None:
    """Search tests/ for ``def <func_name>``. Returns (path, lineno, decorator-text-if-any)."""
    if not TESTS_DIR.exists():
        return None
    needle = re.compile(rf"^(?:async\s+)?def\s+{re.escape(func_name)}\b", re.M)
    for py in TESTS_DIR.rglob("*.py"):
        text = py.read_text(errors="ignore")
        m = needle.search(text)
        if not m:
            continue
        lineno = text[: m.start()].count("\n") + 1
        # Look up to 5 lines above for a pytest.mark.skip / .xfail decorator.
        start = m.start()
        header = text[max(0, start - 400): start]
        preceding_lines = header.splitlines()[-5:]
        decorator_block = "\n".join(preceding_lines)
        return py, lineno, decorator_block
    return None


def classify(bullet: Bullet, work_log: str) -> Bullet:
    name = bullet.name.strip()

    # Non-test bullets (hypothesis:, integration:, prose) — flag as non-test.
    if not name.startswith("test_"):
        # Still check if the author mentioned them in the work log.
        if name in work_log or name[:40] in work_log:
            bullet.state = "⊘ out-of-scope"
            bullet.evidence = "declared in slice work log"
        else:
            bullet.state = "⊘ non-test"
            bullet.evidence = "property/integration/prose bullet — confirm out-of-scope in work log"
        return bullet

    found = _find_test_definition(name)
    if found:
        path, lineno, decorator_block = found
        skip = _SKIP_DECORATOR_RE.search(decorator_block)
        rel = path.relative_to(ROOT).as_posix()
        if skip:
            bullet.state = "⏭ skipped"
            bullet.evidence = f"`{rel}:{lineno}` (reason: {skip.group(3)})"
        else:
            bullet.state = "✓ passing"
            bullet.evidence = f"`{rel}:{lineno}`"
        return bullet

    # Not found in tests/ — check work log.
    if name in work_log:
        bullet.state = "⊘ out-of-scope"
        bullet.evidence = "declared in slice work log"
        return bullet

    bullet.state = "✗ missing"
    bullet.evidence = "—"
    return bullet


def render_markdown(bullets: list[Bullet]) -> str:
    lines = [
        "| # | Bullet | State | Evidence |",
        "|---|---|---|---|",
    ]
    for b in bullets:
        # Escape pipes inside evidence text for the markdown table.
        ev = b.evidence.replace("|", "\\|")
        note = f" — {b.note}" if b.note else ""
        lines.append(f"| {b.index} | `{b.name}`{note} | {b.state} | {ev} |")
    return "\n".join(lines)


def render_summary(bullets: list[Bullet]) -> str:
    counts: dict[str, int] = {}
    for b in bullets:
        counts[b.state] = counts.get(b.state, 0) + 1
    order = ["✓ passing", "⏭ skipped", "⊘ out-of-scope", "⊘ non-test", "✗ missing"]
    parts = [f"{counts[k]} {k}" for k in order if k in counts]
    total = len(bullets)
    missing = counts.get("✗ missing", 0)
    return (
        f"\nTotal: {total} bullet(s) — " + ", ".join(parts) + "\n"
        + (
            f"\n⚠ {missing} missing bullet(s) — Test Contract Closure Rule violated. "
            "Either write the test, mark @pytest.mark.skip with a reason, or declare "
            "out-of-scope in the slice's ## Work log.\n" if missing else "\n✓ Closure Rule satisfied.\n"
        )
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slice_id", help="Slice id — e.g. slice-plane-episodic")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a markdown table")
    args = ap.parse_args()

    try:
        _slice_path, slice_text = _read_slice(args.slice_id)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    specs = _extract_specs(slice_text)
    if not specs:
        print(
            f"error: no specs found under '## Specs to implement' in slice file",
            file=sys.stderr,
        )
        return 2

    work_log = _extract_work_log(slice_text)

    all_bullets: list[Bullet] = []
    for spec in specs:
        spec_rel = spec.relative_to(VAULT).as_posix()
        all_bullets.extend(_parse_bullets(spec.read_text(), spec_rel))

    classified = [classify(b, work_log) for b in all_bullets]

    if args.json:
        print(json.dumps(
            {
                "slice": args.slice_id,
                "specs": [s.relative_to(VAULT).as_posix() for s in specs],
                "bullets": [
                    {"index": b.index, "spec": b.spec, "name": b.name, "note": b.note,
                     "state": b.state, "evidence": b.evidence}
                    for b in classified
                ],
            },
            indent=2,
        ))
    else:
        print(f"Test Contract coverage for **{args.slice_id}**\n")
        print(f"Specs: {', '.join(f'`{s.relative_to(VAULT).as_posix()}`' for s in specs)}\n")
        print(render_markdown(classified))
        print(render_summary(classified))

    # Exit 1 if any bullet is ✗ missing (gate can use this).
    return 1 if any(b.state == "✗ missing" for b in classified) else 0


if __name__ == "__main__":
    sys.exit(main())
