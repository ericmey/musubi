---
title: "ADR 0017: Use `watchdog` for vault filesystem watcher"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-19
deciders: [Eric, Nyla]
tags: [section/decisions, status/accepted, type/adr, watcher, dependencies]
updated: 2026-04-19
up: "[[13-decisions/index]]"
reviewed: true
---

# ADR 0017: Use `watchdog` for vault filesystem watcher

**Status:** accepted
**Date:** 2026-04-19
**Deciders:** Eric, Nyla

## Context
`slice-vault-sync` needs to observe file creates / modifies / deletes / moves in the operator's Obsidian vault directory. The implementation must be cross-platform (macOS, Linux primary; Windows acceptable if it doesn't cost us performance).

## Decision
Use the `watchdog` package (>= 4.0) as the cross-platform fs-watcher abstraction.

## Consequences
- Adds one top-level dep (~35 KB installed, no transitive bloat).
- Platform-specific backends are leveraged automatically: inotify (Linux), fsevents (macOS), ReadDirectoryChangesW (Windows).
- No cross-slice boundary cost; the dependency is strictly contained within `src/musubi/vault/watcher.py`.

## Alternatives considered
- **Polling** (`os.scandir` every N seconds) — unacceptable latency for the "operator saves a file; agent sees it within N seconds" contract.
- **Direct inotify** (`pyinotify` / `inotify_simple`) — Linux-only; this rules out the macOS-primary dev workflow.
- **Writing our own** — wheel-reinvention; introduces maintenance overhead for platform-specific C bindings.
