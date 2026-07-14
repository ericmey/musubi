"""H5 red contract: every plane transition uses the canonical three-way coordinator boundary."""

from __future__ import annotations

import inspect
from dataclasses import fields
from typing import Any

import pytest

from musubi.lifecycle.coordinator import TransitionIntent
from musubi.planes.artifact.plane import ArtifactPlane
from musubi.planes.concept.plane import ConceptPlane
from musubi.planes.curated.plane import CuratedPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.planes.thoughts.plane import ThoughtsPlane

_PLANES: tuple[type[Any], ...] = (
    EpisodicPlane,
    ConceptPlane,
    ThoughtsPlane,
    ArtifactPlane,
    CuratedPlane,
)


@pytest.mark.xfail(
    strict=True,
    reason="H5: five plane transition methods still write state directly instead of delegating",
)
def test_h5_g1_no_direct_state_transition_setpayload_outside_coordinator() -> None:
    offenders = [
        plane.__name__
        for plane in _PLANES
        if ".set_payload(" in inspect.getsource(plane.transition)
    ]
    assert offenders == []


@pytest.mark.xfail(
    strict=True,
    reason="H5: the accounted five-plane transition bypass denominator has not reached zero",
)
def test_h5_present_denominator_is_empty_after_accounted_migration() -> None:
    direct_writers = {
        f"{plane.__module__}:{plane.transition.__name__}"
        for plane in _PLANES
        if ".set_payload(" in inspect.getsource(plane.transition)
    }
    assert direct_writers == set()


@pytest.mark.xfail(
    strict=True,
    reason="H5: plane transition signatures do not yet require the coordinator/three-way Result",
)
def test_h5_each_plane_transition_requires_coordinator_and_preserves_final_pending_err() -> None:
    defects: list[str] = []
    for plane in _PLANES:
        signature = inspect.signature(plane.transition)
        coordinator = signature.parameters.get("coordinator")
        if coordinator is None or coordinator.default is not inspect.Parameter.empty:
            defects.append(f"{plane.__name__}:coordinator-not-required")
        if "Result" not in str(signature.return_annotation):
            defects.append(f"{plane.__name__}:return-is-not-Result")
    assert defects == []


@pytest.mark.xfail(
    strict=True,
    reason="H5: concept promotion receipt fields are not yet part of the atomic TransitionIntent",
)
def test_h5_concept_promotion_receipt_is_in_the_atomic_intended_patch() -> None:
    intent_fields = {field.name for field in fields(TransitionIntent)}
    assert {"promoted_to", "promoted_at"} <= intent_fields
