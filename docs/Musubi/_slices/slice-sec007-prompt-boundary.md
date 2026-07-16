---
title: "Slice: SEC-007 — Secure JSON LLM Prompt Boundary"
slice_id: slice-sec007-prompt-boundary
status: done
owner: gemini-3-1-shiori
phase: "Auth"
section: _slices
type: slice
tags: [section/slices, status/done, type/slice]
updated: 2026-07-15
reviewed: true
depends-on: []
blocks: []
---

# Slice: SEC-007 — Secure JSON LLM Prompt Boundary

Tracks #559.

## What

Enforces the deterministic `SEC-007` prompt-boundary separation. Centralizes explicit LLM prompt isolation into `prompt_boundary.py`, mapping hard-coded instructions to the `system` role while strictly reserving JSON-encoded untrusted memory strings exclusively for the `user` payload role.

## Specs to implement
- [[07-interfaces/index]]

## Files
- `owns_paths`:
  - `src/musubi/llm/prompt_boundary.py`
  - `tests/llm/test_prompt_boundary_structural.py`
  - `docs/Musubi/_slices/slice-sec007-prompt-boundary.md`
- Authorized cross-slice borrowed paths:
  - `src/musubi/llm/ollama.py`
  - `src/musubi/llm/reflection_client.py`
  - `src/musubi/llm/promotion_client.py`
  - `tests/llm/test_ollama.py`
  - `tests/llm/test_reflection_client.py`
  - `tests/llm/test_promotion_client.py`

## Test Contract
1. `test_sec007_prompt_boundary_system_user_separation`
2. `test_sec007_prompt_boundary_rejects_unserializable_objects`

## Work log
- Replaced string interpolation templates with lossless explicit JSON serializations across `ollama.py`, `reflection_client.py`, and `promotion_client.py`.
- Formally bounded execution to evaluate untrusted payload content against `user` arrays, isolating instruction sets to the protected `system` role.
- Authored the core SEC-007 tests-first suite preventing payload spoofing, batch string boundary breaks, and raw string escapes.
- Updated the Ollama prompt-load and chat-payload regression tests to assert the same system/user JSON boundary used by the structural contract.
- Implements the full Issue #559 scope; closure remains pending independent review and merge. Issue #559 is an authorized cross-slice correction (LLM paths are cross-owned by synthesis, maturation, and reflection), and the borrowed-path list above records that boundary explicitly.
