"""Fail-closed validation of configured credentials inside a candidate image."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt

from musubi.config import get_settings
from musubi.settings import Settings
from musubi.types.common import Err, Ok

from .tokens import InvalidTokenError, validate_token

Emit = Callable[[str], None]


def _read_token(path: Path) -> str | None:
    """Read only MUSUBI_TOKEN from an env file without expanding its contents."""

    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "MUSUBI_TOKEN":
            continue
        token = value.strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
            token = token[1:-1]
        return token or None
    return None


def _load_manifest(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    raw: Any = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("manifest must be an object")
    live = raw.get("live")
    templates = raw.get("templates")
    if not isinstance(live, list) or not live:
        raise ValueError("manifest must declare a non-empty live set")
    if not isinstance(templates, list):
        raise ValueError("manifest must declare its template set")

    live_rows: list[dict[str, str]] = []
    for row in live:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("file"), str)
            or not isinstance(row.get("presence"), str)
        ):
            raise ValueError("each live entry requires file and presence strings")
        live_rows.append({"file": row["file"], "presence": row["presence"]})

    template_rows: list[dict[str, str]] = []
    for row in templates:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("file"), str)
            or row.get("classification") != "non-consumed-template"
        ):
            raise ValueError("each template requires the non-consumed-template classification")
        template_rows.append({"file": row["file"], "classification": "non-consumed-template"})

    names = [row["file"] for row in live_rows + template_rows]
    if len(names) != len(set(names)):
        raise ValueError("credential files cannot appear in more than one class")
    return live_rows, template_rows


def _inconsistent_control_is_rejected(settings: Settings) -> bool:
    """Prove the candidate validator executes and retains the REQ-7 rejection."""

    now = datetime.now(UTC)
    presence = "preflight/control"
    token = jwt.encode(
        {
            "iss": str(settings.oauth_authority).rstrip("/"),
            "sub": "preflight-control",
            "aud": "musubi",
            "presence": presence,
            "scope": f"{presence}/episodic:r",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "jti": "candidate-preflight-negative-control",
        },
        settings.jwt_signing_key.get_secret_value(),
        algorithm="HS256",
    )
    result = validate_token(token, settings=settings)
    return (
        isinstance(result, Err)
        and isinstance(result.error, InvalidTokenError)
        and result.error.detail == "token subject is inconsistent with presence identity"
    )


def run_preflight(
    *,
    manifest_path: Path,
    credential_dir: Path,
    settings: Settings,
    emit: Emit,
) -> bool:
    """Validate the complete declared live set and a signed negative control."""

    try:
        live, templates = _load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError):
        emit("FAIL manifest invalid")
        return False

    passed = 0
    overall_ok = True
    for row in live:
        expected_presence = row["presence"]
        token = _read_token(credential_dir / row["file"])
        if token is None:
            emit(f"FAIL live {expected_presence} missing")
            overall_ok = False
            continue
        result = validate_token(token, settings=settings)
        if not isinstance(result, Ok) or result.value.presence != expected_presence:
            emit(f"FAIL live {expected_presence} rejected")
            overall_ok = False
            continue
        emit(f"PASS live {expected_presence}")
        passed += 1

    control_ok = _inconsistent_control_is_rejected(settings)
    emit(
        "PASS control inconsistent-identity rejected"
        if control_ok
        else "FAIL control inconsistent-identity accepted"
    )
    overall_ok = overall_ok and control_ok

    for row in templates:
        emit(f"INFO template {row['file']} {row['classification']}")

    status = "PASS" if overall_ok else "FAIL"
    emit(
        f"{status} summary live={passed}/{len(live)} "
        f"control={int(control_ok)}/1 templates={len(templates)}"
    )
    return overall_ok


def _stdout(line: str) -> None:
    sys.stdout.write(f"{line}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--credential-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    return (
        0
        if run_preflight(
            manifest_path=args.manifest,
            credential_dir=args.credential_dir,
            settings=get_settings(),
            emit=_stdout,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
