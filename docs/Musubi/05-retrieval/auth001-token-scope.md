---
title: AUTH-001 All-Namespace Recall with Configurable Exclusions
section: 05-retrieval
type: contract
status: active
tags: [section/retrieval, status/active, type/contract]
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
---
# AUTH-001 All-Namespace Recall with Configurable Exclusions

A token's recall authorization is not restricted to a single namespace. By default the caller's recall spans every concrete namespace in the caller's `identity_family` across the caller's authorized planes, with optional explicit narrowing. A canonical exclusion list (an operator-configured baseline plus per-agent additions) is enforced centrally before fanout; nothing is excluded unless an operator configures it, and explicit / wildcard / cache / recent / streaming / adapter paths cannot bypass configured exclusions. Writes remain bound to the active canonical namespace.

## Contract

- HTTP `RetrieveQuery.namespace` is optional (`str | None = Field(default=None, min_length=1, ...)`). Omitting it (or sending `null`) means "recall across all authorized namespaces"; supplying it preserves the existing concrete / fanout / wildcard narrowing.
- `Settings` is the single canonical source for exclusions. Composed at request time as the union of:
  - `Settings.default_excluded_namespaces` (operator-configured baseline; default empty)
  - `Settings.per_agent_excluded_namespaces[subject] | Settings.per_agent_excluded_namespaces[presence]`
- `enforce_namespace_policy(context, *, targets, settings)` is the shared READ-only enforcement seam. Called once per route, after target resolution and wildcard expansion. It FIRST runs `resolve_namespace_scope` on every concrete target, then drops any target whose namespace matches the Settings exclusions via explicit parsed segment root matching.
- Writes are unchanged. Exclusions are READ-ONLY and are not applied to writes. `resolve_namespace_scope(... access="w")` runs the existing write flow based strictly on normal authorization scopes.
- Wildcard expansion is non-bypassable. The exclusion is applied AFTER `_expand_wildcard_targets`, so a wildcard pattern that would otherwise resolve to an excluded namespace returns zero targets for that pattern.
- The SDK's `retrieve()` and `retrieve_stream()` change from `namespace: str` to `namespace: str | None = None` so internal non-HTTP callers can represent the default. The LiveKit and MCP adapters are bound to a specific presence (`self.namespace` from config) and continue to pass a string; they inherit the canonical enforcement through the same HTTP path.

## Test Contract

1. `test_default_read_spans_at_least_two_non_excluded_namespaces`
2. `test_default_exclusion_not_bypassed_by_empty_settings_override`
3. `test_default_exclusion_not_bypassed_by_settings_subtract`
4. `test_default_exclusion_not_bypassed_by_direct_target`
5. `test_default_exclusion_not_bypassed_by_wildcard`
6. `test_default_exclusion_not_bypassed_by_recent_lane`
7. `test_default_exclusion_not_bypassed_by_streaming`
8. `test_default_exclusion_not_bypassed_by_adapter_path`
9. `test_per_agent_exclusions_add_to_default_baseline`
10. `test_per_agent_settings_adds_to_default`
11. `test_per_agent_settings_keyed_by_subject_or_presence_both_contribute`
12. `test_unauthorized_namespaces_remain_denied_not_silently_broadened`
13. `test_canonical_config_source_is_single_no_scattered_exceptions`
14. `test_explicit_narrowing_still_narrows`
15. `test_write_to_excluded_namespace_permitted_under_existing_write_scope`
16. `test_unconfigured_baseline_excludes_nothing`
