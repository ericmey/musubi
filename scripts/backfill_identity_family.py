#!/usr/bin/env python3
"""One-shot backfill: populate `identity_family` on every existing point.

Background: as of musubi v1.5.5, every memory carries an `identity_family`
payload field derived from the namespace's first path component (e.g.
`aoi/command-chair/episodic` → `identity_family="aoi"`). The field is the
load-bearing key for cross-substrate federation — retrieval, ranking, and
synthesis filter on it so every presence under one identity (Aoi across
voice / command-chair / shared / etc.) is treated as one continuous
memory stream rather than separate silos.

New writes get the field automatically via the Pydantic validator on
`MusubiObject`. This script populates the field on existing points
that pre-date the change.

USAGE (from the musubi host, inside the core container):

    sudo docker cp scripts/backfill_identity_family.py musubi-core-1:/tmp/
    sudo docker exec -e BACKFILL_CONFIRM=1 musubi-core-1 \\
        python3 /tmp/backfill_identity_family.py

WITHOUT `BACKFILL_CONFIRM=1` the script prints what it would do (dry run).
The `MUSUBI_QDRANT_URL` and `MUSUBI_QDRANT_API_KEY` env vars are read
from the container's already-loaded settings; no extra config needed.

The operation is idempotent — running it twice changes nothing on the
second pass because every point already has the field populated.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

import httpx

QDRANT_URL = os.environ.get("MUSUBI_QDRANT_URL", "http://qdrant:6333")
QDRANT_KEY = os.environ.get("MUSUBI_QDRANT_API_KEY", os.environ.get("QDRANT_API_KEY", ""))
DRY_RUN = os.environ.get("BACKFILL_CONFIRM", "") != "1"

# Every collection that stores `MusubiObject`-derived points and therefore
# inherits the `identity_family` field via the Pydantic validator. Kept in
# sync with `musubi.store.specs.REGISTRY` (the source of truth) — if a new
# collection is added there, add it here too.
COLLECTIONS = (
    "musubi_episodic",
    "musubi_concept",
    "musubi_curated",
    "musubi_thought",
    "musubi_artifact",
    "musubi_artifact_chunks",
)

H = {"api-key": QDRANT_KEY, "content-type": "application/json"}


def family_of(namespace: str) -> str:
    """Mirror of `musubi.types.common.family_of` — kept inline so the
    script runs in any Python without a musubi import."""
    if "/" not in namespace:
        raise ValueError(f"namespace {namespace!r} has no separator")
    return namespace.split("/", 1)[0]


def scroll_all(client: httpx.Client, collection: str) -> list[dict]:
    """Page through every point in a collection, returning payload only
    (vectors not needed for backfill)."""
    out: list[dict] = []
    offset = None
    while True:
        body = {"limit": 256, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        try:
            resp = client.post(
                f"{QDRANT_URL}/collections/{collection}/points/scroll",
                headers=H,
                json=body,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # Collection may not exist on this deployment yet (e.g.
                # smoke envs without artifact chunks). Skip cleanly.
                return []
            raise
        result = resp.json()["result"]
        out.extend(result.get("points", []))
        offset = result.get("next_page_offset")
        if offset is None:
            break
    return out


def set_payload(client: httpx.Client, collection: str, point_id: str, family: str) -> None:
    """Upsert `identity_family` on a single point, leaving every other
    payload field untouched."""
    resp = client.post(
        f"{QDRANT_URL}/collections/{collection}/points/payload",
        headers=H,
        json={
            "payload": {"identity_family": family},
            "points": [point_id],
        },
    )
    resp.raise_for_status()


def main() -> int:
    print(f"=== Identity-family backfill ({'DRY RUN' if DRY_RUN else 'EXECUTING'}) ===")
    print(f"Qdrant: {QDRANT_URL}")
    print()

    with httpx.Client(timeout=60.0) as client:
        total_seen = 0
        total_needs_update = 0
        total_updated = 0
        per_family_counts: dict[str, int] = defaultdict(int)
        per_collection_summary: dict[str, dict[str, int]] = {}

        for collection in COLLECTIONS:
            print(f"[{collection}]", flush=True)
            points = scroll_all(client, collection)
            seen = len(points)
            already_set = 0
            needs_update = 0
            updated = 0
            failures = 0
            missing_namespace = 0

            for p in points:
                payload = p.get("payload") or {}
                namespace = payload.get("namespace")
                if not namespace or not isinstance(namespace, str):
                    missing_namespace += 1
                    continue

                family = family_of(namespace)
                per_family_counts[family] += 1
                existing = payload.get("identity_family")

                if existing == family:
                    already_set += 1
                    continue

                needs_update += 1
                if DRY_RUN:
                    continue

                try:
                    set_payload(client, collection, p["id"], family)
                    updated += 1
                except Exception as exc:
                    print(f"  ! failed for {p['id']}: {exc}", flush=True)
                    failures += 1

            per_collection_summary[collection] = {
                "seen": seen,
                "already_set": already_set,
                "needs_update": needs_update,
                "updated": updated,
                "failures": failures,
                "missing_namespace": missing_namespace,
            }
            total_seen += seen
            total_needs_update += needs_update
            total_updated += updated
            print(
                f"  seen={seen} already_set={already_set} "
                f"needs_update={needs_update} updated={updated} "
                f"failures={failures} missing_namespace={missing_namespace}",
                flush=True,
            )

        print()
        print("=== Summary ===")
        print(f"Total points seen: {total_seen}")
        print(f"Total needing update: {total_needs_update}")
        if DRY_RUN:
            print("DRY RUN — no writes performed.")
            print("Re-run with BACKFILL_CONFIRM=1 to execute.")
        else:
            print(f"Total updated: {total_updated}")

        print()
        print("Family distribution across all collections:")
        for fam in sorted(per_family_counts):
            print(f"  {fam:20} {per_family_counts[fam]:>6}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
