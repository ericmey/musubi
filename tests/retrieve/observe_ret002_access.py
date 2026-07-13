"""RET-002 / RET-008 — CURRENT-BEHAVIOUR OBSERVATIONS. Asserts nothing about desired.

Yua (router) required exactly this before any code:

    "Before code, prove exact current call graph and which dropped candidates bump...
     Add current-behavior red observations: deep episodic include_lineage=false must
     currently stay 0; concept/curated deep currently stay 0; blended dropped episodic
     candidate may increment. Accepted invariant remains explicit final-return
     accounting across planes/modes, independent of hydration."

Her source trace, which this file exists to CONFIRM against the live store:

    deep.py:161        hydrates ONLY when query.include_lineage
    _hydrate_one       calls EpisodicPlane.get(..., bump_access=True)   <- the default
    ConceptPlane.get   no marking
    CuratedPlane.get   no marking
    blended            runs deep per leg, THEN dedups + applies the final limit

So "was this memory used?" is currently answered as a **side effect of hydration** —
which means the answer depends on the plane, on whether lineage was requested, and on
whether the row survived a dedup step that runs *after* it was already marked.

This file prints what IS. It does not say what SHOULD BE. That is Yua's contract.

    python3 tests/retrieve/observe_ret002_access.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import FRESH_STATES, Fixture, Musubi, Store  # noqa: E402

ENV = Path.home() / ".musubi/musubi-mcp-aoi.env"
NS = "aoi/command-chair/episodic"

musubi = Musubi(ENV)
store = Store()
fix = Fixture(musubi, store, NS)

rows: list[tuple[str, str, str]] = []   # (observation, observed, note)


def line(label: str, observed: object, note: str = "") -> None:
    rows.append((label, str(observed), note))
    print(f"  {label:<52} {observed!s:<10} {note}")


print("=" * 96)
print("RET-002 / RET-008 — OBSERVED CURRENT BEHAVIOUR (no verdicts)")
print("=" * 96)
print()

# ── O1: does each MODE mark a returned hit? ──────────────────────────────────
print("O1  Does a mode mark a hit it RETURNED to the caller?")
for mode in ("fast", "blended", "deep"):
    oid, marker = fix.seed()
    before = store.observe(oid, "access_count").value
    hits = 0
    for _ in range(3):
        res = musubi.recall(NS, marker, mode=mode, limit=5, state_filter=FRESH_STATES)
        if any(r.get("object_id") == oid for r in res):
            hits += 1
    time.sleep(1.5)
    after = store.observe(oid, "access_count").value
    line(f"    mode={mode:<8} returned_to_caller={hits}/3", f"{before} -> {after}",
         "MARKS" if after != before else "DOES NOT MARK")
print()

# ── O2: deep WITHOUT lineage — Yua predicts no marking (hydration is skipped) ──
print("O2  deep with include_lineage=false (hydration skipped per deep.py:161)")
oid, marker = fix.seed()
before = store.observe(oid, "access_count").value
for _ in range(3):
    musubi._req("POST", "/retrieve", {                      # noqa: SLF001 - deliberate raw call
        "namespace": NS, "query_text": marker, "mode": "deep", "limit": 5,
        "state_filter": FRESH_STATES, "include_lineage": False,
    })
time.sleep(1.5)
after = store.observe(oid, "access_count").value
line("    deep include_lineage=false", f"{before} -> {after}",
     "MARKS" if after != before else "DOES NOT MARK (hydration skipped)")

print("    deep with include_lineage=true")
oid2, marker2 = fix.seed()
b2 = store.observe(oid2, "access_count").value
for _ in range(3):
    musubi._req("POST", "/retrieve", {                      # noqa: SLF001
        "namespace": NS, "query_text": marker2, "mode": "deep", "limit": 5,
        "state_filter": FRESH_STATES, "include_lineage": True,
    })
time.sleep(1.5)
a2 = store.observe(oid2, "access_count").value
line("    deep include_lineage=true", f"{b2} -> {a2}",
     "MARKS" if a2 != b2 else "DOES NOT MARK")
print()

# ── O3: does a DROPPED candidate get marked? (the over-marking question) ─────
# REISSUED CLEAN. My first version wrote 5 near-identical rows and called them 5
# memories. Yua: "'seeded=5' input calls is not five memories" — capture SEMANTICALLY
# DEDUPES, so those writes collapsed into fewer points and every delta was noise.
# seed_many() now PROVES 5 distinct rows, each version=1 / access=0 / reinforcement=0,
# or it refuses to run.
print("O3  Are candidates marked and then DROPPED before the caller sees them?")
oids, anchor = fix.seed_cohort(5)
print(f"    seeded {len(set(oids))} PROVEN-DISTINCT memories sharing anchor {anchor!r}")

before_all = {o: (store.observe(o, "access_count").value or 0) for o in oids}

LIMIT = 2
returned: set[str] = set()
for _ in range(2):
    res = musubi.recall(NS, anchor, mode="blended", limit=LIMIT, state_filter=FRESH_STATES)
    returned |= {r.get("object_id") for r in res if r.get("object_id")}
time.sleep(2.5)
after_all = {o: (store.observe(o, "access_count").value or 0) for o in oids}

seeded_returned = returned & set(oids)
marked = {o for o in oids if after_all[o] > before_all[o]}
if not seeded_returned:
    line("    *** PROBE VACUOUS", "0 fixtures returned", "cannot conclude anything")
else:
    line("    fixtures RETURNED to caller (limit=2)", len(seeded_returned), "")
    line("    fixtures MARKED", len(marked), "")
    line("    RETURNED and marked", len(seeded_returned & marked), "")
    line("    NOT returned but MARKED anyway", len(marked - seeded_returned),
         "*** OVER-MARKING ***" if (marked - seeded_returned) else "none")
    line("    RETURNED but NOT marked", len(seeded_returned - marked),
         "*** UNDER-MARKING ***" if (seeded_returned - marked) else "none")
print()

print("=" * 96)
print("These are observations. The invariant — explicit final-return accounting across")
print("planes and modes, independent of hydration — is Yua's contract, not this file's.")
print("=" * 96)
