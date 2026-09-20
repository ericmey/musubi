# Upgrade Musubi Core — image bump procedure

Companion to [`.github/workflows/publish-core-image.yml`](../../.github/workflows/publish-core-image.yml).
Use this when you need to move `musubi.example.local` to a newer
`ghcr.io/ericmey/musubi-core` digest. For a first-deploy-from-scratch,
see [`first-deploy.md`](first-deploy.md) instead.

Cadence: on demand. Every merge to `main` publishes a fresh floating
`:main` tag + a digest. Every `v*` tag push publishes `:vX.Y.Z` +
digest — *any* `v*` tag push does this, including a manual `git
push origin v1.2.3` from an operator. The caveat is release-please's
automated tag push: with the default `GITHUB_TOKEN` it gets silenced
by GitHub's anti-recursion guard, so release-please's workflow must
authenticate with `RELEASE_PLEASE_PAT` for its tag pushes to fire
this build. See the ADR 0026 addendum dated 2026-04-23 for the full
story. The deploy itself is a separate, human-reviewed PR.

---

## 1. Confirm the new image is published

**Command:**

```bash
gh workflow view publish-core-image.yml --web
# or, for just the latest run:
gh run list --workflow publish-core-image.yml --limit 1
```

**Expected output:** the most recent run is `completed / success` for
the commit / tag you want to ship.

**Recovery — image not published yet:** if the target commit has no
matching `publish-core-image.yml` run (rare — tag-triggered publish
runs automatically, but a tag that was pushed before the workflow
existed, or a branch-HEAD build, needs a manual kick), trigger a build
via `workflow_dispatch`:

```bash
gh workflow run publish-core-image.yml --ref <tag-or-branch>
gh run watch $(gh run list --workflow publish-core-image.yml \
  --limit 1 --json databaseId --jq '.[0].databaseId')
```

Then return to the step above and capture the digest.

**Destructive:** no.

**Rollback:** not applicable (this is a check).

---

## 2. Capture the digest

**Command:**

```bash
gh run view --workflow publish-core-image.yml --log \
 | grep -A 1 '"digest"' | head
# or — easier — open the run's summary page and copy the fenced
# block labelled "Pin in deploy/ansible/group_vars/all.yml as:".
```

**Expected output:** a `sha256:<64-hex>` value you can paste into
`group_vars/all.yml`.

**Destructive:** no.

**Rollback:** not applicable.

---

## 3. Open the pin-bump PR

**Command:**

```bash
cd ~/Projects/musubi
git checkout -b ops/core-image-bump-$(date +%Y%m%d)
sed -i '' \
 -E 's|^musubi_core_image: .*|musubi_core_image: "ghcr.io/ericmey/musubi-core@sha256:<paste digest here>"|' \
 deploy/ansible/group_vars/all.yml
git add deploy/ansible/group_vars/all.yml
git commit -m "ops: bump musubi_core_image to @sha256:<first 12 chars>"
git push -u origin "$(git branch --show-current)"
gh pr create --base main --title "ops: bump musubi_core_image to @sha256:<first 12 chars>"
```

**Expected output:** a reviewable PR showing a single-line diff in
`group_vars/all.yml`. No other file should change.

**Destructive:** no (until merged).

**Rollback:** close the PR without merging. Nothing in the environment
has changed yet.

---

## 4. Preflight, merge + deploy

**Command:**

```bash
# Before merging the pin PR, from the credential-bearing Ansible controller:
cd ~/musubi
git pull --ff-only origin main
export CANDIDATE_IMAGE=ghcr.io/ericmey/musubi-core@sha256:<paste digest here>
export MUSUBI_CREDENTIAL_DIR=~/.musubi
export MUSUBI_PREFLIGHT_AUTHORITY_ENV=~/.musubi/preflight-authority.env

cosign verify \
  --certificate-identity-regexp 'https://github.com/ericmey/musubi/.github/workflows/publish-core-image.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "${CANDIDATE_IMAGE}"
docker pull "${CANDIDATE_IMAGE}"
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --entrypoint python \
  --mount "type=bind,src=${MUSUBI_PREFLIGHT_AUTHORITY_ENV},dst=/preflight/authority.env,readonly" \
  --mount "type=bind,src=${MUSUBI_CREDENTIAL_DIR},dst=/credentials,readonly" \
  --mount "type=bind,src=${PWD}/deploy/credential-preflight.json,dst=/preflight/manifest.json,readonly" \
  "${CANDIDATE_IMAGE}" \
  -m musubi.auth.credential_preflight \
  --manifest /preflight/manifest.json \
  --credential-dir /credentials \
  --authority-env /preflight/authority.env

# Merge only after the command above reports every live credential and control PASS.
gh pr merge <number> --squash
git pull --ff-only
ANSIBLE_VAULT_PASSWORD_FILE=~/.ansible/.vault_pass \
  MUSUBI_PREFLIGHT_AUTHORITY_ENV=~/.musubi/preflight-authority.env \
  scripts/musubi-deploy --apply core,lifecycle-worker
```

The pin workflow never arms auto-merge. Before the digest lands, the operator
verifies its keyless signature and runs the exact candidate image as the
controller UID/GID so it can read controller-owned `0600` credentials. The
preflight inventories every discovered `musubi-mcp*.env`: an unclassified file,
missing or rejected live credential, or non-discriminating negative control
fails closed. The `musubi-mcp.env` mint-path template is classified separately
and is not part of the eligible live set.

`update.yml` repeats signature verification and the candidate preflight before
it can recreate Core. This defense is intrinsic to the playbook rather than a
caller-provided attestation, so direct Core updates run the same gate.
`MUSUBI_PREFLIGHT_AUTHORITY_ENV` must name a minimal env containing exactly one
`JWT_SIGNING_KEY` and one `OAUTH_AUTHORITY`; it is mounted rather than injected
wholesale, and duplicate or unknown keys fail closed. `MUSUBI_CREDENTIAL_DIR`
may override the default `~/.musubi` directory.

**Expected output:** `update.yml` reports one changed task (the
`docker_compose_v2` task that recreates `core`). Everything else
unchanged. `/v1/ops/health` returns `{"status":"ok"}`.

**Destructive:** yes — replaces the running `core` container. A
failed healthcheck after this step means production is degraded.

**Rollback:** see step 6 below.

---

## 5. Verify

**Command:**

```bash
# From the operator's workstation (or the control host):
curl -sS http://musubi.example.local:8100/v1/ops/status | jq .
ssh ericmey@musubi.example.local \
 'sudo docker inspect musubi-core-1 --format "{{.Image}}"'
```

**Expected output:**

- `status` is `"ok"` with every component `healthy: true`.
- `docker inspect` returns an image ID whose digest matches the pin.

**Destructive:** no.

**Rollback:** if `status` is not `"ok"`, proceed to step 6
immediately — do not wait for alarms to fire.

---

## 6. Rollback (if needed)

**Command:**

```bash
# Find the previous value of musubi_core_image:
git -C ~/musubi switch main
git -C ~/musubi pull --ff-only
git -C ~/musubi log -p -- deploy/ansible/group_vars/all.yml | head -40
# Revert the bump commit:
git -C ~/musubi revert --no-edit <bump-commit-sha>
git -C ~/musubi push origin main
# Re-run deploy with the older digest:
ansible-playbook -i ~/.musubi-secrets/inventory-vars.yml \
 deploy/ansible/deploy.yml --ask-vault-pass
```

**Expected output:** `docker_compose_v2` recreates `core` with the
old image; `/v1/ops/status` flips back to `ok`.

**Destructive:** yes — tears down the broken new container. Accept
the ~10 seconds of 503s.

**Rollback:** if the previous digest is ALSO broken, escalate — a
deeper rollback means restoring a Qdrant snapshot (see
[`../backup/README.md`](../backup/README.md)) and re-running
`deploy.yml`.
