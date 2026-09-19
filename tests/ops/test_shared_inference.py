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
INGRESS = ANSIBLE / "templates" / "shared-inference-ingress.Caddyfile.j2"
SECRETS = ANSIBLE / "templates" / "shared-inference-secrets.tpl.j2"
ENV = ANSIBLE / "templates" / "env.production.j2"
POLICY = ANSIBLE / "templates" / "shared-inference-firewall.sh.j2"


def _services(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"]


def test_shared_inference_is_owned_by_a_separate_deployment_unit() -> None:
    services = _services(INFERENCE_COMPOSE)
    assert {"tei-dense", "tei-sparse", "tei-reranker", "inference-ingress"} <= set(services)
    app_services = _services(APP_COMPOSE)
    assert not ({"tei-dense", "tei-sparse", "tei-reranker"} & set(app_services))
    unit = INFERENCE_UNIT.read_text()
    assert "shared-inference-compose.yml" in unit
    assert "PartOf=musubi.service" not in unit


def test_tei_backends_are_not_host_published() -> None:
    services = _services(INFERENCE_COMPOSE)
    for name in ("tei-dense", "tei-sparse", "tei-reranker"):
        assert "ports" not in services[name], name
    assert "ports" in services["inference-ingress"]


def test_every_shared_endpoint_requires_authentication() -> None:
    ingress = INGRESS.read_text()
    for path in ("/dense/*", "/sparse/*", "/reranker/*"):
        assert path in ingress
    assert ingress.count("Authorization") >= 3
    assert "reverse_proxy tei-dense:80" in ingress
    assert "reverse_proxy tei-sparse:80" in ingress
    assert "reverse_proxy tei-reranker:80" in ingress


def test_consumers_receive_distinct_runtime_credentials() -> None:
    secrets = SECRETS.read_text()
    assert "MUSUBI_INFERENCE_TOKEN=" in secrets
    assert "CHORD_INFERENCE_TOKEN=" in secrets
    assert "op://" in secrets
    assert "MUSUBI_INFERENCE_TOKEN=${MUSUBI_INFERENCE_TOKEN}" in INFERENCE_COMPOSE.read_text()
    assert "CHORD_INFERENCE_TOKEN=${CHORD_INFERENCE_TOKEN}" in INFERENCE_COMPOSE.read_text()


def test_interrupted_policy_stage_never_becomes_the_only_live_policy() -> None:
    policy = POLICY.read_text()
    stage = policy.index("STAGE=")
    attach_stage = policy.index("-j \"$STAGE\"")
    retire_old = policy.index("-X \"$ACTIVE\"")
    assert stage < attach_stage < retire_old
    assert "-C DOCKER-USER" in policy


def test_failed_cutover_keeps_the_old_authenticated_endpoint_protected() -> None:
    deploy = (ANSIBLE / "deploy.yml").read_text()
    assert "shared inference parity" in deploy.lower()
    assert "remove Compose-owned TEI" in deploy
    assert deploy.index("shared inference parity") < deploy.index("remove Compose-owned TEI")


def test_musubi_can_cut_over_and_roll_back_by_configuration() -> None:
    env = ENV.read_text()
    for key in ("TEI_DENSE_URL", "TEI_SPARSE_URL", "TEI_RERANKER_URL"):
        assert re.search(rf"^{key}=\{{\{{ musubi_[a-z_]+_url \}}\}}$", env, re.M)
    assert "TEI_AUTH_TOKEN=${MUSUBI_INFERENCE_TOKEN}" in env


def test_live_values_do_not_enter_public_sources() -> None:
    paths = (INFERENCE_COMPOSE, INFERENCE_UNIT, INGRESS, SECRETS, ENV, POLICY)
    ipv4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    hostname = re.compile(r"\b(?:musubi|mizuki)\.mey\.house\b", re.I)
    for path in paths:
        text = path.read_text()
        assert not [value for value in ipv4.findall(text) if not value.startswith("127.")], path
        assert not hostname.search(text), path
