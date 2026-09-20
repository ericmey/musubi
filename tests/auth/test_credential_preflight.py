"""Issue #806 candidate-image live-credential compatibility gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from pydantic import AnyHttpUrl, SecretStr

from musubi.auth import credential_preflight
from musubi.auth.credential_preflight import run_preflight
from musubi.config import CredentialPreflightSettings, get_credential_preflight_settings
from musubi.settings import Settings


@pytest.fixture
def api_settings() -> Settings:
    return Settings.model_construct(
        jwt_signing_key=SecretStr("a-very-long-test-signing-key-for-hs256-tokens-32+bytes"),
        oauth_authority=AnyHttpUrl("https://auth.example.test"),
    )


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
                    {"file": "musubi-mcp-aoi.env", "presence": "aoi/command-chair"},
                    {"file": "musubi-mcp-yua.env", "presence": "yua/command-chair"},
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
    _write_env(
        tmp_path / "musubi-mcp-aoi.env", _token(api_settings, "aoi/command-chair")
    )
    _write_env(
        tmp_path / "musubi-mcp-yua.env", _token(api_settings, "yua/command-chair")
    )
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
    _write_env(
        tmp_path / "musubi-mcp-aoi.env", _token(api_settings, "aoi/command-chair")
    )
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
    _write_env(tmp_path / "musubi-mcp-aoi.env", accepted)
    _write_env(tmp_path / "musubi-mcp-yua.env", rejected)
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
    _write_env(
        tmp_path / "musubi-mcp-aoi.env", _token(api_settings, "aoi/command-chair")
    )
    _write_env(
        tmp_path / "musubi-mcp-yua.env", _token(api_settings, "yua/command-chair")
    )
    lines: list[str] = []

    result = run_preflight(
        manifest_path=_manifest(tmp_path),
        credential_dir=tmp_path,
        settings=api_settings,
        emit=lines.append,
    )

    assert result is True
    assert "INFO template musubi-mcp.env non-consumed-template" in lines


def test_candidate_preflight_reads_quoted_token_without_expanding_other_env(
    tmp_path: Path, api_settings: Settings
) -> None:
    token = _token(api_settings, "aoi/command-chair")
    (tmp_path / "musubi-mcp-aoi.env").write_text(
        f'# ignored\nOTHER=value=with=equals\nMUSUBI_TOKEN="{token}"\n'
    )
    _write_env(
        tmp_path / "musubi-mcp-yua.env", _token(api_settings, "yua/command-chair")
    )
    lines: list[str] = []

    assert run_preflight(
        manifest_path=_manifest(tmp_path),
        credential_dir=tmp_path,
        settings=api_settings,
        emit=lines.append,
    )
    assert "PASS live aoi/command-chair" in lines


def test_candidate_preflight_rejects_unclassified_discovered_credential(
    tmp_path: Path, api_settings: Settings
) -> None:
    _write_env(
        tmp_path / "musubi-mcp-aoi.env", _token(api_settings, "aoi/command-chair")
    )
    _write_env(
        tmp_path / "musubi-mcp-yua.env", _token(api_settings, "yua/command-chair")
    )
    _write_env(
        tmp_path / "musubi-mcp-unlisted.env",
        _token(api_settings, "unlisted/command-chair"),
    )
    lines: list[str] = []

    assert not run_preflight(
        manifest_path=_manifest(tmp_path),
        credential_dir=tmp_path,
        settings=api_settings,
        emit=lines.append,
    )
    assert "FAIL credential inventory unclassified musubi-mcp-unlisted.env" in lines


def test_candidate_preflight_reads_mode_0600_live_credentials(
    tmp_path: Path, api_settings: Settings
) -> None:
    for filename, presence in (
        ("musubi-mcp-aoi.env", "aoi/command-chair"),
        ("musubi-mcp-yua.env", "yua/command-chair"),
    ):
        path = tmp_path / filename
        _write_env(path, _token(api_settings, presence))
        path.chmod(0o600)

    assert run_preflight(
        manifest_path=_manifest(tmp_path),
        credential_dir=tmp_path,
        settings=api_settings,
        emit=lambda _line: None,
    )


@pytest.mark.parametrize(
    "manifest_body",
    [
        "not-json",
        "[]",
        '{"live": [], "templates": []}',
        '{"live": [{}], "templates": []}',
        '{"live": [{"file": "aoi.env", "presence": "aoi/command-chair"}]}',
    ],
)
def test_candidate_preflight_rejects_invalid_manifest(
    tmp_path: Path,
    api_settings: Settings,
    manifest_body: str,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(manifest_body)
    lines: list[str] = []

    assert not run_preflight(
        manifest_path=manifest,
        credential_dir=tmp_path,
        settings=api_settings,
        emit=lines.append,
    )
    assert lines == ["FAIL manifest invalid"]


def test_candidate_preflight_cli_uses_runtime_settings_and_emits_summary(
    tmp_path: Path,
    api_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_env(
        tmp_path / "musubi-mcp-aoi.env", _token(api_settings, "aoi/command-chair")
    )
    _write_env(
        tmp_path / "musubi-mcp-yua.env", _token(api_settings, "yua/command-chair")
    )
    monkeypatch.setattr(
        credential_preflight,
        "get_credential_preflight_settings",
        lambda: api_settings,
    )

    exit_code = credential_preflight.main(
        [
            "--manifest",
            str(_manifest(tmp_path)),
            "--credential-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert "PASS summary live=2/2 control=1/1 templates=1" in capsys.readouterr().out


def test_candidate_preflight_cli_fails_with_bounded_invalid_settings_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def invalid_settings() -> CredentialPreflightSettings:
        return CredentialPreflightSettings.model_validate({})

    monkeypatch.setattr(
        credential_preflight,
        "get_credential_preflight_settings",
        invalid_settings,
    )

    exit_code = credential_preflight.main(
        ["--manifest", str(tmp_path / "missing"), "--credential-dir", str(tmp_path)]
    )

    assert exit_code == 1
    assert capsys.readouterr().out == "FAIL preflight settings invalid\n"


def test_candidate_preflight_settings_require_only_auth_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SIGNING_KEY", "preflight-only-signing-key")
    monkeypatch.setenv("OAUTH_AUTHORITY", "https://auth.example.test")

    settings = get_credential_preflight_settings()

    assert settings.jwt_signing_key.get_secret_value() == "preflight-only-signing-key"
    assert str(settings.oauth_authority) == "https://auth.example.test/"
