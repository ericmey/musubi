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

# Unauthenticated GET for the PUBLIC ops surface (/v1/ops/health|status|metrics).
#
# REQ-8 (Issue #413) makes a PRESENTED-but-invalid bearer a 401 on every route,
# public ones included. Sending a credential to a health probe therefore couples
# two independent facts: "is the service healthy" and "is my ops token still
# valid." A stale or rotated MUSUBI_TOKEN would fail the health check and read as
# an outage, which is alert noise pointing at the wrong system.
#
# So the public surface is probed with NO credential — absent stays public, which
# is REQ-8 control 1 and unchanged by this work.
#
# Deliberately NOT dropping AUTH_ARGS globally (Yua's ruling, 2026-08-03): the
# PROTECTED smoke calls must keep the bearer, so a stale credential still fails
# the auth portion of smoke loudly instead of masquerading as service-health.
public_get() {
  local path="$1"
  curl -fsS "${MUSUBI_BASE_URL}${path}"
}
