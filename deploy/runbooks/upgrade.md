# Upgrade a running Musubi stack

Run this procedure to apply an in-place upgrade — typically a
`musubi_core_image` digest bump, but also any change to
`docker-compose.yml.j2`, `.env.production`, or the prometheus scrape
config. For a first-deploy-from-scratch, use
[`first-deploy.md`](first-deploy.md) instead. For the narrower
"just bump the core image" flow see
[`upgrade-image.md`](upgrade-image.md).

The `deploy/ansible/update.yml` playbook drives every step below.

## Shared-inference / TEI image upgrades

TEI is owned by `shared-inference.service`, not by the application Compose
project. Consequently, `update.yml` and a normal application deploy do not
recreate TEI. When `musubi_tei_image` changes, use this bounded path from the
Ansible controller instead of adding `tei-*` to `changed_services`.

Set the common inventory arguments and read the reviewed image reference from
the repository:

```bash
cd ~/musubi
git pull --ff-only origin main
export MUSUBI_ANSIBLE_ARGS="-i deploy/ansible/inventory.yml -e @${HOME}/.musubi-secrets/inventory-vars.yml -e @${HOME}/.musubi-secrets/vault.yml"
export TEI_IMAGE="$(python3 -c 'import yaml; print(yaml.safe_load(open("deploy/ansible/group_vars/all.yml"))["musubi_tei_image"])')"
export MUSUBI_SSH="$(python3 -c 'import os, yaml; values = yaml.safe_load(open(os.path.expanduser("~/.musubi-secrets/inventory-vars.yml"))); print(values["operator_ssh_user"] + "@" + values["musubi_host"])')"
```

Pre-pull without interrupting the running containers, preserve the live
definition, render only the independently managed Compose file, and validate
it before the single restart:

```bash
ansible musubi ${MUSUBI_ANSIBLE_ARGS} --become \
  -m ansible.builtin.command -a "docker pull ${TEI_IMAGE}"
ansible musubi ${MUSUBI_ANSIBLE_ARGS} --become \
  -m ansible.builtin.command \
  -a "cp -a /etc/musubi/shared-inference-compose.yml /etc/musubi/shared-inference-compose.yml.rollback"
ansible-playbook ${MUSUBI_ANSIBLE_ARGS} deploy/ansible/deploy.yml \
  --tags shared-inference-config
ansible musubi ${MUSUBI_ANSIBLE_ARGS} --become \
  -m ansible.builtin.command \
  -a "docker compose -p shared-inference -f /etc/musubi/shared-inference-compose.yml config --quiet"
ansible musubi ${MUSUBI_ANSIBLE_ARGS} --become \
  -m ansible.builtin.systemd_service \
  -a "name=shared-inference.service state=restarted"
```

Wait for `tei-dense`, `tei-sparse`, and `tei-reranker` to report healthy, then
run the production client-shape probe inside Core. It uses the already-mounted
URLs and credentials without printing them:

```bash
ssh "${MUSUBI_SSH}" 'sudo docker exec -i musubi-core-1 python -' <<'PY'
import base64
import os

import httpx

auth = (os.environ["TEI_BASIC_AUTH_USERNAME"], os.environ["TEI_BASIC_AUTH_PASSWORD"])
requests = (
    (os.environ["TEI_DENSE_URL"], "/embed", {"inputs": ["upgrade probe"], "truncate": True}),
    (os.environ["TEI_SPARSE_URL"], "/embed_sparse", {"inputs": ["upgrade probe"]}),
    (os.environ["TEI_RERANKER_URL"], "/rerank", {"query": "upgrade", "texts": ["upgrade probe", "weather"]}),
)
with httpx.Client(auth=auth, timeout=30) as client:
    results = [client.post(base.rstrip("/") + path, json=body) for base, path, body in requests]
    assert all(response.status_code == 200 for response in results)
    assert len(results[0].json()[0]) == 1024
    assert results[1].json()[0]
    assert sorted(item["index"] for item in results[2].json()) == [0, 1]

    openai_url = os.environ["TEI_DENSE_URL"].rstrip("/") + "/v1/embeddings"
    common = {"model": "text-embeddings-inference", "input": ["upgrade probe"]}
    floats = client.post(openai_url, json={**common, "encoding_format": "float"})
    encoded = client.post(openai_url, json={**common, "encoding_format": "base64"})
    assert floats.status_code == encoded.status_code == 200
    assert len(floats.json()["data"][0]["embedding"]) == 1024
    wire = encoded.json()["data"][0]["embedding"]
    assert isinstance(wire, str) and len(base64.b64decode(wire, validate=True)) == 4096
print("shared-inference upgrade probes: PASS")
PY
```

If rendering, startup, or a probe fails, restore the saved definition and
restart once during the same maintenance window:

```bash
ansible musubi ${MUSUBI_ANSIBLE_ARGS} --become \
  -m ansible.builtin.command \
  -a "cp -a /etc/musubi/shared-inference-compose.yml.rollback /etc/musubi/shared-inference-compose.yml"
ansible musubi ${MUSUBI_ANSIBLE_ARGS} --become \
  -m ansible.builtin.systemd_service \
  -a "name=shared-inference.service state=restarted"
```

The persistent `/var/lib/musubi/tei-models` cache is not replaced in either
direction. Keep the rollback file until the new containers and consumer smoke
checks have remained healthy.

---

## 1. Pre-flight

**Command:**

```bash
# On the control host:
cd ~/musubi
git pull --ff-only

# Confirm the compose render will succeed with the current vars:
ansible-playbook \
 -i deploy/ansible/inventory.yml \
 -e @~/.musubi-secrets/inventory-vars.yml \
 -e @~/.musubi-secrets/vault.yml \
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
gh pr create --base main --title "ops: bump musubi_core_image"
```

**Expected output:**

Single-line diff in `group_vars/all.yml`. PR greens on CI.

**Destructive:** no (until merged).

**Rollback:** close the PR without merging.

---

## 3. Dry-run against the live host

**Command:**

```bash
export MUSUBI_PREFLIGHT_AUTHORITY_ENV=~/.musubi/preflight-authority.env
ansible-playbook \
 -i deploy/ansible/inventory.yml \
 -e @~/.musubi-secrets/inventory-vars.yml \
 -e @~/.musubi-secrets/vault.yml \
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
export MUSUBI_PREFLIGHT_AUTHORITY_ENV=~/.musubi/preflight-authority.env
ansible-playbook \
 -i deploy/ansible/inventory.yml \
 -e @~/.musubi-secrets/inventory-vars.yml \
 -e @~/.musubi-secrets/vault.yml \
 deploy/ansible/update.yml --ask-vault-pass
# Or for an application multi-service bump:
ansible-playbook \
 -i deploy/ansible/inventory.yml \
 -e @~/.musubi-secrets/inventory-vars.yml \
 -e @~/.musubi-secrets/vault.yml \
 deploy/ansible/update.yml \
 -e '{"changed_services":["core","lifecycle-worker"]}' --ask-vault-pass
```

> **Use the JSON extra-vars form for `changed_services`.** Ansible's
> `-e key=value` spelling always yields a **string**, so
> `-e changed_services='["core"]'` arrives as the literal text `["core"]`;
> `join(' ')` then joins its characters and `loop:` iterates them. The
> playbook now normalises the string spelling and asserts the result before
> touching the stack, but the JSON form is what to write. See #665.

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

# Live consumer blast-radius smoke. Run once before deploy with
# MUSUBI_CONSUMER_PHASE=pre-deploy and again here with post-deploy.
# Each command must exercise the real consumer, not only curl Musubi.
# The script rejects unset values, literal <placeholder> text, and no-ops.
MUSUBI_CONSUMER_PHASE=post-deploy \
MUSUBI_CONSUMER_COMMAND_CHAIR_CMD='<command-chair live smoke command>' \
MUSUBI_CONSUMER_PHONE_AGENTS_CMD='<phone-agent live smoke command>' \
MUSUBI_CONSUMER_OPENCLAW_NYLA_CMD='<openclaw-on-nyla live smoke command>' \
MUSUBI_CONSUMER_VICE_CMD='<vice live app smoke command>' \
deploy/smoke/check_consumers.sh
```

**Expected output:**

- `status` is `ok` and every component is `healthy: true`.
- The last `upgrade-history.jsonl` entry is this run (matching
 timestamp, listed services, current `core_image`).
- `check_consumers.sh` reports `[PASS]` for all four live consumer
 classes: command-chair agents, phone agents, OpenClaw on Nyla, and
 Vice. If any consumer fails, treat the deploy as failed even when Core
 health is green.

**Destructive:** no.

**Rollback:** if `/v1/ops/status` is not `ok` or any live consumer
regression smoke fails, proceed to step 6 immediately.

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
git -C ~/musubi push origin main

# Re-run update.yml:
export MUSUBI_PREFLIGHT_AUTHORITY_ENV=~/.musubi/preflight-authority.env
ansible-playbook \
 -i deploy/ansible/inventory.yml \
 -e @~/.musubi-secrets/inventory-vars.yml \
 -e @~/.musubi-secrets/vault.yml \
 deploy/ansible/update.yml --ask-vault-pass
```

**Expected output:**

The reverted image digest pulls, `core` recreates with the old
digest, `/v1/ops/status` returns `ok`.

**Destructive:** yes — tears down the broken container.

**Rollback:** if the previous digest is ALSO broken, escalate: restore
from a Qdrant backup per [`../backup/README.md`](../backup/README.md)
and re-run `deploy.yml`.
