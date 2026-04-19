# Musubi Ansible

This directory contains the first-deploy Ansible scaffold for the Musubi workload host. It uses committed placeholder hosts only; real hostnames, IPs, SSH users, and secrets stay in `.agent-context.local.md` or an encrypted `vault.yml`.

## Layout

- `inventory.yml` defines an Ansible control host placeholder and a Musubi workload host placeholder.
- `group_vars/all.yml` holds safe defaults, filesystem paths, image pins, health URLs, and the Phase 1 Qwen 3 4B GPU placement knobs.
- `bootstrap.yml` prepares a fresh Ubuntu 24.04 host: packages, Docker, NVIDIA runtime, `musubi` user, data directories, firewall, templates, and systemd unit.
- `deploy.yml` refreshes the compose anchor, pulls missing pinned images, starts the stack, and checks Core health.
- `config.yml` refreshes `.env.production` and restarts the stack when runtime config changes.
- `health.yml` runs ad-hoc host, Docker, Musubi, Core, and Ollama checks.
- `vault.example.yml` documents secret keys. Copy it to `vault.yml`, fill real values, and encrypt it before use.

## First Use

Install collections:

```bash
ansible-galaxy collection install -r deploy/ansible/requirements.yml
```

Create encrypted secrets:

```bash
cp deploy/ansible/vault.example.yml deploy/ansible/vault.yml
ansible-vault encrypt deploy/ansible/vault.yml
```

Override placeholders outside git, then check syntax:

```bash
ansible-playbook -i deploy/ansible/inventory.yml deploy/ansible/bootstrap.yml --syntax-check
ansible-playbook -i deploy/ansible/inventory.yml deploy/ansible/deploy.yml --syntax-check
ansible-playbook -i deploy/ansible/inventory.yml deploy/ansible/config.yml --syntax-check
ansible-playbook -i deploy/ansible/inventory.yml deploy/ansible/health.yml --syntax-check
```

Dry-run the bootstrap before the first apply:

```bash
ansible-playbook -i deploy/ansible/inventory.yml deploy/ansible/bootstrap.yml --check --diff -e @deploy/ansible/vault.yml --ask-vault-pass
```

## Boundaries

This slice provides the host-level scaffold and a compose template anchor only. Detailed Compose service ownership, backup automation, and observability stacks belong to the downstream ops slices.
