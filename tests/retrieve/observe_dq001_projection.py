"""DQ-001 — CURRENT-BEHAVIOUR OBSERVATIONS: what a caller actually RECEIVES.

Yua (router), reproduced by source:

    "ranked/recent projection is content-or-title first 300 chars; ignores summary even
     when present; HTTP exposes neither summary nor include_payload/brief opt-in. Docs
     promise episodic summary auto-generated in maturation and re-embed for long content,
     implementation absent.

     Invariant: no silent loss of load-bearing content; returned row must declare
     truncation and full length, surface supplied/generated bounded summary, and provide
     authorized full-content continuation by object ID."

MY MISS. I observed "every result is exactly 300 characters" hours before she filed this,
saw a GET return the full text, concluded "no truncation," and moved on. The GET was not
the surface anybody recalls through.

What this file measures: **the gap between what is STORED and what is DELIVERED.**

A memory whose load-bearing clause lives past character 300 — "...and the settled decision
is: do NOT relitigate this" — is a memory that, at recall time, says the opposite of what
it means. That is not lossy compression. That is a memory that lies.

No verdicts here. Budgets, summary policy and the API contract are Yua's.

    python3 tests/retrieve/observe_dq001_projection.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import FRESH_STATES, Fixture, Musubi, Store  # noqa: E402

ENV = Path.home() / ".musubi/musubi-mcp-aoi.env"
NS = "aoi/command-chair/lifecycle"        # lifecycle plane: probes, not real memories
NS_RECALL = "aoi/command-chair/episodic"  # ranked recall needs a retrievable plane

musubi = Musubi(ENV)
store = Store()
fix = Fixture(musubi, store, NS_RECALL)


def line(label: str, value: object, note: str = "") -> None:
    print(f"  {label:<50} {value!s:<12} {note}")


print("=" * 98)
print("DQ-001 — WHAT IS STORED vs WHAT IS DELIVERED (observations only)")
print("=" * 98)
print()

# ── O1: a memory whose POINT is past character 300 ───────────────────────────
print("O1  A memory whose load-bearing clause lives PAST character 300")
print("    (this is the shape of every real lesson, retro and decision we write)")

PAD = ("Context that matters but is not the conclusion. " * 8)  # ~380 chars of preamble
POINT = "THE SETTLED DECISION IS: DO NOT RELITIGATE THIS. The opposite is false."
marker = f"dq{int(time.time())}"
content = f"{marker}. {PAD} {POINT}"
oid = musubi.write(NS_RECALL, content, importance=9)
time.sleep(3)

stored = (store.payload(oid) or {}).get("content") or ""
line("    stored length (raw Qdrant payload)", len(stored))
line("    the point is present in the STORE?", POINT in stored)

res = musubi.recall(NS_RECALL, marker, mode="blended", limit=5, state_filter=FRESH_STATES)
row = next((r for r in res if r.get("object_id") == oid), None)
if row is None:
    line("    *** the memory was not returned at all", "-", "cannot observe projection")
else:
    delivered = row.get("content") or ""
    line("    DELIVERED length (what the caller sees)", len(delivered))
    line("    the point survives DELIVERY?", POINT in delivered,
         "" if POINT in delivered else "*** THE CONCLUSION IS GONE ***")
    line("    does the row DECLARE it was truncated?",
         any(k in row for k in ("truncated", "full_length", "content_length")),
         "no field says so" if not any(k in row for k in ("truncated", "full_length")) else "")
    line("    row keys returned to the caller", ",".join(sorted(row.keys())))
    print()
    print(f"    delivered ends: ...{delivered[-70:]!r}")
    print(f"    the caller NEVER SEES:  {stored[len(delivered):len(delivered)+80]!r}")
print()

# ── O2: is a SUPPLIED summary honoured? ──────────────────────────────────────
print("O2  If the writer supplies a summary, does recall use it?")
marker2 = f"dqs{int(time.time())}"
body = f"{marker2}. " + ("Filler that buries the lede. " * 14) + " FINAL CLAUSE: the answer is 42."
oid2 = musubi._req("POST", "/episodic", {                    # noqa: SLF001 - raw, deliberate
    "namespace": NS_RECALL, "content": body,
    "summary": "SUPPLIED SUMMARY: the answer is 42.",
    "tags": ["kind:episode", "staleness:episodic"], "importance": 8,
}).get("object_id")
time.sleep(3)
pl2 = store.payload(oid2) or {}
line("    summary accepted and STORED?", "summary" in pl2,
     f"stored={pl2.get('summary')!r}" if "summary" in pl2 else "the field is not persisted")
res2 = musubi.recall(NS_RECALL, marker2, mode="blended", limit=5, state_filter=FRESH_STATES)
row2 = next((r for r in res2 if r.get("object_id") == oid2), None)
if row2:
    line("    summary DELIVERED to the caller?", "summary" in row2,
         "recall ignores it" if "summary" not in row2 else "")
    line("    'the answer is 42' survives delivery?", "42" in (row2.get("content") or ""))
print()

# ── O3: how far past the cutoff can a memory go? ─────────────────────────────
print("O3  A large memory (the shape of a retro or a session close)")
big_marker = f"dqb{int(time.time())}"
big = f"{big_marker}. " + ("Paragraph of substance. " * 400) + " TAIL MARKER PRESENT."
oid3 = musubi.write(NS_RECALL, big, importance=7)
time.sleep(3)
stored3 = (store.payload(oid3) or {}).get("content") or ""
line("    stored length", len(stored3))
res3 = musubi.recall(NS_RECALL, big_marker, mode="blended", limit=5, state_filter=FRESH_STATES)
row3 = next((r for r in res3 if r.get("object_id") == oid3), None)
if row3:
    d3 = row3.get("content") or ""
    line("    delivered length", len(d3))
    line("    fraction of the memory the caller receives",
         f"{100*len(d3)/max(1,len(stored3)):.1f}%")
    line("    tail marker survives?", "TAIL MARKER PRESENT" in d3,
         "*** the end of the memory is unreachable via recall ***"
         if "TAIL MARKER PRESENT" not in d3 else "")
print()

print("=" * 98)
print("Observed only. The invariant — no silent loss of load-bearing content; declare")
print("truncation and full length; surface a bounded summary; offer authorized full-content")
print("continuation by object_id — is Yua's contract, not this file's.")
print("=" * 98)
