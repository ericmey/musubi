"""C4 / ART-001 upload-route wire truth: the 202 ``state`` is the INDEXING axis (`indexing`, or
`failed` at capacity), never the lifecycle state; GET reflects the same."""

from __future__ import annotations

import io
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from httpx import Response
from qdrant_client import QdrantClient

from musubi.api.dependencies import get_lifecycle_service
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.settings import Settings
from musubi.types.common import generate_ksuid
from tests.api.conftest import mint_token

_NS = "eric/claude-code/artifact"


def _upload(client: TestClient, token: str, content: bytes = b"# A\ncontent here\n") -> Response:
    return cast(
        Response,
        client.post(
            "/v1/artifacts",
            headers={"Authorization": f"Bearer {token}"},
            data={"namespace": _NS, "title": "idx", "content_type": "text/markdown"},
            files={"file": ("doc.md", io.BytesIO(content), "text/markdown")},
        ),
    )


def test_upload_202_state_is_indexing_axis_and_get_truth(
    client: TestClient, api_settings: Settings
) -> None:
    token = mint_token(api_settings, scopes=[f"{_NS}:rw"])
    r = _upload(client, token)
    assert r.status_code == 202
    assert r.json()["state"] == "indexing"  # INDEXING axis — NOT lifecycle 'matured'
    oid = r.json()["object_id"]
    g = client.get(
        f"/v1/artifacts/{oid}",
        params={"namespace": _NS},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert g.status_code == 200 and g.json()["artifact_state"] == "indexing"


def test_upload_at_capacity_202_state_failed_and_get_truth(
    client: TestClient, api_settings: Settings, qdrant: QdrantClient, tmp_path: Path
) -> None:
    token = mint_token(api_settings, scopes=[f"{_NS}:rw"])
    # a full single-slot coordinator so the route's enqueue hits capacity.
    capped = LifecycleTransitionCoordinator(
        client=qdrant, db_path=tmp_path / "capped.db", pending_cap=1
    )
    assert capped.enqueue_index_intent(object_id=generate_ksuid(), namespace=_NS) == "admitted"
    client.app.dependency_overrides[get_lifecycle_service] = lambda: capped  # type: ignore[attr-defined]
    try:
        r = _upload(client, token)
        assert r.status_code == 202
        assert r.json()["state"] == "failed"  # visible terminal, never silently stuck indexing
        oid = r.json()["object_id"]
        g = client.get(
            f"/v1/artifacts/{oid}",
            params={"namespace": _NS},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert g.status_code == 200 and g.json()["artifact_state"] == "failed"
    finally:
        del client.app.dependency_overrides[get_lifecycle_service]  # type: ignore[attr-defined]
