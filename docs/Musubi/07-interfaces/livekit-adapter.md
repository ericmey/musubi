---
title: LiveKit Adapter
section: 07-interfaces
tags: [adapter, interfaces, livekit, section/interfaces, status/complete, type/spec, voice]
type: spec
status: complete
updated: 2026-10-01
up: "[[07-interfaces/index]]"
reviewed: false
implements: "github.com/sourceblender/musubi-livekit"
---
# LiveKit Adapter

The LiveKit voice integration lives in its own repository, [`sourceblender/musubi-livekit`](https://github.com/sourceblender/musubi-livekit). It ships the `musubi-livekit` Python package (imported as `musubi_livekit`), which embeds in a LiveKit agent worker. That repository is the reference for its design (the Slow Thinker / Fast Talker pattern, context cache, session capture, latency budget) and for its tests.

## What core contains

Only a compatibility shim: `src/musubi/adapters/livekit/`.

- `musubi.adapters.livekit` and its submodules (`adapter`, `cache`, `config`, `fast_talker`, `heuristics`, `redaction`, `slow_thinker`) re-export the same names from `musubi_livekit`: `LiveKitAdapter`, `LiveKitAdapterConfig`, `SlowThinker`, `FastTalker`, `ContextCache`, `detect_interesting_fact`, `redact_pii`, and so on.
- Without `musubi-livekit` installed, importing the old path raises `ModuleNotFoundError: Install musubi-livekit to use musubi.adapters.livekit`. A missing dependency of the plugin itself is re-raised unchanged, not hidden behind that message.
- New code should import `musubi_livekit` directly.

The shim's behaviour is covered by `tests/adapters/test_livekit_compat.py`.

## Namespace conventions

The voice integration uses the ordinary namespace rules from [[07-interfaces/canonical-api]], with `voice` as the presence:

- captures go to `<agent>/voice/episodic`, for example `alex/voice/episodic`;
- recall uses the 2-segment root `alex/voice` with `planes: ["curated", "concept", "episodic"]` (one cross-plane call, [ADR-0028](../13-decisions/0028-retrieve-2seg-namespace-crossplane.md)), or a wildcard such as `alex/*/episodic` for cross-presence context.

## Related

- [Connect](../../guide/connect.md): the list of integrations and where each one lives.
- [[07-interfaces/agent-tools]]: the canonical tool surface integrations implement.
- [[05-retrieval/fast-path]] and [[05-retrieval/deep-path]]: the retrieval budgets the voice pattern is built around.

## Test Contract

**Module under test:** `src/musubi/adapters/livekit/` (the compatibility shim only)

1. `tests/adapters/test_livekit_compat.py::test_legacy_exports_are_plugin_objects`
2. `tests/adapters/test_livekit_compat.py::test_missing_plugin_has_explicit_install_error`
3. `tests/adapters/test_livekit_compat.py::test_foreign_missing_dependency_is_not_hidden`

The adapter's own tests live in `musubi-livekit`.
