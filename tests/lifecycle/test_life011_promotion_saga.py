from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from musubi.lifecycle.coordinator import TransitionFinal
from musubi.lifecycle.promotion import _promote_concept
from musubi.types.common import Err, Ok, generate_ksuid
from musubi.types.concept import SynthesizedConcept


# Need to build a mock PromotionDeps
class MockVaultWriter:
    def __init__(self, tmp_path: Any) -> None:
        self.vault_root = tmp_path
        self.written: dict[str, Any] = {}

    def write_curated(self, rel_path: Any, fm_obj: Any, body: Any) -> None:
        self.written[rel_path] = (fm_obj, body)


class MockCuratedPlane:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.should_fail = False

    async def create(self, memory: Any) -> Any:
        if self.should_fail:
            raise RuntimeError("curated_plane.create failed")
        self.created.append(memory)
        return memory


class MockConceptPlane:
    def __init__(self) -> None:
        self.should_fail = False

    async def transition(self, *args: Any, **kwargs: Any) -> Any:
        if self.should_fail:
            from musubi.lifecycle.coordinator import TransitionError

            return Err(error=TransitionError(code="test_failure"))
        return Ok(value=TransitionFinal(operation_key="test", event_id="test", kind="final"))

    async def record_promotion_rejection(self, *args: Any, **kwargs: Any) -> Any:
        pass


class MockThoughtEmitter:
    def __init__(self) -> None:
        self.should_fail = False

    async def emit(self, *args: Any, **kwargs: Any) -> Any:
        if self.should_fail:
            raise RuntimeError("emit failed")


class MockLLM:
    async def render_curated_markdown(self, *args: Any, **kwargs: Any) -> Any:
        from musubi.lifecycle.promotion import PromotionRender

        return PromotionRender(body="## H2\n" + "body" * 50, wikilinks=[], sections=[])


class MockDeps:
    def __init__(self, tmp_path: Any) -> None:
        self.llm = MockLLM()
        self.vault_writer = MockVaultWriter(tmp_path)
        self.curated_plane = MockCuratedPlane()
        self.concept_plane = MockConceptPlane()
        self.thoughts = MockThoughtEmitter()
        self.coordinator = MagicMock()


def make_concept() -> SynthesizedConcept:
    return SynthesizedConcept(
        object_id=generate_ksuid(),
        namespace="eric/test/concept",
        title="Test Concept",
        content="content",
        synthesis_rationale="reason",
        state="matured",
    )


@pytest.mark.anyio
async def test_life011_saga_recovers_curated_create_failure(tmp_path: Path) -> None:
    deps = MockDeps(tmp_path)
    concept = make_concept()

    deps.curated_plane.should_fail = True

    # 1. First attempt writes file but fails at Qdrant create
    res1 = await _promote_concept(deps, concept)  # type: ignore[arg-type]
    assert res1 is False
    assert len(deps.vault_writer.written) == 1

    # Simulate writing the file to disk so the next run finds it
    rel_path = next(iter(deps.vault_writer.written.keys()))
    fm_obj, body = deps.vault_writer.written[rel_path]
    file_path = tmp_path / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    # just dump frontmatter and body
    from musubi.vault.frontmatter import dump_frontmatter

    file_path.write_text(dump_frontmatter(fm_obj.model_dump(), body))

    # Clear mocks for run 2
    deps.vault_writer.written.clear()
    deps.curated_plane.should_fail = False

    # 2. Second attempt should succeed and REUSE the object_id
    res2 = await _promote_concept(deps, concept)  # type: ignore[arg-type]
    assert res2 is True

    # Vault should have been rewritten (idempotently) with the same object_id
    assert len(deps.vault_writer.written) == 1
    new_fm_obj = next(iter(deps.vault_writer.written.values()))[0]
    assert new_fm_obj.object_id == fm_obj.object_id

    # Qdrant should have exactly one point with the reused object_id
    assert len(deps.curated_plane.created) == 1
    assert deps.curated_plane.created[0].object_id == fm_obj.object_id


@pytest.mark.anyio
async def test_life011_saga_recovers_concept_transition_failure(tmp_path: Path) -> None:
    deps = MockDeps(tmp_path)
    concept = make_concept()

    deps.concept_plane.should_fail = True

    # 1. First attempt writes file and Qdrant, but fails at concept transition
    res1 = await _promote_concept(deps, concept)  # type: ignore[arg-type]
    assert res1 is False
    assert len(deps.curated_plane.created) == 1
    first_curated_id = deps.curated_plane.created[0].object_id

    rel_path = next(iter(deps.vault_writer.written.keys()))
    fm_obj, body = deps.vault_writer.written[rel_path]
    file_path = tmp_path / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    from musubi.vault.frontmatter import dump_frontmatter

    file_path.write_text(dump_frontmatter(fm_obj.model_dump(), body))

    # Clear mocks for run 2
    deps.curated_plane.created.clear()
    deps.concept_plane.should_fail = False

    # 2. Second attempt should succeed and reuse the exact identity
    res2 = await _promote_concept(deps, concept)  # type: ignore[arg-type]
    assert res2 is True

    assert len(deps.curated_plane.created) == 1
    assert deps.curated_plane.created[0].object_id == first_curated_id


@pytest.mark.anyio
async def test_life011_saga_absorbs_thought_emit_failure_without_rejection(tmp_path: Path) -> None:
    deps = MockDeps(tmp_path)
    concept = make_concept()

    deps.thoughts.should_fail = True

    # Transition succeeds, but thought emit crashes
    res1 = await _promote_concept(deps, concept)  # type: ignore[arg-type]

    # The promotion is structurally sound. It must return True.
    assert res1 is True
    assert len(deps.curated_plane.created) == 1
