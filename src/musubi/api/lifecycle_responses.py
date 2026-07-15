"""Typed HTTP representation of a durably admitted lifecycle transition."""

from __future__ import annotations

from typing import Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from musubi.lifecycle.coordinator import TransitionPending


class TransitionPendingBody(BaseModel):
    """A transition accepted for reconciler completion, never fabricated as final."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pending"] = "pending"
    operation_key: str = Field(min_length=1)
    event_id: str = Field(min_length=1)


def pending_response(outcome: TransitionPending) -> JSONResponse:
    body = TransitionPendingBody(
        operation_key=outcome.operation_key,
        event_id=outcome.event_id,
    )
    return JSONResponse(status_code=202, content=body.model_dump())


__all__ = ["TransitionPendingBody", "pending_response"]
