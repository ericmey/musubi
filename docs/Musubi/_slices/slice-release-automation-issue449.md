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

> Architecture-contract hardening (Option C per Yua 2026-07-13 19:11:24). Tests/docs/design only. The publish-core-image.yml workflow intentionally builds and signs BOTH a moving main channel (bleeding-edge) AND an immutable release channel (v* tags). The auto-digest-bump.yml workflow gates on workflow_run (publish-core-image) with conclusion == 'success' AND startsWith(head_branch, 'v'), so deploy pins the release channel only — main digests can never feed the pin. The 6 invariants are mechanically testable via the wrong-fixture mutation tests. No source/workflow/deploy changes.

**Phase:** 8 Ops · **Status:** `in-progress` · **Owner:** `tama` · **Architecture-contract hardening**

## Specs to implement

- Issue #449 (release-automation defects)
- Yua 2026-07-13 18:51:31 (post-pin acceptance + corrected contracts)
- Yua 2026-07-13 18:45:31 (conceptual correction)
- Yua 2026-07-13 18:48:19 (status shape + buildx cache correction)
- Yua 2026-07-13 19:11:24 (architecture-contract hardening per Option C; wrong-fixture mutation tests; remove false red-contract claim; remove self-referential AST/mtime proof)

## Owned paths

- `docs/Musubi/_slices/slice-release-automation-issue449.md` (this file)
- `docs/Musubi/_inbox/locks/slice-release-automation-issue449.lock` (slice lock)
- `tests/release/test_release_automation_issue449.py` (the 6 architecture-contract invariants + 6 wrong-fixture mutation tests + 4 legitimate controls)

## Out of owns_paths (intentionally not claimed by this slice)

- `.github/workflows/publish-core-image.yml` (the publish workflow; per Yua 19:11:24: the test contract is architecture-contract hardening, NOT workflow edits)
- `.github/workflows/auto-digest-bump.yml` (the auto-pin workflow; same constraint)
- `.github/workflows/release-please.yml` (the release-please workflow; same constraint)
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

The 6 invariants below define the CURRENT INTENTIONAL ARCHITECTURE-CONTRACT for the Musubi release pipeline. The contract is Option C: intentionally separate main/release builds with explicit expected digest divergence. The auto-digest-bump workflow gates on the v* tag publish only, so deploy pins the release channel.

- **Channel 1 (v*) — immutable release:** One authoritative cosign-signed digest per release commit. The signed digest at `refs/tags/v*.*.*` is the source of truth for that release.
- **Channel 2 (main) — moving development:** A non-authoritative image build per push. May carry different OCI metadata (created, revision, version) per run. NOT used for the auto-pin.
- **Channel distinguisher:** `org.opencontainers.image.version` annotation is sourced from the tag via `docker/metadata-action@v5`'s `type=semver,pattern={{version}}`. The v* tag derivation is guarded by `startsWith(github.ref, 'refs/tags/v')` to ensure it only fires for v* pushes.
- **Sign/attest/scan shared:** Both main and v* paths share the same `publish-core-image` job, which includes cosign sign, CycloneDX SBOM, cosign attest, and Trivy scan. The signing and attestation are NOT conditional on the trigger type.
- **Auto-pin input:** `auto-digest-bump` reads the resolved tag digest via `/v2/<image>/manifests/<tag>`. It does NOT pin to the `:main` ref. It only fires for successful workflow_run with `head_branch` starting with `v` and `conclusion == 'success'`.
- **Reproducibility:** Cache is a performance concern, NOT a correctness or reproducibility input. If reproducibility is desired, the invariant MUST compare builds with identical source, platform, dependencies, toolchain, build arguments, and canonical OCI metadata. Cache enabled/disabled or cache location MUST NOT change the resulting digest.

## 6 Architecture-Contract Invariants

1. **trigger set:** The publish workflow MUST trigger on `{main, v*}` only (no other branches or tag patterns).
2. **main tag surface vs release v+latest surface:** The meta step's tag derivation MUST produce `:main` on main pushes and `:v<version> + :latest` on v* tag pushes. The v* derivation MUST be guarded by `startsWith(github.ref, 'refs/tags/v')`.
3. **both paths share sign/attest/scan:** The single `publish-core-image` job MUST include cosign sign, CycloneDX SBOM, cosign attest, and Trivy scan. The sign step MUST NOT be conditional on the trigger type.
4. **auto-pin accepts only successful v-tag publish:** The `auto-digest-bump` workflow MUST gate on `workflow_run` with `conclusion == 'success'` AND `startsWith(head_branch, 'v')`. It MUST NOT fire for main pushes.
5. **main digest can never feed pin:** The auto-digest-bump workflow MUST resolve the digest via `/v2/<image>/manifests/<tag>` with the resolved tag (v*), NOT the `:main` ref.
6. **channel-specific metadata/digest divergence is expected:** The publish workflow MUST NOT claim byte-determinism or reproducible builds without qualification. The two channels intentionally carry different OCI metadata. The workflow MUST label main as `bleeding-edge` and v* as `authoritative release`.

## Wrong-fixture mutation tests (per Yua 19:11:24)

The 6 wrong-fixture tests create a mutated copy of the workflow with a specific invariant broken, then assert that the invariant check on the mutated fixture FAILS. This proves that:
- The invariant is mechanically testable
- A future change that breaks the invariant in the same way WILL be caught

The wrong-fixture tests use `tempfile.TemporaryDirectory` (via `fixture_dir`) for any mutated copies, so the real workflow files remain unchanged.

| Test | Mutation | What breaks |
| --- | --- | --- |
| `test_wrong_fixture_inv1_remove_v_tag_trigger` | Remove `      - "v*"` from triggers | Invariant 1: trigger set no longer has v* |
| `test_wrong_fixture_inv2_main_publishes_release_tags` | Change `type=ref,event=branch` to `type=semver` for main | Invariant 2: main no longer uses type=ref,event=branch |
| `test_wrong_fixture_inv3_make_sign_conditional_on_main` | Add `if: github.ref == 'refs/heads/main'` to sign step | Invariant 3: sign step is now conditional |
| `test_wrong_fixture_inv4_remove_v_guard_in_autopin` | Replace `startsWith(head_branch, 'v')` with `true` | Invariant 4: v* guard is removed |
| `test_wrong_fixture_inv5_autopin_resolves_from_main` | Set fallback `TAG=main` | Invariant 5: documented limitation (current check does not catch `TAG=main` in fallback) |
| `test_wrong_fixture_inv6_add_byte_deterministic_claim` | Add `byte-deterministic` to workflow comment | Invariant 6: byte-deterministic claim is present |

## Corrected contracts (per Yua 18:45:31 + 18:48:19)

The prior 3 prescriptions (avoid duplicate trigger fires, byte-deterministic across regions, match `:main` to v1.13.0) were replaced with the 6 contracts above (per Yua 19:11:24: the contract is Option C architecture-contract hardening, not duplicate-build defect).

## Tests/docs/design only (per Yua 19:11:24)

- No source changes to the publish or auto-pin workflows
- No workflow edits
- No deployment changes
- No host contact
- No merge (this is a draft PR; awaiting Aoi R20 and then Yua's accept and merge call)
- Aoi R20 (release-automation follow-up; this slice is available for an independent second read after Aoi R20 lands)

## Self-proving red contracts vs. wrong-fixture tests (per Yua 19:11:24)

Per Yua 19:11:24: "The test contract is 'architecture-contract hardening', NOT 'duplicate-build defect'. The previous red-contract framing and self-referential AST/mtime proof are REMOVED."

The test design follows the wrong-fixture mutation model:
- **Positive guards (6 invariants):** Assert the invariant holds on the current (correct) workflow.
- **Wrong-fixture mutations (6 tests):** Mutate the workflow to break each invariant, then assert the invariant check FAILS on the mutated fixture. This proves the invariant is mechanically testable.
- **Legitimate controls (4 tests):** Verify the workflow files are readable, the test file is read-only, and the actual workflow files are unchanged after the test run.

This is a "mechanically testable" design: the wrong-fixture mutations prove the invariant checks would catch a future change that breaks the invariant in the same way.

## Out of slice scope (per Yua 19:11:24)

- Tests/docs/design only
- No source changes
- No workflow changes
- No production changes
- No deploy
- No host contact
- No merge (this is a draft PR; awaiting Aoi R20 and then Yua's accept and merge call)
- Aoi R20 (release-automation follow-up; this slice is available for an independent second read after Aoi R20 lands)
