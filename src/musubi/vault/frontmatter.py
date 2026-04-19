"""Vault frontmatter schema and YAML round-trip parser."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from io import StringIO
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from ruamel.yaml import YAML

from musubi.types.common import KSUID, LifecycleState


class ArtifactRefFrontmatter(BaseModel):
    """Reference to a supporting source artifact chunk."""

    artifact_id: KSUID
    chunk_id: KSUID | None = None
    quote: str | None = Field(default=None, max_length=1000)


class CuratedFrontmatter(BaseModel):
    """The pydantic model enforced on curated markdown file frontmatter."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )

    # Identity
    object_id: KSUID | None = None
    namespace: str | None = Field(default=None, pattern=r"^[a-z0-9-]+/[a-z0-9-_]+/[a-z]+$")
    schema_version: int = 1

    # Content metadata
    title: str = Field(min_length=1, max_length=200)
    topics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=7, ge=1, le=10)
    summary: str | None = Field(default=None, max_length=1000)

    # Lifecycle
    state: LifecycleState = "matured"
    version: int = Field(default=1, ge=1)
    musubi_managed: bool = Field(default=False, alias="musubi-managed")

    # Temporal
    created: datetime
    updated: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    # Lineage
    supersedes: list[KSUID] = Field(default_factory=list)
    superseded_by: KSUID | None = None
    promoted_from: KSUID | None = None
    promoted_at: datetime | None = None
    merged_from: list[KSUID] = Field(default_factory=list)
    supported_by: list[ArtifactRefFrontmatter] = Field(default_factory=list)
    linked_to_topics: list[str] = Field(default_factory=list)
    contradicts: list[KSUID] = Field(default_factory=list)

    # Read state (optional, rare in frontmatter)
    read_by: list[str] = Field(default_factory=list)

    @field_validator("object_id", mode="before")
    @classmethod
    def allow_empty_ksuid(cls, v: Any) -> Any:
        if v == "" or v is None:
            return None
        if hasattr(v, "__str__"):
            return str(v)
        return v

    @field_validator("created", "updated", "valid_from", "valid_until", mode="before")
    @classmethod
    def validate_timezone_aware(cls, v: Any) -> Any:
        if isinstance(v, datetime) and v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (ISO8601 with Z or offset)")
        return v

    @field_validator("tags", mode="after")
    @classmethod
    def canonicalize_tags(cls, v: list[str]) -> list[str]:
        # Lowercase, strip, hyphenate, dedup
        out: list[str] = []
        seen = set()
        for tag in v:
            can = tag.lower().strip().replace(" ", "-")
            if can and can not in seen:
                out.append(can)
                seen.add(can)
        return out

    @field_validator("topics", mode="after")
    @classmethod
    def lowercase_topics(cls, v: list[str]) -> list[str]:
        return [t.lower() for t in v]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown string into frontmatter dict and body text."""
    if not text.startswith("---"):
        return {}, text.strip()

    parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.M)
    if len(parts) < 3:
        return {}, text.strip()

    yaml = _yaml_loader()
    try:
        data = yaml.load(parts[1])
    except Exception:
        return {}, parts[2].strip()

    if not isinstance(data, Mapping):
        return {}, parts[2].strip()

    # Preserve the original CommentedMap for round-tripping if needed,
    # but return a clean dict for model validation.
    clean_data = {k: v for k, v in data.items() if v != "" and v is not None}
    return clean_data, parts[2].strip()


def dump_frontmatter(data: dict[str, Any], body: str) -> str:
    """Serialize data as YAML frontmatter followed by body."""
    # If data is already a Mapping (like CommentedMap), we can try to use it directly
    # to preserve comments/order. But for simplicity and correctness vs model:
    model = CuratedFrontmatter.model_validate(data)
    dump_data = model.model_dump(by_alias=True, exclude_none=True, mode="json")

    yaml = _yaml_loader()
    stream = StringIO()
    yaml.dump(dump_data, stream)
    fm = stream.getvalue()
    return f"---\n{fm}---\n\n{body.strip()}"


def _yaml_loader() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096  # prevent line wraps in long summaries
    yaml.default_flow_style = False
    # Represent None as empty string in YAML
    yaml.representer.add_representer(
        type(None), lambda self, data: self.represent_scalar("tag:yaml.org,2002:null", "")
    )
    return yaml
