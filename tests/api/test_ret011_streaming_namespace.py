"""RET-011 / #510 — the streaming endpoint agrees: a concrete target is presence-exact.

`POST /v1/retrieve/stream` calls the same `run_orchestration_retrieve` seam, so the
exact-namespace filter fix applies to streaming too. Two presences of one family with
identical content; a concrete stream query for one must never stream the other's row.
RED on current main (identity_family filter widening).
"""

import json

import pytest
from starlette.testclient import TestClient

from musubi.settings import Settings
from tests.api.conftest import mint_token

pytestmark = pytest.mark.anyio

_PRES_A = "eric/presalpha/episodic"
_PRES_B = "eric/presbravo/episodic"
_CONTENT = "identical streaming marker content stored verbatim by both presences"


def test_streaming_concrete_target_is_presence_exact(
    client: TestClient, api_settings: Settings
) -> None:
    token = mint_token(
        api_settings,
        scopes=[f"{_PRES_A}:rw", f"{_PRES_B}:rw"],
    )
    for ns in (_PRES_A, _PRES_B):
        resp = client.post(
            "/v1/episodic",
            headers={"Authorization": f"Bearer {token}"},
            json={"namespace": ns, "content": _CONTENT},
        )
        assert resp.status_code // 100 == 2, resp.text

    r = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": _PRES_A,
            "query_text": "identical streaming marker",
            "mode": "fast",
            "limit": 10,
        },
    )
    assert r.status_code == 200, r.text
    namespaces = {json.loads(line)["namespace"] for line in r.text.split("\n") if line}
    assert namespaces <= {_PRES_A}, (
        f"streaming concrete target leaked sibling presence: {namespaces}"
    )
