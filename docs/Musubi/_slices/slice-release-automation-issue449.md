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

1. **push trigger set:** `{main, v*}` only (no other branches or tag patterns). workflow_dispatch is a SEPARATE operator trigger.
2. **main tag surface vs release v+latest surface:** main → `:main`; v* → `:v<version> + :latest`. Mutually exclusive meta-step guards: `type=ref,event=branch` with `github.ref == 'refs/heads/main'` for main; `type=semver,pattern={{version}}` with `startsWith(github.ref, 'refs/tags/v')` for v*.
3. **all supply-chain steps shared:** `cosign sign`, `anchore/sbom-action@v0` (CycloneDX SBOM), `cosign attest`, `aquasecurity/trivy-action` (Trivy table + SARIF). None conditional on the trigger type.
4. **auto-pin accepts only successful v-tag publish:** gates on `workflow_run` with `conclusion == 'success'` AND `startsWith(head_branch, 'v')`. NEVER main.
5. **main digest can never feed pin:** resolves via `/v2/<image>/manifests/<tag>`; NEVER `:main` ref.
6. **channel-specific metadata/digest divergence is expected:** mutually exclusive main ref and release semver guards; the contract is that divergence is ALLOWED, not GUARANTEED.

## 1 Strict red (reproduces the hardening defect)

`test_red_hardening_defect_manual_dispatch_main` asserts the current source exhibits the manual-dispatch-main hardening defect. It MUST fail against the current source for the intended reason. Source/workflow fix is FORBIDDEN until Yua accepts this red commit.

## 6 Wrong-fixture mutation tests (mechanically testable)

The wrong-fixture tests create a mutated copy of the workflow with a specific invariant broken, then assert the invariant check on the mutated fixture FAILS. This proves that the invariant is mechanically testable.

| Test | Mutation | What breaks |
| --- | --- | --- |
| `test_wrong_fixture_inv1_remove_v_tag_trigger` | Remove `      - "v*"` from triggers | Invariant 1: trigger set no longer has v* |
| `test_wrong_fixture_inv2_main_publishes_release_tags` | Change `type=ref,event=branch` to `type=semver` for main | Invariant 2: main no longer uses type=ref,event=branch |
| `test_wrong_fixture_inv3_gate_sign_on_main` | Add `if: github.ref == 'refs/heads/main'` to sign step | Invariant 3: sign step is now conditional |
| `test_wrong_fixture_inv4_remove_v_guard_in_autopin` | Replace `startsWith(head_branch, 'v')` with `true` | Invariant 4: v* guard is removed |
| `test_wrong_fixture_inv5_add_inputs_tag_v_guard` | Add v* guard to inputs.tag | This is a HYPOTHETICAL FIX; the guard correctly catches tag=main |
| `test_wrong_fixture_inv6_remove_channel_distinction` | Remove the `github.ref == 'refs/heads/main'` guard from main | Invariant 6: main and v* are no longer mutually exclusive |

## 6 Legitimate controls (prove the tests are not vacuous)

1. `test_control_publish_workflow_readable` — the publish workflow file is readable and has the expected structure.
2. `test_control_autopin_workflow_readable` — the auto-pin workflow file is readable.
3. `test_control_explicit_v_tag_input_dispatches` — an explicit v-tag manual dispatch correctly produces a v* tag pin (legitimate control: the v* path must work).
4. `test_control_blank_input_falls_back_to_latest_release` — a blank input falls back to the latest release (legitimate control: the fallback must work).
5. `test_control_mutation_helper_writes_to_temp_not_real` — the mutation helper writes to a temp path, NOT the real workflow files. The real source hashes are unchanged after the test run.
6. `test_control_test_file_is_read_only` — this test file is read-only.

## Per-wrong discrimination matrix (summary)

| Wrong | Invariant | What mutation does | What the test catches |
| --- | --- | --- | --- |
| Remove v* trigger | Inv 1 | v* tag trigger removed | The trigger set no longer has v* |
| Main publishes release tags | Inv 2 | main type=semver | Main no longer uses type=ref,event=branch |
| Gate sign on main | Inv 3 | sign step conditional on main | Sign step is now conditional |
| Remove v* guard in autopin | Inv 4 | v* guard replaced with `true` | The guard is removed |
| Add inputs.tag v* guard | Inv 5 (hypothetical fix) | Add v* guard | The fixed workflow passes Inv 5 |
| Remove main guard | Inv 6 | Remove the `refs/heads/main` guard | Main and v* are no longer mutually exclusive |

## Tests/docs/design only (per Yua 19:11:24)

- No source changes to the publish or auto-pin workflows
- No workflow edits
- No deployment changes
- No host contact
- No merge (this is a draft PR; awaiting Aoi R20 and then Yua's accept and merge call)
- Aoi R20 (release-automation follow-up; this slice is available for an independent second read after Aoi R20 lands)
