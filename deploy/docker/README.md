# Musubi Docker Stack

This directory contains deploy-time Docker artifacts for the Musubi host and
the Kong gateway configuration that fronts it.

## Files

- `../../docker-compose.yml` is the canonical Compose stack rendered to
  `/etc/musubi/docker-compose.yml` by Ansible.
- `.env.production.example` documents the environment keys expected by Core.
- `kong.yml` is a placeholder-safe decK-style Kong configuration for the
  Musubi routes.
- `smoke-health.sh` is the warm-cache health smoke used after deploys and
  restore drills.

## Operator Flow

```bash
cd /etc/musubi
docker compose --env-file .env.production config --quiet
docker compose --env-file .env.production up -d
/path/to/smoke-health.sh
```

Only Core publishes a host port. Qdrant, TEI, and Ollama remain on the
`musubi-net` bridge network and are reached by service-name DNS from Core.
Kong runs outside this stack and routes to Core through the host-local Core
port.
