"""Test contract for slice-adapter-mcp."""

from __future__ import annotations

import pytest


# Parsing:
@pytest.mark.skip(reason="not implemented")
def test_all_tool_definitions_match_pydantic() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_tool_input_schemas_valid_json_schema() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_tool_output_schemas_match_response_shape() -> None:
    pass


# Tool invocation:
@pytest.mark.skip(reason="not implemented")
def test_memory_capture_invokes_sdk_with_mapped_args() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_memory_recall_invokes_retrieve_fast_mode() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_thought_send_invokes_sdk() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_artifact_upload_streams_bytes() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_prompts_are_tool_compositions() -> None:
    pass


# Scope enforcement:
@pytest.mark.skip(reason="not implemented")
def test_out_of_scope_capture_returns_mcp_error_not_exception() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_namespace_override_validated_against_token_scope() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_operator_only_tools_not_exposed() -> None:
    pass


# Auth:
@pytest.mark.skip(reason="not implemented")
def test_oauth_pkce_flow_integration() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_stdio_transport_uses_env_token() -> None:
    pass


# Errors:
@pytest.mark.skip(reason="not implemented")
def test_musubi_errors_mapped_to_mcp_codes_consistently() -> None:
    pass


# Resources:
@pytest.mark.skip(reason="not implemented")
def test_musubi_uri_resolves_to_structured_content() -> None:
    pass


@pytest.mark.skip(reason="not implemented")
def test_missing_object_returns_mcp_not_found() -> None:
    pass


# Streaming:
@pytest.mark.skip(reason="not implemented")
def test_streaming_memory_recall_yields_partial_results() -> None:
    pass


# Integration:
@pytest.mark.skip(reason="deferred to integration tests")
def test_integration_runs_canonical_contract_suite_against_adapter_live_Musubi_container() -> None:
    pass


@pytest.mark.skip(reason="deferred to integration tests")
def test_integration_claude_code_spawns_adapter_via_stdio_captures_recalls_round_trip_lt_500ms() -> (
    None
):
    pass
