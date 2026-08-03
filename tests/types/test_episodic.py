"""Tests for ``EpisodicMemory``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from musubi.types import EpisodicMemory

_ARTIFACT_ID = "3GJhJKrgAOyI9ebWT8dLYUtUMGL"


def _retraction_evidence(**changes: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "kind": "artifact_escrow_v1",
        "artifact_namespace": "eric/claude-code/artifact",
        "artifact_ref": {"artifact_id": _ARTIFACT_ID},
        "original_sha256": "a" * 64,
        "original_utf8_bytes": 17,
        "quoted_prefix_utf8_bytes": 10,
        "omitted_bytes": 7,
        "vector_basis": "original",
        "preserved_pointer": {
            "kind": "v2",
            "live_point": "episodic-content-current",
        },
        "operation_identity_hash": "b" * 64,
        "request_digest": "c" * 64,
    }
    evidence.update(changes)
    return evidence


def _memory_with_evidence(
    evidence: dict[str, Any],
    *,
    namespace: str = "eric/claude-code/episodic",
    content: str = "[RETRACTED]",
) -> EpisodicMemory:
    return EpisodicMemory.model_validate(
        {
            "namespace": namespace,
            "content": content,
            "state": "matured",
            "retraction_evidence": evidence,
        }
    )


def test_defaults_to_provisional(sample_episodic: EpisodicMemory) -> None:
    assert sample_episodic.state == "provisional"


def test_rejects_foreign_state(episodic_namespace: str) -> None:
    # Episodic can't be "promoted" (concept-only) or "synthesized".
    with pytest.raises(ValueError):
        EpisodicMemory(
            namespace=episodic_namespace,
            content="x",
            state="synthesized",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        EpisodicMemory(
            namespace=episodic_namespace,
            content="x",
            state="promoted",  # type: ignore[arg-type]
        )


def test_content_required_non_empty(episodic_namespace: str) -> None:
    with pytest.raises(ValueError):
        EpisodicMemory(namespace=episodic_namespace, content="")


def test_event_at_tz_enforced(episodic_namespace: str) -> None:
    naive = datetime(2026, 4, 17, 14, 23)
    with pytest.raises(ValueError, match="timezone-aware"):
        EpisodicMemory(namespace=episodic_namespace, content="x", event_at=naive)


def test_modality_default_is_text(sample_episodic: EpisodicMemory) -> None:
    assert sample_episodic.modality == "text"


def test_modality_constrained_to_known_set(episodic_namespace: str) -> None:
    with pytest.raises(ValueError):
        EpisodicMemory(
            namespace=episodic_namespace,
            content="x",
            modality="video",  # type: ignore[arg-type]
        )


def test_roundtrip_json(sample_episodic: EpisodicMemory) -> None:
    restored = EpisodicMemory.model_validate_json(sample_episodic.model_dump_json())
    assert restored == sample_episodic


def test_episodic_importance_last_scored_at_accepts_utc_datetime(episodic_namespace: str) -> None:
    from musubi.types.common import utc_now

    mem = EpisodicMemory(
        namespace=episodic_namespace, content="x", importance_last_scored_at=utc_now()
    )
    assert mem.importance_last_scored_at is not None
    assert mem.importance_last_scored_epoch is not None


def test_episodic_importance_last_scored_at_rejects_naive_datetime(episodic_namespace: str) -> None:
    from datetime import datetime

    naive = datetime(2026, 4, 17, 14, 23)
    with pytest.raises(ValueError, match="timezone-aware"):
        EpisodicMemory(namespace=episodic_namespace, content="x", importance_last_scored_at=naive)


def test_episodic_topics_field_exists(episodic_namespace: str) -> None:
    mem = EpisodicMemory(namespace=episodic_namespace, content="x", topics=["a", "b"])
    assert mem.topics == ["a", "b"]


def test_episodic_importance_last_scored_epoch_index_declared() -> None:
    from musubi.store.specs import INDEXES_BY_COLLECTION

    deltas = INDEXES_BY_COLLECTION["musubi_episodic"]
    assert any(
        i.field_name == "importance_last_scored_epoch" and i.schema == "float" for i in deltas
    )


def test_all_fields_round_trip_through_model_dump_model_validate(episodic_namespace: str) -> None:
    from musubi.types.common import utc_now

    now = utc_now()
    mem = EpisodicMemory(
        namespace=episodic_namespace, content="x", importance_last_scored_at=now, topics=["topic1"]
    )
    restored = EpisodicMemory.model_validate(mem.model_dump())
    assert restored == mem


def test_retraction_evidence_strict_shape_round_trips_without_storage_fields() -> None:
    memory = _memory_with_evidence(
        _retraction_evidence(),
        content="[RETRACTED] quoted prefix: a readable",
    )

    dumped = memory.model_dump(mode="json")
    restored = EpisodicMemory.model_validate(dumped)

    assert restored == memory
    assert dumped["retraction_evidence"] == _retraction_evidence()
    assert (
        not {
            "point_kind",
            "live_point",
            "pointer_version",
            "committed_operation_id",
            "vector_layout_version",
        }
        & dumped.keys()
    )


def test_absent_retraction_evidence_preserves_existing_wire_shape() -> None:
    memory = EpisodicMemory(
        namespace="eric/claude-code/episodic",
        content="ordinary memory",
    )

    assert "retraction_evidence" not in memory.model_dump(mode="json")
    assert "retraction_evidence" in EpisodicMemory.model_json_schema()["properties"]


@pytest.mark.parametrize(
    "change,match",
    [
        ({"artifact_ref": {"artifact_id": "not-a-ksuid"}}, "KSUID"),
        ({"artifact_ref": {"artifact_id": _ARTIFACT_ID, "chunk_id": _ARTIFACT_ID}}, "chunk_id"),
        ({"artifact_ref": {"artifact_id": _ARTIFACT_ID, "quote": "original"}}, "quote"),
        ({"original_sha256": "A" * 64}, "original_sha256"),
        ({"operation_identity_hash": "b" * 63}, "operation_identity_hash"),
        ({"request_digest": "g" * 64}, "request_digest"),
        ({"vector_basis": "tombstone"}, "vector_basis"),
        (
            {"quoted_prefix_utf8_bytes": 0, "omitted_bytes": 17},
            "quoted_prefix_utf8_bytes",
        ),
        ({"unexpected": True}, "unexpected"),
    ],
)
def test_retraction_evidence_rejects_malformed_or_noncanonical_fields(
    change: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        _memory_with_evidence(_retraction_evidence(**change))


def test_retraction_evidence_requires_derived_sibling_artifact_namespace() -> None:
    with pytest.raises(ValueError, match="sibling artifact namespace"):
        _memory_with_evidence(_retraction_evidence(artifact_namespace="aoi/command-chair/artifact"))


def test_retraction_evidence_rejects_partial_shape() -> None:
    evidence = _retraction_evidence()
    del evidence["request_digest"]

    with pytest.raises(ValueError, match="request_digest"):
        _memory_with_evidence(evidence)


def test_retraction_evidence_prefix_and_omitted_bytes_reconcile_with_original_length() -> None:
    with pytest.raises(ValueError, match=r"quoted_prefix_utf8_bytes.*omitted_bytes"):
        _memory_with_evidence(_retraction_evidence(omitted_bytes=8))


def test_retraction_evidence_requires_exactly_one_typed_preserved_pointer_shape() -> None:
    legacy = _memory_with_evidence(_retraction_evidence(preserved_pointer={"kind": "legacy_self"}))
    assert legacy.retraction_evidence is not None
    assert legacy.retraction_evidence.preserved_pointer.kind == "legacy_self"

    with pytest.raises(ValueError, match="preserved_pointer"):
        _memory_with_evidence(
            _retraction_evidence(
                preserved_pointer={"kind": "legacy_self", "live_point": "smuggled"}
            )
        )
