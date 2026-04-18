"""Tests for ``musubi.types.common`` — primitives, helpers, Result, ArtifactRef."""

from __future__ import annotations

from datetime import datetime

import pytest

from musubi.types import (
    ArtifactRef,
    Err,
    Ok,
    epoch_of,
    generate_ksuid,
    utc_now,
    validate_ksuid,
    validate_namespace,
)
from musubi.types.common import KSUID_LENGTH


class TestNamespaceValidator:
    @pytest.mark.parametrize(
        "ns",
        [
            "eric/claude-code/episodic",
            "tenant1/presence-alpha/curated",
            "a/b/concept",
            "eric/yua_01/thought",
            "eric/uploads/artifact",
            "eric/sys/lifecycle",
        ],
    )
    def test_valid_namespaces_accepted(self, ns: str) -> None:
        assert validate_namespace(ns) == ns

    @pytest.mark.parametrize(
        "ns",
        [
            "Eric/claude/episodic",  # uppercase tenant
            "eric/claude/foo",  # unknown plane
            "eric/claude",  # missing plane
            "/eric/claude/episodic",  # leading slash
            "eric//episodic",  # empty presence
            "eric/claude/episodic/extra",  # too many segments
            "eric/claude code/episodic",  # space in presence
        ],
    )
    def test_malformed_namespaces_rejected(self, ns: str) -> None:
        with pytest.raises(ValueError, match="tenant/presence/plane"):
            validate_namespace(ns)


class TestKSUID:
    def test_generate_produces_27_char_base62(self) -> None:
        k = generate_ksuid()
        assert len(k) == KSUID_LENGTH
        assert k.isalnum()
        validate_ksuid(k)  # should not raise

    def test_validate_rejects_short_id(self) -> None:
        with pytest.raises(ValueError, match="27-char base62 KSUID"):
            validate_ksuid("tooshort")

    def test_validate_rejects_non_base62_chars(self) -> None:
        with pytest.raises(ValueError, match="27-char base62 KSUID"):
            validate_ksuid("_" * 27)

    def test_generated_ids_are_unique(self) -> None:
        assert len({generate_ksuid() for _ in range(100)}) == 100


class TestTimeHelpers:
    def test_utc_now_is_tz_aware(self) -> None:
        now = utc_now()
        assert now.tzinfo is not None

    def test_epoch_of_matches_timestamp(self) -> None:
        now = utc_now()
        assert abs(epoch_of(now) - now.timestamp()) < 1e-9

    def test_epoch_of_rejects_naive_datetime(self) -> None:
        naive = datetime(2026, 4, 17, 14, 23)
        with pytest.raises(ValueError, match="timezone-aware"):
            epoch_of(naive)


class TestArtifactRef:
    def test_full_ref_roundtrips(self) -> None:
        ref = ArtifactRef(
            artifact_id=generate_ksuid(),
            chunk_id=generate_ksuid(),
            quote="a citation",
        )
        restored = ArtifactRef.model_validate_json(ref.model_dump_json())
        assert restored == ref

    def test_whole_artifact_ref_has_null_chunk(self) -> None:
        ref = ArtifactRef(artifact_id=generate_ksuid())
        assert ref.chunk_id is None
        assert ref.quote is None

    def test_frozen(self) -> None:
        ref = ArtifactRef(artifact_id=generate_ksuid())
        with pytest.raises(Exception):  # pydantic ValidationError when frozen
            ref.quote = "nope"


class TestResult:
    def test_ok_unwrap_returns_value(self) -> None:
        assert Ok[int](value=42).unwrap() == 42

    def test_err_unwrap_raises(self) -> None:
        with pytest.raises(RuntimeError, match="unwrap"):
            Err[str](error="bad").unwrap()

    def test_ok_maps(self) -> None:
        doubled = Ok[int](value=3).map(lambda x: x * 2)
        assert isinstance(doubled, Ok)
        assert doubled.unwrap() == 6

    def test_kind_tags(self) -> None:
        assert Ok[int](value=1).kind == "ok"
        assert Err[str](error="x").kind == "err"

    def test_is_ok_is_err(self) -> None:
        o: Ok[int] = Ok(value=1)
        e: Err[str] = Err(error="x")
        assert o.is_ok() and not o.is_err()
        assert e.is_err() and not e.is_ok()
