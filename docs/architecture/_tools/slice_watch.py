#!/usr/bin/env python3
"""
slice_watch.py — diff slice states against a cached snapshot, notify on transitions.

Design: polling, not file-watching. Simpler, more reliable, survives crashes.

Usage:
  python3 _tools/slice_watch.py                 # one-shot: print transitions to stdout
  python3 _tools/slice_watch.py --loop 60       # poll every 60s (foreground)
  MUSUBI_NOTIFY=pushover \\
    MUSUBI_PUSHOVER_TOKEN=... MUSUBI_PUSHOVER_USER=... \\
    python3 _tools/slice_watch.py --loop 60     # poll + push

Backends (selected by MUSUBI_NOTIFY env var):
  stdout    (default)   — print transitions
  desktop              — macOS `osascript`, Linux `notify-send`
  pushover             — requires MUSUBI_PUSHOVER_TOKEN, MUSUBI_PUSHOVER_USER
  slack                — requires MUSUBI_SLACK_WEBHOOK
  discord              — requires MUSUBI_DISCORD_WEBHOOK

Zero dependencies beyond stdlib. State cache lives at _tools/.slice_state.json
(gitignored via .gitignore).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
STATE_FILE = VAULT / "_tools" / ".slice_state.json"
FM_START = re.compile(r"\A---\s*\n")
FM_END = re.compile(r"\n---\s*\n")

# ---------- Read slice state ---------------------------------------------------


def read_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not FM_START.match(text):
        return {}
    m = FM_END.search(text, 4)
    if not m:
        return {}
    block = text[4 : m.start()]
    out: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.strip().startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def current_state() -> dict[str, dict]:
    sroot = VAULT / "_slices"
    state: dict[str, dict] = {}
    for p in sorted(sroot.glob("slice-*.md")):
        fm = read_frontmatter(p)
        if fm.get("type") != "slice":
            continue
        sid = fm.get("slice_id") or p.stem
        state[sid] = {
            "status": fm.get("status", "ready"),
            "owner": fm.get("owner", "unassigned"),
            "phase": fm.get("phase", ""),
            "updated": fm.get("updated", ""),
        }
    return state


def load_previous() -> dict[str, dict]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------- Diff ---------------------------------------------------------------


def diff(prev: dict[str, dict], cur: dict[str, dict]) -> list[str]:
    events: list[str] = []
    seen: set[str] = set()
    for sid, now in cur.items():
        seen.add(sid)
        was = prev.get(sid)
        if was is None:
            events.append(f"🆕 {sid} added (status={now['status']}, owner={now['owner']})")
        else:
            if was.get("status") != now["status"]:
                events.append(
                    f"➡️  {sid}: {was.get('status')} → {now['status']} (owner={now['owner']})"
                )
            if was.get("owner") != now["owner"] and was.get("status") == now["status"]:
                events.append(f"👤 {sid}: owner {was.get('owner')} → {now['owner']}")
    for sid in set(prev) - seen:
        events.append(f"🗑️  {sid} removed")
    return events


# ---------- Backends -----------------------------------------------------------


def notify_stdout(events: list[str]) -> None:
    for e in events:
        print(e)


def notify_desktop(events: list[str]) -> None:
    summary = f"Musubi: {len(events)} slice event(s)"
    body = "\n".join(events)[:500]
    if sys.platform == "darwin":
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{summary}"'],
            check=False,
        )
    elif sys.platform.startswith("linux"):
        subprocess.run(["notify-send", summary, body], check=False)
    else:
        notify_stdout(events)


def notify_pushover(events: list[str]) -> None:
    token = os.environ.get("MUSUBI_PUSHOVER_TOKEN")
    user = os.environ.get("MUSUBI_PUSHOVER_USER")
    if not (token and user):
        print("pushover: missing MUSUBI_PUSHOVER_TOKEN or MUSUBI_PUSHOVER_USER", file=sys.stderr)
        return
    body = urllib.parse.urlencode(
        {
            "token": token,
            "user": user,
            "title": f"Musubi: {len(events)} slice event(s)",
            "message": "\n".join(events)[:1024],
        }
    ).encode()
    urllib.request.urlopen("https://api.pushover.net/1/messages.json", data=body, timeout=5).read()


def notify_slack(events: list[str]) -> None:
    url = os.environ.get("MUSUBI_SLACK_WEBHOOK")
    if not url:
        print("slack: missing MUSUBI_SLACK_WEBHOOK", file=sys.stderr)
        return
    data = json.dumps(
        {"text": "*Musubi slice update*\n" + "\n".join(f"• {e}" for e in events)}
    ).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def notify_discord(events: list[str]) -> None:
    url = os.environ.get("MUSUBI_DISCORD_WEBHOOK")
    if not url:
        print("discord: missing MUSUBI_DISCORD_WEBHOOK", file=sys.stderr)
        return
    data = json.dumps({"content": "**Musubi slice update**\n" + "\n".join(events)}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


BACKENDS = {
    "stdout": notify_stdout,
    "desktop": notify_desktop,
    "pushover": notify_pushover,
    "slack": notify_slack,
    "discord": notify_discord,
}

# ---------- Tick ---------------------------------------------------------------


def tick() -> int:
    prev = load_previous()
    cur = current_state()
    events = diff(prev, cur)
    if events:
        backend = os.environ.get("MUSUBI_NOTIFY", "stdout")
        BACKENDS.get(backend, notify_stdout)(events)
    save_state(cur)
    return len(events)


# ---------- Main ---------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="poll interval in seconds (0 = one-shot)")
    args = ap.parse_args()

    if args.loop <= 0:
        n = tick()
        return 0
    while True:
        try:
            tick()
        except Exception as e:
            print(f"tick error: {e}", file=sys.stderr)
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
