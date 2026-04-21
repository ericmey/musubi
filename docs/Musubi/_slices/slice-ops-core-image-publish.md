---
title: "Slice: Publish musubi-core to GHCR via CI"
slice_id: slice-ops-core-image-publish
section: _slices
type: slice
status: ready
owner: unassigned
phase: "8 Ops"
tags: [section/slices, status/ready, type/slice, ops, ci, image, ghcr]
updated: 2026-04-20
reviewed: false
depends-on: ["[[_slices/slice-ops-first-deploy]]"]
blocks: ["[[_slices/slice-ops-update-workflow]]"]
---

# Slice: Publish musubi-core to GHCR via CI

> Replace the one-time `docker save | ssh | docker load` transfer that
> brought Musubi Core onto `musubi.example.local` with a GitHub Actions
> workflow that builds and publishes `ghcr.io/ericmey/musubi-core` on
> every tag (and optionally on every merge to `v2`), so any host can
> `docker pull` the image by digest.

**Phase:** 8 Ops · **Status:** `ready` · **Owner:** `unassigned`

## Why this slice exists

The first deploy on 2026-04-20 built the Musubi Core image locally on
Eric's Mac (`docker buildx build --platform linux/amd64`), then
transferred it to `musubi.example.local` via a `docker save | ssh | docker
load` pipe. That worked for the single-host first-deploy but creates three
problems as soon as we leave that narrow path:

1. **A fresh host cannot deploy itself.** `deploy/ansible/deploy.yml`'s
   `community.docker.docker_compose_v2_pull` step can't pull an image
   that only exists locally on one Mac. The first deploy worked only
   because the image was on the target before Ansible tried to pull.
2. **No reproducibility across machines.** The build on Eric's ARM Mac
   depends on whatever Docker buildx context happens to be active. A
   different operator on a different machine gets a different image
   from the same commit.
3. **No digest pinning.** `group_vars/all.yml` currently has
   `musubi_core_image: "musubi-core:dev"` — a floating tag. When the
   image is rebuilt, the tag points at new bytes with no audit trail
   and no rollback target. The companion `slice-ops-update-workflow`
   can't safely upgrade without a pinned source.

The repo has a working `Dockerfile` at the root (shipped in PR #148) and
the build is mechanically straightforward — this slice wires that into
CI + pins the digest.

## Specs to implement

- [[08-deployment/compose-stack]] — the `musubi_core_image` reference
  that this slice replaces.
- [[08-deployment/index]] §Pinning versions — the pinning policy that
  expects digest-pinned images.
- [[13-decisions/0015-monorepo-supersedes-multi-repo]] — confirms that
  publishing from this monorepo to a single namespace is the intended
  pattern.

## Owned paths (you MAY write here)

- `.github/workflows/publish-core-image.yml` (new — the GitHub Actions
  workflow).
- `deploy/ansible/group_vars/all.yml` (edit — flip
  `musubi_core_image` from `musubi-core:dev` to
  `ghcr.io/ericmey/musubi-core@sha256:<digest>`).
- `docs/Musubi/08-deployment/compose-stack.md` (edit — update the
  pinning docs to reflect the real GHCR path).
- `docs/Musubi/08-deployment/index.md` (edit — same).
<!-- Intentionally NOT listing `deploy/runbooks/first-deploy.md` under
owns_paths — it's owned by `slice-ops-first-deploy`. The small edit to
retire the `docker save | ssh | docker load` step belongs in the same
PR as this slice via a cross-slice ticket + `spec-update:` commit
trailer (see Cross-slice tickets below). -->
- `deploy/runbooks/upgrade-image.md` (new — per-image-bump procedure
  that replaces the ad-hoc `docker save | ssh | docker load`).
- `tests/ops/test_image_publish.py` (new — structural tests on the
  workflow YAML and the ansible variable).

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `Dockerfile`, `.dockerignore` — shipped in PR #148, not this slice's
  concern. If the Dockerfile needs a change for publish, open a
  cross-slice ticket.
- `src/musubi/` — this is ops, not Core code.
- `docs/Musubi/07-interfaces/`, `openapi.yaml`, `proto/` — API contract,
  frozen.
- `deploy/ansible/bootstrap.yml`, `config.yml`, `deploy.yml`,
  `health.yml` — playbook logic is `slice-ops-update-workflow`'s
  territory if you need to change how images get pulled.

## Depends on

- [[_slices/slice-ops-first-deploy]] (done — `Dockerfile` exists, first
  deploy succeeded, `musubi_core_image` reference exists in
  `group_vars/all.yml`).

## Unblocks

- [[_slices/slice-ops-update-workflow]] — needs a real registry source
  to pull from; it can't upgrade an image that only lives on one Mac.
- **Fresh-host provisioning.** Once the image is on GHCR, a new
  `musubi.example.local` replacement host can run `bootstrap.yml` +
  `deploy.yml` end-to-end without any manual image transfer.
- **Rollback on-deploy.** A digest pin means the runbook's "rollback"
  step can be "pin the prior digest and re-run deploy.yml" — currently
  there's no previous-image reference to roll back to.

## What lands in this slice

### 1. `.github/workflows/publish-core-image.yml`

Triggers:

- `push` to tag `v*` — builds and publishes with both `:v<version>` and
  `@sha256:<digest>`. The authoritative release path.
- `push` to branch `v2` — builds and publishes with tag `:v2` (floating)
  + captures the digest as a build artefact. Lets operators run the
  bleeding edge without cutting a tag.
- `workflow_dispatch` — manual run for one-off images.

Steps:

1. Checkout.
2. Set up `docker/setup-qemu-action` + `docker/setup-buildx-action` (for
   reliable linux/amd64 cross-build).
3. Log in to GHCR using `GITHUB_TOKEN` (has `packages:write` on the
   repo's container namespace by default).
4. `docker/build-push-action` with:
   - `platforms: linux/amd64`
   - `tags`: `ghcr.io/ericmey/musubi-core:v<version>` and
     `ghcr.io/ericmey/musubi-core:v2` as appropriate.
   - `labels`: OCI image-source labels pointing at the commit.
   - `cache-from` / `cache-to` via GHA cache so subsequent builds reuse
     layers.
5. Capture the resulting digest; surface as a workflow output + a
   GitHub release asset on tag-triggered runs.

The workflow NEVER writes to `group_vars/all.yml` — the digest bump is
a separate PR that an operator (or a future bot) opens after reviewing
the new image. Don't couple the "publish" step with the "deploy" step;
they're different decisions.

### 2. `deploy/ansible/group_vars/all.yml` flip

Replace:

```yaml
musubi_core_image: "musubi-core:dev"
```

with the first-published GHCR digest, e.g.:

```yaml
musubi_core_image: "ghcr.io/ericmey/musubi-core@sha256:<digest-from-workflow-run>"
```

Document the bump procedure in the companion `slice-ops-update-workflow`
so operators know how to advance the pin safely.

### 3. `deploy/runbooks/first-deploy.md` § 4 edits

Retire the implicit `docker save | ssh | docker load` that tonight's
first deploy relied on. Replace with:

> **Before running `deploy.yml`:** confirm that the digest in
> `group_vars/all.yml → musubi_core_image` corresponds to a
> successfully-published workflow run on GHCR
> (https://github.com/ericmey/musubi/pkgs/container/musubi-core). If
> the image isn't published yet, trigger
> `.github/workflows/publish-core-image.yml` via `workflow_dispatch`
> and wait for the digest before proceeding.

### 4. Tests: `tests/ops/test_image_publish.py`

Structural — no live registry calls. Assertions:

- The workflow YAML parses; has exactly one `publish-core-image` job.
- Triggers include `push.tags: ['v*']`, `push.branches: ['v2']`,
  `workflow_dispatch`.
- The job uses `docker/build-push-action` with `push: true` and a tag
  list that includes both a `:v<version>` tag and a GHCR path.
- The job has a step that logs in to `ghcr.io`.
- `group_vars/all.yml`'s `musubi_core_image` value is either `"musubi-
  core:dev"` (pre-slice state, caught as failing until the digest bump
  lands) or matches `ghcr.io/ericmey/musubi-core@sha256:[0-9a-f]{64}`.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] `publish-core-image.yml` exists and runs green on a test tag (cut
      a `v0.2.1-test` or similar scratch tag; delete after).
- [ ] The workflow's published image is visible at
      `ghcr.io/ericmey/musubi-core` with the expected tag + digest.
- [ ] `group_vars/all.yml` flipped from the local-build tag to a
      digest-pinned GHCR reference.
- [ ] `ansible-playbook deploy.yml` run against `musubi.example.local`
      pulls from GHCR successfully (no `docker save | ssh` needed).
      Recorded in `[[00-index/work-log]]`.
- [ ] Runbook updated to reflect the new pre-deploy image-publish
      check.
- [ ] `tests/ops/test_image_publish.py` passes; coverage ≥ 80 % on
      its module.
- [ ] `spec-update:` commit trailer used for any changes to
      `docs/Musubi/08-deployment/*.md`.

**Explicitly NOT in scope:**

- Automating the `group_vars` digest bump (a Dependabot-style or
  Renovate-style workflow). Keep the bump a human-reviewed PR for now.
- Signing the image (cosign, sigstore). Separate concern; open a
  follow-up slice if signing becomes a requirement.
- Building for `linux/arm64` in addition to `amd64`. RTX-3080 hosts are
  x86_64; arm64 builds are speculation until a real consumer surfaces.

## Test Contract

**Workflow YAML structural:**

1. `test_workflow_file_parses`
2. `test_workflow_has_publish_core_image_job`
3. `test_workflow_triggers_include_tags_v2_and_dispatch`
4. `test_workflow_uses_build_push_action_with_push_true`
5. `test_workflow_logs_into_ghcr`
6. `test_workflow_builds_for_linux_amd64`
7. `test_workflow_tags_include_ghcr_namespace`

**Ansible integration:**

8. `test_group_vars_musubi_core_image_is_ghcr_digest_pinned`
9. `test_group_vars_musubi_core_image_parses_as_oci_reference`

**Runbook integration:**

10. `test_first_deploy_runbook_no_longer_mentions_docker_save_pipe`
11. `test_first_deploy_runbook_mentions_workflow_dispatch_as_recovery`

## Cross-slice tickets opened by this slice

- **To `slice-ops-first-deploy`** — small edit to
  `deploy/runbooks/first-deploy.md` to retire the `docker save | ssh |
  docker load` transfer step and replace it with "confirm the GHCR
  digest is published before running deploy.yml". File is owned by
  slice-ops-first-deploy; this slice's PR touches it with a
  `spec-update: deploy/runbooks/first-deploy.md` commit trailer.
- _(open another if the `Dockerfile` needs changes for publish, e.g.
  multi-arch builds.)_

## Work log

_(empty — awaiting claim)_

## PR links

_(empty)_
