"""SEC-008: test the accepted network boundary for read-only ops routes."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OPS_ROUTER = ROOT / "src" / "musubi" / "api" / "routers" / "ops.py"
COMPOSE = ROOT / "docker-compose.yml"
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


def test_public_core_binding_defaults_to_loopback_without_host_firewall_tasks() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    assert compose["services"]["core"]["ports"] == [
        "${MUSUBI_CORE_BIND:-127.0.0.1}:${MUSUBI_CORE_PORT:-8100}:8100"
    ]
    assert not (ROOT / "deploy" / "ansible").exists()


def test_sec008_adr_names_owner_blast_radius_and_review_triggers() -> None:
    text = ADR.read_text()
    for required in (
        "## Negative proof",
        "## Blast radius and residual risk",
        "## Owner and review triggers",
        "Owner: Musubi operations (Admin).",
        "Review by 2026-10-15",
        "not safe for public Internet exposure",
    ):
        assert required in text
