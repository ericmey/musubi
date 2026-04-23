---
title: "ADR 0026: release-please drives version bumps and tag cutting"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-21
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr]
updated: 2026-04-23
up: "[[13-decisions/index]]"
reviewed: false
supersedes: ""
superseded-by: ""
---

# ADR 0026: release-please drives version bumps and tag cutting

**Status:** accepted
**Date:** 2026-04-21
**Deciders:** Eric

## Context

PR #154 landed the GHCR publish workflow. It triggers on `v*` tag
pushes — the tag is the operator's signal that "this commit should
ship as a versioned, digest-pinnable release." Today that means a
human runs `git tag v0.3.0 && git push --tags` after merging a
batch of work to `v2`.

Two real problems with that:

1. **Version number is ad-hoc.** Without a convention, operators
   guess at major/minor/patch boundaries. A breaking change can slip
   in under a patch bump because nobody ran `git log` first.
2. **CHANGELOG is a bus factor.** Generating release notes by hand
   at tag-cut time means the one person who remembers what merged
   writes them — or they don't get written.

Conventional commits (we already use them — `feat:`, `fix:`,
`ci:`, etc.) contain all the info needed to automate both.

## Decision

Adopt `googleapis/release-please-action@v4` to manage versioning:

- Every merge to `v2` triggers the `release-please.yml` workflow.
- It scans commits since the last tag for conventional-commit
  prefixes and opens (or updates) a single **release PR** titled
  `chore(release): vX.Y.Z`.
- The release PR contains two changes:
  - Version bump in `pyproject.toml` (via the release-type: python).
  - Appended entry in `CHANGELOG.md` with grouped sections
    (Features / Bug Fixes / Performance / Refactors / Documentation /
    CI).
- Merging the release PR auto-creates the `vX.Y.Z` tag + GitHub
  Release.
- The tag push triggers `publish-core-image.yml`, which
  builds + scans (Trivy) + signs (cosign) + publishes to GHCR.
  **Requires release-please to authenticate with a PAT (or GitHub
  App token), not the default `GITHUB_TOKEN`** — see [Addendum
  2026-04-23](#addendum-2026-04-23-tag-push-requires-a-pat).

End-to-end: **operator merges feature PRs into v2 → approves the
release PR when the batch is ready → signed versioned image appears
on GHCR.**

## Configuration

- `.release-please-config.json` — release-please config. Keeps
  `bump-minor-pre-major: true` so `feat:` on v0.x pre-1.0 bumps the
  minor (0.2.0 → 0.3.0). Chores and tests are hidden from the
  changelog by default; override with `!` to force inclusion.
- `.release-please-manifest.json` — current version ("0.2.0").
  release-please edits this on merge of the release PR.

## Commit conventions

| Prefix      | Intent                                    | Bump  | In changelog? |
|-------------|-------------------------------------------|-------|---------------|
| `feat:`     | new user-visible capability               | minor | yes           |
| `fix:`      | bug fix                                   | patch | yes           |
| `perf:`     | performance improvement                   | patch | yes           |
| `refactor:` | code restructure, no behaviour change     | patch | yes           |
| `docs:`     | documentation                             | patch | yes           |
| `ci:` / `ops:`| CI or operational tooling               | patch | yes           |
| `chore:`    | repo hygiene                              | —     | hidden        |
| `test:`     | test-only change                          | —     | hidden        |
| `<any>!:`   | breaking change                           | major | yes, flagged  |

Scopes (`feat(api):`, `fix(ops):`) are preserved in the changelog
line.

## When this decision gets revisited

- If a release PR gets stale because operators forget to merge it:
  add a reminder bot or shorten the batch window.
- If breaking changes need a more rigorous process (deprecation
  notices, migration guides): introduce a companion ADR; release-
  please itself can handle the `!` prefix + "Breaking Changes"
  section in the meantime.
- If we ever go multi-package (e.g. separate `musubi-sdk` release
  train), switch `release-please` to monorepo mode and add a second
  entry in the manifest. ADR 0015 (monorepo) already anticipates this.

## Consequences

**Positive:**

- Version numbers follow semantic rules every time.
- CHANGELOG is generated from git history instead of memory.
- Signed image publish chain is triggered from a normal PR merge —
  no operator needs to know the `git tag` incantation.
- Tag → publish → scan → sign happens automatically; operator
  reviews the release PR content rather than running manual steps.

**Negative:**

- Adds a workflow that opens PRs. First-time contributors may be
  confused by the "chore(release): vX.Y.Z" PR appearing on merges.
  Document in the CONTRIBUTING guide (future slice).
- Commit prefix discipline matters. A merge without a conventional
  prefix doesn't show up in the changelog — silent gap.
  Enforcement lives in a future commit-lint hook; for now, review
  discipline.
- Release PR can become stale if operators merge many feature PRs
  before reviewing. The PR self-updates, but an operator that
  never merges it sees no releases — mitigate by treating the
  release PR like any other review-worthy artefact.

## Alternatives considered

- **Manual `git tag`.** Fine at homelab scale; we started there.
  Doesn't scale if we want a contributor workflow or a schedule.
- **semantic-release.** More battle-tested in JS ecosystems but
  heavier configuration for Python and less good at monorepo
  support.
- **Commitizen + CHANGELOG autogen via `cz bump`.** Python-native,
  but requires a local tool invocation rather than a merge-
  triggered workflow.

## Related

- [[13-decisions/0015-monorepo-supersedes-multi-repo]]
- [[_slices/slice-ops-core-image-publish]] (the publish workflow)
- `.github/workflows/release-please.yml`
- `.release-please-config.json` + `.release-please-manifest.json`

## Addendum 2026-04-23 — tag push requires a PAT

The original "tag push triggers `publish-core-image.yml`" step
assumed `GITHUB_TOKEN` would suffice. **It does not.** GitHub's
anti-recursion guard silently suppresses workflow runs for events
authored by the default `GITHUB_TOKEN` (see
[GitHub Actions: triggering a workflow from a workflow](https://docs.github.com/en/actions/using-workflows/triggering-a-workflow#triggering-a-workflow-from-a-workflow)).
That includes the `vX.Y.Z` tag push release-please creates on merge.

We didn't notice immediately because:

- `publish-core-image.yml` also fires on `push: branches: [main]`,
  so the release-please merge commit still produces a built +
  signed image — just tagged `:main` instead of `:vX.Y.Z`.
- Digest pins don't care about version tags (digests are
  immutable content addresses), so the ansible deploy continued
  to work by pasting the digest from the `:main` build.

Observed impact: v0.4.0, v0.5.0, v0.5.1 were all published to GHCR
without a `:vX.Y.Z` image tag. Only `:main`, `:latest`, and
`:v0.3.0` (manual tag, pre-release-please) exist on the registry.

**Fix.** Authenticate release-please with a PAT (or GitHub App
token) that has `repo` + `workflow` scopes, stored as
`secrets.RELEASE_PLEASE_PAT`. Tag pushes authored by a PAT *do*
fire downstream workflows. Rotation: bump the secret, confirm the
next release-please run publishes `:vX.Y.Z`. The workflow falls
back to `GITHUB_TOKEN` if the secret is missing so release PRs
continue to open (just without tagged images).

**Why the addendum, not a revision of the Decision section.**
The original decision ("release-please drives versioning") is
still right. Only the *mechanism* for cross-workflow triggering
was incorrectly described. Keeping the original text + an
addendum preserves the history of why v0.4.0/v0.5.0/v0.5.1 shipped
without `:vX.Y.Z` tags.
