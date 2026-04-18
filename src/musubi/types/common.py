"""Shared primitives used across every memory object.

Contents:

- ``LifecycleState`` — canonical state enum.
- ``Result`` — typed Ok/Err union for transition-style returns.
- ``ArtifactRef`` — small citation struct embedded in memory objects.
- ``NAMESPACE_RE`` / ``validate_namespace`` — ``tenant/presence/plane`` form.
- ``utc_now`` / ``epoch_of`` — timestamp helpers (see vault conventions §Time).
- ``generate_ksuid`` — 27-char base62 KSUID minted via ``svix-ksuid``.

The data-model specs in ``04-data-model/`` are authoritative; when this module
disagrees with them, the specs win.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, NoReturn

from ksuid import Ksuid
from pydantic import AfterValidator, BaseModel, ConfigDict

SCHEMA_VERSION: Final[int] = 1
"""Current payload schema version. Writer always writes this; reader is forward-compatible."""

LifecycleState = Literal[
    "provisional",
    "matured",
    "promoted",
    "synthesized",
    "demoted",
    "archived",
    "superseded",
]

ArtifactIndexingState = Literal["indexing", "indexed", "failed"]
"""Second-axis state on ``SourceArtifact`` — independent of the main lifecycle axis."""

Modality = Literal["text", "voice-transcript", "tool-call", "system-event"]


# ---------------------------------------------------------------------------
# Namespace: ``tenant/presence/plane``
# ---------------------------------------------------------------------------

NAMESPACE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9][a-z0-9_-]*"
    r"/[a-z0-9][a-z0-9_-]*"
    r"/(episodic|curated|concept|artifact|thought|lifecycle)$"
)
"""A namespace is ``<tenant>/<presence>/<plane>``. Lowercase; dash/underscore only."""


def validate_namespace(ns: str) -> str:
    if not NAMESPACE_RE.match(ns):
        raise ValueError(
            f"namespace {ns!r} does not match tenant/presence/plane form "
            f"(regex: {NAMESPACE_RE.pattern})"
        )
    return ns


Namespace = Annotated[str, AfterValidator(validate_namespace)]


# ---------------------------------------------------------------------------
# KSUID helpers
# ---------------------------------------------------------------------------

KSUID_LENGTH: Final[int] = 27
_KSUID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9A-Za-z]{27}$")


def generate_ksuid() -> str:
    """Return a fresh 27-char base62 KSUID string."""
    return str(Ksuid())


def validate_ksuid(value: str) -> str:
    if not _KSUID_RE.match(value):
        raise ValueError(f"not a 27-char base62 KSUID: {value!r}")
    return value


KSUID = Annotated[str, AfterValidator(validate_ksuid)]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Timezone-aware ``datetime`` in UTC. Never use ``datetime.now()`` without ``tz``."""
    return datetime.now(UTC)


def epoch_of(ts: datetime) -> float:
    """Seconds-since-epoch for a ``datetime``. Rejects naive datetimes."""
    if ts.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return ts.timestamp()


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("datetime must be timezone-aware; naive datetimes forbidden")
    return ts.astimezone(UTC)


# ---------------------------------------------------------------------------
# Artifact citation struct
# ---------------------------------------------------------------------------


class ArtifactRef(BaseModel):
    """A pointer into a ``SourceArtifact`` — whole artifact or a specific chunk.

    Used by ``MemoryObject.supported_by`` to cite evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: KSUID
    chunk_id: KSUID | None = None
    quote: str | None = None


# ---------------------------------------------------------------------------
# Result[T, E] — typed Ok/Err for transition-style APIs (PEP 695 type params)
# ---------------------------------------------------------------------------


class Ok[T](BaseModel):
    """Successful ``Result`` carrying a value of type ``T``."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["ok"] = "ok"
    value: T

    def is_ok(self) -> Literal[True]:
        return True

    def is_err(self) -> Literal[False]:
        return False

    def unwrap(self) -> T:
        return self.value

    def map[U](self, f: Callable[[T], U]) -> Ok[U]:
        return Ok(value=f(self.value))


class Err[E](BaseModel):
    """Failure ``Result`` carrying an error of type ``E``."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["err"] = "err"
    error: E

    def is_ok(self) -> Literal[False]:
        return False

    def is_err(self) -> Literal[True]:
        return True

    def unwrap(self) -> NoReturn:
        raise RuntimeError(f"called unwrap() on Err: {self.error!r}")


type Result[T, E] = Ok[T] | Err[E]
"""Discriminated union. Typical use: ``Result[TransitionResult, TransitionError]``."""


__all__ = [
    "KSUID",
    "KSUID_LENGTH",
    "NAMESPACE_RE",
    "SCHEMA_VERSION",
    "ArtifactIndexingState",
    "ArtifactRef",
    "Err",
    "LifecycleState",
    "Modality",
    "Namespace",
    "Ok",
    "Result",
    "ensure_utc",
    "epoch_of",
    "generate_ksuid",
    "utc_now",
    "validate_ksuid",
    "validate_namespace",
]
