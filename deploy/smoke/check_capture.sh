#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"
require_capture_namespace

content="musubi first-deploy smoke capture"
capture_payload="$(
  CONTENT="$content" python3 - <<'PY'
import json
import os

print(json.dumps({
    "namespace": os.environ["MUSUBI_NAMESPACE"],
    "content": os.environ["CONTENT"],
    "tags": ["smoke", "first-deploy"],
    "importance": 5,
}))
PY
)"
json_post "/v1/episodic" "$capture_payload" >/dev/null

retrieve_payload="$(
  CONTENT="$content" python3 - <<'PY'
import json
import os

print(json.dumps({
    "namespace": os.environ["MUSUBI_NAMESPACE"],
    "query_text": os.environ["CONTENT"],
    "mode": "fast",
    "limit": 1,
}))
PY
)"
retrieve_json="$(json_post "/v1/retrieve" "$retrieve_payload")"

if CONTENT="$content" RETRIEVE_JSON="$retrieve_json" python3 - <<'PY'
import json
import os
import sys

expected = os.environ["CONTENT"]
results = json.loads(os.environ["RETRIEVE_JSON"]).get("results", [])
sys.exit(0 if any(row.get("content") == expected for row in results) else 1)
PY
then
  pass "capture round trip"
else
  fail "capture round trip"
fi
