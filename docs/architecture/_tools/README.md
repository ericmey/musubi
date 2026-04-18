---
title: Vault Tools
section: _tools
type: index
status: complete
tags: [type/index, status/complete, tooling]
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: true
---

# `_tools/` — validation and notifications

Standalone Python scripts that operate on the vault. Zero dependencies beyond
stdlib (PyYAML is used if present, falls back to a mini parser otherwise).

## Scripts

### `check.py` — vault + slice + spec validator

Validates frontmatter integrity, slice DAG consistency, owned-path conflicts,
spec hygiene, and lock-state coherence.

```bash
# One-shot full check (exit nonzero on any error):
python3 _tools/check.py all

# Focused runs:
python3 _tools/check.py vault    # frontmatter fields, H1/title match, tag namespaces
python3 _tools/check.py slices   # DAG, owns_paths uniqueness, locks vs status
python3 _tools/check.py specs    # Test Contract, implements:, spec status hygiene

# Machine-readable output for CI:
python3 _tools/check.py all --json | jq .
```

What it catches:

| Check | Severity | Rule |
|---|---|---|
| Missing frontmatter field | error | `title`, `section`, `type`, `status`, `tags`, `updated` required |
| Section field mismatch | warning | `section:` must equal parent folder |
| H1 ≠ title | warning | the visible H1 should match the frontmatter `title` |
| Missing tag namespace | warning | every note needs `status/*` and `type/*` tags |
| `depends-on` target missing | error | slice references a slice that doesn't exist |
| Back-edge missing | warning | `A depends-on B` but `B` does not `blocks A` |
| `owns_paths` conflict | error | two slices claim the same path |
| Invalid slice status | error | not in `{ready,in-progress,in-review,blocked,done}` |
| No lock for in-progress slice | warning | status says working but no `.lock` file present |
| Orphan lock | warning | `.lock` file exists but slice isn't in-progress |
| Stale lock | warning | lock mtime > 4h ago without heartbeat |
| Complete spec with no Test Contract | warning | spec flagged complete but body has no `Test Contract` section |
| Complete spec without `implements:` | warning | spec flagged complete but no code-path pointer |

### `slice_watch.py` — slice-state transition notifier

Polls the `_slices/` folder, diffs against a cached snapshot, and emits one
event per transition.

```bash
# One-shot — print to stdout:
python3 _tools/slice_watch.py

# Foreground poller (handy in a tmux session):
python3 _tools/slice_watch.py --loop 60
```

Backends are pluggable via environment variable:

| `MUSUBI_NOTIFY` | Additional env needed | Notes |
|---|---|---|
| `stdout` (default) | — | Prints events; good for CI / cron with email wrapper. |
| `desktop` | — | macOS `osascript`; Linux `notify-send`. |
| `pushover` | `MUSUBI_PUSHOVER_TOKEN`, `MUSUBI_PUSHOVER_USER` | Per-device pushes. Cheapest for a one-person shop. |
| `slack` | `MUSUBI_SLACK_WEBHOOK` | Incoming webhook URL. |
| `discord` | `MUSUBI_DISCORD_WEBHOOK` | Incoming webhook URL. |

State cache: `_tools/.slice_state.json` (gitignored).

### `.gitignore`

Excludes the state cache and compiled Python.

## Running automatically

### macOS (launchd, user-level)

```xml
<!-- ~/Library/LaunchAgents/com.musubi.slicewatch.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>             <string>com.musubi.slicewatch</string>
    <key>ProgramArguments</key>
    <array>
      <string>/usr/bin/python3</string>
      <string>/Users/eric/Vaults/musubi/_tools/slice_watch.py</string>
      <string>--loop</string><string>60</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
      <key>MUSUBI_NOTIFY</key>          <string>desktop</string>
    </dict>
    <key>RunAtLoad</key>         <true/>
    <key>KeepAlive</key>         <true/>
    <key>StandardErrorPath</key> <string>/Users/eric/.musubi/slicewatch.err</string>
    <key>StandardOutPath</key>   <string>/Users/eric/.musubi/slicewatch.out</string>
  </dict>
</plist>
```

Load: `launchctl load ~/Library/LaunchAgents/com.musubi.slicewatch.plist`

### Linux (systemd user unit)

```ini
# ~/.config/systemd/user/musubi-slicewatch.service
[Unit]
Description=Musubi slice watcher

[Service]
ExecStart=/usr/bin/python3 %h/Vaults/musubi/_tools/slice_watch.py --loop 60
Environment=MUSUBI_NOTIFY=desktop
Restart=always

[Install]
WantedBy=default.target
```

Enable: `systemctl --user enable --now musubi-slicewatch`

### Every commit (git hook)

```bash
# .git/hooks/pre-push
#!/usr/bin/env bash
python3 _tools/check.py all || { echo "vault lint failed"; exit 1; }
```

## Makefile fragment

Drop this into the musubi code repo's `Makefile` once the repo exists. Paths
assume the vault lives at `docs/architecture/` inside the code repo.

```makefile
# ---- Vault gates ----------------------------------------------------------
VAULT ?= docs/architecture

.PHONY: vault-check spec-check slice-check agent-check slice-watch

vault-check:
	python3 $(VAULT)/_tools/check.py vault

spec-check:
	python3 $(VAULT)/_tools/check.py specs

slice-check:
	python3 $(VAULT)/_tools/check.py slices

agent-check: vault-check spec-check slice-check
	@echo "All vault gates green."

slice-watch:
	python3 $(VAULT)/_tools/slice_watch.py --loop 60

# ---- CI entry -------------------------------------------------------------

.PHONY: check
check: fmt-check lint typecheck test agent-check
```

Wire `agent-check` into CI and you'll catch bad slice state, broken specs, and
dangling locks on every PR.

## Related

- [[_slices/index|Slice Registry]] — what this tool validates.
- [[00-index/agent-handoff|Agent Handoff Protocol]] — the lifecycle these checks enforce.
- [[00-index/definition-of-done]] — merge gate checklist.
