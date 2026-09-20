"""Executable contract for the authenticated shared-inference extraction.

Fixtures deliberately use documentation addresses. Live topology and secrets
belong in private inventory and 1Password, never in this public repository.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deploy" / "ansible"
APP_COMPOSE = ANSIBLE / "templates" / "docker-compose.yml.j2"
INFERENCE_COMPOSE = ANSIBLE / "templates" / "shared-inference-compose.yml.j2"
INFERENCE_UNIT = ANSIBLE / "templates" / "shared-inference.service.j2"
INGRESS = ANSIBLE / "templates" / "shared-inference-ingress.nginx.conf.j2"
SECRETS = ANSIBLE / "templates" / "shared-inference.htpasswd.tpl.j2"
APP_SECRETS = ANSIBLE / "templates" / "secrets.tpl.j2"
TRANSITION_HTPASSWD = ANSIBLE / "templates" / "shared-inference.htpasswd.transition.tpl.j2"
AUTH_MIGRATION = ANSIBLE / "shared-inference-auth-migrate.yml"
ENV = ANSIBLE / "templates" / "env.production.j2"
ADR = ROOT / "docs" / "Musubi" / "13-decisions" / "0045-authenticated-shared-inference-services.md"
SLICE = ROOT / "docs" / "Musubi" / "_slices" / "slice-ops-shared-inference.md"


def _services(path: Path) -> dict[str, Any]:
    rendered = re.sub(r"\{\{[^\n]+?\}\}", "template_value", path.read_text())
    return cast(dict[str, Any], yaml.safe_load(rendered)["services"])


def _compose(path: Path) -> dict[str, Any]:
    rendered = re.sub(r"\{\{[^\n]+?\}\}", "template_value", path.read_text())
    return cast(dict[str, Any], yaml.safe_load(rendered))


def _render_expression(expression: str, **values: Any) -> Any:
    rendered = Environment(undefined=StrictUndefined).from_string(expression).render(**values)
    return yaml.safe_load(rendered)


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


def test_only_prometheus_joins_the_raw_inference_network() -> None:
    app = _compose(APP_COMPOSE)
    shared = _compose(INFERENCE_COMPOSE)
    assert shared["networks"]["inference-backend"]["name"] == "musubi-inference-backend"
    assert app["networks"]["inference-monitoring"] == {
        "name": "musubi-inference-backend",
        "external": True,
    }
    app_members = {
        name
        for name, service in app["services"].items()
        if "inference-monitoring" in service.get("networks", [])
    }
    raw_members = {
        name
        for name, service in shared["services"].items()
        if "inference-backend" in service.get("networks", [])
    }
    assert app_members == {"prometheus"}
    assert raw_members == {
        "tei-dense",
        "tei-sparse",
        "tei-reranker",
        "inference-ingress",
    }


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
    assert "musubi-v2:op://" in secrets
    assert "chord:op://" in secrets
    assert "op://" in secrets
    assert not secrets.startswith("#")
    compose = INFERENCE_COMPOSE.read_text()
    assert "/run/shared-inference-secrets/shared-inference.htpasswd" in compose
    app_secrets = APP_SECRETS.read_text()
    for key in ("TEI_DENSE_URL", "TEI_SPARSE_URL", "TEI_RERANKER_URL"):
        assert re.search(rf"^{key}=op://", app_secrets, re.M)
    assert "musubi_v2_username" in app_secrets
    assert "musubi_v2_password" in app_secrets


def test_exposed_credential_rotation_overlaps_old_and_new_before_cutover() -> None:
    transition = TRANSITION_HTPASSWD.read_text()
    assert "musubi:op://" in transition
    assert "musubi-v2:op://" in transition
    assert "chord:op://" in transition

    playbook = yaml.safe_load(AUTH_MIGRATION.read_text())
    tasks = playbook[0]["tasks"]
    names_all = [task["name"] for task in tasks]
    assert names_all.index("Inspect completed authentication migration marker") < names_all.index(
        "Allocate auth rollback material"
    )
    assert names_all.index("Verify the completed migration is still healthy") < names_all.index(
        "Stop after validating an already completed migration"
    )
    assert names_all.index(
        "Verify the completed migration still reaches every authenticated route"
    ) < names_all.index("Stop after validating an already completed migration")
    completed = {task["name"]: task for task in tasks}
    for name in (
        "Verify the completed migration is still healthy",
        "Verify the completed migration still reaches every authenticated route",
        "Stop after validating an already completed migration",
    ):
        assert completed[name]["when"] == "completed_auth_migration.stat.exists"
    assert (
        completed["Stop after validating an already completed migration"]["ansible.builtin.meta"]
        == "end_host"
    )
    completed_probe = completed[
        "Verify the completed migration still reaches every authenticated route"
    ]["ansible.builtin.command"]["cmd"]
    assert "docker compose -f" in completed_probe
    assert "exec -T core python -c" in completed_probe
    assert "docker exec musubi-core-1" not in completed_probe
    rotation = next(task for task in tasks if "block" in task)
    names = [task["name"] for task in rotation["block"]]
    for staged_name in (
        "Stage overlapping ingress credential references",
        "Stage replacement Musubi credential references",
    ):
        assert staged_name in names_all
        assert staged_name not in names
    early_cleanup = playbook[0]["handlers"][0]
    assert early_cleanup["loop"] == [
        "{{ auth_rotation_backup.path }}",
        "{{ musubi_config_dir }}/shared-inference.htpasswd.rotation.tpl",
        "{{ musubi_config_dir }}/secrets.shared-inference-v2.tpl",
    ]
    assert names.index("Install the overlapping credential set") < names.index(
        "Render the replacement shared-inference Compose definition"
    )
    assert names.index("Render the replacement shared-inference Compose definition") < names.index(
        "Reconcile the producer network before starting its consumers"
    )
    assert names.index(
        "Reconcile the producer network before starting its consumers"
    ) < names.index("Prove both old and replacement credentials during overlap")
    assert names.index("Prove both old and replacement credentials during overlap") < names.index(
        "Render the replacement Musubi Compose definition"
    )
    assert names.index("Render the replacement Musubi Compose definition") < names.index(
        "Pull the pinned replacement Musubi image"
    )
    assert names.index("Pull the pinned replacement Musubi image") < names.index(
        "Restart Musubi with the replacement credential"
    )
    assert names.index("Restart Musubi with the replacement credential") < names.index(
        "Wait for the replacement Musubi core to become healthy"
    )
    assert names.index("Wait for the replacement Musubi core to become healthy") < names.index(
        "Inspect migrated Musubi consumers"
    )
    assert names.index("Restart Musubi with the replacement credential") < names.index(
        "Remove the exposed credential from the ingress"
    )
    assert names.index("Remove the exposed credential from the ingress") < names.index(
        "Prove the exposed credential is rejected"
    )

    cleanup = next(
        task for task in rotation["always"] if task["name"] == "Remove staged auth templates"
    )
    assert cleanup["loop"] == [
        "{{ musubi_config_dir }}/shared-inference.htpasswd.rotation.tpl",
        "{{ musubi_config_dir }}/secrets.shared-inference-v2.tpl",
    ]

    by_name = {task["name"]: task for task in rotation["block"]}
    install = by_name["Install the overlapping credential set"]["ansible.builtin.shell"]["cmd"]
    assert (
        "/usr/bin/timeout --signal=TERM --kill-after=5s 60s /usr/bin/op inject --force" in install
    )
    assert 'mv -f "$candidate" /run/shared-inference-secrets/shared-inference.htpasswd' in install
    assert "docker compose" not in install

    for name in (
        "Remove the exposed credential from the ingress",
        "Remount overlapping credentials without restarting ready producers",
    ):
        action = by_name[name].get("ansible.builtin.shell") or by_name[name].get(
            "ansible.builtin.command"
        )
        assert action is not None
        command = action["cmd"]
        if name == "Remove the exposed credential from the ingress":
            assert (
                "/usr/bin/timeout --signal=TERM --kill-after=5s 60s /usr/bin/op inject --force"
                in command
            )
            assert 'cat "$candidate" >' not in command
            assert (
                'mv -f "$candidate" /run/shared-inference-secrets/shared-inference.htpasswd'
                in command
            )
        assert "/usr/bin/timeout --signal=TERM --kill-after=5s 30s" in command
        assert "up -d --no-deps --force-recreate inference-ingress" in command
        assert "exec -T inference-ingress nginx -s reload" not in command

    for name, result_name in (
        ("Prove both old and replacement credentials during overlap", "auth_overlap_probe"),
        ("Prove the exposed credential is rejected", "retired_credential_probe"),
    ):
        probe = by_name[name]
        expected_operations = 2 if result_name == "auth_overlap_probe" else 1
        assert (
            probe["ansible.builtin.shell"]["cmd"].count(
                "/usr/bin/timeout --signal=TERM --kill-after=1s 5s /usr/bin/op run"
            )
            == expected_operations
        )
        assert probe["ansible.builtin.shell"]["cmd"].count("--connect-timeout 2 --max-time 3") == (
            expected_operations
        )
        assert probe["register"] == result_name
        assert probe["retries"] == 30
        assert probe["delay"] == 1
        assert probe["until"] == f"{result_name}.rc == 0"


def test_old_credential_probes_support_url_or_header_auth() -> None:
    playbook = yaml.safe_load(AUTH_MIGRATION.read_text())
    rotation = next(task for task in playbook[0]["tasks"] if "block" in task)
    by_name = {task["name"]: task for task in rotation["block"]}
    for name in (
        "Prove both old and replacement credentials during overlap",
        "Prove the exposed credential is rejected",
    ):
        command = by_name[name]["ansible.builtin.shell"]["cmd"]
        assert "${TEI_BASIC_AUTH_USERNAME:-}" in command
        assert "${TEI_BASIC_AUTH_PASSWORD:-}" in command
        assert 'printf "user = \\"%s:%s\\"\\n"' in command
        assert 'printf "url = \\"%s/health\\"\\n"' in command


def test_credential_rotation_rolls_back_every_coupled_artifact() -> None:
    playbook = yaml.safe_load(AUTH_MIGRATION.read_text())
    tasks = playbook[0]["tasks"]
    preserved = next(
        task for task in tasks if task["name"] == "Preserve every coupled authentication artifact"
    )
    backed_up = {item["dest"] for item in preserved["loop"]}
    rotation = next(task for task in tasks if "block" in task)
    rescue = "\n".join(str(task) for task in rotation["rescue"])
    for artifact in (
        "secrets.tpl",
        "docker-compose.yml",
        "shared-inference-compose.yml",
        ".env.production",
        "shared-inference.htpasswd.tpl",
        "shared-inference.htpasswd",
    ):
        assert artifact in backed_up
        assert artifact in rescue
    assert "Restart Musubi with the restored credential" in rescue
    restore_runtime = next(
        task
        for task in rotation["rescue"]
        if task["name"] == "Restore the previous runtime shared-inference.htpasswd"
    )
    restore_command = restore_runtime["ansible.builtin.shell"]["cmd"]
    assert "> /run/shared-inference-secrets/shared-inference.htpasswd" not in restore_command
    assert (
        'cat "{{ auth_rotation_backup.path }}/shared-inference.htpasswd" > "$candidate"'
        in restore_command
    )
    assert 'chown 101:101 "$candidate"' in restore_command
    assert 'chmod 0400 "$candidate"' in restore_command
    assert (
        'mv -f "$candidate" /run/shared-inference-secrets/shared-inference.htpasswd'
        in restore_command
    )
    assert "docker compose" not in restore_command
    assert "exec -T inference-ingress nginx -s reload" not in restore_command
    restore_topology = next(
        task
        for task in rotation["rescue"]
        if task["name"]
        == "Restore the previous producer topology after a first-time migration failure"
    )
    topology_command = restore_topology["ansible.builtin.command"]["cmd"]
    assert "/usr/bin/timeout --signal=TERM --kill-after=5s 320s" in topology_command
    assert "up -d --force-recreate --wait --wait-timeout 300" in topology_command
    assert restore_topology["when"] == "producer_reconcile_required"
    remount = next(
        task
        for task in rotation["rescue"]
        if task["name"] == "Remount the restored ingress credential without restarting producers"
    )
    remount_command = remount["ansible.builtin.command"]["cmd"]
    assert "/usr/bin/timeout --signal=TERM --kill-after=5s 30s" in remount_command
    assert "up -d --no-deps --force-recreate inference-ingress" in remount_command
    assert remount["when"] == "not producer_reconcile_required"
    restored_probe = next(
        task
        for task in rotation["rescue"]
        if task["name"] == "Prove the restored ingress credential is ready"
    )
    assert (
        restored_probe["ansible.builtin.shell"]["cmd"].count(
            "/usr/bin/timeout --signal=TERM --kill-after=1s 5s /usr/bin/op run"
        )
        == 1
    )
    assert "--connect-timeout 2 --max-time 3" in restored_probe["ansible.builtin.shell"]["cmd"]
    assert restored_probe["register"] == "restored_credential_probe"
    assert restored_probe["retries"] == 30
    assert restored_probe["delay"] == 1
    assert restored_probe["until"] == "restored_credential_probe.rc == 0"


def test_auth_migration_reconciles_the_producer_network_before_consumers() -> None:
    playbook = yaml.safe_load(AUTH_MIGRATION.read_text())
    top_level = {task["name"]: task for task in playbook[0]["tasks"]}
    inspect = top_level["Inspect every shared-inference producer before rotation"]
    assert inspect["loop"] == ["tei-dense", "tei-sparse", "tei-reranker"]
    projection = top_level["Project producer readiness without assuming a container exists"][
        "ansible.builtin.set_fact"
    ]["producer_status"]
    for safe_default in (
        "item.exists | default(false)",
        "(item.container | default({})).get('State', {}).get('Running', false)",
        ".get('Health', {}).get(",
        ".get('NetworkSettings', {}).get('Networks', {})",
    ):
        assert safe_default in projection
    decision = top_level["Decide whether producer topology requires reconciliation"][
        "ansible.builtin.set_fact"
    ]["producer_reconcile_required"]
    for condition in (
        "not producer_backend_network.exists",
        "producer_status",
        "selectattr('exists', 'equalto', true)",
        "selectattr('running', 'equalto', true)",
        "selectattr('health', 'equalto', 'healthy')",
        "selectattr('attached', 'equalto', true)",
    ):
        assert condition in decision
    rotation = next(task for task in playbook[0]["tasks"] if "block" in task)
    by_name = {task["name"]: task for task in rotation["block"]}
    names = list(by_name)

    assert (
        names.index("Render the replacement shared-inference Compose definition")
        < names.index("Reconcile the producer network before starting its consumers")
        < names.index("Prove both old and replacement credentials during overlap")
    )
    reconcile = by_name["Reconcile the producer network before starting its consumers"]
    command = reconcile["ansible.builtin.command"]["cmd"]
    assert "/usr/bin/timeout --signal=TERM --kill-after=5s 320s" in command
    assert "docker compose -p shared-inference" in command
    assert "up -d --force-recreate --wait --wait-timeout 300" in command
    assert reconcile["when"] == "producer_reconcile_required"

    restore_names = [task["name"] for task in rotation["rescue"]]
    assert restore_names.index("Restore the previous shared-inference Compose definition") < (
        restore_names.index("Restore the previous runtime shared-inference.htpasswd")
    )


def test_producer_reconciliation_decision_executes_for_absent_or_healthless_containers() -> None:
    playbook = yaml.safe_load(AUTH_MIGRATION.read_text())
    top_level = {task["name"]: task for task in playbook[0]["tasks"]}
    projection = top_level["Project producer readiness without assuming a container exists"][
        "ansible.builtin.set_fact"
    ]["producer_status"]
    decision = top_level["Decide whether producer topology requires reconciliation"][
        "ansible.builtin.set_fact"
    ]["producer_reconcile_required"]

    healthy = {
        "exists": True,
        "container": {
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "NetworkSettings": {"Networks": {"musubi-inference-backend": {}}},
        },
    }
    missing = {"exists": False}
    healthless = {
        "exists": True,
        "container": {
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {"musubi-inference-backend": {}}},
        },
    }

    def projected(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        status: list[dict[str, Any]] = []
        for item in items:
            status = cast(
                list[dict[str, Any]],
                _render_expression(projection, producer_status=status, item=item),
            )
        return status

    def requires_reconciliation(items: list[dict[str, Any]]) -> bool:
        return cast(
            bool,
            _render_expression(
                decision,
                producer_backend_network={"exists": True},
                producer_status=projected(items),
            ),
        )

    assert requires_reconciliation([healthy, healthy, missing]) is True
    assert requires_reconciliation([healthy, healthy, healthless]) is True
    assert requires_reconciliation([healthy, healthy, healthy]) is False


def test_every_compose_recreate_is_bounded_and_routine_rotations_are_ingress_only() -> None:
    playbook = yaml.safe_load(AUTH_MIGRATION.read_text())
    rotation = next(task for task in playbook[0]["tasks"] if "block" in task)
    compose_tasks: list[tuple[dict[str, Any], str]] = []
    for task in [*rotation["block"], *rotation["rescue"]]:
        action = task.get("ansible.builtin.shell") or task.get("ansible.builtin.command") or {}
        command = action.get("cmd", "")
        if " up -d " in f" {command} ":
            compose_tasks.append((task, command))

    assert len(compose_tasks) == 5
    for task, command in compose_tasks:
        assert re.search(
            r"/usr/bin/timeout --signal=TERM --kill-after=\S+ \S+\s+"
            r"(?:/usr/bin/)?docker compose",
            command,
        )
        if "up -d --no-deps --force-recreate inference-ingress" in command:
            continue
        assert "up -d --force-recreate --wait --wait-timeout 300" in command
        assert task["when"] == "producer_reconcile_required"


def test_auth_migration_deploys_and_verifies_the_pinned_consumer_image() -> None:
    playbook = yaml.safe_load(AUTH_MIGRATION.read_text())
    rotation = next(task for task in playbook[0]["tasks"] if "block" in task)
    by_name = {task["name"]: task for task in rotation["block"]}

    pull = by_name["Pull the pinned replacement Musubi image"]
    command = pull["ansible.builtin.shell"]["cmd"]
    assert "/usr/bin/timeout --signal=TERM --kill-after=1s 109s /usr/bin/op run" in command
    assert "secrets.shared-inference-v2.tpl" in command
    assert "pull --policy always core lifecycle-worker" in command
    assert pull["no_log"] is True

    wait_lifecycle = by_name["Wait for the replacement lifecycle worker to become healthy"]
    assert wait_lifecycle["community.docker.docker_container_info"]["name"] == (
        "musubi-lifecycle-worker-1"
    )
    assert wait_lifecycle["retries"] == 60
    assert wait_lifecycle["delay"] == 5
    lifecycle_ready = " ".join(wait_lifecycle["until"].split())
    for condition in (
        "migrated_lifecycle_info.exists",
        "migrated_lifecycle_info.container.State.Running",
        "migrated_lifecycle_info.container.State.Health.Status == 'healthy'",
    ):
        assert condition in lifecycle_ready
    assert wait_lifecycle["no_log"] is True

    inspect = by_name["Inspect migrated Musubi consumers"]
    assert inspect["loop"] == ["core", "lifecycle-worker"]
    names = list(by_name)
    assert (
        names.index("Wait for the replacement Musubi core to become healthy")
        < names.index("Wait for the replacement lifecycle worker to become healthy")
        < names.index("Inspect migrated Musubi consumers")
    )
    projection = by_name["Project non-secret migrated consumer status"]
    assert projection["no_log"] is True
    projected = projection["ansible.builtin.set_fact"]["migrated_consumer_status"]
    for field in ("service", "image", "running", "health"):
        assert f"'{field}'" in projected
    verify = by_name["Require migrated consumers to run the pinned image"]
    assert "item.image == musubi_core_image" in verify["ansible.builtin.assert"]["that"]
    assert "item.running" in verify["ansible.builtin.assert"]["that"]
    health_predicate = " ".join(verify["ansible.builtin.assert"]["that"][2].split())
    assert "item.service != 'lifecycle-worker'" in health_predicate
    assert "item.health == 'healthy'" in health_predicate
    fail_msg = verify["ansible.builtin.assert"]["fail_msg"]
    for diagnostic in ("item.service", "image_match=", "running=", "health="):
        assert diagnostic in fail_msg
    assert "no_log" not in verify


def test_credential_rotation_keeps_secret_material_out_of_host_argv() -> None:
    migration = AUTH_MIGRATION.read_text()
    assert "--user $" not in migration
    assert "-u $" not in migration
    assert 'printf "user = ' in migration
    assert "--config -" in migration
    assert "no_log: true" in migration


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
