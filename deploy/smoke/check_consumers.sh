#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

phase="${MUSUBI_CONSUMER_PHASE:-post-deploy}"
checks=(
  "command-chair agents:MUSUBI_CONSUMER_COMMAND_CHAIR_CMD"
  "phone agents:MUSUBI_CONSUMER_PHONE_AGENTS_CMD"
  "OpenClaw on Nyla:MUSUBI_CONSUMER_OPENCLAW_NYLA_CMD"
  "Vice app:MUSUBI_CONSUMER_VICE_CMD"
)

failed=0
printf 'Musubi consumer regression smoke (%s)\n' "$phase"

is_placeholder_or_noop() {
  local cmd="$1"
  local trimmed="${cmd#"${cmd%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  [[ "$trimmed" == "true" || "$trimmed" == ":" ]] && return 0
  [[ "$trimmed" == *"<"* || "$trimmed" == *">"* ]]
}

for check in "${checks[@]}"; do
  name="${check%%:*}"
  var="${check##*:}"
  cmd="${!var:-}"
  if [[ -z "$cmd" ]]; then
    fail "consumer ${name}: ${var} is not set" || true
    failed=1
    continue
  fi
  if is_placeholder_or_noop "$cmd"; then
    fail "consumer ${name}: ${var} must be a real live-consumer command, not a placeholder/no-op" || true
    failed=1
    continue
  fi
  if bash -lc "$cmd"; then
    pass "consumer ${name}"
  else
    fail "consumer ${name}" || true
    failed=1
  fi
done

exit "$failed"
