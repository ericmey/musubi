"""RET-002 / #500 — streaming path shares the final-delivery accounting seam.

`POST /v1/retrieve/stream` (writes_retrieve_stream) calls the SAME
`orchestration.retrieve` as the non-streaming route, so the final-boundary accounting
covers streaming too — it is NOT a separate surface (Yua ruling 2026-07-15). This proof
drives the real streaming endpoint and asserts the delivered row is accounted exactly once,
read back through the NON-MUTATING raw payload (never a GET, which would bump the counter).

RED on current main: streaming defaults to `mode="fast"`, which never accounts today.
"""

import json

from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from musubi.store.names import collection_for_plane
from musubi.store.raw_lookup import raw_payload


def _access_count(qdrant: QdrantClient, namespace: str, object_id: str) -> int | None:
    payload = raw_payload(
        qdrant, collection_for_plane("episodic"), namespace=namespace, object_id=object_id
    )
    return None if payload is None else payload.get("access_count")


def test_streaming_retrieval_accounts_each_delivered_row_once(
    client: TestClient, qdrant: QdrantClient, valid_token: str
) -> None:
    namespace = "eric/claude-code/episodic"
    resp = client.post(
        "/v1/episodic",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={"namespace": namespace, "content": "stream accounting marker content"},
    )
    assert resp.status_code // 100 == 2

    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {valid_token}"},
        json={
            "namespace": namespace,
            "query_text": "stream accounting marker",
            "mode": "fast",
            "limit": 5,
            # DATA-001 P2: the write creates a PROVISIONAL row, now filtered by fast mode
            # post-hydration; this test asserts access accounting, not state — request it visible.
            "state_filter": ["provisional"],
        },
    )
    assert r.status_code == 200, r.text
    lines = [line for line in r.text.split("\n") if line]
    assert lines, "streaming path delivered no rows"
    delivered = json.loads(lines[0])
    oid = delivered["object_id"]
    assert delivered["plane"] == "episodic"

    # The row the caller was handed over the stream must be accounted exactly once.
    assert _access_count(qdrant, namespace, oid) == 1
