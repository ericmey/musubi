"""REQ-10 — the single-worker invariant must FAIL CLOSED in runtime config, not just in a doc.

Yua req 10 (2026-07-12T21:18): "Phase-0 single-worker invariant fails closed in runtime
config, not only a unit assertion."

Why it matters: `IdempotencyCache` is in-memory and process-local (idempotency.py:10 — "fine
for a single worker. A deployment with multiple workers will move this to Redis"). If the API
is ever served with more than one worker, EACH worker gets its own cache, so the SEC-002 /
IDEM-001 replay + lease guarantees evaporate SILENTLY — no error, just a torn cache.

Today the invariant is IMPLICIT, not enforced. The runtime serves
`uvicorn musubi.api.app:create_app --factory` (deploy/systemd/musubi-api.service) with NO
`--workers` flag — so it is single-worker only by uvicorn's DEFAULT. Nothing PREVENTS a future
`--workers 4` or a `WEB_CONCURRENCY=4` env from booting a broken multi-worker cache.

This file:
  1. Reference prototype of the fail-closed guard (green) — the exact check the fix should add.
  2. A today-reality control (green) proving the guard is ABSENT: create_app builds happily
     under a multi-worker signal today (the vulnerability is real).
  3. A strict-xfail executable future contract: create_app MUST fail closed under a multi-worker
     signal. Fails today; flips XPASS->fail when src adds the guard — no test edit needed.
  4. A runtime-config proof (green): the systemd ExecStart runs create_app with no --workers,
     documenting that the runtime layer does not pin the invariant — so the guard must live in
     create_app itself.

Tests/docs only. No src.

    uv run pytest tests/api/test_req10_single_worker_fail_closed.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from musubi.api.app import create_app
from musubi.settings import Settings

_SERVICE_FILE = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "musubi-api.service"
_WORKER_ENV = "WEB_CONCURRENCY"          # the standard uvicorn/gunicorn worker-count signal


# --------------------------------------------------------------------------- #
# 1. reference prototype of the guard (green) — the spec the fix should add
# --------------------------------------------------------------------------- #

def _reject_multi_worker(worker_count: int) -> None:
    """Fail-closed: a process-local idempotency cache is only correct at worker_count == 1.
    Anything greater must refuse to boot; missing/unset is treated as the safe default (1)."""
    if worker_count > 1:
        raise RuntimeError(
            f"idempotency cache is process-local; {worker_count} workers would tear it — "
            f"set a shared backend or run a single worker (fail-closed)")


def test_guard_prototype_rejects_multi_worker() -> None:
    _reject_multi_worker(1)                       # single worker is fine
    with pytest.raises(RuntimeError, match="process-local"):
        _reject_multi_worker(2)
    with pytest.raises(RuntimeError):
        _reject_multi_worker(8)


# --------------------------------------------------------------------------- #
# 2 + 3. against the real create_app
# --------------------------------------------------------------------------- #

def test_create_app_has_no_guard_today_control(
    api_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TODAY-REALITY control (not xfail): with a multi-worker signal set, create_app builds
    WITHOUT complaint — proving the guard is genuinely absent, so the red below fails for the
    right reason (missing enforcement), not a typo."""
    monkeypatch.setenv(_WORKER_ENV, "4")
    app = create_app(settings=api_settings)       # must NOT raise today
    assert app is not None
    assert os.environ[_WORKER_ENV] == "4"


@pytest.mark.xfail(strict=True, reason="REQ-10: create_app does not fail closed on multi-worker config yet — fix pending")
def test_create_app_must_fail_closed_on_multi_worker(
    api_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SECURE CONTRACT: booting the app with a multi-worker signal while the idempotency cache
    is process-local must raise (fail closed). Executable future contract — when src reads
    WEB_CONCURRENCY (or an api_workers setting) and rejects >1, this flips green with no edit."""
    monkeypatch.setenv(_WORKER_ENV, "4")
    with pytest.raises((RuntimeError, ValueError)):
        create_app(settings=api_settings)


def test_single_worker_still_boots(api_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Feature preservation: the guard must NOT break the legitimate single-worker boot. Must
    stay green before AND after the fix."""
    monkeypatch.setenv(_WORKER_ENV, "1")
    app = create_app(settings=api_settings)
    assert app is not None


# --------------------------------------------------------------------------- #
# 4. runtime-config proof (green): the invariant is unenforced at the runtime layer
# --------------------------------------------------------------------------- #

def test_systemd_execstart_relies_on_implicit_single_worker() -> None:
    """The runtime ExecStart runs create_app with NO --workers flag — single-worker only by
    uvicorn's default, not by any pin. This is WHY the guard must live in create_app: the
    runtime layer does not prevent a future --workers >1."""
    assert _SERVICE_FILE.exists(), f"service file missing: {_SERVICE_FILE}"
    text = _SERVICE_FILE.read_text()
    execstart = next((ln for ln in text.splitlines() if ln.strip().startswith("ExecStart=")), None)
    assert execstart is not None, "no ExecStart in the service file"
    assert "create_app" in execstart, f"unexpected ExecStart target: {execstart}"
    # The invariant is IMPLICIT: no --workers pin. If a future edit adds --workers N>1 here
    # without a create_app guard, the cache tears silently — which is the whole point of REQ-10.
    assert "--workers" not in execstart, (
        "ExecStart now sets --workers but no create_app fail-closed guard exists — REQ-10 hole")
