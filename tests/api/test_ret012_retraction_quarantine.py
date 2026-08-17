"""RET-012 terminal lifecycle semantics for escrow-backed retractions."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from musubi.lifecycle import LifecycleEventSink
from musubi.lifecycle.maturation import MaturationConfig, MaturationCursor, episodic_maturation_sweep
from musubi.planes.episodic import EpisodicPlane
from musubi.store.immutable_vectors import ImmutableVectorPublisher
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory

_NS = "eric/claude-code/episodic"


def _body(version: int) -> dict[str, Any]:
    return {
        "namespace": _NS,
        "expected_version": version,
        "on": "2026-08-17",
        "because": "the original claim is false",
        "truth": "The corrected claim is true.",
        "tags": ["retracted", "do-not-act-on"],
    }


def _seed(
    *,
    layout: str,
    state: str,
    plane: EpisodicPlane,
    publisher: ImmutableVectorPublisher,
    coordinator: Any,
) -> EpisodicMemory:
    memory = EpisodicMemory(
        namespace=_NS,
        object_id=generate_ksuid(),
        content="The original false claim.",
        state=state,
        importance=8,
    )
    if layout == "legacy":
        return asyncio.run(plane.create(memory))
    publisher.publish(
        coordinator,
        object_id=memory.object_id,
        namespace=memory.namespace,
        content_payload=memory.model_dump(mode="json"),
    )
    return memory


@pytest.mark.parametrize("layout", ["legacy", "v2"])
@pytest.mark.parametrize("state", ["provisional", "matured"])
def test_retraction_archives_legacy_and_v2_rows_from_any_active_state(
    layout: str,
    state: str,
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout=layout,
        state=state,
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )

    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers={
            "Authorization": f"Bearer {valid_token}",
            "Idempotency-Key": f"quarantine-{layout}-{state}",
        },
        json=_body(memory.version),
    )
    assert response.status_code == 200, response.text
    stored = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert stored is not None
    assert stored.state == "archived"
    assert stored.importance == 1
    assert stored.retraction_evidence is not None


class _NoEnrichment:
    async def score_importance(self, _items: list[Any]) -> None:
        raise AssertionError("an archived retraction must never reach importance enrichment")

    async def infer_topics(self, _items: list[Any]) -> None:
        raise AssertionError("an archived retraction must never reach topic enrichment")


def test_retracted_provisional_row_cannot_reenter_maturation_after_one_hour(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    coordinator: Any,
    _immutable_publishers: tuple[Any, Any],
    qdrant: QdrantClient,
    tmp_path: Path,
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    memory = _seed(
        layout="legacy",
        state="provisional",
        plane=episodic,
        publisher=publisher,
        coordinator=coordinator,
    )
    response = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers={
            "Authorization": f"Bearer {valid_token}",
            "Idempotency-Key": "quarantine-time-advanced",
        },
        json=_body(memory.version),
    )
    assert response.status_code == 200, response.text
    retracted = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert retracted is not None

    sink = LifecycleEventSink(db_path=tmp_path / "events.sqlite")
    cursor = MaturationCursor(db_path=tmp_path / "cursor.sqlite")
    try:
        report = asyncio.run(
            episodic_maturation_sweep(
                client=qdrant,
                sink=sink,
                coordinator=coordinator,
                ollama=_NoEnrichment(),  # type: ignore[arg-type]
                cursor=cursor,
                config=MaturationConfig(min_age_sec=3600),
                now=retracted.updated_at + timedelta(hours=2),
            )
        )
    finally:
        sink.close()

    assert report.selected == 0
    assert report.transitioned == 0
    stored = asyncio.run(episodic.get(namespace=_NS, object_id=memory.object_id))
    assert stored is not None
    assert stored.state == "archived"
    assert stored.importance == 1
