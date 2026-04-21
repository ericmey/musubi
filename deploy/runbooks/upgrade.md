# Upgrade a running Musubi stack

Run this procedure to apply an in-place upgrade — typically a
`musubi_core_image` digest bump, but also any change to
`docker-compose.yml.j2`, `.env.production`, or the prometheus scrape
config. For a first-deploy-from-scratch, use
[`first-deploy.md`](first-deploy.md) instead. For the narrower
"just bump the core image" flow see
[`upgrade-image.md`](upgrade-image.md).

The `deploy/ansible/update.yml` playbook drives every step below.

---

## 1. Pre-flight

**Command:**

```bash
# On the control host:
cd ~/musubi
git pull --ff-only

# Confirm the compose render will succeed with the current vars:
ansible-playbook -i ~/.musubi-secrets/inventory-vars.yml \
 deploy/ansible/update.yml --check --ask-vault-pass
```

**Expected output:**

`--check` reports the expected diffs (image references or compose
mounts that changed) without touching the live stack. If it errors on
a missing vault var, see `deploy/ansible/vault.example.yml`.

**Destructive:** no (`--check` is a dry-run).

**Rollback:** not applicable.

---

## 2. Bump the pin (for image upgrades)

Only needed when the upgrade is an image bump. Skip for compose-only
or config-only changes.

**Command:**

```bash
# Confirm the new digest from the publish workflow:
gh run list --workflow publish-core-image.yml --limit 3

# Open a bump PR:
git checkout -b ops/core-image-bump-$(date +%Y%m%d)
sed -i '' \
 -E 's|^musubi_core_image: .*|musubi_core_image: "ghcr.io/ericmey/musubi-core@sha256:<paste>"|' \
 deploy/ansible/group_vars/all.yml
git commit -am "ops: bump musubi_core_image to @sha256:<first 12 chars>"
gh pr create --base v2 --title "ops: bump musubi_core_image"
```

**Expected output:**

Single-line diff in `group_vars/all.yml`. PR greens on CI.

**Destructive:** no (until merged).

**Rollback:** close the PR without merging.

---

## 3. Dry-run against the live host

**Command:**

```bash
ansible-playbook -i ~/.musubi-secrets/inventory-vars.yml \
 deploy/ansible/update.yml \
 --check --diff --ask-vault-pass
```

**Expected output:**

- The compose-template task shows the old → new `image:` line.
- The `docker_compose_v2` task reports it will recreate the listed
 services (defaults to `[core]`).
- Zero changes to any service you did NOT name — if Qdrant or
 TEI shows as "recreate", stop and investigate.

**Destructive:** no.

**Rollback:** not applicable (nothing mutated yet).

---

## 4. Apply

**Command:**

```bash
ansible-playbook -i ~/.musubi-secrets/inventory-vars.yml \
 deploy/ansible/update.yml --ask-vault-pass
# Or for a multi-service bump:
ansible-playbook -i ~/.musubi-secrets/inventory-vars.yml \
 deploy/ansible/update.yml \
 -e changed_services='["core","tei-dense"]' --ask-vault-pass
```

**Expected output:**

- `policy=always` pull task reports "changed" for services whose
 digest moved, "ok" for the rest.
- `recreate` task finishes with `changed=1` (for the single-service
 default) and every listed service ends `healthy`.
- The `probe-core-health` task returns 200 within one retry.

**Destructive:** yes — recreates the named containers. Accept ~10s of
503s on the recreated services.

**Rollback:** see step 6.

---

## 5. Verify

**Command:**

```bash
# From the operator's workstation (or the control host):
curl -sS http://musubi.example.local:8100/v1/ops/status | jq .

# Inspect the upgrade log:
ssh ericmey@musubi.example.local \
 'sudo tail -1 /var/log/musubi/upgrade-history.jsonl | jq .'
```

**Expected output:**

- `status` is `ok` and every component is `healthy: true`.
- The last `upgrade-history.jsonl` entry is this run (matching
 timestamp, listed services, current `core_image`).

**Destructive:** no.

**Rollback:** if `/v1/ops/status` is not `ok`, proceed to step 6
immediately.

---

## 6. Rollback — revert and re-run

The rollback story is deliberately the same mechanism as forward
upgrade, run in reverse: revert the `group_vars` commit, push, re-run
`update.yml`. A dedicated `--rollback` flag is out of scope for v1
(every rollback we've needed so far has been a one-line revert).

**Command:**

```bash
# On the ansible control host — find the commit to revert:
git -C ~/musubi log -p -- deploy/ansible/group_vars/all.yml | head -40

# Revert:
git -C ~/musubi revert --no-edit <bump-sha>
git -C ~/musubi push origin v2

# Re-run update.yml:
ansible-playbook -i ~/.musubi-secrets/inventory-vars.yml \
 deploy/ansible/update.yml --ask-vault-pass
```

**Expected output:**

The reverted image digest pulls, `core` recreates with the old
digest, `/v1/ops/status` returns `ok`.

**Destructive:** yes — tears down the broken container.

**Rollback:** if the previous digest is ALSO broken, escalate: restore
from a Qdrant backup per [`../backup/README.md`](../backup/README.md)
and re-run `deploy.yml`.
