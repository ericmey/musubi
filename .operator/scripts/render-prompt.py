#!/usr/bin/env python3
"""Musubi render-prompt script.
from typing import Any

Generates a fully-formed agent session prompt from slice metadata + template.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Add .operator/scripts to sys.path so we can import claimable
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    import jinja2
except ImportError:
    print(
        "ERROR: jinja2 is required. Try `pip install jinja2` or `uv run pip install jinja2`.",
        file=sys.stderr,
    )
    sys.exit(1)

from claimable import REPO_ROOT, Slice, load_issues, load_slices  # noqa: E402


def get_parallel_agents(slices: dict[str, Slice], exclude_sid: str) -> list[dict[str, Any]]:
    in_flight = []
    for sid, s in slices.items():
        if sid == exclude_sid:
            continue
        if s.status == "in-progress":
            lock_file = REPO_ROOT / "docs" / "architecture" / "_inbox" / "locks" / f"{sid}.lock"
            if lock_file.exists():
                try:
                    content = lock_file.read_text().strip()
                    agent = content.split()[0] if content else "unknown"
                    mtime = lock_file.stat().st_mtime
                    age_h = (time.time() - mtime) / 3600.0
                    if age_h <= 4:
                        in_flight.append(
                            {"slice_id": sid, "agent": agent, "age_h": round(age_h, 1)}
                        )
                except Exception:
                    pass
    return sorted(in_flight, key=lambda x: x["age_h"])


def render_prompt(
    agent_name: str,
    slice_id: str,
    clone_path: str,
    template_name: str,
    as_json: bool,
    dry_run: bool,
) -> None:
    slices = load_slices()
    issues = load_issues()

    if slice_id not in slices:
        print(f"ERROR: Slice '{slice_id}' not found in vault.", file=sys.stderr)
        sys.exit(1)

    s = slices[slice_id]

    issue_number = issues[slice_id].number if slice_id in issues else "?"
    if issue_number == "?":
        print(f"ERROR: No matching GitHub Issue found for '{slice_id}'.", file=sys.stderr)
        sys.exit(1)

    if not s.specs and not s.owns_paths:
        print(f"ERROR: Slice '{slice_id}' has no specs or owns_paths declared.", file=sys.stderr)
        sys.exit(1)

    dependencies = []
    for dep in s.depends_on:
        if dep in slices:
            dependencies.append({"slice_id": dep, "status": slices[dep].status})
        else:
            dependencies.append({"slice_id": dep, "status": "missing"})

    parallel_agents = get_parallel_agents(slices, slice_id)

    data = {
        "agent_name": agent_name,
        "slice_id": slice_id,
        "issue_number": issue_number,
        "slice_file_path": str(s.path.relative_to(REPO_ROOT)),
        "specs": s.specs,
        "owned_paths": s.owns_paths,
        "forbidden_paths": s.forbidden_paths,
        "dependencies": dependencies,
        "parallel_agents": parallel_agents,
        "clone_path": clone_path,
    }

    if as_json:
        print(json.dumps(data, indent=2))
        return

    if dry_run:
        print("--- DRY RUN DATA ---")
        for k, v in data.items():
            print(f"{k}: {v}")
        return

    templates_dir = REPO_ROOT / ".operator" / "prompts"
    template_file = templates_dir / f"{template_name}.md.template"

    if not template_file.exists():
        print(f"ERROR: Template file '{template_file}' not found.", file=sys.stderr)
        sys.exit(1)

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    try:
        template = env.get_template(f"{template_name}.md.template")
        rendered = template.render(**data)
        print(rendered.strip())
    except jinja2.TemplateError as e:
        print(f"ERROR rendering template: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render agent prompt for a slice")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Render slice-start prompt")
    start_parser.add_argument("--agent", required=True, help="Agent name (e.g. codex-gpt5)")
    start_parser.add_argument("--slice", required=True, dest="slice_id", help="Slice ID")
    start_parser.add_argument(
        "--clone-path", required=True, help="Local clone path for agent workspace"
    )
    start_parser.add_argument("--template", default="slice-start", help="Alternate template name")
    start_parser.add_argument("--json", action="store_true", help="Machine-readable output")
    start_parser.add_argument(
        "--dry-run", action="store_true", help="Show slot values without rendering"
    )

    args = parser.parse_args()

    if args.command == "start":
        render_prompt(
            agent_name=args.agent,
            slice_id=args.slice_id,
            clone_path=args.clone_path,
            template_name=args.template,
            as_json=args.json,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
