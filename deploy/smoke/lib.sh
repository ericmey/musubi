#!/usr/bin/env bash
set -euo pipefail

MUSUBI_BASE_URL="${MUSUBI_BASE_URL:-http://127.0.0.1:8100}"
MUSUBI_TOKEN="${MUSUBI_TOKEN:-}"
MUSUBI_NAMESPACE="${MUSUBI_NAMESPACE:-eric/ops/episodic}"
MUSUBI_THOUGHT_NAMESPACE="${MUSUBI_THOUGHT_NAMESPACE:-eric/ops/thought}"
MUSUBI_PRESENCE="${MUSUBI_PRESENCE:-eric/ops-smoke}"

AUTH_ARGS=()
if [[ -n "$MUSUBI_TOKEN" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${MUSUBI_TOKEN}")
fi

pass() {
  printf '[PASS] %s\n' "$1"
}

fail() {
  printf '[FAIL] %s\n' "$1"
  return 1
}

json_post() {
  local path="$1"
  local payload="$2"
  curl -fsS "${AUTH_ARGS[@]}" -H "Content-Type: application/json" \
    -X POST "${MUSUBI_BASE_URL}${path}" --data "$payload"
}

json_get() {
  local path="$1"
  curl -fsS "${AUTH_ARGS[@]}" "${MUSUBI_BASE_URL}${path}"
}
