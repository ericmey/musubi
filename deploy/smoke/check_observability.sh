#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

metrics="$(json_get "/v1/ops/metrics")"

if METRICS="$metrics" python3 - <<'PY'
import os
import sys

text = os.environ["METRICS"]
sys.exit(0 if "# HELP " in text and "# TYPE " in text else 1)
PY
then
  pass "prometheus text"
else
  fail "prometheus text"
fi

if METRICS="$metrics" python3 - <<'PY'
import os
import re
import sys

families = set(re.findall(r"^# HELP ([a-zA-Z_:][a-zA-Z0-9_:]*) ", os.environ["METRICS"], re.M))
sys.exit(0 if len(families) >= 2 else 1)
PY
then
  pass "metric families"
else
  fail "metric families"
fi
