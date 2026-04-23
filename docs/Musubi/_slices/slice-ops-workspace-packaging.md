---
title: "Slice: Workspace packaging — restructure into per-component wheels"
slice_id: slice-ops-workspace-packaging
section: _slices
type: slice
status: retired
owner: unassigned
phase: "8 Ops"
tags: [section/slices, status/retired, type/slice, packaging, distribution]
updated: 2026-04-23
reviewed: false
depends-on: []
blocks: []
stubbed-by: "[[13-decisions/0022-extension-ecosystem-naming]]"
---

# Slice: Workspace packaging — restructure into per-component wheels

> Restructure Musubi into a uv workspace publishing multiple wheels from the same repo. Enables external consumers (LiveKit workers, future MCP stdio, downstream Python agents) to `pip install musubi-<component>` and pull only what they need.

**Phase:** 8 Ops · **Status:** `blocked` · **Owner:** `unassigned`

> **Note: this slice is blocked on demand, not on dependencies.** Claim it when a real consumer needs thin installs. Current triggers: (1) a LiveKit worker dev wants `uv add musubi-livekit` without pulling the full server stack, (2) the SDK needs independent PyPI publishing for external agents, (3) `mcp-musubi` stdio plugin work starts and needs a shared package namespace. Until one of these materialises, the current single-wheel layout works fine. Status `blocked` keeps it off the claimable board (it would conflict with `slice-ops-integration-harness` on `pyproject.toml` + `Makefile` otherwise).

## Specs to implement

- [[13-decisions/0015-monorepo-supersedes-multi-repo]] §Decision (the monorepo policy this slice operationalises)
- [[13-decisions/0022-extension-ecosystem-naming]] §Distribution (the subpackage + wheel pattern)

## Owned paths (you MAY write here)

- `packages/` (new top-level directory containing subpackage projects)
- `.github/workflows/packaging.yml` (add per-package build+publish workflow)

> Note: restructure-time edits to `pyproject.toml`, `Makefile`, and `uv.lock` are implicitly owned by this slice at claim time (the implementing agent adds them to owned paths when flipping to `in-progress`). They're NOT pre-registered here because `slice-ops-integration-harness` also touches `Makefile` today — whichever slice is active first touches them; the other extends.

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- **All source code under `src/musubi/`** — this slice MOVES files, it doesn't modify them. `git mv`-only. Any content edit to a .py file is out of scope; open a follow-up slice.
- Test files under `tests/` — pytest wiring gets updated (pointer paths), actual test code doesn't.
- `docs/Musubi/` — specs don't change; only the packaging layout.
- `openapi.yaml`, `proto/` — unaffected.

## Depends on

- _(none — packaging restructure is a self-contained operator task)_

## Unblocks

- `slice-mcp-stdio` (future, if built): cleanly ships as `musubi-mcp-stdio` wheel without server deps.
- External PyPI publishing of `musubi-client`.
- `pip install musubi-livekit` for LiveKit workers without pulling server stack.

## Proposed target layout

```
musubi/                                     ← repo root
├── pyproject.toml                          ← workspace root (publishes nothing)
├── packages/
│   ├── musubi-server/
│   │   ├── pyproject.toml                  ← publishes "musubi" wheel
│   │   └── src/musubi/
│   │       ├── api/, planes/, retrieve/, lifecycle/, ingestion/, types/, ...
│   ├── musubi-client/
│   │   ├── pyproject.toml                  ← publishes "musubi-client" wheel
│   │   └── src/musubi/sdk/
│   ├── musubi-livekit/
│   │   ├── pyproject.toml                  ← publishes "musubi-livekit" wheel
│   │   └── src/musubi/adapters/livekit/
│   └── musubi-mcp/
│       ├── pyproject.toml                  ← publishes "musubi-mcp" wheel
│       └── src/musubi/adapters/mcp/
├── tests/                                  ← stays at repo root; each package's pyproject
│   ├── api/, planes/, retrieve/, ...       ←   declares which test paths it covers via
│   ├── sdk/                                ←   pytest config, keeping tests unified
│   ├── adapters/
│   └── ...
├── deploy/, docs/, openapi.yaml, proto/    ← unchanged
└── Makefile                                ← updated targets: `uv sync --all-packages`, etc.
```

Python's PEP 420 implicit namespace packages let multiple wheels contribute to `musubi.*` — imports like `from musubi.adapters.livekit import SlowThinker` work unchanged regardless of whether one wheel or many are installed.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] `packages/musubi-server/`, `packages/musubi-client/`, `packages/musubi-livekit/`, `packages/musubi-mcp/` all exist with working `pyproject.toml` per package.
- [ ] `uv sync --all-packages` at repo root resolves the full workspace and installs all packages editably.
- [ ] `make check` runs ruff + mypy + pytest + coverage across all packages (single invocation, same targets as today).
- [ ] `uv build --package musubi-livekit` produces a wheel with **only** LiveKit adapter code — verify by `python -m zipfile -l dist/musubi_livekit-*.whl` shows no `api/`, `planes/`, `retrieve/` entries.
- [ ] Consumer install path works: in a fresh virtualenv on a separate machine, `pip install "git+https://github.com/ericmey/musubi.git@v2#subdirectory=packages/musubi-livekit"` installs `musubi-livekit` + transitive `musubi-client` + `httpx` + `pydantic` only. Verified with `pip list` — no `qdrant-client`, `fastapi`, etc.
- [ ] GitHub Actions workflow builds + publishes the correct wheel on a per-package git tag (e.g., `musubi-livekit-v0.1.0` triggers only `musubi-livekit` publish).
- [ ] No source code changes — `git log --stat` on the feat commit shows only renames (`R100`) and `pyproject.toml` adds. Any `.py` file with `+` / `-` outside `pyproject.toml` is out of scope; land in a follow-up.
- [ ] Branch coverage unchanged on all owned code (moving files shouldn't reduce coverage).
- [ ] `make check` green on local + CI; all existing tests pass.
- [ ] Documentation: `docs/Musubi/08-deployment/packaging.md` added explaining the workspace layout, per-package publish workflow, and consumer install patterns.
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 — operator — slice stubbed per ADR-0022

- [[13-decisions/0022-extension-ecosystem-naming]] §Distribution commits Musubi to a uv-workspace layout for per-component wheel publishing.
- This slice operationalises that decision when a real consumer needs thin installs.
- Stubbed in `ready` state; not priority-queued until a concrete trigger materialises (see note at top).
- Agent picking this up: budget ~half a day for the mechanical move + ~half a day for CI publish plumbing + testing. Consult the consumer (LiveKit worker dev, etc.) to confirm their expected install command before starting, so the subpackage boundary matches demand.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
