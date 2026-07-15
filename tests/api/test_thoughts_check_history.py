from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from musubi.types.common import generate_ksuid
from tests.api.conftest import mint_token


def _upsert_thought(
    qdrant: QdrantClient,
    namespace: str,
    from_p: str,
    to_p: str,
    read: bool = False,
    read_by: list[str] | None = None,
) -> str:
    obj_id = generate_ksuid()

    qdrant.upsert(
        collection_name="musubi_thought",
        points=[
            PointStruct(
                id=str(uuid4()),
                vector={},
                payload={
                    "object_id": obj_id,
                    "namespace": namespace,
                    "from_presence": from_p,
                    "to_presence": to_p,
                    "read": read,
                    "read_by": read_by or [],
                    "state": "provisional",
                    "content": f"test {from_p} to {to_p}",
                },
            )
        ],
    )
    return obj_id


def test_check_includes_unicast_and_broadcast_excludes_unrelated(
    client: TestClient, qdrant: QdrantClient, api_settings: Any
) -> None:
    ns = "eric/ns/thought"
    me = "eric/me"

    # Unicast to me
    t1 = _upsert_thought(qdrant, ns, "eric/other", me)
    # Broadcast to all
    t2 = _upsert_thought(qdrant, ns, "eric/other", "all")

    # Excludes other-recipient
    _upsert_thought(qdrant, ns, "eric/other", "eric/someone_else")
    # Excludes self-send
    _upsert_thought(qdrant, ns, me, "all")
    _upsert_thought(qdrant, ns, me, me)
    # Excludes already-read-by-me
    _upsert_thought(qdrant, ns, "eric/other", me, read_by=[me])
    _upsert_thought(qdrant, ns, "eric/other", "all", read_by=["eric/someone_else", me])

    token = mint_token(api_settings, scopes=[f"{ns}:r"])
    r = client.post(
        "/v1/thoughts/check",
        headers={"Authorization": f"Bearer {token}"},
        json={"namespace": ns, "presence": me, "limit": 50},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    ids = {i["object_id"] for i in items}
    assert ids == {t1, t2}


def test_history_includes_sent_and_received_excludes_unrelated(
    client: TestClient, qdrant: QdrantClient, api_settings: Any
) -> None:
    ns = "eric/ns/thought"
    me = "eric/me"

    # Sent by me
    t1 = _upsert_thought(qdrant, ns, me, "eric/other")
    # Unicast to me
    t2 = _upsert_thought(qdrant, ns, "eric/other", me)
    # Broadcast to all
    t3 = _upsert_thought(qdrant, ns, "eric/other", "all")
    # Sent by me to all
    t4 = _upsert_thought(qdrant, ns, me, "all")

    # Excludes unrelated in same namespace
    _upsert_thought(qdrant, ns, "eric/someone", "eric/someone_else")

    token = mint_token(api_settings, scopes=[f"{ns}:r"])
    r = client.post(
        "/v1/thoughts/history",
        headers={"Authorization": f"Bearer {token}"},
        json={"namespace": ns, "presence": me, "query_text": "", "limit": 50},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 4
    ids = {i["object_id"] for i in items}
    assert ids == {t1, t2, t3, t4}


def test_namespace_auth_enforced_before_read(client: TestClient, api_settings: Any) -> None:
    ns = "eric/ns/thought"
    me = "eric/me"

    # Requesting a namespace without scope
    token = mint_token(api_settings, scopes=["other/ns:r"])
    r = client.post(
        "/v1/thoughts/check",
        headers={"Authorization": f"Bearer {token}"},
        json={"namespace": ns, "presence": me, "limit": 50},
    )
    assert r.status_code == 403

    r2 = client.post(
        "/v1/thoughts/history",
        headers={"Authorization": f"Bearer {token}"},
        json={"namespace": ns, "presence": me, "query_text": "", "limit": 50},
    )
    assert r2.status_code == 403
