# Musubi Ansible

This directory contains the Musubi first-deploy Ansible scaffold. The repo is
the source of truth for playbooks + roles + templates. Operator-only state
(real hostnames, encrypted secrets) lives **outside** any git checkout — see
the control-host setup below.

## Control-host model

Playbooks run from an Ansible control host. The control host:

- Clones this repo to `~/musubi` (fresh `git pull` before each deploy).
- Keeps Musubi-specific secrets + inventory overrides under
 `~/.musubi-secrets/` (gitignored directory, 700-perm).
- Reads the ansible-vault password file named by `ANSIBLE_VAULT_PASSWORD_FILE`.
  Export it once in the control host's shell profile (for example
  `export ANSIBLE_VAULT_PASSWORD_FILE=~/.ansible_vault_pass`, mode `0600`);
  every command, runbook and script here uses that variable.

Running playbooks from this repo's working tree on a developer laptop is
possible for `--syntax-check` and `--check --diff` dry-runs, but the
operational path is always the ansible control host → playbook → musubi workload host.

## Layout

| Path | Role |
|-----------------------------------|----------------------------------------------------------------|
| `inventory.yml` | Parametrised inventory — Jinja vars for hostnames, IPs, users. |
| `group_vars/all.yml` | Defaults: filesystem paths, image pins, health URLs, Ollama model. |
| `bootstrap.yml` | Fresh Ubuntu 24.04 → Docker + NVIDIA + users + dirs + firewall. |
| `deploy.yml` | Compose stack bring-up (pull images, start, health gate). |
| `config.yml` | Refresh `.env.production`, restart stack on config change. |
| `health.yml` | Ad-hoc host + Docker + Musubi + Core + Ollama checks. |
| `vault.example.yml` | Template for `~/.musubi-secrets/vault.yml`. |
| `setup-control-host.sh` | One-shot bootstrap: creates `~/.musubi-secrets/` + seeds files. |
| `requirements.yml` | Ansible Galaxy collection deps. |

## First-time control-host bootstrap (on the ansible control host)

```bash
ssh <ansible-host>
git clone git@github.com:sourceblender/musubi.git ~/musubi
cd ~/musubi
ansible-galaxy collection install -r deploy/ansible/requirements.yml
deploy/ansible/setup-control-host.sh
```

The script creates `~/.musubi-secrets/` with two templated files
(`inventory-vars.yml`, `vault.yml`) and a local README. It's safe to re-run;
existing files are preserved.

After it prints the next-steps banner:

1. Edit `~/.musubi-secrets/inventory-vars.yml` — fill in `musubi_host`,
 `musubi_ip`, `musubi_inference_hostname`, `operator_ssh_user` (and Kong vars if/when Kong is
 re-enabled per [ADR 0024](../../docs/Musubi/13-decisions/0024-kong-deferred-for-musubi-v1.md)).
 Set `musubi_otel_otlp_endpoint` and `musubi_prometheus_remote_write_url` only
 if this deployment sends telemetry to an external collector. Set
 `musubi_deployment_environment` to the label you want on metrics.
2. Edit `~/.musubi-secrets/vault.yml` with real secret values (see
 `vault.example.yml` for the key list).
3. Encrypt it:
 ```bash
 ansible-vault encrypt ~/.musubi-secrets/vault.yml
 ```

## Per-deploy workflow (on the ansible control host)

```bash
cd ~/musubi && git pull --ff-only

# ANSIBLE_VAULT_PASSWORD_FILE is already exported (see above).
ansible-playbook \
 -i deploy/ansible/inventory.yml \
 -e @~/.musubi-secrets/inventory-vars.yml \
 -e @~/.musubi-secrets/vault.yml \
 deploy/ansible/<playbook>.yml
```

Where `<playbook>` is one of `bootstrap`, `config`, `deploy`, `update`, or
`health`.

`update.yml` Core/lifecycle-worker applies additionally require an explicit
minimal validator environment and operator-owned credential inventory on the
control host:

```bash
MUSUBI_PREFLIGHT_AUTHORITY_ENV=~/.musubi/preflight-authority.env \
  MUSUBI_PREFLIGHT_MANIFEST=/absolute/path/to/operator-manifest.json \
  scripts/musubi-deploy --apply core,lifecycle-worker
```

That root-readable env contains exactly one `JWT_SIGNING_KEY` and one
`OAUTH_AUTHORITY`. The manifest lists the operator's live credentials; use
`deploy/credential-preflight.example.json` as a schema example and keep the
filled inventory outside the repository. Before mounting these files or the credential directory, the
playbook verifies the exact digest's cosign identity. The candidate rejects
unknown or duplicate authority keys, runs as the controller UID/GID, inventories
every `musubi-mcp*.env`, and fails before touching the workload host if any file
is unclassified, ambiguous, missing, or rejected.

Dry-run first whenever possible:

```bash
... -e ... deploy/ansible/bootstrap.yml --check --diff
```

## Developer-laptop dry-run (limited)

From a developer's local clone (without access to the real vault.yml),
syntax-check and non-sensitive dry-runs still work:

```bash
ansible-playbook -i deploy/ansible/inventory.yml --syntax-check deploy/ansible/bootstrap.yml

# health.yml without vault, targeting a resolved musubi_host:
ansible-playbook \
 -i deploy/ansible/inventory.yml \
 -e musubi_host=musubi.example.local -e musubi_ip=10.0.0.45 \
 -e ansible_become=false \
 --check --diff \
 deploy/ansible/health.yml
```

The real `bootstrap.yml`, `config.yml`, and `deploy.yml` need the encrypted
vault — they must run from the ansible control host.

## Runtime secrets (1Password Connect)

The JWT signing key and Qdrant API key are not Ansible-vault variables and are
never rendered into `.env.production` or a persistent token file. The workload
host must have the 1Password CLI at `/usr/bin/op` and a root-owned `0600`
`/etc/musubi/connect.env` containing the Connect endpoint and read-only token.
Every runtime playbook fails closed when that file is absent or has the wrong
owner or mode.

The committed templates contain references, not credential values:

- `secrets.tpl` is resolved by `op run` into the Compose process environment.
- `qdrant.token.tpl` is resolved by `op inject` into
  `/run/musubi-secrets/qdrant.token` on each systemd start.
- `musubi.service` owns the render-before-start ordering and removes the runtime
  directory automatically when the unit stops.

Provision `connect.env` through the fleet's root-only host bootstrap path before
running Musubi bootstrap. Do not add the Connect token to inventory variables,
Ansible vault, command output, diffs, or this repository. Updating either
1Password item takes effect only after `systemctl restart musubi`.

## Why this split

- **Repo = single source of truth.** Playbook edits go through PR review.
- **Secrets live outside any git clone.** `~/.musubi-secrets/` survives
 `rm -rf ~/musubi && git clone` and can't be accidentally `git add`ed.
- **One vault password file, named once.** `ANSIBLE_VAULT_PASSWORD_FILE` is the
  only place its path appears, so commands and scripts can't disagree about it.
- **The committed inventory is a valid template**, not a file that has to
 be hand-patched before use. Running it unparameterised fails fast with a
 clear Jinja undefined-variable error.

## Boundaries

This directory ships the host-level Ansible scaffold. Compose services,
backup automation and observability are documented under
[`docs/Musubi/08-deployment/`](../../docs/Musubi/08-deployment/) and
[`docs/Musubi/09-operations/`](../../docs/Musubi/09-operations/).
