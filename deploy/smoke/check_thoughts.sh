#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

content="musubi first-deploy smoke thought"
send_payload="$(
  CONTENT="$content" python3 - <<'PY'
import json
import os

presence = os.environ.get("MUSUBI_PRESENCE", "eric/ops-smoke")
print(json.dumps({
    "namespace": os.environ.get("MUSUBI_THOUGHT_NAMESPACE", "eric/ops/thought"),
    "from_presence": presence,
    "to_presence": presence,
    "content": os.environ["CONTENT"],
    "channel": "smoke",
    "importance": 5,
}))
PY
)"
json_post "/v1/thoughts/send" "$send_payload" >/dev/null

check_payload="$(
  python3 - <<'PY'
import json
import os

print(json.dumps({
    "namespace": os.environ.get("MUSUBI_THOUGHT_NAMESPACE", "eric/ops/thought"),
    "presence": os.environ.get("MUSUBI_PRESENCE", "eric/ops-smoke"),
}))
PY
)"
check_json="$(json_post "/v1/thoughts/check" "$check_payload")"

if CONTENT="$content" CHECK_JSON="$check_json" python3 - <<'PY'
import json
import os
import sys

expected = os.environ["CONTENT"]
items = json.loads(os.environ["CHECK_JSON"]).get("items", [])
sys.exit(0 if any(item.get("content") == expected for item in items) else 1)
PY
then
  pass "thoughts round trip"
else
  fail "thoughts round trip"
fi
