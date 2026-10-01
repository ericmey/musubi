"""Public Compose must be runnable without the house Ansible layout."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docker-compose.yml"
GPU = ROOT / "deploy/docker/compose.local-gpu.yml"
ENV_EXAMPLE = ROOT / ".env.example"
QUICKSTART = ROOT / "quickstart/docker-compose.yml"


def _compose(files: list[Path], env_file: Path) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is unavailable")
    args = [docker, "compose", "--env-file", str(env_file)]
    for path in files:
        args.extend(["-f", str(path)])
    args.extend(["config", "--format", "json"])
    result = subprocess.run(
        args,
        cwd=ROOT,
        env={**os.environ, "MUSUBI_ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        check=True,
    )
    import json

    return json.loads(result.stdout)


@pytest.fixture
def operator_env(tmp_path: Path) -> Path:
    path = tmp_path / "musubi.env"
    path.write_text(
        "\n".join(
            [
                "JWT_SIGNING_KEY=test-only-32-character-signing-key-123456",
                "QDRANT_API_KEY=test-only-qdrant-key",
                "OAUTH_AUTHORITY=https://auth.example.invalid/",
                "TEI_DENSE_URL=https://inference.example.invalid/dense",
                "TEI_SPARSE_URL=https://inference.example.invalid/sparse",
                "TEI_RERANKER_URL=https://inference.example.invalid/rerank",
                "OLLAMA_URL=https://inference.example.invalid/ollama",
                "EMBEDDING_MODEL=example/dense",
                "SPARSE_MODEL=example/sparse",
                "RERANKER_MODEL=example/reranker",
                "LLM_MODEL=example/llm",
                "MUSUBI_TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:86-1.9.4",
            ]
        )
        + "\n"
    )
    return path


def test_public_compose_has_real_matching_core_pins() -> None:
    base = yaml.safe_load(BASE.read_text())
    quickstart = yaml.safe_load(QUICKSTART.read_text())
    services = base["services"]
    assert set(services) == {"core", "lifecycle-worker", "qdrant"}
    pins = {
        services["core"]["image"],
        services["lifecycle-worker"]["image"],
        quickstart["x-core"]["image"],
    }
    assert len(pins) == 1
    assert re.fullmatch(
        r"ghcr\.io/sourceblender/musubi-core@sha256:[0-9a-f]{64}",
        pins.pop(),
    )


def test_public_compose_remote_mode_is_host_independent(operator_env: Path) -> None:
    config = _compose([BASE], operator_env)
    services = config["services"]
    assert set(services) == {"core", "lifecycle-worker", "qdrant"}
    assert services["core"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert all("ports" not in services[name] for name in ("qdrant", "lifecycle-worker"))
    for service in services.values():
        for volume in service.get("volumes", []):
            assert volume["type"] == "volume"
    assert services["core"]["environment"]["TEI_DENSE_URL"].startswith("https://")


def test_public_compose_local_gpu_mode_has_no_inference_port(operator_env: Path) -> None:
    config = _compose([BASE, GPU], operator_env)
    services = config["services"]
    assert {"tei-dense", "tei-sparse", "tei-reranker", "ollama"} <= set(services)
    for name in ("tei-dense", "tei-sparse", "tei-reranker", "ollama"):
        assert "ports" not in services[name]
        devices = services[name]["deploy"]["resources"]["reservations"]["devices"]
        assert any("gpu" in device["capabilities"] for device in devices)


def test_public_compose_example_requires_operator_secrets_and_endpoints() -> None:
    example = ENV_EXAMPLE.read_text()
    for key in (
        "JWT_SIGNING_KEY",
        "QDRANT_API_KEY",
        "TEI_DENSE_URL",
        "TEI_SPARSE_URL",
        "TEI_RERANKER_URL",
        "OLLAMA_URL",
    ):
        assert re.search(rf"(?m)^{key}=", example)
    assert "quickstart-demo-signing-key" not in example
