---
title: "Slice: AUTH-001 all-namespace recall with configurable exclusions"
slice_id: slice-auth001-token-scope
issue: 523
section: _slices
type: slice
status: in-review
owner: cowork-tama
phase: "Auth"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---

# Slice: AUTH-001 all-namespace recall with configurable exclusions

## What

Closes the agent-token recall scope gap (Issue #523). A token's
recall authorization is not restricted to a single namespace by
default. An agent may read across all registered namespaces, with
optional explicit narrowing. A canonical per-agent exclusion list is
enforced centrally before fanout; ``salesai`` is excluded by
default, and explicit / wildcard / cache / recent / streaming /
adapter paths cannot bypass exclusions. Writes remain bound to the
active canonical namespace.

This slice is bounded to:
- ``src/musubi/auth/scopes.py`` — add ``enforce_namespace_policy`` (the
  shared READ-only enforcement seam) and
  ``enumerate_authorized_namespaces`` (the default-to-all target
  resolution).
- ``src/musubi/settings.py`` — add
  ``default_excluded_namespaces`` (default ``frozenset({"salesai"})``)
  and ``per_agent_excluded_namespaces`` (default ``{}``; identity
  precedence: both subject and presence contribute by union).
- ``src/musubi/api/routers/retrieve.py``,
  ``src/musubi/api/routers/context.py``,
  ``src/musubi/api/routers/writes_retrieve_stream.py`` — make
  ``namespace`` optional in the body model; call
  ``enforce_namespace_policy`` once after target resolution.
- ``src/musubi/sdk/client.py``,
  ``src/musubi/sdk/async_client.py`` — change
  ``namespace: str`` to ``namespace: str | None = None`` in
  ``retrieve()`` and ``retrieve_stream()`` so internal non-HTTP
  callers can express the default.
- ``tests/api/test_auth001_token_scope.py`` — 16-test contract (14
  RED discriminating + 2 GREEN preservation guards).

## Why

Today, every recall path enforces a per-namespace scope check
(``resolve_namespace_scope``) and a token with ``scope="**:rw"``
can read across every namespace the identity family federates over
in the same identity, with no central mechanism to keep work
memories (``salesai``) from contaminating home / personal agent
context. The exclusion policy is split between a default
namespace narrowing in the call body and the per-namespace scope
loop in the router, with no canonical source for "this agent may
not recall X." Per the issue, the per-agent exclusion list must be
enforced centrally before fanout for every entry point.

## Contract

1. **Default recall spans all authorized namespaces.** When the
   caller does not narrow (HTTP body ``namespace`` is omitted or
   ``null``; SDK ``retrieve(namespace=None)``), the orchestrator
   enumerates every concrete namespace in the caller's
   ``identity_family`` across the caller's authorized planes,
   then runs the exclusion + scope check. The legacy narrowing
   semantics (3-segment concrete, 2-segment fanout, wildcard) are
   preserved when ``namespace`` is supplied.

2. **Salesai is a mandatory baseline exclusion.** The default
   ``excluded_namespaces`` is ``frozenset({"salesai"})``, set in
   ``Settings.default_excluded_namespaces``. Settings
   ADD to the mandatory set; they cannot
   subtract ``salesai``. The composition:

   ```
   mandatory  = frozenset(Settings.default_excluded_namespaces)
              # default: {"salesai"}
   per_agent = frozenset(Settings.per_agent_excluded_namespaces
                          .get(subject, ()))
              | frozenset(Settings.per_agent_excluded_namespaces
                          .get(presence, ()))
              # both contribute, no precedence
              # additive only; cannot subtract mandatory
   excluded  = mandatory | per_agent
   ```

   The composed exclusions are derived directly from ``Settings``
   and are the single canonical source the enforcement seam reads.

3. **Per-agent settings are one canonical ``Settings`` mapping**
   (``Settings.per_agent_excluded_namespaces: dict[str, tuple[str, ...]]``)
   keyed by stable authenticated subject OR presence. Both
   contribute via union (identity precedence documented as
   "both contribute, no precedence"). No separate config file,
   no registry subsystem, no new module — the ``Settings`` model
   is the single source.

4. **Exclusion enforcement is READ-ONLY.** ``enforce_namespace_policy``
   is called on the read path only. Writes go through the existing
   ``resolve_namespace_scope(... access="w")`` flow unchanged.
   A write to ``<salesai>/<plane>`` is permitted (under the
   existing write scope). This matches Eric's product decision:
   "an agent may need to write work memories while excluding them
   from later general recall."

5. **No scattered route exceptions.** Every read entry point (HTTP
   ``/v1/retrieve``, ``/v1/context``, ``/v1/retrieve/stream``, SDK
   ``retrieve()`` / ``retrieve_stream()``, LiveKit adapter, MCP
   tools, auth middleware) calls ``enforce_namespace_policy``
   exactly once, after target resolution and wildcard expansion.
   The function is the single source of truth for the exclusion
   policy. Hardcoding route-specific exclusions is a code-review
   must-fix.

6. **Wildcard expansion is non-bypassable.** The exclusion is
   applied AFTER ``_expand_wildcard_targets`` so a wildcard
   pattern that would otherwise resolve to an excluded namespace
   returns zero targets for that pattern (not a bypass).

7. **Internal non-HTTP callers can represent ``namespace=None``.**
   The SDK's ``retrieve()`` and ``retrieve_stream()`` signatures
   change from ``namespace: str`` to ``namespace: str | None = None``
   so callers can pass ``None`` to express the default. The
   LiveKit and MCP adapters are bound to a specific presence
   (``self.namespace`` from config) and continue to pass a string;
   they inherit the canonical enforcement through the same HTTP
   path.

## API change (additive, backward compatible)

The wire shape of ``RetrieveQuery`` (and ``ContextQuery``,
``RetrieveStreamQuery``) changes:

- **Before:** ``namespace: str`` (required, non-empty).
- **After:** ``namespace: str | None = Field(default=None, ...)``.

A client that continues to send ``namespace`` keeps the existing
behavior bit-for-bit. A client that omits ``namespace`` (or
sends ``null``) gets the default-to-all-authorized expansion.
There is no second boolean (``expand_to_all`` was explicitly
withheld per the design ACK); the only signal is whether
``namespace`` is supplied. Per Yua's WITHHOLD: "Do not add a
second boolean whose default False preserves the old narrow
behavior, and do not allow expand_to_all plus a supplied
namespace to ambiguously ignore the namespace."

The SDK's ``retrieve()`` and ``retrieve_stream()`` change from
``namespace: str`` to ``namespace: str | None = None``. The body
dict forwarded to the HTTP endpoint includes the value as-is
(``"namespace": None`` is a valid JSON null).

## Specs to implement

- [[05-retrieval/auth001-token-scope]] (to be authored in the same
  PR; references the open Issue #523 and the bounded scope above).

## Acceptance

The first contract is bounded to fifteen tests in
``tests/api/test_auth001_token_scope.py``: sixteen RED
discriminating tests, two GREEN preservation guards. Test
function names transcribe the Test Contract bullets verbatim per
the AGENTS.md Test Contract Closure Rule.

### Test Contract (16 bullets, state 1 = passing at handoff)

1. `test_default_read_spans_at_least_two_non_excluded_namespaces` — RED
2. `test_salesai_cannot_be_reenabled_by_empty_settings_override` — RED
3. `test_salesai_cannot_be_reenabled_by_settings_subtract` — RED
4. `test_salesai_cannot_be_reenabled_by_direct_target` — RED
5. `test_salesai_cannot_be_reenabled_by_wildcard` — RED
6. `test_salesai_cannot_be_reenabled_by_recent_lane` — RED
7. `test_salesai_cannot_be_reenabled_by_streaming` — RED
8. `test_salesai_cannot_be_reenabled_by_adapter_path` — RED
9. `test_settings_exclusions_add_to_mandatory_not_subtract` — RED
10. `test_per_agent_settings_adds_to_mandatory` — RED
11. `test_per_agent_settings_keyed_by_subject_or_presence_both_contribute` — RED
12. `test_unauthorized_namespaces_remain_denied_not_silently_broadened` — RED
13. `test_canonical_config_source_is_single_no_scattered_exceptions` — RED
14. `test_explicit_narrowing_still_narrows` — GREEN guard
15. `test_write_to_active_salesai_namespace_permitted_under_existing_write_scope` — GREEN guard

At handoff, every bullet above is in state 1 (passing test whose
name transcribes the bullet text verbatim) per the AGENTS.md
Closure Rule. The first commit on the branch shows the RED /
guard evidence: the sixteen RED tests fail under current
behaviour, the two GREEN guards pass. The seam impl commit
flips the RED to green.

## Issue #523 assignment path (work-log audit trail)

The GitHub Issue #523 is left **unassigned** in this slice. The
agent-bridge assignee path failed with the GraphQL error
``Could not resolve to a user or bot with the login 'minimax-m3'``
on ``gh issue edit 523 --add-assignee minimax-m3``. The owner
frontmatter on this slice is ``cowork-tama`` per the design
ACK; the GitHub-side assignee is an org-admin / repo-owner
action that is out of scope for the slice work. The Issue label
is flipped to ``status:in-progress`` so the work is visibly
claimed, and the slice frontmatter is the authoritative intent
record per the AGENTS.md Dual-update rule.

This is logged in the Work log below. The Issue assignment is a
follow-up action, not a block on the slice.

## Work log

### 2026-07-15 — cowork-tama (design review, API shape ACK, slice doc + test contract)

- **Drift / inspect.** Started at worktree HEAD ``86e1dc4`` (origin/main);
  new worktree ``/tmp/auth001/wt`` on branch
  ``slice/auth001-token-scope``. No drift on this lane.
- **Inspect (auth seams).** ``AuthContext`` in
  ``src/musubi/auth/tokens.py`` has no exclusion field today.
  ``resolve_namespace_scope`` in ``src/musubi/auth/scopes.py`` is
  called per-target in a loop from every router (HTTP
  ``/v1/retrieve``, ``/v1/context``, ``/v1/retrieve/stream``, auth
  middleware). Target resolution: ``_resolve_targets`` +
  ``_expand_wildcard_targets`` produce ``(namespace, plane)`` tuples.
- **Inspect (SDK / adapter / voice).** SDK ``retrieve()`` and
  ``retrieve_stream()`` have ``namespace: str`` (required). LiveKit
  and MCP adapters pass a bound ``namespace`` string. The SDK is
  the only internal non-HTTP caller that needs the
  ``namespace=None`` representation; the LiveKit / MCP adapters
  are bound to a specific presence and inherit the canonical
  enforcement through the HTTP path.
- **Issue claim path.** Issue #523 label flipped to
  ``status:in-progress``; assignee add failed with the same
  ``minimax-m3`` GraphQL error as #512; logged in the slice doc
  as a non-blocking open-defect.
- **Design review (three binding corrections + final API shape).**
  Yua's three binding corrections all applied: READS ONLY;
  salesai mandatory, additive composition
  (mandatory | per_agent, no subtraction); default
  spans all authorized namespaces. Final API shape (after Yua's
  WITHHOLD on the ``expand_to_all`` flag): make the formerly
  required read field ``namespace`` optional; the only signal is
  whether ``namespace`` is supplied. Backward compatible for
  clients that continue to send it; directly expresses the
  product decision. No second boolean.
- **Internal non-HTTP caller.** Only the SDK's ``retrieve()`` and
  ``retrieve_stream()`` need the signature change to
  ``namespace: str | None = None``. All other internal callers
  are bound to a specific presence and continue to pass a
  string.
- **Test contract.** 16 tests, bounded per AGENTS.md Closure
  Rule: 13 RED discriminating + 2 GREEN preservation guards.
  Every proof point Yua named is covered.

## Out of scope (NOT closed by this slice)

- Write exclusion. Writes remain bound to the active namespace;
  the seam is read-only.
- Per-agent exclusion registry as a separate subsystem. The
  ``Settings`` mapping IS the registry; one canonical source, no
  new files.
- Backfill of historical call sites that use the old API. The
  contract change is additive (``namespace`` becomes optional);
  existing clients that send ``namespace`` are unaffected.
- Settings may NOT subtract from mandatory ``salesai`` (per
  design ACK). Settings can only ADD exclusions.
- The default-to-all enumeration is scoped to the caller's
  ``identity_family`` (a tenant-scoped fanout). It does NOT
  cross tenants; that is a separate ``family_of`` boundary
  enforced at the hybrid search layer.

## Out-of-band continuation

- LiveKit adapter: when the adapter is bound to a specific
  presence and the user wants to recall "all of my presences,"
  the API call should pass ``namespace=None`` (an SDK-level
  change for the LiveKit integration is a follow-up; the
  adapter's bound ``self.namespace`` is not a user-supplied
  parameter today).
- The ``enforce_namespace_policy`` seam is a per-target filter.
  A future slice may add a per-target ``allowed_namespaces`` field
  to the ``AuthContext`` for finer-grained per-presence recall
  restrictions (e.g., a per-presence quota). The seam is
  designed to accept this without architectural change.
- The default-to-all enumeration scans the caller's
  ``identity_family`` across all collections. A tenant with very
  large numbers of namespaces may want a paginated enumeration;
  that is a follow-up (the seam is a single function, the
  pagination surface is orthogonal).
