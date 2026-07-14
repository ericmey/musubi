"""H5 concept-write Pending contracts."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from musubi.api.dependencies import get_concept_plane
from musubi.lifecycle.coordinator import TransitionPending
from musubi.types.common import Ok, generate_ksuid
from tests.api.conftest import mint_token


class _PendingConceptPlane:
    async def exists(self, **_kwargs: Any) -> bool:
        return True

    async def transition(self, **_kwargs: Any) -> Ok[TransitionPending]:
        return Ok(
            value=TransitionPending(
                operation_key="h5-http-pending",
                event_id=generate_ksuid(),
            )
        )


def test_h5_concept_promote_http_pending_is_typed_202(
    app_factory: Any,
    api_settings: Any,
) -> None:
    app_factory.dependency_overrides[get_concept_plane] = _PendingConceptPlane
    token = mint_token(api_settings, scopes=["operator"])
    with TestClient(app_factory) as client:
        response = client.post(
            f"/v1/concepts/{generate_ksuid()}/promote",
            headers={"Authorization": f"Bearer {token}"},
            params={"namespace": "eric/shared/concept"},
            json={"promoted_to": generate_ksuid(), "reason": "h5"},
        )
    assert response.status_code == 202
    assert response.json() == {
        "status": "pending",
        "operation_key": "h5-http-pending",
        "event_id": response.json()["event_id"],
    }


def test_h5_concept_delete_http_pending_is_typed_202(
    app_factory: Any,
    api_settings: Any,
) -> None:
    app_factory.dependency_overrides[get_concept_plane] = _PendingConceptPlane
    namespace = "eric/shared/concept"
    token = mint_token(api_settings, scopes=[f"{namespace}:rw"])
    with TestClient(app_factory) as client:
        response = client.delete(
            f"/v1/concepts/{generate_ksuid()}",
            headers={"Authorization": f"Bearer {token}"},
            params={"namespace": namespace},
        )
    assert response.status_code == 202
    assert response.json() == {
        "status": "pending",
        "operation_key": "h5-http-pending",
        "event_id": response.json()["event_id"],
    }
