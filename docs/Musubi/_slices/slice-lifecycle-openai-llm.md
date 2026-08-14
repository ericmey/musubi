---
title: "Slice: lifecycle OpenAI-compatible LLM backend"
slice_id: slice-lifecycle-openai-llm
section: _slices
type: slice
status: in-review
owner: claude-opus-5
phase: "6 Lifecycle"
tags: [section/slices, status/in-review, type/slice, lifecycle, llm, deployment]
updated: 2026-08-14
reviewed: false
issue: 694
depends-on: []
blocks: []
---

# Slice: lifecycle OpenAI-compatible LLM backend

Make lifecycle inference a deployment-selected transport while preserving the
native Ollama default, and isolate enrichment failures per batch as the
maturation spec already requires.

## Scope

`owns_paths`:

- `src/musubi/settings.py`
- `src/musubi/llm/ollama.py`
- `src/musubi/lifecycle/maturation.py`
- `tests/test_config.py`
- `tests/llm/test_openai_compat.py`
- `tests/lifecycle/test_maturation.py`
- `tests/ops/test_1password_connect_deploy.py`
- `docs/Musubi/06-ingestion/maturation.md`
- `docs/Musubi/06-ingestion/concept-synthesis.md`
- `docs/Musubi/13-decisions/0043-lifecycle-llm-openai-compatible-endpoint.md`
- `deploy/ansible/templates/env.production.j2`
- `deploy/ansible/templates/secrets.tpl.j2`
- `deploy/ansible/templates/docker-compose.yml.j2`
- `deploy/docker/.env.production.example`
- `docs/Musubi/_slices/slice-lifecycle-openai-llm.md`

`forbidden_paths`:

- `src/musubi/api/**`
- `openapi.yaml`
- `proto/**`

## Specs to implement

- [[06-ingestion/maturation]]
- [[06-ingestion/concept-synthesis]]
- [[13-decisions/0043-lifecycle-llm-openai-compatible-endpoint]]

## Test Contract

1. `test_lifecycle_llm_defaults_preserve_ollama_behavior`
2. `test_lifecycle_llm_openai_override_roundtrip`
3. `test_lifecycle_llm_api_rejects_unknown_value`
4. `test_lifecycle_llm_openai_requires_explicit_base_url`
5. `test_lifecycle_llm_openai_requires_explicit_nonblank_model`
6. `test_request_shape_bearer_and_strict_json_schema`
7. `test_base_url_with_trailing_v1_is_not_doubled`
8. `test_batched_call_isolates_failed_batches`
9. `test_batched_call_all_batches_failing_returns_empty_not_none`
10. `test_lifecycle_llm_key_uses_runtime_secret_boundary`

## Work log

- 2026-08-14, claude-opus-5: Implemented the second wire, settings selection,
  per-batch isolation, deployment templates, ADR, and initial regressions in
  PR #693. Full initial suite reported 2,662 passing; CI exposed typing and ADR
  frontmatter closure gaps during review.
- 2026-08-14, codex-yua: Acceptance review added fail-loud conditional config,
  moved bearer material to the existing 1Password runtime boundary, removed
  house-specific current-state claims from checked-in examples/spec prose,
  closed the maturation Test Contract, and created issue #694 as the actual
  cross-contract ownership record.
