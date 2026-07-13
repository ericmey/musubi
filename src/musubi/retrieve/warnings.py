"""RET-007 — the one warning language for retrieval degradation.

A :class:`RetrievalWarning` is a **structured, bounded** signal that a retrieval leg degraded but did
not fail the whole request: a machine-readable ``code`` plus the ``plane`` it happened on. It is a
frozen dataclass (NOT a ``str`` subclass — the code and the plane are distinct fields, not one string
carrying smuggled metadata), so it survives slicing / sorting / cross-plane merges without loss.

Every retrieval mode (fast, deep, blended, orchestration) emits these SAME structured warnings — one
warning language, no free-text-to-code translation seam. The bounded vocabulary:

- ``sparse_embedding_failed`` / ``reranker_failed`` — a component degraded within a single leg.
- ``plane_timeout_<plane>`` / ``plane_error_<plane>`` — a whole plane leg timed out / errored, but
  other planes survived.

``code`` is always exactly one allowlisted value; ``plane`` is always one of the fixed planes. The
router flattens ``code`` onto the additive wire ``warnings`` array and dedupes by ``(code, plane)``.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The fixed plane vocabulary. A warning's ``plane`` is always one of these.
FIXED_PLANES = frozenset({"episodic", "curated", "concept", "artifact", "thought"})

#: Bounded codes that carry no plane suffix (the plane still travels in ``.plane``).
_SIMPLE_CODES = frozenset({"sparse_embedding_failed", "reranker_failed"})

_PLANE_PREFIXES = ("plane_timeout_", "plane_error_")


@dataclass(frozen=True, slots=True)
class RetrievalWarning:
    """A bounded, structured retrieval-degradation signal: an allowlisted ``code`` + its ``plane``."""

    code: str
    plane: str


def is_allowlisted(warning: RetrievalWarning) -> bool:
    """True iff ``warning`` carries an allowlisted code AND a fixed plane. A ``plane_*_`` code's
    suffix must also name a fixed plane and match the structured ``plane`` field."""
    if warning.plane not in FIXED_PLANES:
        return False
    code = warning.code
    if code in _SIMPLE_CODES:
        return True
    for prefix in _PLANE_PREFIXES:
        if code.startswith(prefix):
            suffix = code[len(prefix) :]
            return suffix in FIXED_PLANES and suffix == warning.plane
    return False


def plane_timeout(plane: str) -> RetrievalWarning:
    """A whole-plane leg timed out (other planes may have survived)."""
    return RetrievalWarning(code=f"plane_timeout_{plane}", plane=plane)


def plane_error(plane: str) -> RetrievalWarning:
    """A whole-plane leg errored (non-timeout, non-fatal — other planes survived)."""
    return RetrievalWarning(code=f"plane_error_{plane}", plane=plane)


def sparse_embedding_failed(plane: str) -> RetrievalWarning:
    """The sparse channel degraded within a leg; retrieval fell back to dense-only."""
    return RetrievalWarning(code="sparse_embedding_failed", plane=plane)


def reranker_failed(plane: str) -> RetrievalWarning:
    """The reranker degraded within a leg; retrieval fell back to the fused ranking."""
    return RetrievalWarning(code="reranker_failed", plane=plane)


def dedupe(warnings: tuple[RetrievalWarning, ...]) -> tuple[RetrievalWarning, ...]:
    """Collapse to distinct ``(code, plane)`` at the request boundary, preserving first-seen order."""
    seen: set[tuple[str, str]] = set()
    out: list[RetrievalWarning] = []
    for w in warnings:
        key = (w.code, w.plane)
        if key not in seen:
            seen.add(key)
            out.append(w)
    return tuple(out)


__all__ = [
    "FIXED_PLANES",
    "RetrievalWarning",
    "dedupe",
    "is_allowlisted",
    "plane_error",
    "plane_timeout",
    "reranker_failed",
    "sparse_embedding_failed",
]
