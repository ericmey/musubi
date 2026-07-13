"""Red contract for the SEC-005 deployment source reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deploy" / "ansible"
TEMPLATES = ANSIBLE / "templates"
SYSTEMD = TEMPLATES / "musubi.service.j2"
COMPOSE = TEMPLATES / "docker-compose.yml.j2"
CONFIG = ANSIBLE / "config.yml"
DEPLOY = ANSIBLE / "deploy.yml"

RED = pytest.mark.xfail(
    strict=True,
    reason="red proof for issue #423: current main cannot reproduce production",
)


@RED
def test_systemd_renders_qdrant_token_to_runtime_directory_before_compose() -> None:
    text = SYSTEMD.read_text()
    inject = text.index("op inject")
    start = text.index("op run")

    assert "RuntimeDirectory=musubi-secrets" in text
    assert "/run/musubi-secrets/qdrant.token" in text
    assert inject < start


@RED
def test_prometheus_mounts_rendered_runtime_qdrant_token_read_only() -> None:
    text = COMPOSE.read_text()

    assert "/run/musubi-secrets/qdrant.token:/etc/prometheus/qdrant.token:ro" in text
    assert "/etc/musubi/qdrant.token:/etc/prometheus/qdrant.token:ro" not in text


@RED
def test_material_musubi_secrets_are_not_rendered_to_persistent_files() -> None:
    deployment_text = "\n".join(path.read_text() for path in (CONFIG, DEPLOY, SYSTEMD, COMPOSE))

    assert ".env.production" not in deployment_text
    assert 'dest: "{{ musubi_config_dir }}/qdrant.token"' not in deployment_text
    assert not (TEMPLATES / "qdrant.token.j2").exists()


@RED
def test_config_play_renders_op_reference_templates_and_restarts_on_change() -> None:
    text = CONFIG.read_text()

    assert "templates/secrets.tpl.j2" in text
    assert "templates/qdrant.token.tpl.j2" in text
    assert "templates/musubi.service.j2" in text
    assert "Restart Musubi stack" in text


@RED
def test_deploy_play_uses_runtime_secret_templates() -> None:
    text = DEPLOY.read_text()

    assert "templates/secrets.tpl.j2" in text
    assert "templates/qdrant.token.tpl.j2" in text
    assert "qdrant.token.j2" not in text


@RED
def test_op_connect_inputs_are_root_only_and_secret_tasks_are_no_log() -> None:
    config = yaml.safe_load(CONFIG.read_text())
    tasks = config[0]["tasks"]
    secret_tasks = [
        task
        for task in tasks
        if any(
            name in yaml.safe_dump(task)
            for name in ("secrets.tpl", "qdrant.token.tpl", "connect.env")
        )
    ]

    assert secret_tasks
    for task in secret_tasks:
        assert task.get("no_log") is True
        template = task.get("ansible.builtin.template", {})
        if template:
            assert template.get("owner") == "root"
            assert template.get("mode") in {"0600", "0640"}


def test_ansible_templates_remain_parseable_controls() -> None:
    for path in (CONFIG, DEPLOY):
        parsed = yaml.safe_load(path.read_text())
        assert isinstance(parsed, list)
        assert parsed

    assert SYSTEMD.read_text().startswith("[Unit]\n")
    assert "services:" in COMPOSE.read_text()
