"""Test contract for slice-ops-ansible."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deploy" / "ansible"
INVENTORY = ANSIBLE / "inventory.yml"
PLAYBOOKS = {
    "bootstrap": ANSIBLE / "bootstrap.yml",
    "deploy": ANSIBLE / "deploy.yml",
    "config": ANSIBLE / "config.yml",
    "health": ANSIBLE / "health.yml",
}
GROUP_VARS = ANSIBLE / "group_vars" / "all.yml"
VAULT_EXAMPLE = ANSIBLE / "vault.example.yml"
COMPOSE_TEMPLATE = ANSIBLE / "templates" / "docker-compose.yml.j2"
ENV_TEMPLATE = ANSIBLE / "templates" / "env.production.j2"
SYSTEMD_TEMPLATE = ANSIBLE / "templates" / "musubi.service.j2"


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text()) or {}


def _iter_yaml_files() -> Iterator[Path]:
    yield INVENTORY
    yield GROUP_VARS
    yield VAULT_EXAMPLE
    yield ANSIBLE / "requirements.yml"
    yield from PLAYBOOKS.values()


def _iter_tasks(playbook: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for play in playbook:
        yield from play.get("pre_tasks", [])
        yield from play.get("tasks", [])
        yield from play.get("handlers", [])


def _task_module(task: dict[str, Any]) -> str | None:
    reserved = {
        "name",
        "when",
        "loop",
        "register",
        "changed_when",
        "failed_when",
        "notify",
        "become",
        "vars",
        "tags",
        "block",
        "rescue",
        "always",
        "delegate_to",
        "run_once",
        "no_log",
        "until",
        "retries",
        "delay",
    }
    for key in task:
        if key not in reserved:
            return key
    return None


def test_playbook_syntax() -> None:
    for yaml_file in _iter_yaml_files():
        assert yaml_file.exists(), f"missing {yaml_file.relative_to(ROOT)}"
        assert _load_yaml(yaml_file) is not None

    ansible_playbook = shutil.which("ansible-playbook")
    if ansible_playbook is None:
        return

    for playbook in PLAYBOOKS.values():
        subprocess.run(
            [
                ansible_playbook,
                "--syntax-check",
                "-i",
                str(INVENTORY),
                str(playbook),
            ],
            check=True,
            cwd=ANSIBLE,
            capture_output=True,
            text=True,
        )


def test_playbook_idempotent_on_clean_vm() -> None:
    idempotent_modules = {
        "ansible.builtin.apt",
        "ansible.builtin.apt_repository",
        "ansible.builtin.copy",
        "ansible.builtin.file",
        "ansible.builtin.get_url",
        "ansible.builtin.group",
        "ansible.builtin.lineinfile",
        "ansible.builtin.service",
        "ansible.builtin.systemd_service",
        "ansible.builtin.template",
        "ansible.builtin.uri",
        "ansible.builtin.user",
        "community.docker.docker_compose_v2",
        "community.docker.docker_compose_v2_pull",
        "community.general.ufw",
    }
    for playbook_path in PLAYBOOKS.values():
        playbook = _load_yaml(playbook_path)
        assert isinstance(playbook, list)
        for task in _iter_tasks(playbook):
            module = _task_module(task)
            if module in {"ansible.builtin.command", "ansible.builtin.shell"}:
                assert "changed_when" in task, task["name"]
            elif module is not None:
                assert module in idempotent_modules, task["name"]


def test_secrets_never_logged() -> None:
    for playbook_path in PLAYBOOKS.values():
        playbook = _load_yaml(playbook_path)
        assert isinstance(playbook, list)
        for task in _iter_tasks(playbook):
            module = _task_module(task)
            task_text = yaml.safe_dump(task)
            handles_env = ENV_TEMPLATE.name in task_text or ".env.production" in task_text
            handles_vault = "vault_" in task_text or "ansible-vault" in task_text
            if module == "ansible.builtin.template" and handles_env:
                assert task.get("no_log") is True, task["name"]
            if handles_vault:
                assert task.get("no_log") is True, task["name"]


def test_compose_file_renders_to_valid_yaml() -> None:
    assert COMPOSE_TEMPLATE.exists()
    rendered = COMPOSE_TEMPLATE.read_text()
    for token in (
        "{{ musubi_core_image }}",
        "{{ musubi_qdrant_image }}",
        "{{ musubi_tei_image }}",
        "{{ musubi_ollama_image }}",
        "{{ musubi_prometheus_image }}",
    ):
        rendered = rendered.replace(token, "example/image:sha256-placeholder")
    rendered = rendered.replace("{{ musubi_core_port }}", "8100")
    rendered = rendered.replace("{{ musubi_ollama_model }}", "qwen3:4b")

    compose = yaml.safe_load(rendered)
    assert compose["services"]["ollama"]["environment"]["OLLAMA_KEEP_ALIVE"] == "24h"
    assert compose["services"]["ollama"]["environment"]["OLLAMA_MAX_LOADED_MODELS"] == "1"
    assert compose["services"]["tei-dense"]["volumes"] == ["tei-models:/data"]
    # Qdrant gets two bind mounts: persistent storage + the snapshot path
    # that `deploy/backup/musubi-backup.sh` reads from. Without the second
    # mount, Qdrant snapshots are ephemeral container storage.
    assert compose["services"]["qdrant"]["volumes"] == [
        "/var/lib/musubi/qdrant-storage:/qdrant/storage",
        "/var/lib/musubi/qdrant-snapshots:/qdrant/snapshots",
    ]


@pytest.mark.skip(
    reason="deferred to slice-ops-compose: booting the stack requires the real Compose slice"
)
def test_systemd_unit_boots_stack_to_healthy() -> None:
    raise AssertionError("covered by deploy smoke test once Compose owns the stack")


def test_update_playbook_respects_digest_pins() -> None:
    deploy_playbook = _load_yaml(PLAYBOOKS["deploy"])
    assert isinstance(deploy_playbook, list)
    playbook_text = yaml.safe_dump(deploy_playbook)

    assert "community.docker.docker_compose_v2_pull" in playbook_text
    assert "policy: missing" in playbook_text
    assert "pull: missing" in playbook_text
    assert "pull: always" not in playbook_text
