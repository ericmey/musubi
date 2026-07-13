"""Tests for the `musubi context` CLI."""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from musubi.cli.main import app

_BASE = "http://localhost:8100/v1"
_TOKEN = "operator-token-fake"


@pytest.fixture(autouse=True)
def _scrub_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUSUBI_API_URL", raising=False)
    monkeypatch.delenv("MUSUBI_TOKEN", raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _context_reply() -> dict[str, object]:
    return {
        "mode": "startup",
        "query_text": "Vice LoRA",
        "max_chars": 1200,
        "used_chars": 44,
        "suppressed": {"superseded": 1},
        "groups": [
            {
                "title": "Current-Project",
                "items": [
                    {
                        "object_id": "v053",
                        "namespace": "yua/command-chair/episodic",
                        "plane": "episodic",
                        "kind": "project-stance",
                        "staleness": "durable",
                        "content": "V-053 promptsmith compiler route.",
                        "evidence_handle": "yua/command-chair/episodic/v053",
                        "why_surfaced": "durable project-stance",
                        "score": 9.1,
                    }
                ],
            }
        ],
    }


def test_context_posts_to_api_and_renders_grouped_output(
    runner: CliRunner,
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(method="POST", url=f"{_BASE}/context", json=_context_reply())
    result = runner.invoke(
        app,
        [
            "context",
            "--namespace",
            "yua/command-chair",
            "--query",
            "Vice LoRA",
            "--planes",
            "episodic,curated",
            "--token",
            _TOKEN,
        ],
    )

    assert result.exit_code == 0, result.output
    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
    body = json.loads(request.read())
    assert body["namespace"] == "yua/command-chair"
    assert body["query_text"] == "Vice LoRA"
    assert body["planes"] == ["episodic", "curated"]
    assert "Current-Project:" in result.output
    assert "V-053 promptsmith compiler route." in result.output


def test_context_json_flag_emits_raw_response(
    runner: CliRunner,
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(method="POST", url=f"{_BASE}/context", json=_context_reply())
    result = runner.invoke(
        app,
        ["context", "--query", "Vice LoRA", "--token", _TOKEN, "--json"],
    )

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["groups"][0]["title"] == "Current-Project"


# --------------------------------------------------------------------------- #
# RET-007 — the non-JSON CLI must VISIBLY render degradation warnings; JSON preserves them naturally.
# Owner slice: slice-ret007-degradation-impl (#422). Tests-only.
# --------------------------------------------------------------------------- #


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


def _degraded_reply() -> dict[str, object]:
    reply = _context_reply()
    reply["warnings"] = ["plane_timeout_episodic"]
    return reply


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="CLI _render() reads only 'groups' and ignores 'warnings' — the non-JSON path renders degraded context indistinguishably from healthy",
)
def test_context_nonjson_renders_warnings(runner: CliRunner, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=f"{_BASE}/context", json=_degraded_reply())
    result = runner.invoke(
        app,
        ["context", "--query", "Vice LoRA", "--planes", "episodic", "--token", _TOKEN],
    )
    assert result.exit_code == 0, result.output
    if "plane_timeout_episodic" not in result.output:
        raise DefectStillPresent(
            f"non-JSON CLI dropped the degradation warning from its rendered output: {result.output!r}"
        )


def test_context_json_preserves_warnings(runner: CliRunner, httpx_mock: HTTPXMock) -> None:
    """CONTROL (green now + post-impl): the --json path dumps the raw response, so warnings survive
    naturally once the wire carries them."""
    httpx_mock.add_response(method="POST", url=f"{_BASE}/context", json=_degraded_reply())
    result = runner.invoke(
        app,
        ["context", "--query", "Vice LoRA", "--token", _TOKEN, "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output).get("warnings") == ["plane_timeout_episodic"]
