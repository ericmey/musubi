"""RET-002 / #500 — /v1/context accounts the FINAL pack, not the retrieval candidates.

`/v1/context` retrieves `candidate_limit` candidates, then `build_context_pack` trims the set.
Because accounting consumes the POST-BUILD pack, a candidate dropped for ANY reason
(max_items / max_chars / filler) is unaccounted by construction. This test discriminates that
mechanism through the `max_items` trim specifically (the cleanest observable drop);
`build_context_pack`'s own max_chars/filler trimming is covered by the existing context_pack
unit tests, not re-integration-tested here.

The fix defers accounting in `orchestration.retrieve` (account_access=False) and accounts the
flattened final pack items in the router. RED before the fix: accounting ran over all
retrieved candidates, so a max_items-dropped candidate was wrongly counted.
"""

import pytest
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from musubi.store.names import collection_for_plane
from musubi.store.raw_lookup import raw_payload

pytestmark = pytest.mark.anyio

_NS = "eric/claude-code/episodic"


def _access_count(qdrant: QdrantClient, object_id: str) -> int:
    payload = raw_payload(
        qdrant, collection_for_plane("episodic"), namespace=_NS, object_id=object_id
    )
    return 0 if payload is None else int(payload.get("access_count", 0) or 0)


def test_context_accounts_only_surfaced_pack_items_not_dropped_candidates(
    client: TestClient, qdrant: QdrantClient, valid_token: str
) -> None:
    hdr = {"Authorization": f"Bearer {valid_token}"}
    seeded: list[str] = []
    for i in range(6):
        resp = client.post(
            "/v1/episodic",
            headers=hdr,
            json={"namespace": _NS, "content": f"context marker distinct body number {i}"},
        )
        assert resp.status_code // 100 == 2, resp.text
        seeded.append(resp.json()["object_id"])

    # Retrieve all 6 as candidates, but let the pack surface at most 2 → ≥4 dropped.
    r = client.post(
        "/v1/context",
        headers=hdr,
        json={
            "namespace": _NS,
            "query_text": "context marker",
            "planes": ["episodic"],
            "candidate_limit": 6,
            "max_items": 2,
        },
    )
    assert r.status_code == 200, r.text
    pack = r.json()
    surfaced = {item["object_id"] for group in pack["groups"] for item in group["items"]}
    assert surfaced, "pack surfaced nothing — probe is vacuous"
    dropped = set(seeded) - surfaced
    assert dropped, "pack surfaced everything — increase candidates / lower max_items"

    for oid in surfaced:
        assert _access_count(qdrant, oid) == 1, (
            f"surfaced item {oid} must be accounted exactly once"
        )
    for oid in dropped:
        assert _access_count(qdrant, oid) == 0, f"dropped candidate {oid} must NOT be accounted"
