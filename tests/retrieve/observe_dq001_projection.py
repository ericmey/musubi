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
import uuid as _u6
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import FRESH_STATES, Fixture, Musubi, Store

ENV = Path.home() / ".musubi/musubi-mcp-aoi.env"
NS = "aoi/command-chair/lifecycle"  # lifecycle plane: probes, not real memories
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

PAD = "Context that matters but is not the conclusion. " * 8  # ~380 chars of preamble
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
    line(
        "    the point survives DELIVERY?",
        POINT in delivered,
        "" if POINT in delivered else "*** THE CONCLUSION IS GONE ***",
    )
    line(
        "    does the row DECLARE it was truncated?",
        any(k in row for k in ("truncated", "full_length", "content_length")),
        "no field says so" if not any(k in row for k in ("truncated", "full_length")) else "",
    )
    line("    row keys returned to the caller", ",".join(sorted(row.keys())))
    print()
    print(f"    delivered ends: ...{delivered[-70:]!r}")
    print(f"    the caller NEVER SEES:  {stored[len(delivered) : len(delivered) + 80]!r}")
print()

# ── O2: is a SUPPLIED summary honoured? ──────────────────────────────────────
print("O2  If the writer supplies a summary, does recall use it?")
marker2 = f"dqs{int(time.time())}"
body = f"{marker2}. " + ("Filler that buries the lede. " * 14) + " FINAL CLAUSE: the answer is 42."
oid2 = musubi._req(
    "POST",
    "/episodic",
    {
        "namespace": NS_RECALL,
        "content": body,
        "summary": "SUPPLIED SUMMARY: the answer is 42.",
        "tags": ["kind:episode", "staleness:episodic"],
        "importance": 8,
    },
).get("object_id")
time.sleep(3)
pl2 = store.payload(oid2) or {}
line(
    "    summary accepted and STORED?",
    "summary" in pl2,
    f"stored={pl2.get('summary')!r}" if "summary" in pl2 else "the field is not persisted",
)
res2 = musubi.recall(NS_RECALL, marker2, mode="blended", limit=5, state_filter=FRESH_STATES)
row2 = next((r for r in res2 if r.get("object_id") == oid2), None)
if row2:
    line(
        "    summary DELIVERED to the caller?",
        "summary" in row2,
        "recall ignores it" if "summary" not in row2 else "",
    )
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
    line(
        "    fraction of the memory the caller receives",
        f"{100 * len(d3) / max(1, len(stored3)):.1f}%",
    )
    line(
        "    tail marker survives?",
        "TAIL MARKER PRESENT" in d3,
        "*** the end of the memory is unreachable via recall ***"
        if "TAIL MARKER PRESENT" not in d3
        else "",
    )
print()

print("=" * 98)
print("Observed only. The invariant — no silent loss of load-bearing content; declare")
print("truncation and full length; surface a bounded summary; offer authorized full-content")
print("continuation by object_id — is Yua's contract, not this file's.")
print("=" * 98)


# ── O4: the exact cut point, in BYTES and CHARS, and the boundary it lands on ─
# Yua's additions: char 301 / 1501 / end; declared vs actual length in bytes AND chars;
# Unicode grapheme boundaries at the cut. A cut measured in the wrong unit, or one that
# splits a multi-byte character, corrupts a memory in a way that is invisible until the
# glyph turns into a replacement box.
print()
print("O5  The cut point — bytes vs chars, and Unicode safety at the boundary")

# a memory whose 300th character region is a multi-byte emoji, so a byte-cut would split it
emoji_marker = f"dqu{int(time.time())}"
prefix = "x" * 295
emoji_content = f"{emoji_marker}. {prefix}🧠🧠🧠 CONCLUSION AFTER THE EMOJI BLOCK."
oidu = musubi.write(NS_RECALL, emoji_content, importance=7)
time.sleep(3)
storedu = (store.payload(oidu) or {}).get("content") or ""
line("    stored char length", len(storedu))
line("    stored BYTE length (utf-8)", len(storedu.encode("utf-8")))
resu = musubi.recall(NS_RECALL, emoji_marker, mode="blended", limit=5, state_filter=FRESH_STATES)
rowu = next((r for r in resu if r.get("object_id") == oidu), None)
if rowu:
    du = rowu.get("content") or ""
    line("    delivered char length", len(du))
    line("    delivered BYTE length (utf-8)", len(du.encode("utf-8")))
    # a valid string re-encodes cleanly; a split grapheme shows as a replacement char
    reencoded_ok = "�" not in du and du == du.encode("utf-8", "ignore").decode("utf-8", "ignore")
    # CORRECTION (Yua): "valid UTF-8" only proves CODE-POINT safety, NOT grapheme-cluster
    # safety. A [:300] slice is code-point-safe but can still cut THROUGH a grapheme —
    # é (e + U+0301), a ZWJ family emoji — leaving valid UTF-8 that means something else.
    line("    delivered is valid UTF-8 (code-point safe)", reencoded_ok)
    line("    cut is CHARACTER-based (len==300), not byte-based", len(du) == 300)
    # ── SELF-PROVING grapheme assertions (Yua: "grade code, not commit message").
    # Every fact is an assert, not a printed boolean. Setup goes through seed_exact so it
    # CANNOT bypass the seed invariant, and it proves the stored content is byte-exact
    # (dedup would return a different string). Fails LOUDLY on setup or result.
    import uuid as _uuid

    def _straddle(cluster: str) -> tuple[str, str, str]:
        gm = f"gz{_uuid.uuid4().hex[:10]}"
        gpre = f"{gm}. "
        body = gpre + ("x" * (299 - len(gpre))) + cluster + "TAIL"
        return gm, body, body

    # combining mark: e (U+0065) + COMBINING ACUTE (U+0301) — decomposed, one grapheme
    combining = "e\u0301"
    assert [ord(c) for c in combining] == [0x65, 0x301], "combining fixture must be decomposed"
    gm_c, body_c, _ = _straddle(combining)
    assert body_c[299] == "e" and body_c[300] == "\u0301", (
        f"combining cut must straddle: got {body_c[299:301]!r}"
    )
    oid_c = fix.seed_exact(body_c, importance=6)  # invariant-checked
    res_c = musubi.recall(NS_RECALL, gm_c, mode="blended", limit=5, state_filter=FRESH_STATES)
    row_c = next((r for r in res_c if r.get("object_id") == oid_c), None)
    assert row_c is not None, "combining-mark fixture was not returned by recall"
    d_c = row_c.get("content") or ""
    assert len(d_c) == 300, f"expected a 300-char projection, got {len(d_c)}"
    combining_split = d_c.endswith("e") and not d_c.endswith("e\u0301")
    assert combining_split, (
        f"expected the accent CUT (delivered ends bare 'e'); got tail {d_c[-3:]!r}"
    )
    line("    combining e+U+0301: accent CUT (asserted)", True, "'é' delivered as 'e'")

    # ZWJ family emoji — one grapheme, 5 code points with U+200D joiners
    zwj = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    gm_z, body_z, _ = _straddle(zwj)
    assert body_z[299] == "\U0001f468", f"zwj cut must straddle: got {body_z[299]!r}"
    oid_z = fix.seed_exact(body_z, importance=6)  # invariant-checked
    assert oid_z != oid_c, "grapheme fixtures must be distinct objects"
    res_z = musubi.recall(NS_RECALL, gm_z, mode="blended", limit=5, state_filter=FRESH_STATES)
    row_z = next((r for r in res_z if r.get("object_id") == oid_z), None)
    assert row_z is not None, "ZWJ fixture was not returned by recall"
    d_z = row_z.get("content") or ""
    assert len(d_z) == 300, f"expected a 300-char projection, got {len(d_z)}"
    zwj_split = (zwj not in d_z) and 0 < sum(cp in d_z for cp in zwj) < len(zwj)
    assert zwj_split, f"expected the family emoji SEVERED; got tail {d_z[-3:]!r}"
    line("    ZWJ family emoji: SEVERED (asserted)", True, "family delivered as a fragment")

# ── O6: key fact at exactly 301 / 1501 / end ─────────────────────────────────
print()
# O6 measures the RECALL PROJECTION cut (300-char). Terminology is explicit because I
# got it wrong: Python slice [:300] KEEPS zero-based indices 0..299 (ordinal chars 1..300)
# and DROPS from zero-based index 300 onward — i.e. the 301st ordinal character is the
# first one lost. NOTE: this is the recall projection only. It does NOT exercise Ollama's
# 1500-char INPUT truncation (DQ-002) — a fact at zero_based_index 1500 is absent here
# simply because it is past 300, which says nothing about the 1500 path.
print("O6  First-lost character of the 300-char recall projection (zero-based index 300)")

# zero_based_index 300 == the 301st ordinal character == first code point dropped by [:300]
ZERO_BASED = 300
ORDINAL = ZERO_BASED + 1  # 301
mk = f"dq{_u6.uuid4().hex[:10]}"
fact = "FACTHERE"
# body layout: mk + "." + filler + fact ; fact's zero-based start index == len(mk)+1+len(filler)
filler = "." * (ZERO_BASED - len(mk) - 1)
body = f"{mk}.{filler}{fact} and the rest continues."
assert body.index(fact) == ZERO_BASED, (
    f"fact must start at zero_based_index {ZERO_BASED} (ordinal char {ORDINAL}); starts at {body.index(fact)}"
)
o = fix.seed_exact(body, importance=8)
r = musubi.recall(NS_RECALL, mk, mode="blended", limit=5, state_filter=FRESH_STATES)
row = next((x for x in r if x.get("object_id") == o), None)
assert row is not None, "O6 fixture not returned by recall"
delivered = row.get("content") or ""
assert len(delivered) == 300, f"expected 300-char projection, got {len(delivered)}"
# the character at zero_based_index 299 (ordinal 300) is the LAST delivered; index 300 is lost
assert delivered == body[:300], "delivered must equal the first 300 chars of the stored body"
assert fact not in delivered, (
    f"a fact starting at zero_based_index {ZERO_BASED} must be LOST by the [:300] cut"
)
line(
    f"    fact at zero_based_index {ZERO_BASED} (ordinal char {ORDINAL}): LOST (asserted)",
    True,
    "first character past the 300-char projection; caller never sees it",
)

print()
print("=" * 98)
print("Observed only. Budgets, summary policy, and cut-unit are Yua's contract.")
print("=" * 98)
