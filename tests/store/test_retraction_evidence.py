"""IDEM-007 shared write-time / VAL-002 retraction-evidence keyhole."""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable
from typing import Any, cast

import pytest

from musubi.types.common import generate_ksuid
from musubi.types.episodic import RetractionEvidence

_NS = "eric/claude-code/episodic"
_CONTENT = "the original false claim"
_CONTENT_BYTES = _CONTENT.encode()


def _evidence(**changes: Any) -> RetractionEvidence:
    body: dict[str, Any] = {
        "kind": "artifact_escrow_v1",
        "artifact_namespace": "eric/claude-code/artifact",
        "artifact_ref": {"artifact_id": generate_ksuid()},
        "original_sha256": hashlib.sha256(_CONTENT_BYTES).hexdigest(),
        "original_utf8_bytes": len(_CONTENT_BYTES),
        "quoted_prefix_utf8_bytes": len(_CONTENT_BYTES),
        "omitted_bytes": 0,
        "vector_basis": "original",
        "preserved_pointer": {"kind": "v2", "live_point": "content-current"},
        "operation_identity_hash": "1" * 64,
        "request_digest": "2" * 64,
    }
    body.update(changes)
    return RetractionEvidence.model_validate(body)


def _anchor(**changes: Any) -> dict[str, Any]:
    body = {
        "namespace": _NS,
        "object_id": generate_ksuid(),
        "point_kind": "anchor",
        "live_point": "content-current",
        "committed_operation_id": "generation-current",
        "content": f"RETRACTED\nOriginal excerpt:\n{_CONTENT}\nCorrected truth: replacement",
    }
    body.update(changes)
    return body


def _target(anchor: dict[str, Any], **changes: Any) -> dict[str, Any]:
    body = {
        "namespace": anchor["namespace"],
        "object_id": anchor["object_id"],
        "point_kind": "content",
        "generation": "generation-current",
        "content": _CONTENT,
    }
    body.update(changes)
    return body


def _binding_errors(
    evidence: RetractionEvidence, anchor: dict[str, Any], target: dict[str, Any]
) -> list[tuple[str, str]] | None:
    module = importlib.import_module("musubi.store.retraction_evidence")
    binding = cast(
        Callable[..., list[tuple[str, str]] | None],
        getattr(module, "retraction_evidence_binding_errors"),
    )
    return binding(
        evidence=evidence,
        anchor_payload=anchor,
        target_payload=target,
    )


def test_shared_keyhole_accepts_every_binding_computed_from_storage() -> None:
    anchor = _anchor()
    assert _binding_errors(_evidence(), anchor, _target(anchor)) == []


@pytest.mark.parametrize(
    ("anchor_change", "target_change", "field"),
    [
        ({"live_point": "other-content"}, {}, "preserved_pointer"),
        ({"committed_operation_id": "other-generation"}, {}, "preserved_pointer"),
        ({}, {"content": "different bytes"}, "original_sha256"),
        ({"content": "RETRACTED without the storage prefix"}, {}, "quoted_prefix_utf8_bytes"),
    ],
)
def test_shared_keyhole_rejects_each_storage_binding_independently(
    anchor_change: dict[str, Any], target_change: dict[str, Any], field: str
) -> None:
    anchor = _anchor(**anchor_change)
    errors = _binding_errors(_evidence(), anchor, _target(anchor, **target_change))
    assert errors is not None
    assert field in {loc for loc, _message in errors}


@pytest.mark.parametrize("malformed", [{}, {"content": None}, {"content": 7}])
def test_shared_keyhole_malformed_target_content_fails_closed_without_raise(
    malformed: dict[str, Any],
) -> None:
    anchor = _anchor()
    target = _target(anchor)
    target.pop("content")
    target.update(malformed)
    errors = _binding_errors(_evidence(), anchor, target)
    assert errors is not None
    assert any(loc == "original_sha256" for loc, _message in errors)


def test_legacy_evidence_is_outside_the_v2_divergence_keyhole() -> None:
    evidence = _evidence(preserved_pointer={"kind": "legacy_self"})
    assert _binding_errors(evidence, _anchor(), _target(_anchor())) is None
