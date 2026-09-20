"""Behavioral contracts for the operator-facing ``musubi-deploy`` wrapper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WRAPPER = ROOT / "scripts" / "musubi-deploy"


def test_multi_service_deploy_passes_structured_json_extra_vars(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ansible_dir = repo / "deploy" / "ansible"
    ansible_dir.mkdir(parents=True)
    (ansible_dir / "update.yml").touch()
    (ansible_dir / "inventory.yml").touch()

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "inventory-vars.yml").touch()
    (secrets / "vault.yml").touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ansible = fake_bin / "ansible-playbook"
    fake_ansible.write_text('#!/bin/sh\nfor arg in "$@"; do printf "ARG=%s\\n" "$arg"; done\n')
    fake_ansible.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "MUSUBI_REPO": str(repo),
            "MUSUBI_SECRETS_DIR": str(secrets),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    result = subprocess.run(
        [str(DEPLOY_WRAPPER), "core,lifecycle-worker"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    argv = [
        line.removeprefix("ARG=") for line in result.stdout.splitlines() if line.startswith("ARG=")
    ]
    assert '{"changed_services":["core","lifecycle-worker"]}' in argv
    assert not any(arg.startswith("changed_services=") for arg in argv)
