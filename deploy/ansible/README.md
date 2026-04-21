# Musubi Ansible

This directory contains the Musubi first-deploy Ansible scaffold. The repo is
the source of truth for playbooks + roles + templates. Operator-only state
(real hostnames, encrypted secrets) lives **outside** any git checkout — see
the control-host setup below.

## Control-host model

Playbooks are run from an ansible control host. In this environment that's
`yua` (`10.0.0.53`) alongside the homelab fleet's own `~/ansible/` repo. The
control host:

- Clones this repo to `~/musubi` (fresh `git pull` before each deploy).
- Keeps Musubi-specific secrets + inventory overrides under
  `~/.musubi-secrets/` (gitignored directory, 700-perm).
- Reuses `~/ansible/.vault_pass` as the ansible-vault password file so the
  Musubi vault and the homelab fleet vault share one secret-management story.

Running playbooks from this repo's working tree on a developer laptop is
possible for `--syntax-check` and `--check --diff` dry-runs, but the
operational path is always yua → playbook → musubi workload host.

## Layout

| Path                              | Role                                                           |
|-----------------------------------|----------------------------------------------------------------|
| `inventory.yml`                   | Parametrised inventory — Jinja vars for hostnames, IPs, users. |
| `group_vars/all.yml`              | Defaults: filesystem paths, image pins, health URLs, Ollama model. |
| `bootstrap.yml`                   | Fresh Ubuntu 24.04 → Docker + NVIDIA + users + dirs + firewall. |
| `deploy.yml`                      | Compose stack bring-up (pull images, start, health gate).      |
| `config.yml`                      | Refresh `.env.production`, restart stack on config change.     |
| `health.yml`                      | Ad-hoc host + Docker + Musubi + Core + Ollama checks.          |
| `vault.example.yml`               | Template for `~/.musubi-secrets/vault.yml`.                    |
| `setup-control-host.sh`           | One-shot bootstrap: creates `~/.musubi-secrets/` + seeds files. |
| `requirements.yml`                | Ansible Galaxy collection deps.                                |

## First-time control-host bootstrap (on yua)

```bash
ssh yua
git clone git@github.com:ericmey/musubi.git ~/musubi
cd ~/musubi
ansible-galaxy collection install -r deploy/ansible/requirements.yml
deploy/ansible/setup-control-host.sh
```

The script creates `~/.musubi-secrets/` with two templated files
(`inventory-vars.yml`, `vault.yml`) and a local README. It's safe to re-run;
existing files are preserved.

After it prints the next-steps banner:

1. Edit `~/.musubi-secrets/inventory-vars.yml` — fill in `musubi_host`,
   `musubi_ip`, `operator_ssh_user` (and Kong vars if/when Kong is
   re-enabled per [ADR 0024](../../docs/Musubi/13-decisions/0024-kong-deferred-for-musubi-v1.md)).
2. Edit `~/.musubi-secrets/vault.yml` with real secret values (see
   `vault.example.yml` for the key list).
3. Encrypt it:
   ```bash
   ansible-vault encrypt ~/.musubi-secrets/vault.yml
   ```

## Per-deploy workflow (on yua)

```bash
cd ~/musubi && git pull --ff-only

ANSIBLE_VAULT_PASSWORD_FILE=~/ansible/.vault_pass \
  ansible-playbook \
  -i deploy/ansible/inventory.yml \
  -e @~/.musubi-secrets/inventory-vars.yml \
  -e @~/.musubi-secrets/vault.yml \
  deploy/ansible/<playbook>.yml
```

Where `<playbook>` is one of `bootstrap`, `config`, `deploy`, `health`.

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
vault — they must run from yua.

## Why this split

- **Repo = single source of truth.** Playbook edits go through PR review.
- **Secrets live outside any git clone.** `~/.musubi-secrets/` survives
  `rm -rf ~/musubi && git clone` and can't be accidentally `git add`ed.
- **One vault password across the homelab.** Reusing `~/ansible/.vault_pass`
  keeps a single ansible-vault story, not two.
- **The committed inventory is a valid template**, not a file that has to
  be hand-patched before use. Running it unparameterised fails fast with a
  clear Jinja undefined-variable error.

## Boundaries

This directory ships the host-level Ansible scaffold. Compose service
ownership, backup automation, and observability each have their own slices;
see [`docs/Musubi/_slices/`](../../docs/Musubi/_slices/) for the canonical
list.
