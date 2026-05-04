"""Test contract for the alerts surface in
[[09-operations/alerts]].

Reduced scope as of 2026-05-03 per [[13-decisions/0033-centralize-observability-on-shiori]]:

- The alert *rules* themselves (`deploy/grafana/alerts/musubi-alerts.yml`)
  + the overview dashboard JSON were removed. Tests that depend on those
  files were deleted with them. Equivalent rule/dashboard hygiene tests
  will live shiori-side in the operator vault.
- The Alertmanager *config* file (`deploy/prometheus/alertmanager.yml`)
  is retained pending a follow-up decision on whether local alertmanager
  is fully obsolete (it was never deployed; tests below verify shape only).
- The chaos-drill bullet is unchanged — still skipped pending live test loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO_ROOT / "deploy"
_ALERTMANAGER_FILE = _DEPLOY / "prometheus" / "alertmanager.yml"


def test_alertmanager_config_loads_without_error() -> None:
    """Bullet 3 — the Alertmanager config file is syntactically valid YAML
    + has the ntfy + email receivers the spec calls out."""
    cfg = yaml.safe_load(_ALERTMANAGER_FILE.read_text())
    assert "route" in cfg
    receiver_names = {r["name"] for r in cfg.get("receivers", [])}
    assert "ntfy" in receiver_names
    assert "email" in receiver_names


@pytest.mark.skip(
    reason="out-of-scope in slice work log: chaos-drill timing requires live Prometheus + Alertmanager loop; deferred to musubi-contract-tests per ADR-0011"
)
def test_chaos_drill_qdrant_down_fires_within_3m() -> None:
    """Bullet 4 — placeholder."""


def test_alertmanager_routes_push_severity_to_ntfy() -> None:
    cfg = yaml.safe_load(_ALERTMANAGER_FILE.read_text())
    routes = cfg["route"].get("routes", [])
    push_routes = [r for r in routes if any('severity="push"' in m for m in r.get("matchers", []))]
    assert push_routes, "no push-routing rule found in alertmanager config"
    assert push_routes[0]["receiver"] == "ntfy"


def test_alertmanager_routes_email_severity_to_email() -> None:
    cfg = yaml.safe_load(_ALERTMANAGER_FILE.read_text())
    routes = cfg["route"].get("routes", [])
    email_routes = [
        r for r in routes if any('severity="email"' in m for m in r.get("matchers", []))
    ]
    assert email_routes, "no email-routing rule found in alertmanager config"
    assert email_routes[0]["receiver"] == "email"
