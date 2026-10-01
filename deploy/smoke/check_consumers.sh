#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

phase="${MUSUBI_CONSUMER_PHASE:-post-deploy}"
checks_file="${MUSUBI_CONSUMER_CHECKS_FILE:-}"
failed=0
count=0
printf 'Musubi consumer regression smoke (%s)\n' "$phase"

if [[ -z "$checks_file" ]]; then
  fail "MUSUBI_CONSUMER_CHECKS_FILE is not set" || true
  exit 1
fi
if [[ ! -f "$checks_file" || ! -r "$checks_file" ]]; then
  fail "MUSUBI_CONSUMER_CHECKS_FILE is not a readable file: $checks_file" || true
  exit 1
fi

is_placeholder_or_noop() {
  local cmd="$1"
  local trimmed="${cmd#"${cmd%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  [[ "$trimmed" == "true" || "$trimmed" == ":" ]] && return 0
  [[ "$trimmed" == *"<"* || "$trimmed" == *">"* ]]
}

while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%$'\r'}"
  [[ -z "$line" || "$line" == \#* ]] && continue
  if [[ "$line" != *$'\t'* ]]; then
    fail "consumer check row needs label<TAB>command" || true
    failed=1
    continue
  fi
  name="${line%%$'\t'*}"
  cmd="${line#*$'\t'}"
  if [[ -z "$name" || -z "$cmd" || "$cmd" == *$'\t'* ]]; then
    fail "consumer check row needs one label and one command" || true
    failed=1
    continue
  fi
  count=$((count + 1))
  if is_placeholder_or_noop "$cmd"; then
    fail "consumer ${name}: command must be a real live-consumer command, not a placeholder/no-op" || true
    failed=1
    continue
  fi
  if bash -lc "$cmd"; then
    pass "consumer ${name}"
  else
    fail "consumer ${name}" || true
    failed=1
  fi
done < "$checks_file"

if [[ "$count" -eq 0 ]]; then
  fail "no consumer checks declared in $checks_file" || true
  failed=1
fi

exit "$failed"
