"""SEC-003 MECHANICAL route inventory — prose cannot hide a new vulnerable route.

Yua: "make inventory evidence mechanical in slice: list every Depends(require_auth)
occurrence and separately list manual body-scope routes checked safe, so future route
additions cannot hide behind prose."

This scans the router source and classifies EVERY require_auth route by where its
namespace comes from. Run it in CI or by hand; a new Form/Path/Body-namespace route shows
up here automatically instead of being trusted to prose.

    python3 tests/api/sec003_route_inventory.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROUTERS = Path(__file__).resolve().parents[2] / "src/musubi/api/routers"

# a route function that carries require_auth, and how it sources `namespace`
DEF = re.compile(r"async def (\w+)\(", re.M)


def classify(src: str, start: int, end: int) -> str:
    body = src[start:end]
    if re.search(r"namespace\w*\s*[:=].*\bForm\b", body):
        return "FORM  (auth reads empty query -> AFFECTED)"
    if re.search(r"namespace\w*\s*[:=].*\bPath\b", body):
        return "PATH  (auth reads empty query -> AFFECTED unless resolved)"
    if re.search(r"namespace\w*\s*[:=].*\b(Body|Field)\b", body):
        return "BODY  (auth reads empty query -> AFFECTED)"
    if re.search(r"namespace\w*\s*[:=].*\bQuery\(None", body):
        return "query-NULLABLE (namespace optional -> SEC-004 fanout territory, not clean-safe)"
    if re.search(r"namespace\w*\s*[:=].*\bQuery\b", body):
        return "query (auth CAN see it -> safe)"
    if re.search(r"operator=True|require_operator", body):
        return "operator-scoped (no namespace check by design)"
    return "NO namespace param (non-namespace route)"


affected: list[str] = []
safe: list[str] = []

for f in sorted(ROUTERS.glob("*.py")):
    src = f.read_text()
    if "require_auth" not in src and "require_operator" not in src:
        continue
    defs = list(DEF.finditer(src))
    for i, m in enumerate(defs):
        fn = m.group(1)
        seg_start = m.start()
        seg_end = defs[i + 1].start() if i + 1 < len(defs) else len(src)
        # only routes that actually depend on auth
        head = src[max(0, seg_start - 400):seg_start]
        if "require_auth" not in head and "require_operator" not in head:
            continue
        kind = classify(src, seg_start, seg_end)
        row = f"  {f.name:<22} {fn:<24} {kind}"
        (affected if "AFFECTED" in kind else safe).append(row)

print("SEC-003 ROUTE INVENTORY (mechanical) —", ROUTERS)
print("\nAFFECTED (namespace not visible to require_auth):")
print("\n".join(affected) or "  (none)")
print("\nSAFE / by-design:")
print("\n".join(safe))
print(f"\nTOTAL affected: {len(affected)}")
