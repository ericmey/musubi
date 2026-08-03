#!/usr/bin/env python3
"""Generate the Test Contract coverage matrix for one slice.

Mechanically audits the [Test Contract Closure
Rule](../../architecture/00-index/agent-guardrails.md#Test-Contract-Closure-Rule)
for a slice at handoff time.

Reads ``docs/Musubi/_slices/<slice-id>.md``, finds the specs it
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
  - ``✗ unparseable``      — a list item in the section (ordered, unordered, or
                              task list) that is not ``test_name``-shaped, so
                              the gate cannot check it. **Review-blocker.**
                              Before Issue #669 these were dropped silently,
                              which let a green be computed over a fraction of
                              the stated contract.
  - ``✗ no-test-contract`` — a linked spec with no ``## Test contract`` section
                              at all. **Review-blocker**, and distinct from a
                              contract that exists and is empty.

Output is either a markdown table (default — paste directly into the PR
template's Test Contract coverage matrix) or JSON.

Usage:

    python3 docs/Musubi/_tools/tc_coverage.py slice-plane-episodic
    python3 docs/Musubi/_tools/tc_coverage.py slice-plane-episodic --json
    make tc-coverage SLICE=slice-plane-episodic
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VAULT = ROOT / "docs" / "Musubi"
TESTS_DIR = ROOT / "tests"

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_TEST_CONTRACT_HEADING_RE = re.compile(r"^##\s+Test [Cc]ontract.*$", re.M)
# Use [ \t] (not \s) throughout so a note capture can't bleed across newlines
# into the next bullet. Earlier bug: \s+(.*?)$ consumed \n then matched the
# next line as the "note" — fixed by restricting to horizontal whitespace.
#
# EVERY list item in a Test Contract section — ordered (`1.`), unordered
# (`-`/`*`/`+`), and GitHub task lists (`- [ ]`) — parseable or not.
#
# Scoping this to `^\d+\.` was the first, incomplete fix for Issue #669: it
# rescued ADR 0040's prose but left five specs whose contracts are written with
# dashes parsing to ZERO. `03-system-design/namespaces.md` is the sharp case —
# it states five properly-named bullets (`test_isolation_read_enforcement`, …)
# that the gate could not see purely because they use `-` instead of `1.`, so
# the namespace isolation tests went unchecked by every slice implementing it.
# The lesson is the bug's own: a parser narrower than the syntax in use reports
# a green it never earned. (Raised in review on PR #670.)
_LIST_ITEM_RE = re.compile(r"^(?:(\d+)\.|[-*+])[ \t]+(.*)$", re.M)
# A leading GitHub task-list checkbox, stripped before reading the item text.
_CHECKBOX_RE = re.compile(r"^\[[ xX]\][ \t]*")
# An item whose text BEGINS with a backticked token — the machine-checkable
# shape. Applied to the item's text, not the whole line, so it works for every
# list syntax above.
_NAMED_ITEM_RE = re.compile(r"^`([^`]+)`[ \t]*(.*)$")
# Leading separator between a bullet's name and its prose. Specs write
# "`test_x` <dash> prose" and render_markdown adds its own separator, so
# without this the table reads "test_x <dash> <dash> prose".
# Matches U+2014, U+2013 and ASCII hyphen; written as escapes because ruff
# RUF001/RUF003 flag the literal glyphs as ambiguous.
_NOTE_SEPARATOR_RE = re.compile("^[\u2014\u2013-]+[ \t]*")
# Fenced code blocks are stripped before counting items so a numbered line
# inside an example block is not mistaken for a contract bullet.
_FENCE_RE = re.compile(r"^```.*?^```", re.M | re.S)
_FUNCTION_DEF_RE = re.compile(r"^(?:async\s+)?def\s+(\w+)\b", re.M)

# States that mean "the gate could not examine this", as opposed to "the gate
# examined it and it is absent" (✗ missing). Both block the Closure Rule, but
# they are different failures and must not be reported as the same one.
UNPARSEABLE = "✗ unparseable"
NO_CONTRACT = "✗ no-test-contract"
_BLOCKING_STATES = ("✗ missing", UNPARSEABLE, NO_CONTRACT)
_SKIP_DECORATOR_RE = re.compile(r"@pytest\.mark\.(skip|xfail)\s*\(\s*reason\s*=\s*([\"'])(.+?)\2")


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
    section = _section_after_heading(
        slice_text, re.compile(r"^##\s+Specs?\s+to\s+implement\s*$", re.M)
    )
    if not section:
        return []
    paths: list[Path] = []
    for link in _WIKILINK_RE.findall(section):
        target = link.strip().rstrip("|")
        if target.startswith("docs/Musubi/"):
            target = target[len("docs/Musubi/") :]
        p = VAULT / f"{target}.md"
        if p.exists():
            paths.append(p)
    return paths


def _extract_work_log(slice_text: str) -> str:
    """Pull the slice's ``## Work log`` section as plain text for out-of-scope detection."""
    return _section_after_heading(slice_text, re.compile(r"^##\s+Work\s+log\s*$", re.M))


def has_test_contract(spec_text: str) -> bool:
    """True if the spec has a ``## Test contract`` heading at all.

    A spec with no such section previously produced zero bullets — identical
    output to a spec whose contract is genuinely empty, and silently counted as
    full coverage. main() turns a False here into a ✗ no-test-contract blocker.
    """
    return _TEST_CONTRACT_HEADING_RE.search(spec_text) is not None


def _parse_bullets(spec_text: str, spec_rel: str) -> list[Bullet]:
    """Parse a spec's Test Contract section into bullets, preserving order.

    Enumerates EVERY list item in the section — ordered, unordered, and task
    lists. An item whose text begins with a backticked token becomes an ordinary
    bullet; anything else becomes ✗ unparseable, which is visible and blocking.

    Before Issue #669 only ``^\\d+\\.[ \\t]+`name`  `` matched, so an item the
    pattern could not read was never constructed as a Bullet at all — it could
    not even be reported ✗ missing, and ``Total: N`` read as the population when
    it was only the parseable subset. Two rounds of evidence:

    - ADR 0040 states 14 obligations in prose and yielded 1.
    - Five specs write their contracts with dashes or task lists and yielded 0,
      including ``03-system-design/namespaces.md``, whose five properly-named
      isolation bullets went unchecked by every slice implementing it. Found in
      review on PR #670, after the first fix widened the parser only to numbers.
    """
    section = _section_after_heading(spec_text, _TEST_CONTRACT_HEADING_RE)
    if not section:
        return []
    section = _FENCE_RE.sub("", section)
    out: list[Bullet] = []
    for position, m in enumerate(_LIST_ITEM_RE.finditer(section), start=1):
        # For an ORDERED item use the number the SPEC states, not match order.
        # The `#` column is a cross-reference: a reviewer reads "✗ unparseable
        # #9" and goes to look at item 9 in the spec. Match order silently
        # diverges from the stated numbering whenever a list is non-contiguous
        # (starts at 9, has gaps), sending them to the wrong obligation.
        # An UNORDERED item states no number, so position is the only reference
        # a reader can use to find it.
        index = int(m.group(1)) if m.group(1) else position
        text = " ".join(_CHECKBOX_RE.sub("", m.group(2)).split())
        named = _NAMED_ITEM_RE.match(text)
        if named:
            # Strip a leading em/en/hyphen separator: specs write
            # "`test_x` — prose", and render_markdown adds its own " — ".
            note = _NOTE_SEPARATOR_RE.sub("", (named.group(2) or "").strip())
            out.append(
                Bullet(
                    spec=spec_rel,
                    index=index,
                    name=named.group(1).strip(),
                    note=note.strip(),
                )
            )
            continue
        out.append(
            Bullet(
                spec=spec_rel,
                index=index,
                name=text[:90] + ("…" if len(text) > 90 else ""),
                state=UNPARSEABLE,
                evidence=(
                    "numbered item is not `test_name`-shaped — the gate cannot "
                    "check it; rewrite as a backticked test name or move it out "
                    "of the Test Contract"
                ),
            )
        )
    return out


def _extract_skip_reason(
    decorator: ast.expr, module_names: dict[str, str] | None = None
) -> str | None:
    """Extract reason from @pytest.mark.skip(reason=...) or xfail.

    Recognises a ``reason=`` kwarg whose value is either a string literal
    (``ast.Constant`` of type ``str``) or a module-level variable that
    resolves to a string constant via ``module_names`` (Issue #457).
    Returns ``None`` for unresolved variable names, missing reasons, or
    non-string values; this preserves the original fallthrough semantics
    for callers that do not supply ``module_names``.
    """
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in ("skip", "xfail"):
        return None
    if not isinstance(func.value, ast.Attribute):
        return None
    if func.value.attr != "mark":
        return None
    if not isinstance(func.value.value, ast.Name) or func.value.value.id != "pytest":
        return None

    for kw in decorator.keywords:
        if kw.arg != "reason":
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
        if module_names is not None and isinstance(kw.value, ast.Name):
            resolved = module_names.get(kw.value.id)
            if resolved is not None:
                return resolved
        # Anything else (f-string, call, attribute, unresolved name) — no reason captured.
    return None


def _assignment_target_value(node: ast.stmt) -> tuple[ast.expr | None, ast.expr | None]:
    """Return (target, value) for simple ``Name = ...`` / ``Name: T = ...`` statements.

    Returns (None, None) for any other statement shape (multi-target assign,
    tuple unpacking, function/class def, import, augmented assign, etc.).
    Used by the positional resolver so we only follow the single-target
    simple-name patterns Python can actually evaluate at module scope.
    """
    if isinstance(node, ast.Assign):
        if len(node.targets) != 1:
            return None, None
        return node.targets[0], node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return node.target, node.value
    return None, None


def _positional_module_string_bindings(
    tree: ast.Module, target_node: ast.FunctionDef | ast.AsyncFunctionDef
) -> dict[str, str]:
    """Resolve module-level ``Name = "literal"`` / ``Name: str = "literal"`` bindings
    AT THE POSITION of ``target_node`` (Issue #457 positional repair, chair-20260714-
    094556-c88885cb).

    Walks ``tree.body`` IN ORDER, tracking each simple-name binding as Python
    would at module-evaluation time:

      - simple-name string Assign/AnnAssign -> update the binding to that string
      - simple-name non-string Assign/AnnAssign -> mark the name as unresolved
        (it was bound, just not to a string; Python would raise TypeError at
        decorator eval if the name is used as a ``reason=`` kwarg)
      - any other statement shape (multi-target, tuple unpacking, function
        def, import, augmented assign, etc.) -> ignored; the binding is
        left as-is (Python may or may not bind a name; we cannot tell from
        the AST alone, so the conservative choice is to leave the prior
        binding intact — which matches what a real Python interpreter
        would observe for the simple cases we care about)
      - any node AFTER ``target_node`` is NOT visited (Python has not
        evaluated it yet when the decorator runs; the binding is not
        visible at decorator time)

    Returns a dict of name -> resolved-string for names currently bound
    to a string at the position of ``target_node``. Names marked unresolved
    are excluded from the result; names not yet bound are also excluded.
    """
    bindings: dict[str, str | None] = {}
    for node in tree.body:
        if node is target_node:
            break
        target, value = _assignment_target_value(node)
        if target is None or value is None:
            continue
        if not isinstance(target, ast.Name):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            bindings[target.id] = value.value
        else:
            # Non-string rebind: the name is bound at this position, just
            # not to a string. Mark unresolved so a later lookup returns None
            # (matching the bounded semantic: if Python tried to use the
            # name as a string reason, it would raise TypeError, which is
            # not the ``str | None`` shape we return; we report None
            # instead of fabricating a string).
            bindings[target.id] = None
    return {k: v for k, v in bindings.items() if v is not None}


def _find_test_definition(func_name: str) -> tuple[Path, int, str | None] | None:
    """Search tests/ for ``def <func_name>``. Returns (path, lineno, reason-if-any).

    The ``reason-if-any`` is sourced from the matching decorator's ``reason=``
    kwarg; it may be a string literal or a module-level name resolved to a
    string constant AT THE POSITION OF THE FUNCTION (Issue #457 positional
    repair, chair-20260714-094556-c88885cb). The positional snapshot stops
    AT (not past) the function node so later rebinds are not visible.
    """
    if not TESTS_DIR.exists():
        return None
    needle = re.compile(rf"^(?:async\s+)?def\s+{re.escape(func_name)}\b", re.M)
    for py in TESTS_DIR.rglob("*.py"):
        text = py.read_text(errors="ignore")
        m = needle.search(text)
        if not m:
            continue

        try:
            tree = ast.parse(text, filename=str(py))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                module_names = _positional_module_string_bindings(tree, node)
                reason = None
                for decorator in node.decorator_list:
                    r = _extract_skip_reason(decorator, module_names=module_names)
                    if r is not None:
                        reason = r
                        break
                return py, node.lineno, reason
    return None


def classify(bullet: Bullet, work_log: str) -> Bullet:
    # Already terminal: the gate could not read this item, so there is nothing
    # to look up. Never let a work-log mention silently clear an unreadable
    # bullet — that would restore the exact silence Issue #669 removed.
    if bullet.state in (UNPARSEABLE, NO_CONTRACT):
        return bullet

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
        path, lineno, skip_reason = found
        rel = path.relative_to(ROOT).as_posix()
        if skip_reason is not None:
            bullet.state = "⏭ skipped"
            bullet.evidence = f"`{rel}:{lineno}` (reason: {skip_reason})"
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
    order = [
        "✓ passing",
        "⏭ skipped",
        "⊘ out-of-scope",
        "⊘ non-test",
        "✗ missing",
        UNPARSEABLE,
        NO_CONTRACT,
    ]
    parts = [f"{counts[k]} {k}" for k in order if k in counts]
    total = len(bullets)
    missing = counts.get("✗ missing", 0)
    unparseable = counts.get(UNPARSEABLE, 0)
    no_contract = counts.get(NO_CONTRACT, 0)
    checkable = total - unparseable - no_contract

    # State the examined-vs-stated ratio unconditionally. The whole failure in
    # Issue #669 was that a narrowed input was invisible in the output: the
    # summary said "Total: 19" when the specs stated 32, and nothing on screen
    # showed the difference. A reader must not have to open the source to learn
    # how much the gate skipped.
    lines = [
        f"\nTotal: {total} stated bullet(s) — " + ", ".join(parts),
        f"Machine-checkable: {checkable}/{total}"
        + (" — every stated bullet is readable by the gate." if checkable == total else ""),
    ]

    blockers = missing + unparseable + no_contract
    if not blockers:
        lines.append("\n✓ Closure Rule satisfied.\n")
        return "\n".join(lines)

    lines.append("")
    if missing:
        lines.append(
            f"⚠ {missing} missing bullet(s) — Test Contract Closure Rule violated. "
            "Either write the test, mark @pytest.mark.skip with a reason, or declare "
            "out-of-scope in the slice's ## Work log."
        )
    if unparseable:
        lines.append(
            f"⚠ {unparseable} unparseable bullet(s) — stated in the spec but not "
            "`test_name`-shaped, so the gate CANNOT check them. These were silently "
            "dropped before Issue #669; a green computed without them examined less "
            "than it appeared to."
        )
    if no_contract:
        lines.append(
            f"⚠ {no_contract} linked spec(s) have no '## Test contract' section at all. "
            "A spec that states no obligations is not the same as a spec that meets "
            "them; declare the contract or unlink the spec."
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
            "error: no specs found under '## Specs to implement' in slice file",
            file=sys.stderr,
        )
        return 2

    work_log = _extract_work_log(slice_text)

    all_bullets: list[Bullet] = []
    for spec in specs:
        spec_rel = spec.relative_to(VAULT).as_posix()
        spec_text = spec.read_text()
        if not has_test_contract(spec_text):
            all_bullets.append(
                Bullet(
                    spec=spec_rel,
                    index=0,
                    name=f"(no '## Test contract' section in {spec_rel})",
                    state=NO_CONTRACT,
                    evidence="spec is linked from '## Specs to implement' but states no contract",
                )
            )
            continue
        all_bullets.extend(_parse_bullets(spec_text, spec_rel))

    classified = [classify(b, work_log) for b in all_bullets]

    # The slice's OWN '## Test Contract' is reported but NOT gated. tc_coverage
    # checks the contracts of the specs a slice implements; a docs-only slice
    # legitimately declares forward obligations for a future implementation
    # slice, and marking those ✗ missing here would be wrong. Reporting the
    # count is not: before Issue #669 these bullets were invisible, so a slice
    # could state nine obligations that no output ever mentioned.
    own_bullets = _parse_bullets(slice_text, f"_slices/{args.slice_id}.md")

    if args.json:
        print(
            json.dumps(
                {
                    "slice": args.slice_id,
                    "specs": [s.relative_to(VAULT).as_posix() for s in specs],
                    "slice_own_contract_bullets": len(own_bullets),
                    "bullets": [
                        {
                            "index": b.index,
                            "spec": b.spec,
                            "name": b.name,
                            "note": b.note,
                            "state": b.state,
                            "evidence": b.evidence,
                        }
                        for b in classified
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"Test Contract coverage for **{args.slice_id}**\n")
        print(f"Specs: {', '.join(f'`{s.relative_to(VAULT).as_posix()}`' for s in specs)}\n")
        print(render_markdown(classified))
        print(render_summary(classified))
        if own_bullets:
            print(
                f"Note: this slice declares {len(own_bullets)} bullet(s) in its own "
                "'## Test Contract'. Those are NOT gated here — tc_coverage checks the "
                "contracts of the specs under '## Specs to implement'. They are an "
                "obligation on the implementation slice that owns the relevant code.\n"
            )

    # Exit 1 if the Closure Rule is violated. ✗ missing means the gate looked
    # and found nothing; ✗ unparseable / ✗ no-test-contract mean the gate could
    # not look at all. All three block — an unexaminable contract must never
    # exit 0, which is precisely how the Issue #669 green was earned.
    return 1 if any(b.state in _BLOCKING_STATES for b in classified) else 0


if __name__ == "__main__":
    sys.exit(main())
