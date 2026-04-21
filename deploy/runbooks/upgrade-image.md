# Upgrade Musubi Core — image bump procedure

Companion to [`.github/workflows/publish-core-image.yml`](../../.github/workflows/publish-core-image.yml).
Use this when you need to move `musubi.example.local` to a newer
`ghcr.io/ericmey/musubi-core` digest. For a first-deploy-from-scratch,
see [`first-deploy.md`](first-deploy.md) instead.

Cadence: on demand. Every merge to `v2` publishes a fresh floating
`:v2` tag + a digest. Every `v*` tag push publishes a versioned tag
+ digest. The deploy itself is a separate, human-reviewed PR.

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
gh pr create --base v2 --title "ops: bump musubi_core_image to @sha256:<first 12 chars>"
```

**Expected output:** a reviewable PR showing a single-line diff in
`group_vars/all.yml`. No other file should change.

**Destructive:** no (until merged).

**Rollback:** close the PR without merging. Nothing in the environment
has changed yet.

---

## 4. Merge + deploy

**Command:**

```bash
# After the PR is approved and merged:
gh pr merge <number> --squash
ssh yua
cd ~/musubi
git pull --ff-only
ansible-playbook -i ~/.musubi-secrets/inventory-vars.yml \
  deploy/ansible/deploy.yml --ask-vault-pass
```

(When [[_slices/slice-ops-update-workflow]] lands, swap the last line
for `ansible-playbook deploy/ansible/update.yml --ask-vault-pass`.
`update.yml` force-pulls the new digest and recreates only the
changed containers, which is what we want for an image bump — see
that slice's spec for the rationale.)

**Expected output:** `deploy.yml` reports one changed task (the
`docker_compose_v2` task that recreates `core`). Everything else
unchanged. `/v1/ops/health` returns `{"status":"ok"}`.

**Destructive:** yes — replaces the running `core` container. A
failed healthcheck after this step means production is degraded.

**Rollback:** see step 6 below.

---

## 5. Verify

**Command:**

```bash
# From the operator's workstation (or yua):
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
git -C ~/musubi log -p -- deploy/ansible/group_vars/all.yml | head -40
# Revert the bump commit:
git -C ~/musubi revert --no-edit <bump-commit-sha>
git -C ~/musubi push origin v2
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
