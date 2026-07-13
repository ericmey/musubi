"""Red contract for the SEC-005 deployment source reconciliation."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deploy" / "ansible"
TEMPLATES = ANSIBLE / "templates"
SYSTEMD = TEMPLATES / "musubi.service.j2"
COMPOSE = TEMPLATES / "docker-compose.yml.j2"
ENV = TEMPLATES / "env.production.j2"
CONFIG = ANSIBLE / "config.yml"
DEPLOY = ANSIBLE / "deploy.yml"
BOOTSTRAP = ANSIBLE / "bootstrap.yml"
UPDATE = ANSIBLE / "update.yml"
VAULT_EXAMPLE = ANSIBLE / "vault.example.yml"
SECRETS_TEMPLATE = TEMPLATES / "secrets.tpl.j2"
QDRANT_TEMPLATE = TEMPLATES / "qdrant.token.tpl.j2"
RUNTIME_PLAYBOOKS = (CONFIG, DEPLOY, BOOTSTRAP, UPDATE)


def test_systemd_renders_qdrant_token_to_runtime_directory_before_compose() -> None:
    text = SYSTEMD.read_text()
    inject = text.index("op inject")
    start = text.index("op run")

    assert "RuntimeDirectory=musubi-secrets" in text
    assert "UMask=0077" in text
    assert "/run/musubi-secrets/qdrant.token" in text
    assert inject < start


def test_prometheus_mounts_rendered_runtime_qdrant_token_read_only() -> None:
    text = COMPOSE.read_text()

    assert "/run/musubi-secrets/qdrant.token:/etc/prometheus/qdrant.token:ro" in text
    assert "/etc/musubi/qdrant.token:/etc/prometheus/qdrant.token:ro" not in text

    deploy = yaml.safe_load(DEPLOY.read_text())[0]
    prometheus_tasks = [
        task
        for task in deploy["tasks"]
        if task.get("ansible.builtin.template", {}).get("src") == "templates/prometheus.yml.j2"
    ]
    assert len(prometheus_tasks) == 1
    assert prometheus_tasks[0]["ansible.builtin.template"]["mode"] == "0644"


def test_material_musubi_secrets_are_not_rendered_to_persistent_files() -> None:
    deployment_text = "\n".join(path.read_text() for path in RUNTIME_PLAYBOOKS)
    env_text = ENV.read_text()

    assert 'dest: "{{ musubi_config_dir }}/qdrant.token"' not in deployment_text
    assert "JWT_SIGNING_KEY={{" not in env_text
    assert "QDRANT_API_KEY={{" not in env_text
    assert not (TEMPLATES / "qdrant.token.j2").exists()
    assert "vault_musubi_jwt_signing_key" not in VAULT_EXAMPLE.read_text()
    assert "vault_qdrant_api_key" not in VAULT_EXAMPLE.read_text()


def test_reference_templates_contain_only_expected_op_paths() -> None:
    secrets_text = SECRETS_TEMPLATE.read_text()
    qdrant_text = QDRANT_TEMPLATE.read_text()

    assert "JWT_SIGNING_KEY=op://Harem World/musubi-jwt-signing-key/credential" in secrets_text
    assert "QDRANT_API_KEY=op://Harem World/musubi-qdrant-auth/credential" in secrets_text
    assert "op://Harem World/musubi-qdrant-auth/credential" in qdrant_text
    assert "vault_" not in secrets_text + qdrant_text


def test_config_play_renders_op_reference_templates_and_restarts_on_change() -> None:
    text = CONFIG.read_text()

    assert "templates/secrets.tpl.j2" in text
    assert "templates/qdrant.token.tpl.j2" in text
    assert "templates/musubi.service.j2" in text
    assert "Restart Musubi stack" in text


def test_deploy_play_uses_runtime_secret_templates() -> None:
    text = DEPLOY.read_text()

    assert "templates/secrets.tpl.j2" in text
    assert "templates/qdrant.token.tpl.j2" in text
    assert "qdrant.token.j2" not in text
    assert "community.docker.docker_compose_v2:" not in text


def test_op_connect_inputs_are_root_only_and_secret_tasks_are_no_log() -> None:
    for playbook_path in RUNTIME_PLAYBOOKS:
        playbook = yaml.safe_load(playbook_path.read_text())
        play = playbook[0]
        preflight_text = yaml.safe_dump(play.get("pre_tasks", []))

        assert "/etc/musubi/connect.env" in preflight_text
        assert "0600" in preflight_text
        assert "no_log: true" in preflight_text

        for task in play.get("tasks", []):
            command = task.get("ansible.builtin.command") or task.get("ansible.builtin.shell")
            if command and "/usr/bin/op run" in str(command):
                assert task.get("no_log") is True

        reference_tasks = [
            task
            for task in play.get("tasks", [])
            if task.get("ansible.builtin.template", {}).get("src")
            in {"templates/secrets.tpl.j2", "templates/qdrant.token.tpl.j2"}
        ]
        assert len(reference_tasks) == 2
        assert all(task["ansible.builtin.template"]["mode"] == "0640" for task in reference_tasks)

    bootstrap = yaml.safe_load(BOOTSTRAP.read_text())[0]
    preflight_names = [task["name"] for task in bootstrap["pre_tasks"]]
    assert preflight_names[:3] == [
        "Inspect 1Password Connect environment file",
        "Require root-only 1Password Connect environment file",
        "Require the 1Password CLI runtime",
    ]


def test_ansible_op_run_tasks_source_the_root_only_connect_environment() -> None:
    op_run_tasks: list[dict[str, object]] = []
    for playbook_path in (DEPLOY, UPDATE):
        play = yaml.safe_load(playbook_path.read_text())[0]
        for task in play.get("tasks", []):
            module = task.get("ansible.builtin.shell") or task.get("ansible.builtin.command")
            if module and "/usr/bin/op run" in str(module):
                op_run_tasks.append(task)

    assert op_run_tasks
    for task in op_run_tasks:
        shell = task.get("ansible.builtin.shell")
        shell_text = str(shell or "")
        assert "/etc/musubi/connect.env" in shell_text
        assert "set -a" in shell_text
        assert task.get("no_log") is True


def test_ansible_templates_remain_parseable_controls() -> None:
    for path in RUNTIME_PLAYBOOKS:
        parsed = yaml.safe_load(path.read_text())
        assert isinstance(parsed, list)
        assert parsed

    assert SYSTEMD.read_text().startswith("[Unit]\n")
    assert "services:" in COMPOSE.read_text()
