"""Tests for `musubi promote` subcommands (issue #220).

Drives the Typer app via `CliRunner`, stubs the HTTP layer via
`pytest-httpx` so we validate the wire shape (URL, params, body,
auth header) without spinning up the FastAPI stack.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from musubi.cli.main import app

_BASE = "http://localhost:8100/v1"
_TOKEN = "operator-token-fake"
_CONCEPT_ID = "3CmTEST0000000000000000000001"
_CURATED_ID = "3CmTEST0000000000000000000002"
_NAMESPACE = "eric/shared/concept"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _concept_reply(**overrides: object) -> dict[str, object]:
    base = {
        "object_id": _CONCEPT_ID,
        "namespace": _NAMESPACE,
        "title": "T",
        "content": "C",
        "synthesis_rationale": "R",
        "state": "promoted",
        "reinforcement_count": 3,
        "importance": 6,
        "merged_from": ["a", "b", "c"],
        "created_at": "2026-04-20T00:00:00+00:00",
        "updated_at": "2026-04-23T00:00:00+00:00",
        "version": 2,
    }
    base.update(overrides)
    return base


def test_force_promote_posts_to_api_with_operator_token(
    runner: CliRunner, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/concepts/{_CONCEPT_ID}/promote?namespace={_NAMESPACE}",
        json=_concept_reply(promoted_to=_CURATED_ID),
    )
    result = runner.invoke(
        app,
        [
            "promote",
            "force",
            _CONCEPT_ID,
            "--namespace",
            _NAMESPACE,
            "--curated-id",
            _CURATED_ID,
            "--token",
            _TOKEN,
        ],
    )
    assert result.exit_code == 0, result.output
    # Assert wire shape.
    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
    body = json.loads(request.read())
    assert body == {"promoted_to": _CURATED_ID, "reason": "operator-force"}
    # Output is the pretty-printed JSON body the server returned.
    output_json = json.loads(result.output)
    assert output_json["object_id"] == _CONCEPT_ID
    assert output_json["state"] == "promoted"


def test_force_promote_custom_reason_flows_into_body(
    runner: CliRunner, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/concepts/{_CONCEPT_ID}/promote?namespace={_NAMESPACE}",
        json=_concept_reply(promoted_to=_CURATED_ID),
    )
    result = runner.invoke(
        app,
        [
            "promote",
            "force",
            _CONCEPT_ID,
            "--namespace",
            _NAMESPACE,
            "--curated-id",
            _CURATED_ID,
            "--reason",
            "escalated by eric",
            "--token",
            _TOKEN,
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(httpx_mock.get_request().read())  # type: ignore[union-attr]
    assert body["reason"] == "escalated by eric"


def test_reject_sets_rejected_fields_and_posts_reason(
    runner: CliRunner, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/concepts/{_CONCEPT_ID}/reject?namespace={_NAMESPACE}",
        json=_concept_reply(
            state="matured",
            promotion_rejected_at="2026-04-23T12:00:00+00:00",
            promotion_rejected_reason="duplicate of other concept",
            promotion_attempts=1,
        ),
    )
    result = runner.invoke(
        app,
        [
            "promote",
            "reject",
            _CONCEPT_ID,
            "--namespace",
            _NAMESPACE,
            "--reason",
            "duplicate of other concept",
            "--token",
            _TOKEN,
        ],
    )
    assert result.exit_code == 0, result.output
    request = httpx_mock.get_request()
    assert request is not None
    body = json.loads(request.read())
    assert body == {"reason": "duplicate of other concept"}
    output_json = json.loads(result.output)
    assert output_json["promotion_rejected_reason"] == "duplicate of other concept"
    assert output_json["promotion_attempts"] == 1


def test_missing_token_exits_non_zero(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    # Scrub any inherited env so the CLI sees "nothing configured".
    monkeypatch.delenv("MUSUBI_TOKEN", raising=False)
    result = runner.invoke(
        app,
        [
            "promote",
            "reject",
            _CONCEPT_ID,
            "--namespace",
            _NAMESPACE,
            "--reason",
            "r",
        ],
    )
    assert result.exit_code == 2
    assert "no operator token configured" in (result.stderr or result.output)


def test_non_2xx_response_surfaces_to_stderr_and_exits_nonzero(
    runner: CliRunner, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/concepts/{_CONCEPT_ID}/reject?namespace={_NAMESPACE}",
        status_code=403,
        text='{"detail":"forbidden"}',
    )
    result = runner.invoke(
        app,
        [
            "promote",
            "reject",
            _CONCEPT_ID,
            "--namespace",
            _NAMESPACE,
            "--reason",
            "r",
            "--token",
            _TOKEN,
        ],
    )
    assert result.exit_code == 1
    assert "403" in (result.stderr or result.output)


def test_env_var_provides_api_url_and_token(
    runner: CliRunner, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_base = "http://musubi.test.local:9000/v1"
    monkeypatch.setenv("MUSUBI_API_URL", custom_base)
    monkeypatch.setenv("MUSUBI_TOKEN", _TOKEN)
    httpx_mock.add_response(
        method="POST",
        url=f"{custom_base}/concepts/{_CONCEPT_ID}/reject?namespace={_NAMESPACE}",
        json=_concept_reply(
            promotion_rejected_reason="r",
            promotion_attempts=1,
        ),
    )
    result = runner.invoke(
        app,
        [
            "promote",
            "reject",
            _CONCEPT_ID,
            "--namespace",
            _NAMESPACE,
            "--reason",
            "r",
        ],
    )
    assert result.exit_code == 0, result.output
    request = httpx_mock.get_request()
    assert request is not None
    assert request.url.host == "musubi.test.local"
    assert request.url.port == 9000
