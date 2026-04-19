---
title: "ADR 0018: Use ruamel.yaml for format-preserving frontmatter"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-19
deciders: [Eric, Nyla]
tags: [section/decisions, status/accepted, type/adr, frontmatter, dependencies]
updated: 2026-04-19
up: "[[13-decisions/index]]"
reviewed: true
---

# ADR 0018: Use `ruamel.yaml` for format-preserving frontmatter

**Status:** accepted
**Date:** 2026-04-19
**Deciders:** Eric, Nyla

## Context
Obsidian vault files are user-edited markdown files with YAML frontmatter. The frontmatter schema spec mandates preserving the operator's formatting (including comments, key ordering, and quoting style) during a read-modify-write cycle. The existing `PyYAML` library destroys comments, arbitrarily reorders keys, and normalizes quoting styles upon dump, violating this contract.

## Decision
Use the `ruamel.yaml` package (>= 0.18) as the near-drop-in replacement for YAML parsing and serialization to explicitly support round-trip formatting preservation.

## Consequences
- Introduces a second YAML library to the dependency graph (`PyYAML` is already present). Both will stay; `PyYAML` handles basic extraction in `_tools/check.py`, whereas `ruamel.yaml` safely encapsulates the complex format-preserving parsing logic solely in `src/musubi/vault/frontmatter.py`.
- Safe round-trip modification without irritating human operators by rewriting their markdown frontmatter blocks.

## Alternatives considered
- **Hand-rolled parser** — Extremely brittle, error-prone, and limits our capacity to support nuanced YAML features (like array syntax or aliasing) consistently.
- **PR to PyYAML** — Submitting a patch to introduce round-trip parsing in `PyYAML` itself is technically out of scope and historically difficult due to fundamentally differing architectural goals for the library.
