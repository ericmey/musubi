"""Executable contract for the authenticated shared-inference extraction.

Fixtures deliberately use documentation addresses. Live topology and secrets
belong in private inventory and 1Password, never in this public repository.
"""

from __future__ import annotations

import re
from pathlib import Path

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


def _services(path: Path) -> dict:
    rendered = re.sub(r"\{\{[^\n]+?\}\}", "template_value", path.read_text())
    return yaml.safe_load(rendered)["services"]


def test_shared_inference_is_owned_by_a_separate_deployment_unit() -> None:
    services = _services(INFERENCE_COMPOSE)
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
    assert "name: shared-inference" in deploy
    bootstrap = (ANSIBLE / "bootstrap.yml").read_text()
    assert "shared-inference-compose.yml" in bootstrap
    assert "shared-inference.service" in bootstrap


def test_tei_backends_are_not_host_published() -> None:
    services = _services(INFERENCE_COMPOSE)
    publishers = {name for name, service in services.items() if service.get("ports")}
    assert publishers == {"inference-ingress"}


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
    assert "docker-compose.pre-shared-inference.yml" in deploy
    assert "Rollback shared inference ownership" in deploy
    assert "tei-dense tei-sparse tei-reranker" in deploy
    assert deploy.index("Stop Compose-owned TEI for the bounded handoff") < deploy.index(
        "Verify shared inference parity")
    assert deploy.index("Verify shared inference parity") < deploy.index(
        "Commit shared inference ownership")


def test_musubi_can_cut_over_and_roll_back_by_configuration() -> None:
    env = APP_SECRETS.read_text()
    for key in ("TEI_DENSE_URL", "TEI_SPARSE_URL", "TEI_RERANKER_URL"):
        assert re.search(rf"^{key}=op://", env, re.M)
    assert all(f"{key}=" not in ENV.read_text() for key in
               ("TEI_DENSE_URL", "TEI_SPARSE_URL", "TEI_RERANKER_URL"))


def test_live_values_do_not_enter_public_sources() -> None:
    paths = (INFERENCE_COMPOSE, INFERENCE_UNIT, INGRESS, SECRETS, APP_SECRETS, ENV)
    ipv4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    hostname = re.compile(r"\b(?:musubi|mizuki)\.mey\.house\b", re.I)
    for path in paths:
        text = path.read_text()
        assert not [value for value in ipv4.findall(text) if not value.startswith("127.")], path
        assert not hostname.search(text), path
