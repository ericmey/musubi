"""RET-018 contract: reranker degradation keeps the base warning and adds bounded cause detail."""

from __future__ import annotations

from musubi.retrieve.warnings import (
    RetrievalWarning,
    dedupe,
    is_allowlisted,
    reranker_failed,
    wire_codes,
)


def test_reranker_failure_cause_is_bounded_and_wire_additive() -> None:
    warning = reranker_failed("episodic", cause="request_rejected")

    assert is_allowlisted(warning)
    assert wire_codes((warning,)) == (
        "reranker_failed",
        "reranker_failed_request_rejected",
    )
    assert not is_allowlisted(
        RetrievalWarning(code="reranker_failed", plane="episodic", cause="HTTP 413: raw body")
    )


def test_reranker_failure_causes_dedupe_without_double_counting_base_warning() -> None:
    warnings = dedupe(
        (
            reranker_failed("episodic", cause="timeout"),
            reranker_failed("episodic", cause="timeout"),
            reranker_failed("episodic", cause="invalid_response"),
        )
    )

    assert [warning.cause for warning in warnings] == ["timeout", "invalid_response"]
    assert wire_codes(warnings) == (
        "reranker_failed",
        "reranker_failed_timeout",
        "reranker_failed_invalid_response",
    )
