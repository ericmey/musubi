"""Tests for ``musubi.types.base`` — ``MusubiObject`` + ``MemoryObject`` invariants.

These tests exercise MemoryObject via ``EpisodicMemory`` (the simplest concrete
subclass) since ``MemoryObject`` is abstract in the domain sense — it has no
direct instantiation path without a ``state``-narrowing subclass.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from musubi.types import (
    SCHEMA_VERSION,
    ArtifactRef,
    EpisodicMemory,
    generate_ksuid,
)


class TestMusubiObjectInvariants:
    def test_schema_version_present_and_defaults_to_current(self, episodic_namespace: str) -> None:
        obj = EpisodicMemory(namespace=episodic_namespace, content="x")
        assert obj.schema_version == SCHEMA_VERSION

    def test_created_epoch_matches_created_at(self, episodic_namespace: str) -> None:
        obj = EpisodicMemory(namespace=episodic_namespace, content="x")
        assert obj.created_epoch is not None
        assert abs(obj.created_epoch - obj.created_at.timestamp()) < 1e-9

    def test_updated_epoch_monotone_non_decreasing(
        self, episodic_namespace: str, fixed_now: datetime
    ) -> None:
        earlier = fixed_now
        later = fixed_now + timedelta(minutes=5)
        obj = EpisodicMemory(
            namespace=episodic_namespace,
            content="x",
            created_at=earlier,
            updated_at=later,
        )
        assert obj.updated_epoch is not None and obj.created_epoch is not None
        assert obj.updated_epoch >= obj.created_epoch

    def test_updated_before_created_rejected(
        self, episodic_namespace: str, fixed_now: datetime
    ) -> None:
        with pytest.raises(ValueError, match="monotone"):
            EpisodicMemory(
                namespace=episodic_namespace,
                content="x",
                created_at=fixed_now,
                updated_at=fixed_now - timedelta(seconds=1),
            )

    def test_version_defaults_to_one_and_cannot_start_at_zero(
        self, episodic_namespace: str
    ) -> None:
        default = EpisodicMemory(namespace=episodic_namespace, content="x")
        assert default.version == 1
        with pytest.raises(ValueError, match="version must start at 1"):
            EpisodicMemory(namespace=episodic_namespace, content="x", version=0)

    def test_namespace_regex_enforced(self) -> None:
        with pytest.raises(ValueError, match="tenant/presence/plane"):
            EpisodicMemory(namespace="not-a-namespace", content="x")

    def test_utc_enforced_on_datetime_inputs(self, episodic_namespace: str) -> None:
        naive = datetime(2026, 4, 17, 14, 23)
        with pytest.raises(ValueError, match="timezone-aware"):
            EpisodicMemory(namespace=episodic_namespace, content="x", created_at=naive)

    def test_extra_fields_rejected(self, episodic_namespace: str) -> None:
        with pytest.raises(ValueError):
            EpisodicMemory(
                namespace=episodic_namespace,
                content="x",
                unknown_field="surprise",  # type: ignore[call-arg]
            )


class TestMemoryObjectLineage:
    def test_supersedes_self_rejected(self, episodic_namespace: str) -> None:
        oid = generate_ksuid()
        with pytest.raises(ValueError, match="cannot supersede itself"):
            EpisodicMemory(
                namespace=episodic_namespace,
                content="x",
                object_id=oid,
                superseded_by=oid,
            )

    def test_self_in_supersedes_list_rejected(self, episodic_namespace: str) -> None:
        oid = generate_ksuid()
        with pytest.raises(ValueError, match="own supersedes list"):
            EpisodicMemory(
                namespace=episodic_namespace,
                content="x",
                object_id=oid,
                supersedes=[oid],
            )

    def test_supported_by_carries_artifact_refs(self, episodic_namespace: str) -> None:
        ref = ArtifactRef(artifact_id=generate_ksuid())
        obj = EpisodicMemory(namespace=episodic_namespace, content="x", supported_by=[ref])
        assert obj.supported_by == [ref]

    def test_importance_bounds_enforced(self, episodic_namespace: str) -> None:
        with pytest.raises(ValueError):
            EpisodicMemory(namespace=episodic_namespace, content="x", importance=0)
        with pytest.raises(ValueError):
            EpisodicMemory(namespace=episodic_namespace, content="x", importance=11)


class TestMemoryObjectValidity:
    def test_valid_from_before_valid_until_enforced(
        self, episodic_namespace: str, fixed_now: datetime
    ) -> None:
        with pytest.raises(ValueError, match="valid_until"):
            EpisodicMemory(
                namespace=episodic_namespace,
                content="x",
                valid_from=fixed_now,
                valid_until=fixed_now - timedelta(days=1),
            )

    def test_validity_epochs_filled(self, episodic_namespace: str, fixed_now: datetime) -> None:
        obj = EpisodicMemory(
            namespace=episodic_namespace,
            content="x",
            valid_from=fixed_now,
            valid_until=fixed_now + timedelta(days=365),
        )
        assert obj.valid_from_epoch == fixed_now.timestamp()
        assert obj.valid_until_epoch == (fixed_now + timedelta(days=365)).timestamp()

    def test_valid_from_equal_to_valid_until_allowed(
        self, episodic_namespace: str, fixed_now: datetime
    ) -> None:
        obj = EpisodicMemory(
            namespace=episodic_namespace,
            content="x",
            valid_from=fixed_now,
            valid_until=fixed_now,
        )
        assert obj.valid_from == obj.valid_until


class TestRoundtrip:
    def test_json_roundtrip_preserves_equality(self, sample_episodic: EpisodicMemory) -> None:
        restored = EpisodicMemory.model_validate_json(sample_episodic.model_dump_json())
        assert restored == sample_episodic

    def test_forward_compat_older_schema_reads_ok(self, sample_episodic: EpisodicMemory) -> None:
        """A schema_version: 1 payload still parses when current is bumped.

        We can't fake the future schema, but the contract is: reader accepts
        older versions. Encode that by demonstrating a payload with explicit
        older schema_version still parses cleanly.
        """
        payload = sample_episodic.model_dump(mode="json")
        payload["schema_version"] = 1
        EpisodicMemory.model_validate(payload)
