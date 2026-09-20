"""Issue #806 candidate-image live-credential compatibility gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt

from musubi.auth.credential_preflight import run_preflight
from musubi.settings import Settings


def _token(settings: Settings, presence: str, *, subject: str | None = None) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": str(settings.oauth_authority).rstrip("/"),
            "sub": subject or presence,
            "aud": "musubi",
            "presence": presence,
            "scope": f"{presence}/episodic:rw",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        settings.jwt_signing_key.get_secret_value(),
        algorithm="HS256",
    )


def _write_env(path: Path, token: str) -> None:
    path.write_text(f"MUSUBI_TOKEN={token}\n")


def _manifest(path: Path) -> Path:
    manifest = path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "live": [
                    {"file": "aoi.env", "presence": "aoi/command-chair"},
                    {"file": "yua.env", "presence": "yua/command-chair"},
                ],
                "templates": [
                    {
                        "file": "musubi-mcp.env",
                        "classification": "non-consumed-template",
                    }
                ],
            }
        )
    )
    return manifest


def test_candidate_preflight_accepts_every_declared_live_credential_and_control(
    tmp_path: Path, api_settings: Settings
) -> None:
    _write_env(tmp_path / "aoi.env", _token(api_settings, "aoi/command-chair"))
    _write_env(tmp_path / "yua.env", _token(api_settings, "yua/command-chair"))
    (tmp_path / "musubi-mcp.env").write_text("MUSUBI_TOKEN=template-not-live\n")
    lines: list[str] = []

    result = run_preflight(
        manifest_path=_manifest(tmp_path),
        credential_dir=tmp_path,
        settings=api_settings,
        emit=lines.append,
    )

    assert result is True
    assert lines == [
        "PASS live aoi/command-chair",
        "PASS live yua/command-chair",
        "PASS control inconsistent-identity rejected",
        "INFO template musubi-mcp.env non-consumed-template",
        "PASS summary live=2/2 control=1/1 templates=1",
    ]


def test_candidate_preflight_fails_closed_when_expected_live_credential_is_missing(
    tmp_path: Path, api_settings: Settings
) -> None:
    _write_env(tmp_path / "aoi.env", _token(api_settings, "aoi/command-chair"))
    lines: list[str] = []

    result = run_preflight(
        manifest_path=_manifest(tmp_path),
        credential_dir=tmp_path,
        settings=api_settings,
        emit=lines.append,
    )

    assert result is False
    assert "FAIL live yua/command-chair missing" in lines


def test_candidate_preflight_fails_closed_on_rejection_without_printing_token(
    tmp_path: Path, api_settings: Settings
) -> None:
    accepted = _token(api_settings, "aoi/command-chair")
    rejected = _token(
        api_settings,
        "yua/command-chair",
        subject="yua-command-chair",
    )
    _write_env(tmp_path / "aoi.env", accepted)
    _write_env(tmp_path / "yua.env", rejected)
    lines: list[str] = []

    result = run_preflight(
        manifest_path=_manifest(tmp_path),
        credential_dir=tmp_path,
        settings=api_settings,
        emit=lines.append,
    )

    assert result is False
    output = "\n".join(lines)
    assert "FAIL live yua/command-chair rejected" in output
    assert accepted not in output
    assert rejected not in output
    assert "token" not in output.lower()


def test_candidate_preflight_does_not_validate_declared_templates(
    tmp_path: Path, api_settings: Settings
) -> None:
    _write_env(tmp_path / "aoi.env", _token(api_settings, "aoi/command-chair"))
    _write_env(tmp_path / "yua.env", _token(api_settings, "yua/command-chair"))
    lines: list[str] = []

    result = run_preflight(
        manifest_path=_manifest(tmp_path),
        credential_dir=tmp_path,
        settings=api_settings,
        emit=lines.append,
    )

    assert result is True
    assert "INFO template musubi-mcp.env non-consumed-template" in lines
