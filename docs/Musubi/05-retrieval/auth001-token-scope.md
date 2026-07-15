---
title: AUTH-001 All-Namespace Recall with Configurable Exclusions
section: 05-retrieval
type: contract
status: active
tags: [section/retrieval, status/active, type/contract]
updated: 2026-07-15
up: "[[05-retrieval/index]]"
reviewed: false
---
# AUTH-001 All-Namespace Recall with Configurable Exclusions

A token's recall authorization is not restricted to a single namespace. By default the caller's recall spans every concrete namespace in the caller's `identity_family` across the caller's authorized planes, with optional explicit narrowing. A canonical per-agent exclusion list is enforced centrally before fanout; `salesai` is excluded by default, and explicit / wildcard / cache / recent / streaming / adapter paths cannot bypass exclusions. Writes remain bound to the active canonical namespace.

## Contract

- HTTP `RetrieveQuery.namespace` is optional (`str | None = Field(default=None, ...)`). Omitting it (or sending `null`) means "recall across all authorized namespaces"; supplying it preserves the existing concrete / fanout / wildcard narrowing.
- `AuthContext.excluded_namespaces: frozenset[str]` is the single canonical source. Composed at token-validation time as the union of:
  - `Settings.default_excluded_namespaces` (default `frozenset({"salesai"})`)
  - `Settings.per_agent_excluded_namespaces[subject] | Settings.per_agent_excluded_namespaces[presence]`
  - the token claim `excluded_namespaces` (additive only; cannot subtract the mandatory default)
- `enforce_namespace_policy(context, *, targets, access="r")` is the shared READ-only enforcement seam. Called once per route, after target resolution and wildcard expansion. Drops any target whose namespace is in `context.excluded_namespaces` and then runs `resolve_namespace_scope(... access=access)` on each surviving target.
- Writes are unchanged. `resolve_namespace_scope(... access="w")` runs the existing write flow with the same `excluded_namespaces` applied as a defense-in-depth (the active namespace is already bound; the exclusion is belt-and-braces).
- Wildcard expansion is non-bypassable. The exclusion is applied AFTER `_expand_wildcard_targets`, so a wildcard pattern that would otherwise resolve to an excluded namespace returns zero targets for that pattern.
- The SDK's `retrieve()` and `retrieve_stream()` change from `namespace: str` to `namespace: str | None = None` so internal non-HTTP callers can represent the default. The LiveKit and MCP adapters are bound to a specific presence (`self.namespace` from config) and continue to pass a string; they inherit the canonical enforcement through the same HTTP path.

## Test Contract

1. `test_default_read_spans_at_least_two_non_excluded_namespaces`
2. `test_salesai_cannot_be_reenabled_by_empty_token_claim`
3. `test_salesai_cannot_be_reenabled_by_token_claim_subtract`
4. `test_salesai_cannot_be_reenabled_by_direct_target`
5. `test_salesai_cannot_be_reenabled_by_wildcard`
6. `test_salesai_cannot_be_reenabled_by_recent_lane`
7. `test_salesai_cannot_be_reenabled_by_streaming`
8. `test_salesai_cannot_be_reenabled_by_adapter_path`
9. `test_token_exclusion_adds_to_mandatory_not_subtracts`
10. `test_per_agent_settings_adds_to_mandatory`
11. `test_per_agent_settings_keyed_by_subject_or_presence_both_contribute`
12. `test_unauthorized_namespaces_remain_denied_not_silently_broadened`
13. `test_canonical_config_source_is_single_no_scattered_exceptions`
14. `test_explicit_narrowing_still_narrows`
15. `test_write_to_active_salesai_namespace_permitted_under_existing_write_scope`
