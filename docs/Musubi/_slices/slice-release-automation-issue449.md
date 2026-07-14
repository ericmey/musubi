---
title: "Slice: Release Automation Architecture-Contract Hardening (Issue #449)"
slice_id: slice-release-automation-issue449
issue: 449
section: _slices
type: slice
status: in-progress
owner: tama
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice, release-automation, v1.13.0-followup]
updated: 2026-07-13
spec-update: 6ea08a9-to-following-commit per Yua 20:57:08 #7 (slice doc must match test file)
reviewed: true
depends-on: []
blocks: []
---

# Slice: Release Automation Architecture-Contract Hardening (Issue #449)

> Architecture-contract hardening (Option C per Yua 2026-07-13 19:11:24). Tests/docs/design only. The publish-core-image.yml workflow intentionally builds and signs BOTH a moving main channel (bleeding-edge) AND an immutable release channel (v* tags). The auto-digest-bump.yml workflow gates on workflow_run (publish-core-image) with conclusion == 'success' AND startsWith(head_branch, 'v'), so deploy pins the release channel only. The 6 invariants are mechanically testable via the wrong-fixture mutation tests. NEW HARDENING DEFECT: workflow_dispatch unconditionally allows an explicit tag=main, so a moving main digest CAN feed the deployment pin through manual dispatch. This is a newly confirmed hardening defect. No source/workflow/deploy changes.

**Phase:** 8 Ops · **Status:** `in-progress` · **Owner:** `tama` · **Architecture-contract hardening**

## Specs to implement

- Issue #449 (release-automation defects)
- Yua 2026-07-13 18:51:31 (post-pin acceptance + corrected contracts)
- Yua 2026-07-13 18:45:31 (conceptual correction)
- Yua 2026-07-13 18:48:19 (status shape + buildx cache correction)
- Yua 2026-07-13 19:11:24 (architecture-contract hardening per Option C; wrong-fixture mutation tests; remove false red-contract claim; remove self-referential AST/mtime proof)
- Yua 2026-07-13 19:41:33 (WITHHOLD on 6e07c56: fix Invariant 5 false-pass; add strict red for manual-dispatch-main hardening defect; fix Invariant 3 to enforce condition policy for each required supply-chain step; fix Invariant 6 to prove channel-specific configuration; fix Invariant 2 to encode latest policy explicitly; fix vacuous control 5; clarify Invariant 1 as PUSH trigger set)

## Owned paths

- `docs/Musubi/_slices/slice-release-automation-issue449.md` (this file)
- `docs/Musubi/_inbox/locks/slice-release-automation-issue449.lock` (slice lock)
- `tests/release/test_release_automation_issue449.py` (the 6 architecture-contract invariants + 1 strict red + 6 wrong-fixture mutation tests + 6 legitimate controls)

## Out of owns_paths (intentionally not claimed by this slice)

- `.github/workflows/publish-core-image.yml` (per Yua 19:11:24: no workflow edits)
- `.github/workflows/auto-digest-bump.yml` (per Yua 19:11:24: no workflow edits)
- `.github/workflows/release-please.yml` (same constraint)
- `src/musubi/**` (the production Musubi source; tests-only)
- `deploy/**` (the deployment source; tests-only)
- Production environment, host, secrets, deploy host (no host contact)

## Forbidden paths

- `.github/workflows/publish-core-image.yml` (per Yua 19:11:24: no workflow edits)
- `.github/workflows/auto-digest-bump.yml` (per Yua 19:11:24: no workflow edits)
- `.github/workflows/release-please.yml` (same constraint)
- Any live `gh workflow run` / `gh release` / `gh api` mutation
- Any `git push` to `main` (slice/branch only)
- Production secrets, `1password://` refs, vault deployment

## Architecture-contract model (Option C per Yua 19:11:24)

The 6 invariants define the CURRENT INTENTIONAL ARCHITECTURE-CONTRACT for the Musubi release pipeline. Option C: intentionally separate main/release builds with explicit expected digest divergence. The auto-digest-bump workflow gates on the v* tag publish only, so deploy pins the release channel.

- **Channel 1 (v*) — immutable release:** One authoritative cosign-signed digest per release commit.
- **Channel 2 (main) — moving development:** A non-authoritative image build per push. May carry different OCI metadata.
- **Channel distinguisher:** `org.opencontainers.image.version` annotation sourced from the tag via `docker/metadata-action@v5`'s `type=semver,pattern={{version}}` with `startsWith(github.ref, 'refs/tags/v')` guard. The main guard is `type=ref,event=branch` with `github.ref == 'refs/heads/main'` guard. Mutually exclusive.
- **Sign/attest/scan shared:** Both main and v* paths share the same `publish-core-image` job, which includes cosign sign, CycloneDX SBOM, cosign attest, and Trivy scan. None of these are conditional on the trigger type.
- **Auto-pin input:** `auto-digest-bump` reads the resolved tag digest via `/v2/<image>/manifests/<tag>`. It gates on `workflow_run` with `conclusion == 'success'` AND `startsWith(head_branch, 'v')`. The `inputs.tag` manual dispatch path does NOT have a v* guard (this is a hardening defect; see "Hardening defect" below).
- **Reproducibility:** Cache is a performance concern, NOT a correctness or reproducibility input.

## Hardening defect (NEW, per Yua 6e07c56 finding 2)

`auto-digest-bump.yml` allows `workflow_dispatch` unconditionally. If the explicit input tag is `main`, the `Resolve tag + digest` step sets `TAG` to `main` and resolves `/manifests/main`. Therefore a moving main digest CAN feed the deployment pin through manual dispatch. This is a newly confirmed hardening defect: release-only manual dispatch enforcement is missing.

The test `test_red_hardening_defect_manual_dispatch_main` reproduces this defect against the current source. Source/workflow fix is FORBIDDEN until Yua accepts this red commit.

## 6 Architecture-Contract Invariants (positive guards)

1. **push trigger set:** `{main, v*}` only (no other branches or tag patterns). workflow_dispatch is a SEPARATE operator trigger. Production helper: `assert_push_trigger_set(path)`.
2. **main tag surface vs release v+latest surface:** main → `:main`; v* → `:v<version> + :latest`. Mutually exclusive meta-step guards: `type=ref,event=branch` with `github.ref == 'refs/heads/main'` for main; `type=semver,pattern={{version}}` with `startsWith(github.ref, 'refs/tags/v')` for v*. Production helper: `assert_distinct_mutex_tags(path)`.
3. **all supply-chain steps shared:** `cosign sign`, `anchore/sbom-action@v0` (CycloneDX SBOM), `cosign attest`, `aquasecurity/trivy-action` (Trivy table + SARIF). None conditional on the trigger type. Production helper: `assert_all_required_steps_present(path)` + `assert_no_if_key_in_publish_step(name, path)`.
4. **auto-pin accepts only successful v-tag publish:** gates on `workflow_run` with `conclusion == 'success'` AND `startsWith(head_branch, 'v')`. The gate is at `jobs.bump.if`, NOT at `workflow_run.branches`. Production helper: `assert_workflow_run_v_gate(path)`.
5. **main digest can never feed pin:** the Resolve tag + digest step must have a valid bash guard `if ! [[ "$TAG" == v* ]]; then ... exit N; fi` that runs BEFORE tag is emitted and inside the matched if block. Production helper: `assert_release_only_manual_dispatch_guard(path)` (executable bash proof).
6. **channel-metadata rule / allowed-divergence contract:** the publish workflow's with.tags has mutually exclusive main ref and release semver guards. Divergence between main and v* digests is ALLOWED, not GUARANTEED. Production helper: `assert_release_channel_consumption(path)`.

## 1 Strict red (reproduces the hardening defect)

`test_red_hardening_defect_manual_dispatch_main` asserts the current source exhibits the manual-dispatch-main hardening defect. It MUST fail against the current source for the intended reason. Source/workflow fix is FORBIDDEN until Yua accepts this red commit.

## Wrong-fixture mutation tests (mechanically testable)

The wrong-fixture tests create a mutated copy of the workflow with a specific invariant broken, then assert the production helper on the mutated fixture FAILS. This proves that the invariant is mechanically testable. Per Yua 20:57:08 #3: every wrong-fixture invokes the same helper as the production guard.

| Test | Invariant | Mutation | What breaks |
| --- | --- | --- | --- |
| `test_wrong_fixture_inv1_remove_v_tag_trigger` | 1 | Remove `v*` tag trigger | Trigger set no longer has v* |
| `test_wrong_fixture_inv2_missing_main_ref_rule` | 2 | Remove `type=ref` rule | Main-ref rule missing |
| `test_wrong_fixture_inv2_missing_semver_rule` | 2 | Remove `type=semver` rule | Semver rule missing |
| `test_wrong_fixture_inv2_missing_raw_rule` | 2 | Remove `type=raw` rule | Manual-raw rule missing |
| `test_wrong_fixture_inv2_semver_enabled_on_main` | 2 | Change semver enable to gate on main | Mutex broken |
| `test_wrong_fixture_inv2_main_enabled_on_tag` | 2 | Change main enable to gate on tag | Mutex broken |
| `test_wrong_fixture_inv2_raw_enabled_outside_dispatch` | 2 | Change raw enable to gate on push | Mutex broken |
| `test_wrong_fixture_inv2_raw_allows_blank` | 2 | Remove non-blank check from raw | Mutex broken |
| `test_wrong_fixture_inv2_token_smear` | 2 | Replace semver enable with main check | Token-smear breaks mutex |
| `test_wrong_fixture_inv2_missing_prefix` | 2 | Remove `prefix=v` from semver | Semver rule malformed |
| `test_wrong_fixture_inv2_missing_value` | 2 | Remove `value=` from raw | Raw rule malformed |
| `test_wrong_fixture_inv3_add_if_key_to_step` (5) | 3 | Add `if:` to each required step | Step conditional on trigger |
| `test_wrong_fixture_inv3_missing_step` (5) | 3 | Remove each required step | Step missing |
| `test_wrong_fixture_inv3_duplicate_step` (5) | 3 | Duplicate each required step | Duplicate name |
| `test_wrong_fixture_inv3_renamed_near_match_decoy` (5) | 3 | Add decoy with `(decoy)` suffix | Renamed-near-match decoy |
| `test_wrong_fixture_inv3_unrelated_substring_decoy` (5) | 3 | Add step that is unrelated substring | Unrelated substring decoy |
| `test_wrong_fixture_inv4_remove_v_gate_in_autopin` | 4 | Remove v* head_branch check | v* gate removed |
| `test_wrong_fixture_inv5_synthetic_fixed` | 5 | (parent) Add the corrected guard | Guard present, contract satisfied |
| `test_wrong_fixture_inv5_bypass_guard` | 5 | Remove the guard from parent | Bypass |
| `test_wrong_fixture_inv5_guard_after_output` | 5 | Move guard after output emission | Wrong placement |
| `test_wrong_fixture_inv5_noop_guard` | 5 | Replace guard with always-pass | No real guard |
| `test_wrong_fixture_inv5_comment_only` | 5 | Replace guard with comment-only | Not executable |
| `test_wrong_fixture_inv5_inverted_guard` | 5 | Replace guard with inverted | Wrong direction |
| `test_wrong_fixture_inv5_guard_outside_resolve` | 5 | Remove guard from Resolve, add decoy step | Guard in wrong step |
| `test_wrong_fixture_inv5_exit_outside_if_block` | 5 | Exit outside the if block | Exit not inside |
| `test_wrong_fixture_inv6_overlap_enables` | 6 | Replace semver enable with main check | Mutex broken |

## 7 Legitimate controls (prove the tests are not vacuous)

1. `test_control_publish_workflow_readable` — the publish workflow file is readable and has the expected structure.
2. `test_control_autopin_workflow_readable` — the auto-pin workflow file is readable.
3. `test_control_explicit_v_tag_input_dispatches` — an explicit v-tag input correctly produces a v* tag pin.
4. `test_control_blank_input_falls_back_to_latest_release` — a blank input falls back to the latest release.
5. `test_control_explicit_main_rejected` — explicit 'main' input is rejected.
6. `test_control_malformed_v_prefix_rejected` — malformed v-prefix values are rejected for both explicit and latest fallback.
7. `test_control_mutation_helper_writes_to_temp_not_real` — the mutation helper writes to a temp path, NOT the real workflow files.

## Per-wrong discrimination matrix (summary)

| Wrong | Invariant | Shared production helper |
| --- | --- | --- |
| Remove v* trigger | Inv 1 | `assert_push_trigger_set` |
| Missing main-ref rule | Inv 2 | `assert_distinct_mutex_tags` |
| Missing semver rule | Inv 2 | `assert_distinct_mutex_tags` |
| Missing raw rule | Inv 2 | `assert_distinct_mutex_tags` |
| Semver enabled on main | Inv 2 | `assert_distinct_mutex_tags` |
| Main enabled on tag | Inv 2 | `assert_distinct_mutex_tags` |
| Raw enabled outside dispatch | Inv 2 | `assert_distinct_mutex_tags` |
| Raw allows blank | Inv 2 | `assert_distinct_mutex_tags` |
| Token-smear | Inv 2 | `assert_distinct_mutex_tags` |
| Missing prefix=v | Inv 2 | `assert_distinct_mutex_tags` |
| Missing value= | Inv 2 | `assert_distinct_mutex_tags` |
| Add if-key to step | Inv 3 | `assert_no_if_key_in_publish_step` |
| Missing step | Inv 3 | `assert_all_required_steps_present` |
| Duplicate step | Inv 3 | `assert_all_required_steps_present` |
| Renamed-near-match decoy | Inv 3 | `assert_all_required_steps_present` |
| Unrelated substring decoy | Inv 3 | `assert_all_required_steps_present` |
| Remove v* gate | Inv 4 | `assert_workflow_run_v_gate` |
| Bypass guard | Inv 5 | `assert_release_only_manual_dispatch_guard` |
| Guard after output | Inv 5 | `assert_release_only_manual_dispatch_guard` |
| Noop guard | Inv 5 | `assert_release_only_manual_dispatch_guard` |
| Comment-only guard | Inv 5 | `assert_release_only_manual_dispatch_guard` |
| Inverted guard | Inv 5 | `assert_release_only_manual_dispatch_guard` |
| Guard outside Resolve | Inv 5 | `assert_release_only_manual_dispatch_guard` |
| Exit outside if block | Inv 5 | `assert_release_only_manual_dispatch_guard` |
| Overlap enables | Inv 6 | `assert_release_channel_consumption` |

## Tests/docs/design only (per Yua 19:11:24)

- No source changes to the publish or auto-pin workflows
- No workflow edits
- No deployment changes
- No host contact
- No merge (this is a draft PR; awaiting Aoi R20 and then Yua's accept and merge call)
- Aoi R20 (release-automation follow-up; this slice is available for an independent second read after Aoi R20 lands)
