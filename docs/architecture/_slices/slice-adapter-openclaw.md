---
title: "Slice: OpenClaw adapter (RETIRED)"
slice_id: slice-adapter-openclaw
section: _slices
type: slice
status: retired
owner: unassigned
phase: "6 Lifecycle"
tags: [section/slices, status/retired, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: []
blocks: []
superseded-by: "github.com/ericmey/openclaw-musubi"
retired-by: "[[13-decisions/0022-extension-ecosystem-naming]]"
---

# Slice: OpenClaw adapter (RETIRED)

> **This slice is retired.** The OpenClaw adapter is a TypeScript browser extension. Per [[13-decisions/0022-extension-ecosystem-naming]] (ADR-0022), non-Python integrations live in sibling `<system>-musubi` repos.

**Phase:** 6 Lifecycle · **Status:** `retired` · **Owner:** `unassigned`

## Where the implementation lives now

**`github.com/ericmey/openclaw-musubi`** — future repo, created when the extension work begins. That repo will own:

- All TypeScript source for the OpenClaw browser extension.
- `manifest.json` + host-system packaging (Chrome Web Store, Mozilla Add-ons, etc.).
- Extension-local tests + docs.

It will consume Musubi's canonical API via HTTPS, using types generated from `openapi.yaml` (in this repo) via `openapi-typescript`. No direct Qdrant access. Interface discipline from [[13-decisions/0011-canonical-api-and-adapters]] preserved.

## What stays in this repo

The **spec** at [[07-interfaces/openclaw-adapter]] stays in Musubi's vault. Specs describe contracts; contracts are Musubi's responsibility regardless of where implementations run. When `openclaw-musubi` is built, its work draws from that spec.

## Retirement rationale

See [[13-decisions/0022-extension-ecosystem-naming]] §Decision for the full reasoning. Short version:

- OpenClaw is TypeScript. Per ADR-0022, non-Python integrations live in external `<system>-musubi` repos because (a) their toolchain is not Python's, (b) their release cadence is host-system-controlled (browser-extension store review), and (c) polyglot monorepo tooling pays cost on every Python PR for no offsetting benefit.
- Adjacent Python adapters (`slice-adapter-livekit`, `slice-adapter-mcp`) stay in-monorepo at `src/musubi/adapters/<name>/` because their distribution problem (installing into an external runtime) is solvable via uv workspace subpackages without moving source out.

## Work log

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-19 — operator — slice retired per ADR-0022

- [[13-decisions/0022-extension-ecosystem-naming]] accepted 2026-04-19.
- OpenClaw adapter is the flagship non-Python component that drove the ADR.
- Issue #5 closed with link to ADR.
- Slice flipped to `status: retired`; `depends-on` + `blocks` cleared; `superseded-by` set to the external repo URL.
- No in-monorepo code was written for this slice prior to retirement.
- The spec at [[07-interfaces/openclaw-adapter]] was updated in the same PR with a `spec-update:` trailer: repo name `musubi-openclaw-adapter` → `openclaw-musubi`, implementation-location paragraph added, TS-consumer installation notes added.

## Cross-slice tickets opened by this slice

- _(none)_

## PR links

- `docs(adr): 0022 extension ecosystem` — retirement PR (see ADR-0022's §Mechanics).
