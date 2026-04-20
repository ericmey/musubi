#!/usr/bin/env python3
"""Musubi status board."""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# Add .operator/scripts to sys.path so we can import claimable
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from claimable import REPO_ROOT, load_issues, load_slices  # noqa: E402


def get_git_head() -> str:
    try:
        res = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


def get_open_prs() -> list[dict[str, Any]]:
    try:
        res = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "number,title,author,isDraft,mergeStateStatus,statusCheckRollup",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        if res.stdout:
            return cast(list[dict[str, Any]], json.loads(res.stdout))
    except Exception:
        pass
    return []


def get_open_issues() -> list[dict[str, Any]]:
    try:
        res = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--json", "number,title,labels"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        if res.stdout:
            return cast(list[dict[str, Any]], json.loads(res.stdout))
    except Exception:
        pass
    return []


def get_branch_age_hours(slice_id: str) -> float:
    try:
        res = subprocess.run(
            ["git", "log", "-1", "--format=%ct", f"origin/slice/{slice_id}"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        )
        if res.stdout.strip():
            ts = int(res.stdout.strip())
            now = time.time()
            return (now - ts) / 3600.0
    except Exception:
        pass
    return 0.0


def build_board_data() -> dict[str, Any]:
    slices = load_slices()
    issues = load_issues()

    prs = get_open_prs()
    all_issues = get_open_issues()

    # Determine slice states
    ready_slices = []
    in_flight = []
    in_review_limbo = []
    stuck_slices = []

    status_counts = {
        "done": 0,
        "ready": 0,
        "retired": 0,
        "in-progress": 0,
        "in-review": 0,
        "blocked": 0,
    }

    for sid, s in slices.items():
        status = s.status
        if status in status_counts:
            status_counts[status] += 1

        # Check claimable (ready + deps met)
        if status == "ready":
            deps_met = True
            for d in s.depends_on:
                if d in slices and slices[d].status != "done":
                    deps_met = False
                    break
            if deps_met:
                issue_num = issues[sid].number if sid in issues else "?"
                ready_slices.append({"id": sid, "issue": issue_num})

        # In-flight
        if status == "in-progress":
            lock_file = REPO_ROOT / "docs" / "architecture" / "_inbox" / "locks" / f"{sid}.lock"
            if lock_file.exists():
                try:
                    content = lock_file.read_text().strip()
                    agent = content.split()[0] if content else "unknown"
                    mtime = lock_file.stat().st_mtime
                    age_h = (time.time() - mtime) / 3600.0
                    if age_h > 4:
                        stuck_slices.append({"id": sid, "reason": f"lock >4h stale ({age_h:.1f}h)"})
                    else:
                        in_flight.append({"id": sid, "agent": agent, "age_h": age_h})
                except Exception:
                    pass

        # In-review limbo
        if status == "in-review":
            age_h = get_branch_age_hours(sid)
            if age_h > 4:
                in_review_limbo.append({"id": sid, "age_h": age_h})

        # Stuck
        if status == "blocked":
            stuck_slices.append({"id": sid, "reason": "status: blocked"})

    # Non-slice issues
    followup_issues = []
    for issue in all_issues:
        labels = [label["name"] for label in issue.get("labels", [])]
        if "slice" not in labels:
            followup_issues.append({"number": issue["number"], "title": issue["title"]})

    return {
        "time": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "head": get_git_head(),
        "prs": prs,
        "ready": ready_slices,
        "in_flight": in_flight,
        "in_review_limbo": in_review_limbo,
        "stuck": stuck_slices,
        "status_counts": status_counts,
        "followups": followup_issues,
    }


def print_board(data: dict[str, Any]) -> None:
    print(f"=== Musubi Board — {data['time']} ===\n")
    print(f"v2 HEAD:  {data['head']}\n")

    prs = data["prs"]
    print(f"Open PRs:    {len(prs)}")
    for pr in prs:
        state_tag = "DRAFT" if pr.get("isDraft") else "READY"
        author = pr.get("author", {}).get("login", "?")
        title = pr.get("title", "")
        # Very simple CI check

        merge_state = pr.get("mergeStateStatus", "UNKNOWN")
        print(
            f"  #{pr['number']} [{state_tag}]     {title[:40]:<40} | {author:<10} | {merge_state}"
        )

    print(f"\nClaimable now: {len(data['ready'])}")
    for r in data["ready"]:
        print(f"  {r['id']:<34} #{r['issue']}")

    print(f"\nIn-flight (claimed, not PR'd): {len(data['in_flight'])}")
    for inf in data["in_flight"]:
        print(f"  {inf['id']}  (agent: {inf['agent']}, claimed {inf['age_h']:.1f}h ago)")

    print(f"\nIn-review limbo (>4h with no activity on branch): {len(data['in_review_limbo'])}")
    for lim in data["in_review_limbo"]:
        print(f"  {lim['id']}  (last commit {lim['age_h']:.1f}h ago)")

    print(f"\nStuck slices: {len(data['stuck'])}  (status:blocked or lock >4h stale)")
    for st in data["stuck"]:
        print(f"  {st['id']}  ({st['reason']})")

    sc = data["status_counts"]
    print("\nSlice DAG health:")
    print(
        f"  done={sc['done']} · ready={sc['ready']} · retired={sc['retired']} · in-progress={sc['in-progress']} · in-review={sc['in-review']} · blocked={sc['blocked']}"
    )

    print(f"\nOpen followup Issues (non-slice): {len(data['followups'])}")
    for fu in data["followups"]:
        print(f"  #{fu['number']}  {fu['title']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Musubi Board")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--watch", action="store_true", help="Refresh every 30s")
    args = parser.parse_args()

    while True:
        data = build_board_data()
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            # clear screen if watch
            if args.watch:
                print("\033[H\033[J", end="")
            print_board(data)

        if not args.watch:
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
