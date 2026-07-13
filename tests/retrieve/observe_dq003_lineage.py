"""DQ-003 — lineage/supersession chain observation, per layer.

Was HYPOTHESIS: Tama correctly stopped at a 403 (no operator credential existed). Eric
authorized minting an operator token 2026-07-12; it is verified accepted by the server
(operator -> 422 body-validation, normal command-chair token -> 403). So this observation
is now runnable against the deployed plane, in an ISOLATED probe namespace, cleaned up
through the authorized path.

Question: when memory B SUPERSEDES memory A, what does each layer show?

  * raw Qdrant payload of A (state, superseded_by)
  * ranked recall of A with default state_filter  (is a superseded memory hidden?)
  * ranked recall of A including archive-side states (is the chain reachable at all?)
  * the returned row's lineage fields (can a caller SEE that A was superseded, and by what?)

No verdicts. Whether a superseded memory should be hidden, and what lineage the wire must
expose, is Yua's contract.

    MUSUBI_OPERATOR_ENV=~/.musubi/musubi-mcp-aoi-operator.env \\
        python3 tests/retrieve/observe_dq003_lineage.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import ALL_STATES, FRESH_STATES, Fixture, Musubi, Store

ENV = Path.home() / ".musubi/musubi-mcp-aoi.env"
OP_ENV = Path(
    os.environ.get("MUSUBI_OPERATOR_ENV", str(Path.home() / ".musubi/musubi-mcp-aoi-operator.env"))
)
NS = "aoi/command-chair/episodic"

musubi = Musubi(ENV)
store = Store()
fix = Fixture(musubi, store, NS)


def _op_env() -> tuple[str, str]:
    s = OP_ENV.read_text()
    import re

    m_url = re.search(r"(?m)^MUSUBI_API_URL=(.+)$", s)
    m_tok = re.search(r"(?m)^MUSUBI_TOKEN=(.+)$", s)
    if m_url is None or m_tok is None:
        raise RuntimeError(f"MUSUBI_API_URL / MUSUBI_TOKEN not found in {OP_ENV}")
    url = m_url.group(1).strip().rstrip("/")
    tok = m_tok.group(1).strip()
    return url, tok


def transition(
    object_id: str,
    to_state: str,
    *,
    actor: str,
    reason: str,
    supersedes: list[str] | None = None,
    superseded_by: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Operator-scoped lifecycle transition. Returns (status, body).

    NOTE: superseded_by is a REQUEST field the caller must set (writes_lifecycle.py:23).
    My first DQ-003 script never passed it, then reported A.superseded_by=None as a
    "broken one-directional edge." Yua caught it: that was the mutation I DIDN'T request,
    not proof the linkage failed. Reciprocity is caller-owned here, not automatic.
    """
    url, tok = _op_env()
    body = {
        "object_id": object_id,
        "to_state": to_state,
        "actor": actor,
        "reason": reason,
        "supersedes": supersedes or [],
    }
    if superseded_by is not None:
        body["superseded_by"] = superseded_by
    req = urllib.request.Request(
        f"{url}/lifecycle/transition", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}


def line(label: str, value: object, note: str = "") -> None:
    print(f"  {label:<52} {value!s:<14} {note}")


print("=" * 98)
print("DQ-003 — SUPERSESSION LINEAGE, PER LAYER (observations only)")
print("=" * 98)
print()

if not OP_ENV.exists():
    print(f"  operator env not found at {OP_ENV} — DQ-003 remains blocked (no operator")
    print("  credential). This is a PERMISSION boundary, not a failed test.")
    sys.exit(0)

# ── seed A and B, distinct and proven-new ────────────────────────────────────
a_oid, a_mark = fix.seed(importance=8)
b_oid, b_mark = fix.seed(importance=8)
line("seeded A", a_oid[:12], f"marker={a_mark}")
line("seeded B", b_oid[:12], f"marker={b_mark}")
print()

# ── B supersedes A (operator transition) ─────────────────────────────────────
# The state machine requires provisional -> matured -> superseded; you cannot jump
# straight to superseded from provisional (observed 2026-07-12: 400 "not permitted;
# allowed from provisional: ['archived','matured']"). Mature A first, THEN supersede it.
print("Transition: A provisional -> matured -> superseded; B supersedes A")
m_status, _ = transition(
    a_oid, "matured", actor="aoi/operator", reason="DQ-003: mature A before supersession"
)
line("A -> matured (required first hop)", m_status)
time.sleep(1)
# A -> superseded AND explicitly point A.superseded_by = B (the field I omitted before)
status, body = transition(
    a_oid,
    "superseded",
    actor="aoi/operator",
    reason="DQ-003 lineage observation",
    superseded_by=b_oid,
)
# B records the forward edge B.supersedes = [A]
status_b, body_b = transition(
    b_oid,
    "matured",
    actor="aoi/operator",
    reason="DQ-003 lineage observation: B supersedes A",
    supersedes=[a_oid],
)
line("A -> superseded", status, body.get("to_state", body.get("error", "")))
line(
    "B supersedes A",
    status_b,
    (body_b.get("supersedes") if isinstance(body_b, dict) else body_b) or body_b.get("error", ""),
)
time.sleep(2)
print()

# ── L1: raw store ────────────────────────────────────────────────────────────
print("L1  Raw Qdrant payload")
pa = store.payload(a_oid) or {}
pb = store.payload(b_oid) or {}
line("    A.state", pa.get("state"))
line(
    "    A.superseded_by (we requested = B)",
    pa.get("superseded_by"),
    "reciprocal edge set"
    if pa.get("superseded_by") == b_oid
    else "NOT persisted despite being requested"
    if not pa.get("superseded_by")
    else f"set to {pa.get('superseded_by')}",
)
line("    B.supersedes", pb.get("supersedes"))
print()

# ── L2: default recall — is a superseded memory hidden? ──────────────────────
print("L2  Ranked recall (default fresh states) — is superseded A hidden?")
res_default = musubi.recall(NS, a_mark, mode="blended", limit=5, state_filter=FRESH_STATES)
a_in_default = any(r.get("object_id") == a_oid for r in res_default)
line(
    "    A returned under FRESH_STATES?",
    a_in_default,
    "hidden from default recall" if not a_in_default else "STILL VISIBLE though superseded",
)

res_all = musubi.recall(NS, a_mark, mode="blended", limit=5, state_filter=ALL_STATES)
a_in_all = any(r.get("object_id") == a_oid for r in res_all)
line(
    "    A returned including archive states?",
    a_in_all,
    "reachable for lineage" if a_in_all else "unreachable even with archive states",
)
print()

# ── L3: can a CALLER see the lineage edge? ───────────────────────────────────
print("L3  Does the returned row expose the lineage edge to the caller?")
row_a = next((r for r in res_all if r.get("object_id") == a_oid), None)
if row_a:
    # HTTP puts lineage at extra.lineage (orchestration.py:518 _summarize_lineage),
    # NOT at top level. My first probe checked top-level keys and wrongly concluded
    # "no lineage field." Inspect the nested structure Yua pointed to.
    extra = row_a.get("extra") or {}
    nested = extra.get("lineage")
    line(
        "    row.extra.lineage present?",
        nested is not None,
        "" if nested is not None else "no lineage in extra",
    )
    if isinstance(nested, dict):
        line("    extra.lineage.superseded_by", nested.get("superseded_by"))
        line("    extra.lineage.supersedes", nested.get("supersedes"))
    line("    top-level row keys", ",".join(sorted(row_a.keys())))
    line("    extra keys", ",".join(sorted(extra.keys())))
else:
    line("    A not retrievable at all", "-", "lineage unobservable from the wire")
print()

# ── cleanup: return A and B to a benign state via the authorized path ────────
print("Cleanup (authorized transition, not deletion):")
c1, _ = transition(a_oid, "archived", actor="aoi/operator", reason="DQ-003 probe cleanup")
transition(b_oid, "matured", actor="aoi/operator", reason="cleanup hop")
time.sleep(0.5)
c2, _ = transition(b_oid, "archived", actor="aoi/operator", reason="DQ-003 probe cleanup")
line("    A -> archived", c1)
line("    B -> archived", c2)

print()
print("=" * 98)
print("Observed only. Hide-superseded policy and required wire lineage are Yua's contract.")
print("=" * 98)
