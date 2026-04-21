#!/usr/bin/env bash
#
# deploy/ansible/setup-control-host.sh
#
# One-shot bootstrap for a Musubi ansible control host (typically yua).
# Creates ~/.musubi-secrets/ with the inventory-vars + vault.yml templates
# and a local README. Safe to re-run — existing files are preserved.
#
# Usage:
#   deploy/ansible/setup-control-host.sh
#
# Override the secrets directory location with MUSUBI_SECRETS_DIR:
#   MUSUBI_SECRETS_DIR=/path/to/secrets deploy/ansible/setup-control-host.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ANSIBLE_DIR="$REPO_ROOT/deploy/ansible"
SECRETS_DIR="${MUSUBI_SECRETS_DIR:-$HOME/.musubi-secrets}"
VAULT_PASS_HINT="${ANSIBLE_VAULT_PASSWORD_FILE:-$HOME/ansible/.vault_pass}"

echo "=== Musubi ansible control-host bootstrap ==="
echo "Repo:            $REPO_ROOT"
echo "Secrets dir:     $SECRETS_DIR"
echo "Vault pass file: $VAULT_PASS_HINT (will be used at playbook runtime)"
echo

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

# --- inventory-vars.yml (non-secret operator overrides) -----------------------
INVENTORY_VARS="$SECRETS_DIR/inventory-vars.yml"
if [[ ! -f "$INVENTORY_VARS" ]]; then
  cat > "$INVENTORY_VARS" <<'YAML'
---
# Musubi ansible inventory overrides (operator-only, NOT encrypted).
#
# Passed at playbook runtime via `-e @~/.musubi-secrets/inventory-vars.yml`.
# Do NOT put secrets here; secrets go in the sibling `vault.yml`, which IS
# ansible-vault-encrypted.
#
# The committed deploy/ansible/inventory.yml is a parametrised template; these
# values fill its Jinja placeholders.

# ssh user on the Musubi workload host
operator_ssh_user: "ericmey"

# Musubi workload host (DNS name + VLAN IP)
musubi_host: ""   # e.g. musubi.mey.house
musubi_ip: ""     # e.g. 10.0.20.45

# Kong API gateway — only needed if/when Kong re-enters the deploy path
# (see docs/Musubi/13-decisions/0024-kong-deferred-for-musubi-v1.md).
# Leave empty for VLAN-internal v1 deploys.
kong_gateway: ""  # e.g. rin.mey.house
kong_ip: ""       # e.g. 10.0.20.50
YAML
  chmod 600 "$INVENTORY_VARS"
  echo "created  $INVENTORY_VARS"
  INVENTORY_VARS_CREATED=1
else
  echo "skipped  $INVENTORY_VARS (already exists)"
  INVENTORY_VARS_CREATED=0
fi

# --- vault.yml (encrypted secrets) --------------------------------------------
VAULT_YML="$SECRETS_DIR/vault.yml"
if [[ ! -f "$VAULT_YML" ]]; then
  cp "$ANSIBLE_DIR/vault.example.yml" "$VAULT_YML"
  chmod 600 "$VAULT_YML"
  echo "created  $VAULT_YML (from vault.example.yml — NOT YET ENCRYPTED)"
  VAULT_CREATED=1
else
  echo "skipped  $VAULT_YML (already exists)"
  VAULT_CREATED=0
fi

# --- README.md (local reference, doesn't replace the repo README) ------------
LOCAL_README="$SECRETS_DIR/README.md"
if [[ ! -f "$LOCAL_README" ]]; then
  cat > "$LOCAL_README" <<EOF
# Musubi operator secrets (this control host only)

Created by \`$ANSIBLE_DIR/setup-control-host.sh\` on $(date -u +%Y-%m-%dT%H:%M:%SZ).
Gitignored-equivalent: this directory is never part of any repo and must not
be committed anywhere.

## Files

- \`inventory-vars.yml\` — non-secret operator overrides (hostnames, IPs, ssh user).
- \`vault.yml\` — ansible-vault-encrypted secrets. See \`$ANSIBLE_DIR/vault.example.yml\`
  for the key list. Encrypt with:
  \`\`\`
  ansible-vault encrypt $VAULT_YML
  \`\`\`

## Running a playbook

\`\`\`bash
ANSIBLE_VAULT_PASSWORD_FILE=$VAULT_PASS_HINT \\
  ansible-playbook \\
  -i $ANSIBLE_DIR/inventory.yml \\
  -e @$INVENTORY_VARS \\
  -e @$VAULT_YML \\
  $ANSIBLE_DIR/<playbook>.yml
\`\`\`

Playbooks: \`bootstrap\`, \`config\`, \`deploy\`, \`health\`.

## Re-run

Re-running \`$ANSIBLE_DIR/setup-control-host.sh\` is safe — existing files are
preserved. Delete individual files and re-run to regenerate.
EOF
  chmod 600 "$LOCAL_README"
  echo "created  $LOCAL_README"
fi

echo
echo "=== Next steps ==="

if [[ "$INVENTORY_VARS_CREATED" -eq 1 ]]; then
  echo "1. Edit $INVENTORY_VARS — fill in musubi_host, musubi_ip, operator_ssh_user."
fi

if [[ "$VAULT_CREATED" -eq 1 ]]; then
  echo "2. Edit $VAULT_YML with real secret values (template from vault.example.yml)."
  echo "3. Encrypt it:"
  echo "     ansible-vault encrypt $VAULT_YML"
fi

if [[ "$INVENTORY_VARS_CREATED" -eq 0 && "$VAULT_CREATED" -eq 0 ]]; then
  echo "Nothing to do — both files already present. You can run playbooks now."
  echo "Syntax-check your current setup:"
  echo "  ansible-playbook -i $ANSIBLE_DIR/inventory.yml --syntax-check $ANSIBLE_DIR/bootstrap.yml"
fi

echo
echo "=== Per-deploy command ==="
echo "ANSIBLE_VAULT_PASSWORD_FILE=$VAULT_PASS_HINT \\"
echo "  ansible-playbook \\"
echo "  -i $ANSIBLE_DIR/inventory.yml \\"
echo "  -e @$INVENTORY_VARS \\"
echo "  -e @$VAULT_YML \\"
echo "  $ANSIBLE_DIR/<playbook>.yml"
echo
echo "Bootstrap complete."
