"""The release pin workflow updates both public Compose entry points."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/auto-digest-bump.yml"
PATCH_SCRIPT = ROOT / ".github/scripts/bump_compose_pin.py"
PUBLIC = ROOT / "docker-compose.yml"
QUICKSTART = ROOT / "quickstart/docker-compose.yml"
NEW_IMAGE = "ghcr.io/sourceblender/musubi-core@sha256:" + "b" * 64
PIN = re.compile(r"ghcr\.io/sourceblender/musubi-core@sha256:[0-9a-f]{64}")


def _workflow() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(WORKFLOW.read_text()))


def _patch_step() -> dict[str, Any]:
    steps = _workflow()["jobs"]["bump"]["steps"]
    return next(step for step in steps if step.get("id") == "patch")


def _run_patch(tmp_path: Path, public_text: str, quickstart_text: str) -> tuple[str, str]:
    public = tmp_path / "public.yml"
    quickstart = tmp_path / "quickstart.yml"
    public.write_text(public_text)
    quickstart.write_text(quickstart_text)
    env = {
        **os.environ,
        "FILE": str(public),
        "QUICKSTART": str(quickstart),
        "NEW_IMAGE": NEW_IMAGE,
        "TAG": "v9.9.9",
    }
    subprocess.run(["python3", str(PATCH_SCRIPT)], env=env, check=True)
    return public.read_text(), quickstart.read_text()


def test_public_pin_matches_quickstart() -> None:
    public = PIN.findall(PUBLIC.read_text())
    quickstart = PIN.findall(QUICKSTART.read_text())
    assert public == quickstart
    assert len(public) == 1


def test_patch_updates_public_and_quickstart_pin(tmp_path: Path) -> None:
    public, quickstart = _run_patch(tmp_path, PUBLIC.read_text(), QUICKSTART.read_text())
    assert PIN.findall(public) == [NEW_IMAGE]
    assert PIN.findall(quickstart) == [NEW_IMAGE]
    assert "x-core-version: &core-version v9.9.9" in public
    assert re.search(r"#\s*v9\.9\.9, cosign-signed", quickstart)


def test_patch_refuses_missing_public_pin(tmp_path: Path) -> None:
    broken = PIN.sub("musubi-core:dev", PUBLIC.read_text())
    try:
        _run_patch(tmp_path, broken, QUICKSTART.read_text())
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("missing public pin must fail")


def test_patch_refuses_missing_quickstart_pin_without_touching_public(tmp_path: Path) -> None:
    public_text = PUBLIC.read_text()
    quickstart_text = PIN.sub("musubi-core:dev", QUICKSTART.read_text())
    try:
        _run_patch(tmp_path, public_text, quickstart_text)
    except subprocess.CalledProcessError:
        assert (tmp_path / "public.yml").read_text() == public_text
    else:
        raise AssertionError("missing quickstart pin must fail")


def test_pin_pr_body_is_for_compose_operators() -> None:
    workflow = WORKFLOW.read_text()
    assert "git add docker-compose.yml quickstart/docker-compose.yml" in workflow
    assert "deploy/ansible/group_vars/all.yml" not in workflow
    assert "MUSUBI_PREFLIGHT_MANIFEST" not in workflow
    assert "docker compose" in workflow
    assert ".github/scripts/bump_compose_pin.py" in str(_patch_step()["run"])
