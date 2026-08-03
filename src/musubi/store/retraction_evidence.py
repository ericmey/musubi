"""Pure storage bindings for the IDEM-007 non-reembedding retraction keyhole.

Both the write seam and VAL-002 call this function. A divergent episodic anchor
therefore cannot be committed under evidence that the sweep would later reject.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from musubi.types.episodic import RetractionEvidence, V2RetractionPointer


def retraction_evidence_binding_errors(
    *,
    evidence: RetractionEvidence,
    anchor_payload: dict[str, Any],
    target_payload: dict[str, Any],
) -> list[tuple[str, str]] | None:
    """Return v2-local binding errors, or ``None`` for legacy evidence.

    Every comparison is against physical storage. Evidence never certifies
    another evidence field. Cross-plane artifact-head verification remains a
    separate complete-scan/write-saga check.
    """
    pointer = evidence.preserved_pointer
    if not isinstance(pointer, V2RetractionPointer):
        return None

    errors: list[tuple[str, str]] = []
    anchor_live_point = anchor_payload.get("live_point")
    if pointer.live_point != anchor_live_point:
        errors.append(
            ("preserved_pointer", "preserved live_point is not the current anchor pointer")
        )
    if (
        target_payload.get("point_kind") != "content"
        or target_payload.get("namespace") != anchor_payload.get("namespace")
        or target_payload.get("object_id") != anchor_payload.get("object_id")
    ):
        errors.append(
            ("preserved_pointer", "preserved live_point is not content for this episodic object")
        )
    if target_payload.get("generation") != anchor_payload.get("committed_operation_id"):
        errors.append(
            ("preserved_pointer", "anchor operation does not equal preserved content generation")
        )

    target_content = target_payload.get("content")
    if not isinstance(target_content, str):
        errors.append(
            (
                "original_sha256",
                "immutable content is missing or not text; evidence binding cannot be verified",
            )
        )
        return errors
    target_bytes = target_content.encode("utf-8")
    if evidence.original_sha256 != sha256(target_bytes).hexdigest():
        errors.append(("original_sha256", "recorded digest does not match immutable content"))
    if evidence.original_utf8_bytes != len(target_bytes):
        errors.append(
            ("original_utf8_bytes", "recorded byte length does not match immutable content")
        )

    prefix_size = evidence.quoted_prefix_utf8_bytes
    try:
        prefix = target_bytes[:prefix_size].decode("utf-8")
    except UnicodeDecodeError:
        errors.append(
            ("quoted_prefix_utf8_bytes", "recorded prefix splits the stored UTF-8 content")
        )
    else:
        anchor_content = anchor_payload.get("content")
        if not isinstance(anchor_content, str) or prefix not in anchor_content:
            errors.append(
                (
                    "quoted_prefix_utf8_bytes",
                    "storage-derived original prefix is absent from the anchor tombstone",
                )
            )
    return errors


__all__ = ["retraction_evidence_binding_errors"]
