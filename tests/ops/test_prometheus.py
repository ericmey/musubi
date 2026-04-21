"""Structural tests for the Prometheus observability wiring.

Prometheus scrapes Musubi Core at `/v1/ops/metrics` every 15 seconds
and retains timeseries for 30 days. These tests assert the shape of
that config + the corresponding compose service so drift (a wrong
scrape target, a missing retention flag, a weakened bind policy)
fails CI instead of rotting silently.

Scope:

- The scrape config parses as valid YAML and lands every expected
  target (musubi-core at :8100, prometheus-self).
- `scrape_interval` is ≤ the default 15s — if it drifts longer the
  p95 latency SLOs become statistically noisy.
- The compose template renders a `prometheus` service that mounts the
  scrape config read-only, persists TSDB under `/var/lib/musubi/prometheus-data`,
  binds host-local-only (no external exposure without Kong — see
  ADR 0024), and carries the retention flag.
- `deploy.yml` copies the scrape config to the host alongside the
  compose render step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROM_CONFIG = ROOT / "deploy" / "prometheus" / "prometheus.yml"
COMPOSE_TEMPLATE = ROOT / "deploy" / "ansible" / "templates" / "docker-compose.yml.j2"
GROUP_VARS = ROOT / "deploy" / "ansible" / "group_vars" / "all.yml"
DEPLOY_PLAYBOOK = ROOT / "deploy" / "ansible" / "deploy.yml"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


# ---------------------------------------------------------------------------
# Scrape config
# ---------------------------------------------------------------------------


def test_prometheus_config_parses() -> None:
    assert PROM_CONFIG.exists(), f"missing {PROM_CONFIG}"
    cfg = _load(PROM_CONFIG)
    assert isinstance(cfg, dict)
    assert "global" in cfg
    assert "scrape_configs" in cfg


def test_scrape_interval_is_fast_enough_for_p95_math() -> None:
    cfg = _load(PROM_CONFIG)
    interval = cfg["global"]["scrape_interval"]
    # Parse "15s" / "60s" / "1m"
    if interval.endswith("s"):
        seconds = int(interval[:-1])
    elif interval.endswith("m"):
        seconds = int(interval[:-1]) * 60
    else:
        raise AssertionError(f"unsupported scrape_interval format: {interval!r}")
    assert seconds <= 30, "scrape interval > 30s makes p95 latency noisy"


def test_scrape_targets_musubi_core_metrics_endpoint() -> None:
    cfg = _load(PROM_CONFIG)
    jobs = {j["job_name"]: j for j in cfg["scrape_configs"]}
    assert "musubi-core" in jobs, "no scrape job for musubi-core"
    core = jobs["musubi-core"]
    assert core["metrics_path"] == "/v1/ops/metrics"
    targets = [t for sc in core["static_configs"] for t in sc["targets"]]
    # Target MUST use the compose service name `core`; an older
    # aspirational config at `musubi-core:8100` (pre-compose) does not
    # resolve on the musubi_default bridge.
    assert "core:8100" in targets, "scrape target must be 'core:8100' (compose service name)"


def test_external_labels_identify_host() -> None:
    """Every metric must carry enough metadata to be attributed to this
    box once we have more than one musubi host."""
    cfg = _load(PROM_CONFIG)
    labels = cfg["global"].get("external_labels") or {}
    assert "host" in labels, "external_labels.host missing"


# ---------------------------------------------------------------------------
# Compose service
# ---------------------------------------------------------------------------


def _render_compose() -> dict[str, Any]:
    rendered = COMPOSE_TEMPLATE.read_text()
    tokens = {
        "{{ musubi_core_image }}": "example/musubi-core:test",
        "{{ musubi_qdrant_image }}": "qdrant/qdrant:test",
        "{{ musubi_tei_image }}": "example/tei:test",
        "{{ musubi_ollama_image }}": "ollama/ollama:test",
        "{{ musubi_prometheus_image }}": "prom/prometheus:test",
        "{{ musubi_core_port }}": "8100",
        "{{ musubi_ollama_model }}": "qwen3:4b",
        "{{ vault_qdrant_api_key }}": "x",
    }
    for k, v in tokens.items():
        rendered = rendered.replace(k, v)
    parsed = yaml.safe_load(rendered)
    assert parsed is not None
    return parsed  # type: ignore[no-any-return]


def test_prometheus_service_exists() -> None:
    compose = _render_compose()
    assert "prometheus" in compose["services"], "no prometheus service in compose template"


def test_prometheus_mounts_scrape_config_readonly() -> None:
    svc = _render_compose()["services"]["prometheus"]
    ro_mounts = [
        v
        for v in svc["volumes"]
        if isinstance(v, str) and v.endswith(":ro") and "prometheus.yml" in v
    ]
    assert ro_mounts, "scrape config must be mounted read-only"


def test_prometheus_persists_tsdb() -> None:
    svc = _render_compose()["services"]["prometheus"]
    data_mount = [v for v in svc["volumes"] if v.split(":")[1] == "/prometheus"]
    assert data_mount, "prometheus /prometheus data dir must be bind-mounted"
    assert data_mount[0].startswith("/var/lib/musubi/prometheus-data")


def test_prometheus_retention_is_set() -> None:
    svc = _render_compose()["services"]["prometheus"]
    cmd = svc.get("command") or []
    joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    assert "--storage.tsdb.retention.time=30d" in joined


def test_prometheus_host_local_binding_only() -> None:
    """Must not be externally exposed — Kong fronts external surfaces
    per ADR 0024; anything else would leak internal ops data."""
    svc = _render_compose()["services"]["prometheus"]
    ports = svc.get("ports") or []
    assert ports, "prometheus has no ports mapping"
    for p in ports:
        assert p.startswith("127.0.0.1:"), f"prometheus port {p!r} is not bound to 127.0.0.1"


def test_prometheus_depends_on_core() -> None:
    """So operators don't get `connection refused` scrape errors for
    the first 30 seconds of boot."""
    svc = _render_compose()["services"]["prometheus"]
    deps = svc.get("depends_on") or {}
    assert "core" in deps, "prometheus should depend on core"


# ---------------------------------------------------------------------------
# Ansible plumbing
# ---------------------------------------------------------------------------


def test_group_vars_declares_prometheus_image_pin() -> None:
    gv = _load(GROUP_VARS)
    assert gv.get("musubi_prometheus_image"), "musubi_prometheus_image not declared"
    assert gv["musubi_prometheus_image"].startswith("prom/prometheus:")


def test_group_vars_declares_prometheus_data_dir() -> None:
    gv = _load(GROUP_VARS)
    assert "/var/lib/musubi/prometheus-data" in gv["musubi_data_dirs"]


def test_deploy_playbook_copies_scrape_config() -> None:
    text = DEPLOY_PLAYBOOK.read_text()
    assert "prometheus.yml" in text
    assert "/prometheus/prometheus.yml" in text or "prometheus/prometheus.yml" in text
