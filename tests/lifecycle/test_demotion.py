"""Test contract for slice-lifecycle-promotion (Demotion)."""

from __future__ import annotations

import pytest

# Episodic
@pytest.mark.skip(reason="not implemented")
def test_episodic_demotion_selects_by_all_four_criteria() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_episodic_demotion_skips_if_accessed() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_episodic_demotion_skips_if_reinforced() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_episodic_demotion_skips_if_high_importance() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_episodic_demotion_skips_if_young() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_episodic_demotion_transitions_and_emits_event() -> None:
    pass

# Concept
@pytest.mark.skip(reason="deferred to slice-plane-concept: missing last_reinforced_at")
def test_concept_demotion_selects_by_last_reinforced() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_concept_demotion_emits_ops_thought() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_concept_reinforcement_resets_demotion_clock() -> None:
    pass

# Artifact
@pytest.mark.skip(reason="not implemented")
def test_artifact_archival_off_by_default() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_artifact_archival_respects_referenced_by() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_artifact_archival_transitions_to_archived_keeps_blob() -> None:
    pass

# Reinstatement
@pytest.mark.skip(reason="not implemented")
def test_reinstate_moves_back_to_matured() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_reinstate_resets_reinforced_clock() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_reinstate_emits_event() -> None:
    pass

# Filter
@pytest.mark.skip(reason="not implemented")
def test_default_retrieval_excludes_demoted() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_include_archived_includes_demoted() -> None:
    pass

# Migration safety
@pytest.mark.skip(reason="not implemented")
def test_demotion_paused_flag_honored() -> None:
    pass

@pytest.mark.skip(reason="not implemented")
def test_demotion_paused_expired_resumes() -> None:
    pass

# Property / Integration
@pytest.mark.skip(reason="deferred to test-property-demotion")
def test_hypothesis_demotion_is_idempotent_across_runs_with_no_change_in_criteria() -> None:
    pass

@pytest.mark.skip(reason="deferred to test-property-demotion")
def test_hypothesis_no_object_that_transitions_to_demoted_was_accessed_within_the_selection_window() -> None:
    pass

@pytest.mark.skip(reason="deferred to integration tests")
def test_integration_seed_1000_memories_with_varied_properties_run_weekly_demotion_count_transitions_matches_criteria() -> None:
    pass

@pytest.mark.skip(reason="deferred to integration tests")
def test_integration_reinstatement_round_trip_demote_reinstate_appears_in_default_retrieval() -> None:
    pass
