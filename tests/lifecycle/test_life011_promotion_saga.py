from pathlib import Path
from typing import Any

import pytest

from musubi.lifecycle.coordinator import TransitionError, TransitionFinal
from musubi.lifecycle.promotion import _promote_concept
from musubi.types.common import Err, Ok, generate_ksuid
from musubi.types.concept import SynthesizedConcept
from musubi.types.curated import CuratedKnowledge


class MockVaultWriter:
    def __init__(self, tmp_path: Path) -> None:
        self.vault_root = tmp_path
        self.written: dict[str, tuple[Any, str]] = {}
        self.should_fail = False

    def write_curated(self, rel_path: str, fm_obj: Any, body: str) -> None:
        if self.should_fail:
            raise OSError("vault write failed")
        self.written[rel_path] = (fm_obj, body)


class MockCuratedPlane:
    def __init__(self) -> None:
        self.rows: dict[str, CuratedKnowledge] = {}
        self.create_calls: list[str] = []
        self.should_fail = False

    async def create(self, memory: CuratedKnowledge) -> CuratedKnowledge:
        self.create_calls.append(str(memory.object_id))
        if self.should_fail:
            raise RuntimeError("curated_plane.create failed")
        for row in self.rows.values():
            if (
                row.namespace == memory.namespace
                and row.vault_path == memory.vault_path
                and row.promoted_from == memory.promoted_from
            ):
                return row
        self.rows[str(memory.object_id)] = memory
        return memory


class MockConceptPlane:
    def __init__(self) -> None:
        self.should_fail = False
        self.transition_calls: list[dict[str, Any]] = []
        self.rejection_calls: list[dict[str, Any]] = []

    async def transition(self, *args: Any, **kwargs: Any) -> Any:
        if self.should_fail:
            return Err(error=TransitionError(code="test_failure"))
        self.transition_calls.append(kwargs)
        return Ok(value=TransitionFinal(operation_key="test", event_id="test", kind="final"))

    async def record_promotion_rejection(self, *args: Any, **kwargs: Any) -> None:
        self.rejection_calls.append(kwargs)


class MockThoughtEmitter:
    def __init__(self) -> None:
        self.should_fail = False

    async def emit(self, *args: Any, **kwargs: Any) -> None:
        if self.should_fail:
            raise RuntimeError("emit failed")


class MockLLM:
    async def render_curated_markdown(self, *args: Any, **kwargs: Any) -> Any:
        from musubi.lifecycle.promotion import PromotionRender

        return PromotionRender(body="## H2\n" + "body" * 50, wikilinks=[], sections=[])


class MockDeps:
    def __init__(self, tmp_path: Path) -> None:
        self.llm = MockLLM()
        self.vault_writer = MockVaultWriter(tmp_path)
        self.curated_plane = MockCuratedPlane()
        self.concept_plane = MockConceptPlane()
        self.thoughts = MockThoughtEmitter()
        self.coordinator = None


def make_concept() -> SynthesizedConcept:
    return SynthesizedConcept(
        object_id=generate_ksuid(),
        namespace="eric/test/concept",
        title="Test Concept",
        content="content",
        synthesis_rationale="reason",
        state="matured",
    )


@pytest.mark.asyncio
async def test_life011_saga_recovers_vault_write_failure(tmp_path: Path) -> None:
    deps = MockDeps(tmp_path)
    concept = make_concept()

    deps.vault_writer.should_fail = True

    res1 = await _promote_concept(deps, concept)  # type: ignore[arg-type]
    assert res1 is False
    assert deps.vault_writer.written == {}
    assert len(deps.curated_plane.rows) == 1
    first_row_id = next(iter(deps.curated_plane.rows))

    deps.vault_writer.should_fail = False

    res2 = await _promote_concept(deps, concept)  # type: ignore[arg-type]
    assert res2 is True

    # Retry proposed a fresh ID, but CuratedPlane re-adopted the existing
    # row.  The vault file and transition must use that canonical ID.
    assert len(deps.curated_plane.rows) == 1
    assert len(deps.curated_plane.create_calls) == 2
    assert deps.curated_plane.create_calls[0] != deps.curated_plane.create_calls[1]
    written_fm = next(iter(deps.vault_writer.written.values()))[0]
    assert str(written_fm.object_id) == first_row_id
    assert deps.concept_plane.transition_calls[-1].get("promoted_to") == first_row_id


@pytest.mark.asyncio
async def test_life011_saga_recovers_concept_transition_failure(tmp_path: Path) -> None:
    deps = MockDeps(tmp_path)
    concept = make_concept()

    deps.concept_plane.should_fail = True

    res1 = await _promote_concept(deps, concept)  # type: ignore[arg-type]
    assert res1 is False
    assert len(deps.curated_plane.rows) == 1

    # Do NOT clear curated_plane.rows
    first_curated_id = next(iter(deps.curated_plane.rows.keys()))

    # Write the file to disk
    rel_path = next(iter(deps.vault_writer.written.keys()))
    fm_obj, body = deps.vault_writer.written[rel_path]
    file_path = tmp_path / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    from musubi.vault.frontmatter import dump_frontmatter

    file_path.write_text(
        dump_frontmatter(fm_obj.model_dump(by_alias=True, exclude_none=True), body)
    )

    deps.concept_plane.should_fail = False

    res2 = await _promote_concept(deps, concept)  # type: ignore[arg-type]
    assert res2 is True

    # Assert len(rows)==1 after BOTH attempts
    assert len(deps.curated_plane.rows) == 1
    # create_calls == [same_id, same_id]
    assert deps.curated_plane.create_calls == [first_curated_id, first_curated_id]
    # final successful transition kwargs promoted_to == same_id
    assert deps.concept_plane.transition_calls[-1].get("promoted_to") == first_curated_id


@pytest.mark.asyncio
async def test_life011_saga_absorbs_thought_emit_failure_without_rejection(tmp_path: Path) -> None:
    deps = MockDeps(tmp_path)
    concept = make_concept()

    deps.thoughts.should_fail = True

    res1 = await _promote_concept(deps, concept)  # type: ignore[arg-type]

    assert res1 is True
    # Emit failure test asserts rejection_calls == []
    assert deps.concept_plane.rejection_calls == []
    # A successful transition was recorded
    assert len(deps.concept_plane.transition_calls) == 1
