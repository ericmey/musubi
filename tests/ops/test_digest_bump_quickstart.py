"""The quickstart pins the same Core digest as group_vars, and the pin bump keeps it so (#836).

`quickstart/docker-compose.yml` promises "same digest as deploy/ansible/group_vars/all.yml".
auto-digest-bump used to patch and stage only group_vars, so every automated pin PR broke
that promise. These tests run the workflow's own patch script against copies of both files.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "auto-digest-bump.yml"
GROUP_VARS = ROOT / "deploy" / "ansible" / "group_vars" / "all.yml"
QUICKSTART = ROOT / "quickstart" / "docker-compose.yml"

CORE_IMAGE = re.compile(r"ghcr\.io/[a-z0-9-]+/musubi-core@sha256:[0-9a-f]{64}")
NEW_IMAGE = "ghcr.io/sourceblender/musubi-core@sha256:" + "b" * 64


def _steps() -> list[dict[str, object]]:
    wf = yaml.safe_load(WORKFLOW.read_text())
    return cast(list[dict[str, object]], wf["jobs"]["bump"]["steps"])


def _patch_step() -> dict[str, object]:
    [step] = [s for s in _steps() if s.get("id") == "patch"]
    return step


def _patch_script() -> str:
    run = str(_patch_step()["run"])
    m = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", run, re.S)
    assert m, "patch step no longer runs an inline python heredoc"
    return m.group(1)


def _run_patch(tmp_path: Path, tag: str = "v9.9.9") -> tuple[str, str]:
    gv = tmp_path / "all.yml"
    qs = tmp_path / "docker-compose.yml"
    gv.write_text(GROUP_VARS.read_text())
    qs.write_text(QUICKSTART.read_text())
    env = dict(os.environ, FILE=str(gv), QUICKSTART=str(qs), NEW_IMAGE=NEW_IMAGE, TAG=tag)
    subprocess.run([sys.executable, "-c", _patch_script()], env=env, check=True)
    return gv.read_text(), qs.read_text()


def test_repo_quickstart_pins_the_same_core_digest_as_group_vars() -> None:
    gv = CORE_IMAGE.findall(GROUP_VARS.read_text())
    qs = CORE_IMAGE.findall(QUICKSTART.read_text())
    assert len(gv) == 1 and len(qs) == 1, (gv, qs)
    assert gv == qs


def test_patch_moves_both_files_to_the_new_digest_and_tag(tmp_path: Path) -> None:
    gv, qs = _run_patch(tmp_path)
    assert CORE_IMAGE.findall(gv) == [NEW_IMAGE]
    assert CORE_IMAGE.findall(qs) == [NEW_IMAGE]
    assert 'musubi_core_version: "v9.9.9"' in gv
    assert re.search(r"#\s*v9\.9\.9, cosign-signed by publish-core-image\.yml", qs)


def test_patch_leaves_every_other_quickstart_line_alone(tmp_path: Path) -> None:
    _, qs = _run_patch(tmp_path)
    before = QUICKSTART.read_text().splitlines()
    after = qs.splitlines()
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(changed) == 2, changed  # the version comment and the core image line


def test_patch_fails_loudly_when_the_quickstart_pin_is_missing(tmp_path: Path) -> None:
    gv = tmp_path / "all.yml"
    qs = tmp_path / "docker-compose.yml"
    gv.write_text(GROUP_VARS.read_text())
    qs.write_text(CORE_IMAGE.sub("musubi-core:dev", QUICKSTART.read_text()))
    env = dict(os.environ, FILE=str(gv), QUICKSTART=str(qs), NEW_IMAGE=NEW_IMAGE, TAG="v9.9.9")
    proc = subprocess.run([sys.executable, "-c", _patch_script()], env=env, capture_output=True)
    assert proc.returncode != 0
    assert b"quickstart" in proc.stderr.lower()


def test_change_detection_and_commit_cover_the_quickstart() -> None:
    step = _patch_step()
    assert step["env"]["QUICKSTART"] == "quickstart/docker-compose.yml"  # type: ignore[index]
    assert 'git diff --quiet "$FILE" "$QUICKSTART"' in str(step["run"])
    commit_runs = [
        str(s.get("run", "")) for s in _steps() if "git switch -c" in str(s.get("run", ""))
    ]
    assert len(commit_runs) == 1
    assert (
        "git add deploy/ansible/group_vars/all.yml quickstart/docker-compose.yml" in commit_runs[0]
    )
