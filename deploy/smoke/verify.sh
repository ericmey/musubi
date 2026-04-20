#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_DIR="${SMOKE_CHECK_DIR:-$SCRIPT_DIR}"
checks=(
  check_api.sh
  check_capture.sh
  check_thoughts.sh
  check_observability.sh
)

failed=0
for check in "${checks[@]}"; do
  if bash "${CHECK_DIR}/${check}"; then
    :
  else
    failed=1
  fi
done

exit "$failed"
