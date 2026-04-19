"""Shared response models used across read routers.

The plane-specific payloads are the same pydantic types from
``src/musubi/types/`` — they round-trip cleanly through FastAPI's JSON
encoder. These wrappers just document the list / collection shapes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str = "v0"


class ComponentStatus(BaseModel):
    name: str
    healthy: bool
    detail: str = ""


class StatusResponse(BaseModel):
    status: str
    components: dict[str, ComponentStatus]


class NamespaceListResponse(BaseModel):
    items: list[str]


class NamespaceStats(BaseModel):
    namespace: str
    counts: dict[str, int]
    last_activity_epoch: float | None = None


class RetrieveResultRow(BaseModel):
    object_id: str
    score: float
    plane: str
    content: str
    namespace: str
    extra: dict[str, Any] = {}


class RetrieveResponse(BaseModel):
    results: list[RetrieveResultRow]
    mode: str
    limit: int


class ContradictionPair(BaseModel):
    object_id: str
    contradicts: list[str]
    namespace: str


class ContradictionListResponse(BaseModel):
    items: list[ContradictionPair]


class LifecycleEventRow(BaseModel):
    event_id: str
    object_id: str
    object_type: str
    namespace: str
    from_state: str
    to_state: str
    actor: str
    reason: str
    occurred_epoch: float


class LifecycleEventListResponse(BaseModel):
    items: list[LifecycleEventRow]


class ThoughtListResponse(BaseModel):
    items: list[dict[str, Any]]


__all__ = [
    "ComponentStatus",
    "ContradictionListResponse",
    "ContradictionPair",
    "HealthResponse",
    "LifecycleEventListResponse",
    "LifecycleEventRow",
    "NamespaceListResponse",
    "NamespaceStats",
    "RetrieveResponse",
    "RetrieveResultRow",
    "StatusResponse",
    "ThoughtListResponse",
]
