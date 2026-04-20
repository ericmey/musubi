"""Test contract for slice-ops-first-deploy."""

from __future__ import annotations

import contextlib
import http.server
import json
import re
import shutil
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "deploy" / "runbooks" / "first-deploy.md"
RUNBOOKS_SPEC = ROOT / "docs" / "architecture" / "09-operations" / "runbooks.md"
SYSTEMD = ROOT / "deploy" / "systemd"
SMOKE = ROOT / "deploy" / "smoke"
KONG = ROOT / "deploy" / "kong" / "musubi-prod.yml"
OPENAPI = ROOT / "openapi.yaml"

RUNBOOK_SECTIONS = (
    "Pre-flight",
    "Snapshot target",
    "Run ansible playbook",
    "Bring up compose stack",
    "Install systemd units",
    "Configure Kong",
    "TLS certificate",
    "Smoke verify",
    "Rollback procedure",
    "Go-live checklist",
)
ALERT_RUNBOOK_SECTIONS = (
    "Qdrant down",
    "Core 5xx high",
    "Vault fs full",
    "GPU OOM",
    "Loop detected",
    "Backup failure 24h",
)


def _read(path: Path) -> str:
    assert path.exists(), f"missing {path.relative_to(ROOT)}"
    return path.read_text()


def _unit(name: str) -> dict[str, dict[str, str]]:
    text = _read(SYSTEMD / name)
    current: str | None = None
    parsed: dict[str, dict[str, str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            parsed[current] = {}
            continue
        assert current is not None, line
        key, value = line.split("=", 1)
        parsed[current][key] = value
    return parsed


def _runbook_step_blocks() -> list[str]:
    text = _read(RUNBOOK)
    return re.split(r"(?m)^## \d+\. ", text)[1:]


class _MockMusubi(http.server.BaseHTTPRequestHandler):
    server: _MockServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _text(self, status: int, payload: str) -> None:
        encoded = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/ops/health":
            self._json(200, {"status": "ok"})
            return
        if path == "/v1/ops/status":
            components = {
                name: {"name": name, "healthy": True}
                for name in ("qdrant", "tei-dense", "tei-sparse", "tei-reranker", "ollama")
            }
            if self.server.qdrant_unhealthy:
                components["qdrant"] = {
                    "name": "qdrant",
                    "healthy": False,
                    "detail": "mock outage",
                }
            self._json(
                200,
                {
                    "status": "ok"
                    if all(component["healthy"] for component in components.values())
                    else "degraded",
                    "components": components,
                },
            )
            return
        if path == "/v1/ops/metrics":
            self._text(
                200,
                "\n".join(
                    (
                        "# HELP musubi_http_requests_total HTTP requests",
                        "# TYPE musubi_http_requests_total counter",
                        'musubi_http_requests_total{method="GET"} 1',
                        "# HELP musubi_component_healthy Component readiness",
                        "# TYPE musubi_component_healthy gauge",
                        'musubi_component_healthy{component="qdrant"} 1',
                    )
                )
                + "\n",
            )
            return
        self._json(404, {"error": {"code": "NOT_FOUND"}})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/v1/memories":
            self.server.last_capture = body.get("content", "")
            self._json(202, {"object_id": "c" * 27, "state": "provisional"})
            return
        if path == "/v1/retrieve":
            content = "wrong content" if self.server.content_mismatch else self.server.last_capture
            self._json(
                200,
                {
                    "results": [
                        {
                            "object_id": "c" * 27,
                            "score": 1.0,
                            "plane": "episodic",
                            "content": content,
                            "namespace": body.get("namespace", "eric/ops/episodic"),
                        }
                    ],
                    "mode": "fast",
                    "limit": 1,
                },
            )
            return
        if path == "/v1/thoughts/send":
            self.server.last_thought = body.get("content", "")
            self._json(202, {"object_id": "t" * 27})
            return
        if path == "/v1/thoughts/check":
            self._json(
                200,
                {
                    "items": [
                        {
                            "object_id": "t" * 27,
                            "content": self.server.last_thought,
                            "from_presence": "eric/ops-smoke",
                        }
                    ]
                },
            )
            return
        self._json(404, {"error": {"code": "NOT_FOUND"}})


class _MockServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, qdrant_unhealthy: bool = False, content_mismatch: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), _MockMusubi)
        self.qdrant_unhealthy = qdrant_unhealthy
        self.content_mismatch = content_mismatch
        self.last_capture = ""
        self.last_thought = ""


@contextlib.contextmanager
def _mock_musubi(
    *, qdrant_unhealthy: bool = False, content_mismatch: bool = False
) -> Iterator[str]:
    server = _MockServer(qdrant_unhealthy=qdrant_unhealthy, content_mismatch=content_mismatch)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = server.server_address
        assert isinstance(address, tuple)
        host, port = address[:2]
        assert isinstance(host, str)
        assert isinstance(port, int)
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run_script(script: str, base_url: str, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SMOKE / script), *extra_args],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "MUSUBI_BASE_URL": base_url,
            "MUSUBI_TOKEN": "test-token",
            "MUSUBI_NAMESPACE": "eric/ops/episodic",
            "MUSUBI_THOUGHT_NAMESPACE": "eric/ops/thought",
            "MUSUBI_PRESENCE": "eric/ops-smoke",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_runbook_has_all_10_sections() -> None:
    text = _read(RUNBOOK)
    for index, heading in enumerate(RUNBOOK_SECTIONS, start=1):
        assert f"## {index}. {heading}" in text


def test_runbook_every_command_has_expected_output_block() -> None:
    for block in _runbook_step_blocks():
        assert "**Command:**" in block
        assert "**Expected output:**" in block
        assert "**Failure modes:**" in block


def test_runbook_mentions_rollback_path_for_every_destructive_step() -> None:
    destructive_blocks = [
        block for block in _runbook_step_blocks() if "**Destructive:** yes" in block
    ]
    assert destructive_blocks
    for block in destructive_blocks:
        assert "**Rollback:**" in block


def test_systemd_unit_api_has_restart_on_failure() -> None:
    service = _unit("musubi-api.service")["Service"]
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "10"


def test_systemd_unit_lifecycle_worker_depends_on_docker() -> None:
    unit = _unit("musubi-lifecycle-worker.service")["Unit"]
    assert "docker.service" in unit["After"]
    assert "docker.service" in unit["Requires"]


def test_systemd_unit_vault_sync_logs_to_journal() -> None:
    service = _unit("musubi-vault-sync.service")["Service"]
    assert service["StandardOutput"] == "journal"
    assert service["StandardError"] == "journal"
    assert service["SyslogIdentifier"] == "musubi-vault-sync"


def test_check_api_passes_when_all_components_healthy() -> None:
    with _mock_musubi() as base_url:
        result = _run_script("check_api.sh", base_url)
    assert result.returncode == 0, result.stderr
    assert "[PASS] api health" in result.stdout
    assert "[PASS] component qdrant" in result.stdout


def test_check_api_fails_when_qdrant_unhealthy() -> None:
    with _mock_musubi(qdrant_unhealthy=True) as base_url:
        result = _run_script("check_api.sh", base_url)
    assert result.returncode != 0
    assert "[FAIL] component qdrant" in result.stdout


def test_check_capture_round_trip_passes_with_real_response() -> None:
    with _mock_musubi() as base_url:
        result = _run_script("check_capture.sh", base_url)
    assert result.returncode == 0, result.stderr
    assert "[PASS] capture round trip" in result.stdout


def test_check_capture_fails_when_content_mismatch() -> None:
    with _mock_musubi(content_mismatch=True) as base_url:
        result = _run_script("check_capture.sh", base_url)
    assert result.returncode != 0
    assert "[FAIL] capture round trip" in result.stdout


def test_check_thoughts_send_check_roundtrip() -> None:
    with _mock_musubi() as base_url:
        result = _run_script("check_thoughts.sh", base_url)
    assert result.returncode == 0, result.stderr
    assert "[PASS] thoughts round trip" in result.stdout


def test_check_observability_scrapes_valid_prometheus_text() -> None:
    with _mock_musubi() as base_url:
        result = _run_script("check_observability.sh", base_url)
    assert result.returncode == 0, result.stderr
    assert "[PASS] prometheus text" in result.stdout
    assert "[PASS] metric families" in result.stdout


def test_verify_sh_aggregates_all_checks() -> None:
    with _mock_musubi() as base_url:
        result = _run_script("verify.sh", base_url)
    assert result.returncode == 0, result.stderr
    assert "[PASS] api health" in result.stdout
    assert "[PASS] capture round trip" in result.stdout
    assert "[PASS] thoughts round trip" in result.stdout
    assert "[PASS] prometheus text" in result.stdout


def test_verify_sh_exits_non_zero_on_any_failure() -> None:
    with _mock_musubi(qdrant_unhealthy=True) as base_url:
        result = _run_script("verify.sh", base_url)
    assert result.returncode != 0
    assert "[FAIL] component qdrant" in result.stdout


def test_kong_config_yaml_parses_via_deck_validate() -> None:
    config = yaml.safe_load(_read(KONG))
    assert config["_format_version"]
    assert "services" in config

    deck = shutil.which("deck")
    if deck is not None:
        subprocess.run(
            [deck, "gateway", "validate", str(KONG)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )


def test_kong_routes_cover_every_v1_endpoint_family() -> None:
    openapi = yaml.safe_load(_read(OPENAPI))
    expected = {
        "/" + "/".join(path.strip("/").split("/")[:2])
        for path in openapi["paths"]
        if path.startswith("/v1/")
    }
    expected.add("/mcp")

    config = yaml.safe_load(_read(KONG))
    actual: set[str] = set()
    for service in config["services"]:
        for route in service.get("routes", []):
            actual.update(route.get("paths", []))

    for family in expected:
        assert any(family == path or family.startswith(path.rstrip("/") + "/") for path in actual)


def test_every_alert_has_a_runbook_section() -> None:
    text = _read(RUNBOOKS_SPEC)
    for heading in ALERT_RUNBOOK_SECTIONS:
        assert f"## {heading}" in text


def test_runbooks_reference_real_files_and_commands() -> None:
    text = _read(RUNBOOKS_SPEC)
    assert "deploy/runbooks/first-deploy.md" in text
    assert RUNBOOK.exists()
    assert "docker compose" in text
    assert "ansible-playbook" in _read(RUNBOOK)
    assert "deploy/smoke/verify.sh" in _read(RUNBOOK)


def test_each_runbook_lists_success_criteria() -> None:
    first_deploy = _read(RUNBOOK)
    assert first_deploy.count("**Expected output:**") == len(RUNBOOK_SECTIONS)
    runbooks = _read(RUNBOOKS_SPEC)
    for heading in ALERT_RUNBOOK_SECTIONS:
        section = runbooks.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
        lower = section.lower()
        assert "if" in lower or "confirm" in lower or "verify" in lower


def test_quarterly_game_day_drills_cycle_through_runbooks() -> None:
    text = _read(RUNBOOKS_SPEC)
    assert "## Quarterly game-day drills" in text
    for drill in ("Qdrant down", "Restore from snapshot", "Backup failure 24h", "First deploy"):
        assert drill in text
