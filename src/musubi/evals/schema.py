from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator


class GoldenQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    relevant: list[Any]
    mode: str
    namespace: str


class SmokeDocument(BaseModel):
    """One deterministic, network-free PR-smoke document."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    text: str
    relevance: StrictInt
    embedding: list[StrictFloat] = Field(min_length=1)

    @model_validator(mode="after")
    def _embedding_is_finite(self) -> SmokeDocument:
        if not all(math.isfinite(value) for value in self.embedding):
            raise ValueError("document embedding values must be finite")
        return self


class SmokeFixture(BaseModel):
    """Typed fixed-embedding input for the PR smoke gate."""

    model_config = ConfigDict(extra="forbid")

    query_embedding: list[StrictFloat] = Field(min_length=1)
    corpus: list[SmokeDocument] = Field(min_length=2)

    @model_validator(mode="after")
    def _fixture_is_coherent(self) -> SmokeFixture:
        if not all(math.isfinite(value) for value in self.query_embedding):
            raise ValueError("query embedding values must be finite")
        dimension = len(self.query_embedding)
        if any(len(document.embedding) != dimension for document in self.corpus):
            raise ValueError("document embeddings must match query embedding dimension")
        ids = [document.id for document in self.corpus]
        if len(ids) != len(set(ids)):
            raise ValueError("smoke document ids must be unique")
        return self
