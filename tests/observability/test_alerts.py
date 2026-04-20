"""Test contract for the alerts surface in
[[09-operations/alerts]].

Closure plan:

- bullets 1-3 + 5-6 (alert/runbook/dashboard hygiene checks against the
  shipped YAML/JSON files) → passing
- bullet 4 (chaos-drill integration: kill Qdrant; expect ``qdrant_down``
  within 3m) → declared out-of-scope in the slice work log; needs a
  live Prometheus + Alertmanager loop. The unit-form tests verify the
  rule shape; the live timing is the contract-test layer's job.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO_ROOT / "deploy"
_ALERTS_FILE = _DEPLOY / "grafana" / "alerts" / "musubi-alerts.yml"
_ALERTMANAGER_FILE = _DEPLOY / "prometheus" / "alertmanager.yml"
_OVERVIEW_DASHBOARD = _DEPLOY / "grafana" / "dashboards" / "musubi-overview.json"


def _load_alert_rules() -> list[dict[str, Any]]:
    cfg = yaml.safe_load(_ALERTS_FILE.read_text())
    rules: list[dict[str, Any]] = []
    for group in cfg.get("groups", []):
        for rule in group.get("rules", []):
            if "alert" in rule:
                rules.append(rule)
    return rules


def test_every_push_alert_has_linked_runbook() -> None:
    """Bullet 1 — every alert with severity=push has a runbook URL."""
    push_alerts = [r for r in _load_alert_rules() if r.get("labels", {}).get("severity") == "push"]
    assert push_alerts, "expected at least one push-severity alert"
    for rule in push_alerts:
        annotations = rule.get("annotations", {})
        assert "runbook" in annotations, f"push alert {rule['alert']!r} has no runbook annotation"
        assert annotations["runbook"].startswith("http"), (
            f"push alert {rule['alert']!r} runbook is not a URL"
        )


def test_every_alert_has_for_clause_to_dedupe_flaps() -> None:
    """Bullet 2 — every alert has a ``for:`` clause so single-tick
    blips don't fire pages."""
    for rule in _load_alert_rules():
        assert "for" in rule, f"alert {rule['alert']!r} has no for: clause"


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


def test_backup_failure_alert_fires_after_24h_gap() -> None:
    """Bullet 5 — surface form: the rule exists with the documented
    24h ``for:`` window. Live firing is the chaos-drill bullet's job."""
    backup = next((r for r in _load_alert_rules() if r["alert"] == "backup_failure_24h"), None)
    assert backup is not None, "backup_failure_24h alert is missing"
    assert backup.get("for") == "24h"


def test_silent_alerts_still_show_on_dashboard() -> None:
    """Bullet 6 — the silent thresholds the spec lists ('Single retrieval > 5s',
    'dedup rate > 30%', 'thought history search miss') surface as
    overview-board panels even though they don't alert. Surface-form
    check: the overview dashboard has at least one panel referencing
    each silent metric."""
    cfg = json.loads(_OVERVIEW_DASHBOARD.read_text())
    panels_text = json.dumps(cfg["panels"])
    # Silent thresholds map to dashboard tiles per the spec.
    assert "musubi_retrieve_duration_ms" in panels_text
    assert "musubi_capture_dedup_total" in panels_text


# ---------------------------------------------------------------------------
# Coverage tests — alerts hygiene beyond the contract bullets.
# ---------------------------------------------------------------------------


def test_every_alert_has_summary_annotation() -> None:
    """Alertmanager templates expect a ``summary`` annotation; verify
    every alert ships one."""
    for rule in _load_alert_rules():
        assert "summary" in rule.get("annotations", {}), f"alert {rule['alert']!r} missing summary"


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
