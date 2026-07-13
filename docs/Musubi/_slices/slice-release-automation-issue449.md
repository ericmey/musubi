---
title: "Slice: Release Automation Guard (Issue #449)"
slice_id: slice-release-automation-issue449
issue: 449
section: _slices
type: slice
status: in-progress
owner: tama
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice, release-automation, v1.13.0-followup]
updated: 2026-07-13
reviewed: false
depends-on: []
blocks: []
---

# Slice: Release Automation Guard (Issue #449)

> Tests/docs/design only per Yua 2026-07-13 18:51:31. Encodes self-proving red contracts for the four corrected release-automation requirements: (1) one authoritative signed tag publish per release tag, (2) auto-pin consumes only the signed immutable tag digest, (3) main and release channels are explicitly distinct in generated metadata/docs, (4) a design-only reproducibility boundary that treats cache as performance, not input. No source/workflow/deploy changes.

**Phase:** 8 Ops · **Status:** `in-progress` · **Owner:** `tama` · **Tests/docs/design only**

## Specs to implement

- Issue #449 (release-automation defects)
- Yua 2026-07-13 18:51:31 (post-pin acceptance + corrected contracts)
- Yua 2026-07-13 18:45:31 (conceptual correction)
- Yua 2026-07-13 18:48:19 (status shape + buildx cache correction)
- Yua 2026-07-13 18:27:42 (release integrity watch)
- Yua 2026-07-13 18:38:09 (release-automation defect filing)

## Owned paths

- `docs/Musubi/_slices/slice-release-automation-issue449.md` (this file)
- `docs/Musubi/_inbox/locks/slice-release-automation-issue449.lock` (slice lock)
- `tests/release/test_release_automation_issue449.py` (the 4 self-proving red contracts + 3 discrimination tests + 4 legitimate controls)

## Out of owns_paths (intentionally not claimed by this slice)

- `.github/workflows/publish-core-image.yml` (the publish workflow; per Yua 18:51:31: "Do not touch the publish workflow"; the tests operate on checked-in workflow/config fixtures or parsed workflow structure, not call live GitHub Actions or mutate releases)
- `.github/workflows/release-please.yml` (the release-please workflow; same constraint)
- `src/musubi/**` (the production Musubi source; tests-only)
- `deploy/**` (the deployment source; tests-only)
- Production environment, host, secrets, deploy host (no host contact)

## Forbidden paths

- `.github/workflows/publish-core-image.yml` (per Yua 18:51:31: "Do not touch the publish workflow or production/deploy source")
- `.github/workflows/release-please.yml` (same constraint)
- Any live `gh workflow run` / `gh release` / `gh api` mutation
- Any `git push` to `main` (slice/branch only; the auto-pin PR is a separate lane)
- Production secrets, `1password://` refs, vault deployment

## Critical corrections (per Yua 2026-07-13 18:51:31)

The 4 contracts below replace the prior 3 prescriptions (avoid duplicate trigger fires, byte-deterministic across regions, match `:main` to v1.13.0) which were based on a stale local observation. The 4 contracts are:

### Contract A: One authoritative signed tag publish per release tag

The release pipeline MUST produce exactly ONE cosign-signed tag publish per release commit at `refs/tags/v*.*.*`. A second authoritative tag publish MUST be suppressed. The `:main` tag may still receive a non-authoritative image build; that build is NOT authoritative.

**Source of truth:** the `publish-core-image.yml` workflow's `on.push.tags: ["v*"]` trigger fires the `Build and publish musubi-core` job which signs the published image with cosign keyless via GitHub OIDC. The authoritative published tag carries the signed digest at `refs/tags/v<version>`.

**Test:** the test reads the checked-in `publish-core-image.yml` workflow file and asserts:
- The workflow has a `jobs.publish-core-image` job
- The job includes a `cosign sign --keyless` step (or equivalent cosign invocation)
- The publish step tags the image with `v*` (the release tag), not with `:main`
- The workflow does NOT publish an authoritative signed digest for non-tag pushes (the main push is non-authoritative)

### Contract B: Auto-pin consumes the signed immutable tag digest, never the moving main tag

The `auto-digest-bump` workflow (and any successor) MUST pin `musubi_core_image` to the digest of the latest signed tag release (e.g., `ghcr.io/ericmey/musubi-core@sha256:ee2c759a...` for v1.13.0). It MUST NOT pin to the `:main` tag.

**Source of truth:** the `auto-digest-bump.yml` workflow (if it exists; otherwise the slice proposes a follow-up to create it) pins the digest by reading the latest `v*.*.*` tag and using its signed digest.

**Test:** the test reads the checked-in `auto-digest-bump.yml` workflow file (or the slice's proposed config) and asserts:
- The workflow reads the latest tag digest from `git ls-remote --tags origin` (or equivalent)
- The workflow sets the pin to the signed tag digest, not the `:main` digest
- The workflow does NOT pin to `refs/tags/main` or any non-versioned ref

### Contract C: Main and release channels are explicitly distinct in generated metadata/docs

The `:main` tag is the moving development channel; signed `v*.*.*` tags are immutable release channels. The release pipeline MUST label these channels as distinct in:
- The release page documentation
- The auto-pin output
- The deploy repo's pin metadata (e.g., `musubi_hosts.yml` in hw-ansible)

**Test:** the test reads the checked-in `publish-core-image.yml` workflow file and asserts:
- The workflow labels `:main` as `bleeding-edge` (or equivalent moving-channel label) in its comment/docs
- The workflow labels `v*` as `authoritative release` (or equivalent immutable-channel label)
- The workflow outputs a distinct label or annotation that the deploy consumer can use to identify the channel

### Contract D: A design-only reproducibility boundary that treats cache as performance, not input

If the team chooses to enforce byte-deterministic builds across regions, the reproducibility invariant MUST compare builds with identical source, platform, dependency/base digests, toolchain, build arguments, and canonical OCI metadata. Cache enabled/disabled or cache location MUST NOT change the resulting digest. Cache state is NOT pinned as an artifact input; cache is a performance concern, not a correctness or reproducibility input.

**Test:** the test reads the checked-in `publish-core-image.yml` workflow file and asserts:
- The workflow does NOT pin `--cache-to type=gha,mode=max` as an artifact input (cache is a performance concern)
- The workflow does NOT use a non-canonical `org.opencontainers.image.created` (which would make builds non-deterministic)
- If reproducibility is desired, the workflow would need to use a deterministic base image (fixed digest, not `latest`) and canonicalize `org.opencontainers.image.created` to the release commit timestamp
- This is a SEPARATE design decision; the test documents the design boundary but does NOT prescribe it as a default

## Self-proving red contracts (4 + 3 discriminations + 4 controls)

The 4 red contracts (test_release_pipeline_produces_exactly_one_authoritative_tag_publish, test_auto_pin_consumes_only_signed_tag_digest, test_release_main_and_tag_channels_are_explicitly_distinct, test_release_reproducibility_treats_cache_as_performance) are the "these break if the wrong design lands" contracts.

The 3 discrimination tests (test_wrong_dual_authoritative_tag_publish, test_wrong_auto_pin_chases_moving_main, test_wrong_cache_pinned_as_correctness_input) are the "these discriminate against the three wrong designs" contracts. Each discrimination test uses a control (correct implementation) and a wrong implementation (a wrong design that would break the contract).

The 4 legitimate controls (test_control_publish_workflow_unchanged, test_control_auto_pin_workflow_unchanged, test_control_release_metadata_clearly_distinguishes_channels, test_control_no_live_github_actions_called) are the "these prove the test itself is not vacuous" contracts.

## Out of slice scope (per Yua 18:51:31)

- Tests/docs/design only
- No source changes
- No workflow changes
- No production changes
- No deploy
- No host contact
- No merge (the slice is a separate narrow branch; the slice PR is a draft)
- Aoi R20 (release-automation follow-up; the slice will be available for an independent second read after Aoi R20 lands)

## Design note (ADR) — Release automation channel model

The 4 corrected contracts above define a channel model for the Musubi release pipeline. The channel model is:

- **Channel 1 — v* (immutable release):**
  - One authoritative cosign-signed digest per release commit
  - The signed digest at `refs/tags/v*.*.*` is the source of truth for that release
  - Deploy auto-pin consumes ONLY the signed tag digest

- **Channel 2 — main (moving development):**
  - A non-authoritative image build per push
  - May carry different OCI metadata (created, revision, version) per run
  - Auto-pin MUST NOT chase this channel

- **Distinguishing the channels:**
  - `org.opencontainers.image.version` annotation is sourced from the tag via `docker/metadata-action@v5`'s `type=semver,pattern={{version}}`
  - The version annotation is the canonical channel distinguisher (consumers can identify the channel via the manifest annotation, not just the tag)

- **Reproducibility boundary:**
  - Cache state is a performance concern, NOT a correctness or reproducibility input
  - If reproducibility is desired, the invariant MUST compare builds with identical source, platform, dependencies, toolchain, build arguments, and canonical OCI metadata
  - This is a SEPARATE design decision, not a default invariant of the current pipeline

The 3 red contracts + 3 discriminations + 4 controls in the test file encode this model. The tests are self-proving and operate on checked-in workflow fixtures (`publish-core-image.yml`, `auto-digest-bump.yml`) or parsed workflow structure only. They do NOT call live GitHub Actions or mutate releases.
