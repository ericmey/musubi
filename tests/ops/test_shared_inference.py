"""Executable contract for the authenticated shared-inference extraction.

Fixtures deliberately use documentation addresses. Live topology and secrets
belong in private inventory and 1Password, never in this public repository.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deploy" / "ansible"
APP_COMPOSE = ANSIBLE / "templates" / "docker-compose.yml.j2"
INFERENCE_COMPOSE = ANSIBLE / "templates" / "shared-inference-compose.yml.j2"
INFERENCE_UNIT = ANSIBLE / "templates" / "shared-inference.service.j2"
INGRESS = ANSIBLE / "templates" / "shared-inference-ingress.nginx.conf.j2"
SECRETS = ANSIBLE / "templates" / "shared-inference.htpasswd.tpl.j2"
APP_SECRETS = ANSIBLE / "templates" / "secrets.tpl.j2"
ENV = ANSIBLE / "templates" / "env.production.j2"
ADR = ROOT / "docs" / "Musubi" / "13-decisions" / "0045-authenticated-shared-inference-services.md"
SLICE = ROOT / "docs" / "Musubi" / "_slices" / "slice-ops-shared-inference.md"


def _services(path: Path) -> dict[str, Any]:
    rendered = re.sub(r"\{\{[^\n]+?\}\}", "template_value", path.read_text())
    return cast(dict[str, Any], yaml.safe_load(rendered)["services"])


def _compose(path: Path) -> dict[str, Any]:
    rendered = re.sub(r"\{\{[^\n]+?\}\}", "template_value", path.read_text())
    return cast(dict[str, Any], yaml.safe_load(rendered))


def test_shared_inference_is_owned_by_a_separate_deployment_unit() -> None:
    document = _compose(INFERENCE_COMPOSE)
    services = document["services"]
    assert {"tei-dense", "tei-sparse", "tei-reranker", "inference-ingress"} <= set(services)
    app_services = _services(APP_COMPOSE)
    assert not ({"tei-dense", "tei-sparse", "tei-reranker"} & set(app_services))
    unit = INFERENCE_UNIT.read_text()
    assert "shared-inference-compose.yml" in unit
    assert "PartOf=musubi.service" not in unit
    deploy = (ANSIBLE / "deploy.yml").read_text()
    for artifact in (
        "shared-inference-compose.yml",
        "shared-inference-ingress.conf",
        "shared-inference.htpasswd.tpl",
        "shared-inference.service",
    ):
        assert artifact in deploy
    assert "Require independently managed shared inference to be running" in deploy
    bootstrap = (ANSIBLE / "bootstrap.yml").read_text()
    assert "shared-inference-compose.yml" in bootstrap
    assert "shared-inference.service" in bootstrap


def test_tei_backends_are_not_host_published() -> None:
    document = _compose(INFERENCE_COMPOSE)
    services = document["services"]
    publishers = {name for name, service in services.items() if service.get("ports")}
    assert publishers == {"inference-ingress"}
    for name in ("tei-dense", "tei-sparse", "tei-reranker"):
        assert services[name]["networks"] == ["inference-backend"]
    assert set(services["inference-ingress"]["networks"]) == {
        "inference-backend",
        "shared-inference",
    }
    assert services["inference-ingress"]["networks"]["shared-inference"] == {
        "aliases": ["template_value"]
    }
    assert "musubi_inference_hostname" in INFERENCE_COMPOSE.read_text()
    assert "inference-backend" in document["networks"]
    app_services = _services(APP_COMPOSE)
    for consumer in ("core", "lifecycle-worker"):
        assert "inference-backend" not in app_services[consumer]["networks"]


def test_every_shared_endpoint_requires_authentication() -> None:
    ingress = INGRESS.read_text()
    for path in ("location /dense/", "location /sparse/", "location /reranker/"):
        assert path in ingress
    assert "auth_basic" in ingress
    assert "auth_basic_user_file /run/secrets/shared-inference.htpasswd" in ingress
    assert "proxy_pass http://tei-dense:80" in ingress
    assert "proxy_pass http://tei-sparse:80" in ingress
    assert "proxy_pass http://tei-reranker:80" in ingress
    assert "access_log off" in ingress
    assert "log_format" not in ingress
    assert "listen 8443 ssl" in ingress
    assert "ssl_certificate " in ingress and "ssl_certificate_key " in ingress
    migration = (ANSIBLE / "shared-inference-migrate.yml").read_text()
    assert "Prove ingress logs retain no request payload or payload hash" in migration
    assert 'case "$logs" in *"$sentinel"*|*"$digest"*) exit 96' in migration


def test_nginx_workers_can_read_only_the_ephemeral_password_hash() -> None:
    unit = INFERENCE_UNIT.read_text()
    assert "chown 101:101 /run/shared-inference-secrets/shared-inference.htpasswd" in unit
    assert "chmod 0400 /run/shared-inference-secrets/shared-inference.htpasswd" in unit
    assert (
        "chmod 0400 /run/shared-inference-secrets/tls.crt "
        "/run/shared-inference-secrets/tls.key" in unit
    )
    assert "chmod 0444" not in unit


def test_log_privacy_probe_uses_the_curl_image_entrypoint_once() -> None:
    migration = (ANSIBLE / "shared-inference-migrate.yml").read_text()
    probe = migration.split(
        "- name: Prove ingress logs retain no request payload or payload hash", maxsplit=1
    )[1].split("- name: Commit shared inference ownership", maxsplit=1)[0]
    assert probe.count("{{ musubi_curl_image }}") == 2
    assert "{{ musubi_curl_image }}\n                curl " not in probe


def test_consumers_receive_distinct_runtime_credentials() -> None:
    secrets = SECRETS.read_text()
    assert "musubi:op://" in secrets
    assert "chord:op://" in secrets
    assert "op://" in secrets
    assert not secrets.startswith("#")
    compose = INFERENCE_COMPOSE.read_text()
    assert "/run/shared-inference-secrets/shared-inference.htpasswd" in compose
    app_secrets = APP_SECRETS.read_text()
    for key in ("TEI_DENSE_URL", "TEI_SPARSE_URL", "TEI_RERANKER_URL"):
        assert re.search(rf"^{key}=op://", app_secrets, re.M)


def test_failed_cutover_keeps_the_old_authenticated_endpoint_protected() -> None:
    deploy = (ANSIBLE / "shared-inference-migrate.yml").read_text()
    assert "block:" in deploy
    assert "rescue:" in deploy
    assert "Allocate a per-attempt rollback directory" in deploy
    assert "Preserve every coupled live artifact for this attempt" in deploy
    assert "Rollback shared inference ownership" in deploy
    assert "tei-dense tei-sparse tei-reranker" in deploy
    assert deploy.index("Stop Compose-owned TEI for the bounded handoff") < deploy.index(
        "Verify shared inference parity"
    )
    assert deploy.index("Verify shared inference parity") < deploy.index(
        "Commit shared inference ownership"
    )
    assert deploy.index("Prove a real Musubi consumer") < deploy.index(
        "Record completed ownership migration"
    )


def test_cutover_waits_for_candidate_readiness_before_rolling_back() -> None:
    playbook = yaml.safe_load((ANSIBLE / "shared-inference-migrate.yml").read_text())
    migration = next(task for task in playbook[0]["tasks"] if "block" in task)
    parity = next(
        task
        for task in migration["block"]
        if task["name"] == "Verify shared inference parity through authentication"
    )
    assert parity["register"] == "inference_parity"
    # A cold candidate took 31 seconds to become request-ready in production.
    # Probe the three independent routes in parallel so that the additional
    # readiness window does not weaken the existing whole-operation deadline.
    assert parity["retries"] == 8
    assert parity["delay"] == 5
    assert parity["until"] == "inference_parity.rc == 0"
    assert parity["no_log"] is True
    command = parity["ansible.builtin.shell"]["cmd"]
    assert "/usr/bin/timeout --signal=TERM --kill-after=1s 5s" in command
    entrypoint = "--entrypoint /bin/sh"
    image = "{{ musubi_curl_image }}"
    assert command.index(entrypoint) < command.index(image) < command.index(" -ec '")
    assert "curl --parallel --parallel-immediate" in command
    assert "--fail --fail-early" in command
    assert command.count("--output /dev/null") == 3
    inner_script = command.split(" -ec '", 1)[1].rsplit("'", 1)[0]
    assert "\n" not in inner_script, "a folded command must not execute its options as commands"
    assert "--connect-timeout 2" in command
    assert "--max-time 3" in command
    outer_deadline = 5 + 1
    worst_case_seconds = (parity["retries"] + 1) * outer_deadline + parity["retries"] * parity[
        "delay"
    ]
    assert worst_case_seconds < 120


def test_log_privacy_probe_continues_commands_across_preserved_yaml_lines() -> None:
    playbook = yaml.safe_load((ANSIBLE / "shared-inference-migrate.yml").read_text())
    migration = next(task for task in playbook[0]["tasks"] if "block" in task)
    privacy = next(
        task
        for task in migration["block"]
        if task["name"] == "Prove ingress logs retain no request payload or payload hash"
    )
    command = privacy["ansible.builtin.shell"]["cmd"]
    inner_script = command.split("/bin/bash -ec '", 1)[1].rsplit("'", 1)[0]
    lines = inner_script.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("-"):
            assert index > 0 and lines[index - 1].rstrip().endswith("\\"), (
                f"line {index + 1} would execute {line.strip().split()[0]} as a command"
            )


def test_backup_compose_uses_the_live_project_identity() -> None:
    """A random backup directory must not become a new Compose project."""
    playbook = yaml.safe_load((ANSIBLE / "shared-inference-migrate.yml").read_text())
    tasks = playbook[0]["tasks"]
    migration = next(task for task in tasks if "block" in task)
    commands = {
        task["name"]: task["ansible.builtin.command"]["cmd"]
        for task in migration["block"]
        if task["name"]
        in {
            "Stop Musubi consumers during the model-owner handoff",
            "Stop Compose-owned TEI for the bounded handoff",
            "Remove stopped Compose-owned TEI containers",
        }
    }
    assert len(commands) == 3
    for command in commands.values():
        assert "--project-directory {{ musubi_config_dir }}" in command
        assert "-f {{ migration_backup.path }}/docker-compose.yml" in command


def test_check_mode_allocates_and_cleans_the_real_backup_directory() -> None:
    playbook = yaml.safe_load((ANSIBLE / "shared-inference-migrate.yml").read_text())
    play = playbook[0]
    tasks = play["tasks"]
    allocation = next(
        task for task in tasks if task["name"] == "Allocate a per-attempt rollback directory"
    )
    migration = next(task for task in tasks if "block" in task)
    cleanup = next(
        task
        for task in migration["always"]
        if task["name"] == "Remove per-attempt rollback material"
    )
    start = next(
        task
        for task in migration["block"]
        if task["name"] == "Start authenticated shared inference"
    )
    commit_secrets = next(
        task for task in migration["block"] if task["name"] == "Commit authenticated inference URLs"
    )
    assert allocation["check_mode"] is False
    assert play["force_handlers"] is True
    assert allocation["notify"] == "Remove rollback material after an early failure"
    early_failure_cleanup = next(
        handler
        for handler in play["handlers"]
        if handler["name"] == "Remove rollback material after an early failure"
    )
    assert early_failure_cleanup["check_mode"] is False
    assert early_failure_cleanup["ansible.builtin.file"] == {
        "path": "{{ migration_backup.path }}",
        "state": "absent",
    }
    assert start["when"] == "not ansible_check_mode"
    assert commit_secrets["when"] == "not ansible_check_mode"
    assert cleanup["check_mode"] is False
    assert cleanup["ansible.builtin.file"]["path"] == "{{ migration_backup.path }}"


def test_migration_requires_the_private_tls_hostname() -> None:
    for filename, section in (
        ("bootstrap.yml", "pre_tasks"),
        ("deploy.yml", "pre_tasks"),
        ("shared-inference-migrate.yml", "tasks"),
    ):
        playbook = yaml.safe_load((ANSIBLE / filename).read_text())
        requirement = next(
            task
            for task in playbook[0][section]
            if task["name"] == "Require the private shared inference hostname"
        )
        assertions = requirement["ansible.builtin.assert"]["that"]
        assert "musubi_inference_hostname is defined" in assertions
        assert "musubi_inference_hostname | length > 0" in assertions
        assert 'musubi_inference_hostname != "example.invalid"' in assertions
    setup = (ANSIBLE / "setup-control-host.sh").read_text()
    assert 'musubi_inference_hostname: ""' in setup
    assert "musubi_inference_hostname" in (ANSIBLE / "README.md").read_text()


def test_musubi_can_cut_over_and_roll_back_by_configuration() -> None:
    env = APP_SECRETS.read_text()
    for key in ("TEI_DENSE_URL", "TEI_SPARSE_URL", "TEI_RERANKER_URL"):
        assert re.search(rf"^{key}=op://", env, re.M)
    assert all(
        f"{key}=" not in ENV.read_text()
        for key in ("TEI_DENSE_URL", "TEI_SPARSE_URL", "TEI_RERANKER_URL")
    )
    app = APP_COMPOSE.read_text()
    for key in ("TEI_DENSE_URL", "TEI_SPARSE_URL", "TEI_RERANKER_URL"):
        assert app.count(f"{key}: ${{{key}}}") == 2


def test_normal_app_deploy_does_not_restart_shared_inference() -> None:
    deploy = (ANSIBLE / "deploy.yml").read_text()
    assert "Require independently managed shared inference to be running" in deploy
    assert (
        "state: restarted\n"
        not in deploy[deploy.index("shared-inference") : deploy.index("Start storage services")]
    )
    unit = (ANSIBLE / "templates" / "musubi.service.j2").read_text()
    assert "Requires=docker.service shared-inference.service" not in unit


def test_live_values_do_not_enter_public_sources() -> None:
    paths = (
        INFERENCE_COMPOSE,
        INFERENCE_UNIT,
        INGRESS,
        SECRETS,
        APP_SECRETS,
        ENV,
        ANSIBLE / "bootstrap.yml",
        ANSIBLE / "deploy.yml",
        ANSIBLE / "shared-inference-migrate.yml",
        ANSIBLE / "group_vars" / "all.yml",
        ADR,
        SLICE,
    )
    ipv4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    hostname = re.compile(r"\b(?:musubi|mizuki)\.mey\.house\b", re.I)
    allowed_examples = {"127.0.0.1", "10.0.0.0"}
    for path in paths:
        text = path.read_text()
        assert not [value for value in ipv4.findall(text) if value not in allowed_examples], path
        assert not hostname.search(text), path
