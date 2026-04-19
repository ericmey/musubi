#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-/etc/musubi/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-/etc/musubi/.env.production}"
PROJECT_DIR="${PROJECT_DIR:-/etc/musubi}"

cd "$PROJECT_DIR"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --wait --timeout 300

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps --format json |
  python3 -c 'import json, sys
rows = [json.loads(line) for line in sys.stdin if line.strip()]
unhealthy = [
    row.get("Service") or row.get("Name")
    for row in rows
    if row.get("Health") not in {"healthy", ""}
]
if unhealthy:
    raise SystemExit("unhealthy services: " + ", ".join(unhealthy))
'

curl -fsS http://127.0.0.1:8100/v1/ops/health >/dev/null
