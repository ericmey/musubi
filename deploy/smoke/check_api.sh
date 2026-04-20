#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

json_get "/v1/ops/health" >/dev/null && pass "api health" || fail "api health"

status_json="$(json_get "/v1/ops/status")"
STATUS_JSON="$status_json" python3 - <<'PY'
import json
import os
import sys

status = json.loads(os.environ["STATUS_JSON"])
components = status.get("components", {})
required = ("qdrant", "tei-dense", "tei-sparse", "tei-reranker", "ollama")
ok = True
for name in required:
    healthy = bool(components.get(name, {}).get("healthy"))
    if healthy:
        print(f"[PASS] component {name}")
    else:
        print(f"[FAIL] component {name}")
        ok = False
sys.exit(0 if ok else 1)
PY
