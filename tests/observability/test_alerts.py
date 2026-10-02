"""Test contract for the alerts surface in [[09-operations/alerts]].

Musubi ships no alert rules and runs no Alertmanager. What it does ship is an
example routing file, `deploy/prometheus/alertmanager.yml`, that operators copy
into the Alertmanager they run. These tests check that example's shape, and
that it does not send alerts anywhere they would be lost or exposed.
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


def test_alertmanager_example_sends_nothing_to_core() -> None:
    """Core serves no alert-receiving route, so a webhook to it drops alerts."""
    cfg = yaml.safe_load(_ALERTMANAGER_FILE.read_text())
    urls = [w["url"] for r in cfg.get("receivers", []) for w in r.get("webhook_configs", [])]
    assert urls, "the example should still show a webhook receiver"
    for url in urls:
        assert "musubi-core" not in url and "/v1/" not in url, url
    assert cfg["route"]["receiver"] in {r["name"] for r in cfg["receivers"]}


def test_alertmanager_example_names_no_public_ntfy_topic() -> None:
    """A topic on the public ntfy.sh server is readable by anyone who knows its name."""
    text = _ALERTMANAGER_FILE.read_text()
    assert "ntfy.sh/" not in text
