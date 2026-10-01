"""SEC-008: test the accepted network boundary for read-only ops routes."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPS_ROUTER = ROOT / "src" / "musubi" / "api" / "routers" / "ops.py"
BOOTSTRAP = ROOT / "deploy" / "ansible" / "bootstrap.yml"
INGRESS_VALIDATOR = ROOT / "deploy" / "ansible" / "validate_ingress.py"
PROMETHEUS = ROOT / "deploy" / "ansible" / "templates" / "prometheus.yml.j2"
COMPOSE = ROOT / "deploy" / "ansible" / "templates" / "docker-compose.yml.j2"
ADR = ROOT / "docs" / "Musubi" / "13-decisions" / "0038-network-protect-read-only-ops-endpoints.md"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text()
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    start = min((decorator.lineno for decorator in node.decorator_list), default=node.lineno)
    assert node.end_lineno is not None
    return "\n".join(source.splitlines()[start - 1 : node.end_lineno])


def test_read_only_ops_exception_stays_bounded() -> None:
    status = _function_source(OPS_ROUTER, "status")
    metrics = _function_source(OPS_ROUTER, "metrics")
    debug = _function_source(OPS_ROUTER, "trigger_synthesis")

    assert '@router.get("/status"' in status
    assert '@router.get("/metrics"' in metrics
    assert "require_operator" not in status
    assert "require_operator" not in metrics
    assert '@router.post(\n    "/debug/trigger-synthesis"' in debug
    assert "Depends(require_operator())" in debug


def test_core_ingress_is_default_deny_and_source_restricted() -> None:
    text = BOOTSTRAP.read_text()
    assert "policy: deny\n        direction: incoming" in text
    assert 'from_ip: "{{ musubi_admin_ssh_cidr }}"' in text
    assert '"{{ playbook_dir }}/validate_ingress.py"' in text
    assert 'from_ip: "{{ musubi_kong_ip }}"' in text
    assert "when: musubi_kong_ip | default('') | length > 0" in text
    assert 'from_ip: "{{ musubi_vlan_cidr }}"' in text
    assert "when: musubi_kong_ip | default('') | length == 0" in text
    assert "delegate_to: localhost" in text
    assert "check_mode: false" in text


def test_ingress_validator_rejects_world_open_sources() -> None:
    bounded = {
        "musubi_admin_ssh_cidr": "192.0.2.0/24",
        "musubi_kong_ip": "198.51.100.5",
        "musubi_vlan_cidr": "",
    }
    cases = (
        ({**bounded, "musubi_admin_ssh_cidr": "any"}, False),
        ({**bounded, "musubi_admin_ssh_cidr": "0.0.0.0/00"}, False),
        ({**bounded, "musubi_admin_ssh_cidr": "0::/0"}, False),
        ({**bounded, "musubi_admin_ssh_cidr": ""}, False),
        ({**bounded, "musubi_kong_ip": "0.0.0.0"}, False),
        ({**bounded, "musubi_kong_ip": "any"}, False),
        ({**bounded, "musubi_kong_ip": "", "musubi_vlan_cidr": "any"}, False),
        ({**bounded, "musubi_kong_ip": "", "musubi_vlan_cidr": ""}, False),
        (bounded, True),
        ({**bounded, "musubi_kong_ip": "", "musubi_vlan_cidr": "203.0.113.0/24"}, True),
    )
    for values, allowed in cases:
        result = subprocess.run(
            ["python3", str(INGRESS_VALIDATOR)],
            input=json.dumps(values),
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 0) is allowed, values


def test_prometheus_scrapes_core_privately_and_stays_loopback_only() -> None:
    prometheus = PROMETHEUS.read_text()
    compose = COMPOSE.read_text()
    assert "metrics_path: /v1/ops/metrics" in prometheus
    assert 'targets: ["core:8100"]' in prometheus
    assert '"127.0.0.1:9090:9090"' in compose


def test_sec008_adr_names_owner_blast_radius_and_review_triggers() -> None:
    text = ADR.read_text()
    for required in (
        "## Negative proof",
        "## Blast radius and residual risk",
        "## Owner and review triggers",
        "Owner: Yua / Musubi operations.",
        "Review by 2026-10-15",
        "not safe for public Internet exposure",
    ):
        assert required in text
