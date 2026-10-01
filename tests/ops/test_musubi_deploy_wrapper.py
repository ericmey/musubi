"""Behavioral contracts for the operator-facing ``musubi-deploy`` wrapper."""

from __future__ import annotations

import json
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
    structured_extra_vars = [
        json.loads(argv[index + 1])
        for index, argument in enumerate(argv[:-1])
        if argument == "-e" and argv[index + 1].startswith("{")
    ]
    assert {"changed_services": ["core", "lifecycle-worker"]} in structured_extra_vars
    assert not any(arg.startswith("changed_services=") for arg in argv)


def test_apply_requires_a_readable_operator_manifest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ansible_dir = repo / "deploy" / "ansible"
    ansible_dir.mkdir(parents=True)
    (ansible_dir / "update.yml").touch()
    (ansible_dir / "inventory.yml").touch()

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "inventory-vars.yml").touch()
    (secrets / "vault.yml").touch()
    authority = secrets / "authority.env"
    authority.touch()
    manifest = secrets / "manifest.json"
    manifest.write_text(
        '{"live":[{"file":"musubi-mcp-example.env","presence":"example/agent"}],"templates":[]}'
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ansible = fake_bin / "ansible-playbook"
    fake_ansible.write_text('#!/bin/sh\nprintf "ANSIBLE_RAN\\n"\n')
    fake_ansible.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "MUSUBI_REPO": str(repo),
            "MUSUBI_SECRETS_DIR": str(secrets),
            "MUSUBI_PREFLIGHT_AUTHORITY_ENV": str(authority),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    env.pop("MUSUBI_PREFLIGHT_MANIFEST", None)

    def apply() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(DEPLOY_WRAPPER), "--apply", "core"],
            input="y\n",
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

    absent = apply()
    assert absent.returncode == 2
    assert "MUSUBI_PREFLIGHT_MANIFEST" in absent.stderr
    assert "ANSIBLE_RAN" not in absent.stdout

    env["MUSUBI_PREFLIGHT_MANIFEST"] = str(secrets / "missing.json")
    missing = apply()
    assert missing.returncode == 2
    assert "preflight manifest not readable" in missing.stderr
    assert "ANSIBLE_RAN" not in missing.stdout

    env["MUSUBI_PREFLIGHT_MANIFEST"] = "manifest.json"
    relative = apply()
    assert relative.returncode == 2
    assert "must be an absolute path" in relative.stderr
    assert "ANSIBLE_RAN" not in relative.stdout

    env["MUSUBI_PREFLIGHT_MANIFEST"] = str(manifest)
    present = apply()
    assert present.returncode == 0
    assert "ANSIBLE_RAN" in present.stdout
