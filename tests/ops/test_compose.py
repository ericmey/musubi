"""Test contract for slice-ops-compose."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.yml"
DEPLOY_DOCKER = ROOT / "deploy" / "docker"
SMOKE_SCRIPT = DEPLOY_DOCKER / "smoke-health.sh"

EXPECTED_SERVICES = {
    "qdrant",
    "tei-dense",
    "tei-sparse",
    "tei-reranker",
    "ollama",
    "core",
}
GPU_SERVICES = {"tei-dense", "tei-sparse", "tei-reranker", "ollama"}
CORE_DEPENDENCIES = {"qdrant", "tei-dense", "tei-sparse", "tei-reranker", "ollama"}
REQUIRED_BIND_MOUNTS = {
    "/var/lib/musubi/vault",
    "/var/lib/musubi/artifact-blobs",
    "/var/lib/musubi/lifecycle-work.sqlite",
    "/var/log/musubi",
}


def _load_compose() -> dict[str, Any]:
    assert COMPOSE_FILE.exists(), "docker-compose.yml must be committed at repo root"
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    assert isinstance(compose, dict)
    return compose


def _services() -> dict[str, dict[str, Any]]:
    services = _load_compose().get("services")
    assert isinstance(services, dict)
    assert set(services) == EXPECTED_SERVICES
    return services


def _published_ports(service: dict[str, Any]) -> list[str | dict[str, Any]]:
    ports = service.get("ports", [])
    assert isinstance(ports, list)
    return ports


def _host_mount(source: str, volume: str | dict[str, Any]) -> str | None:
    if isinstance(volume, str):
        host, _, _container = volume.partition(":")
        return host if host == source else None
    if isinstance(volume, dict) and volume.get("type") == "bind":
        mount_source = volume.get("source")
        return str(mount_source) if mount_source == source else None
    return None


def test_compose_config_valid() -> None:
    compose = _load_compose()
    assert "services" in compose
    assert "networks" in compose

    docker = shutil.which("docker")
    if docker is not None:
        subprocess.run(
            [docker, "compose", "-f", str(COMPOSE_FILE), "config", "--quiet"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )


def test_every_service_has_healthcheck() -> None:
    for service_name, service in _services().items():
        healthcheck = service.get("healthcheck")
        assert isinstance(healthcheck, dict), f"{service_name} needs a healthcheck"
        assert healthcheck.get("test"), f"{service_name} healthcheck needs a test"
        assert healthcheck.get("interval"), f"{service_name} healthcheck needs interval"
        assert healthcheck.get("timeout"), f"{service_name} healthcheck needs timeout"
        assert healthcheck.get("retries"), f"{service_name} healthcheck needs retries"


def test_every_image_pinned_by_digest() -> None:
    for service_name, service in _services().items():
        image = service.get("image")
        assert isinstance(image, str)
        assert "@sha256:" in image, f"{service_name} image must be digest-pinned"


def test_core_depends_on_all_dependencies_healthy() -> None:
    core = _services()["core"]
    depends_on = core.get("depends_on")
    assert isinstance(depends_on, dict)
    assert set(depends_on) == CORE_DEPENDENCIES
    for service_name, dependency in depends_on.items():
        assert dependency == {"condition": "service_healthy"}, service_name


def test_only_core_publishes_a_host_port() -> None:
    services = _services()
    for service_name, service in services.items():
        ports = _published_ports(service)
        if service_name == "core":
            assert ports == ["127.0.0.1:8100:8100"]
        else:
            assert ports == [], f"{service_name} must stay bridge-only"


def test_gpu_services_list_gpu_reservation() -> None:
    services = _services()
    for service_name in GPU_SERVICES:
        devices = (
            services[service_name]
            .get("deploy", {})
            .get("resources", {})
            .get("reservations", {})
            .get("devices", [])
        )
        assert {"capabilities": ["gpu"]} in devices, service_name


def test_bind_mounts_exist_on_host() -> None:
    core = _services()["core"]
    volumes = core.get("volumes")
    assert isinstance(volumes, list)
    for source in REQUIRED_BIND_MOUNTS:
        assert any(_host_mount(source, volume) for volume in volumes), source


def test_compose_up_to_healthy_under_5min_on_warm_cache() -> None:
    assert SMOKE_SCRIPT.exists(), "warm-cache smoke script must be committed"
    script = SMOKE_SCRIPT.read_text()
    assert "docker compose" in script
    assert "--timeout 300" in script
    assert "compose ps --format json" in script
