"""H5 red contract: every plane transition uses the canonical three-way coordinator boundary."""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from musubi.lifecycle.coordinator import (
    LifecycleTransitionCoordinator,
    TransitionIntent,
    TransitionPending,
    _intended_patch,
)
from musubi.lifecycle.transitions import TransitionResult
from musubi.planes.artifact.plane import ArtifactPlane
from musubi.planes.concept.plane import ConceptPlane
from musubi.planes.curated.plane import CuratedPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.planes.thoughts.plane import ThoughtsPlane
from musubi.types.common import Ok, generate_ksuid, utc_now
from musubi.types.concept import SynthesizedConcept
from musubi.types.lifecycle_event import LifecycleEvent

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


@pytest.mark.xfail(
    strict=True,
    reason="H5: concept receipt fields do not yet participate in digest/reconcile/readback",
)
def test_h5_concept_promotion_receipt_participates_in_replay_and_full_readback() -> None:
    promoted_to = generate_ksuid()
    promoted_at = utc_now().isoformat()
    base = {
        "collection": "musubi_concept",
        "object_id": generate_ksuid(),
        "namespace": "eric/shared/concept",
        "expected_version": 7,
        "target_state": "promoted",
        "actor": "test",
        "reason": "h5-receipt",
    }
    intent = TransitionIntent(**base, promoted_to=promoted_to, promoted_at=promoted_at)
    other = TransitionIntent(**base, promoted_to=generate_ksuid(), promoted_at=promoted_at)
    patch = _intended_patch(intent)
    assert patch["promoted_to"] == promoted_to
    assert patch["promoted_at"] == promoted_at

    coordinator = object.__new__(LifecycleTransitionCoordinator)
    assert coordinator._intent_digest(intent) != coordinator._intent_digest(other)
    actual = {
        "object_id": intent.object_id,
        "namespace": intent.namespace,
        **patch,
    }
    assert coordinator._confirm(patch, intent.object_id, intent.namespace, actual, 1) == "confirmed"
    assert "patch_json" in inspect.getsource(LifecycleTransitionCoordinator._reconcile_locked)


class _NonIterableOk(Ok[Any]):
    """Result that fails old tuple-unpack callers while preserving typed value access."""

    def __iter__(self) -> Any:
        raise AssertionError("transition Result must be consumed by variant, not tuple-unpacked")


class _ResultProbe:
    """Duck-typed Ok whose value access proves the caller consumed the variant."""

    kind = "ok"

    def __init__(self, value: object) -> None:
        self._value = value
        self.reads = 0

    @property
    def value(self) -> object:
        self.reads += 1
        return self._value


def _transition_result(*, object_id: str, namespace: str, to_state: str) -> TransitionResult:
    event = LifecycleEvent(
        object_id=object_id,
        object_type="concept",
        namespace=namespace,
        from_state="matured",
        to_state=to_state,
        actor="test",
        reason="h5-test",
    )
    return TransitionResult(
        object_id=object_id,
        object_type="concept",
        from_state="matured",
        to_state=to_state,
        version=2,
        event=event,
    )


def _promotion_deps(tmp_path: Path, outcome: object) -> tuple[Any, list[Any], list[str]]:
    from musubi.lifecycle.promotion import PromotionRender

    notifications: list[Any] = []
    rejections: list[str] = []

    class LLM:
        async def render_curated_markdown(self, **_kwargs: Any) -> PromotionRender:
            return PromotionRender(
                body="## H5\n" + "x" * 100,
                wikilinks=[],
                sections=["H5"],
            )

    class Vault:
        vault_root = tmp_path

        def write_curated(self, path: str, _frontmatter: Any, _body: str) -> Path:
            return tmp_path / path

    class Curated:
        async def create(self, memory: Any) -> Any:
            return memory

    class Concept:
        async def transition(self, **_kwargs: Any) -> object:
            return outcome

        async def record_promotion_rejection(self, **kwargs: Any) -> None:
            rejections.append(str(kwargs["reason"]))

    class Thoughts:
        async def emit(self, *args: Any, **kwargs: Any) -> None:
            notifications.append((args, kwargs))

    return (
        SimpleNamespace(
            llm=LLM(),
            vault_writer=Vault(),
            curated_plane=Curated(),
            concept_plane=Concept(),
            thoughts=Thoughts(),
        ),
        notifications,
        rejections,
    )


def _eligible_concept() -> SynthesizedConcept:
    now = utc_now()
    return SynthesizedConcept(
        object_id=generate_ksuid(),
        namespace="eric/shared/concept",
        title="H5 transition",
        content="content",
        synthesis_rationale="rationale",
        state="matured",
        reinforcement_count=3,
        importance=6,
        created_at=now - timedelta(days=3),
        updated_at=now - timedelta(days=3),
        merged_from=[generate_ksuid() for _ in range(3)],
    )


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="H5: promotion currently tuple-unpacks Pending and records a false rejection",
)
async def test_h5_promotion_pending_defers_notification_and_rejection(tmp_path: Path) -> None:
    from musubi.lifecycle.promotion import _promote_concept

    pending = TransitionPending(operation_key="h5-pending", event_id=generate_ksuid())
    deps, notifications, rejections = _promotion_deps(tmp_path, _NonIterableOk(value=pending))
    assert await _promote_concept(deps, _eligible_concept()) is False
    assert notifications == []
    assert rejections == []


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="H5: promotion currently tuple-unpacks the typed Final result",
)
async def test_h5_promotion_final_runs_dependent_work_once(tmp_path: Path) -> None:
    from musubi.lifecycle.promotion import _promote_concept

    concept = _eligible_concept()
    final = _transition_result(
        object_id=str(concept.object_id),
        namespace=str(concept.namespace),
        to_state="promoted",
    )
    outcome = _ResultProbe(final)
    deps, notifications, rejections = _promotion_deps(tmp_path, outcome)
    assert await _promote_concept(deps, concept) is True
    assert outcome.reads == 1
    assert len(notifications) == 1
    assert rejections == []


def _demotion_deps(outcome: object) -> tuple[Any, list[Any]]:
    point = SimpleNamespace(
        payload={
            "namespace": "eric/shared/concept",
            "object_id": generate_ksuid(),
            "created_epoch": 0.0,
            "last_reinforced_epoch": 0.0,
        }
    )

    class Qdrant:
        def scroll(self, **_kwargs: Any) -> tuple[list[Any], None]:
            return [point], None

    class Concept:
        async def transition(self, **_kwargs: Any) -> object:
            return outcome

    thoughts: list[Any] = []

    class Thoughts:
        async def emit(self, *args: Any, **kwargs: Any) -> None:
            thoughts.append((args, kwargs))

    return SimpleNamespace(qdrant=Qdrant(), concept_plane=Concept(), thoughts=Thoughts()), thoughts


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="H5: demotion currently counts Pending as completed",
)
async def test_h5_demotion_pending_does_not_increment_completed() -> None:
    from musubi.lifecycle.demotion import demotion_concept

    pending = TransitionPending(operation_key="h5-demotion", event_id=generate_ksuid())
    deps, thoughts = _demotion_deps(_NonIterableOk(value=pending))
    assert await demotion_concept(deps) == 0
    assert thoughts == []


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="H5: demotion currently ignores rather than consumes the typed Final result",
)
async def test_h5_demotion_final_increments_completed_once() -> None:
    from musubi.lifecycle.demotion import demotion_concept

    object_id = generate_ksuid()
    final = _transition_result(
        object_id=object_id,
        namespace="eric/shared/concept",
        to_state="demoted",
    )
    outcome = _ResultProbe(final)
    deps, thoughts = _demotion_deps(outcome)
    assert await demotion_concept(deps) == 1
    assert outcome.reads == 1
    assert len(thoughts) == 1


@pytest.mark.xfail(
    strict=True,
    reason="H5: migrated callers do not yet consume every typed transition Result",
)
def test_h5_coordinator_result_is_consumed_at_every_migrated_caller() -> None:
    roots = Path(__file__).parents[2] / "src" / "musubi"
    paths = (
        roots / "lifecycle" / "promotion.py",
        roots / "lifecycle" / "demotion.py",
        roots / "api" / "routers" / "writes_concept.py",
    )
    defects: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if not isinstance(func, ast.Attribute) or func.attr != "transition":
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Expr):
                defects.append(f"{path.name}:{node.lineno}:bare-result")
            if isinstance(parent, ast.Assign) and any(
                isinstance(target, (ast.Tuple, ast.List)) for target in parent.targets
            ):
                defects.append(f"{path.name}:{node.lineno}:tuple-unpack")
        if "TransitionPending" not in source:
            defects.append(f"{path.name}:missing-pending-branch")
    assert defects == []
