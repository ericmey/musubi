"""REQ-10 — the single-worker invariant must FAIL CLOSED across deployment AND app config.

Yua req 10 (21:18), worker gap (22:11): WEB_CONCURRENCY is not the only worker launch path —
`uvicorn --workers 4` forks apps without create_app knowing the global count. So the invariant
must be contracted at BOTH layers, and the config must not be able to drift to >1 unnoticed.

Why it matters: `IdempotencyCache` is in-memory and process-local (idempotency.py:10). More
than one worker gives each its own cache, so the SEC-002 / IDEM-001 replay + lease guarantees
tear SILENTLY. Today single-worker holds only by uvicorn's DEFAULT — nothing pins or enforces
it.

Contract, four parts:
  DEPLOYMENT
    - the systemd ExecStart must EXPLICITLY pin a single worker (`--workers 1`) — red, fails
      today (no --workers flag at all).
    - a drift guard: the ExecStart may NEVER carry `--workers N` with N > 1 — green today,
      fails loudly the moment someone edits it upward. This is the "cannot drift unnoticed".
  APP CONFIG
    - create_app must fail closed when `WEB_CONCURRENCY > 1` — red, fails today.
    - Settings must reject a configured `api_workers > 1` — red, no such field today.
  Plus today-reality controls proving the guards are absent, and feature-preservation that a
  legitimate single worker still boots.

Tests/docs only. No src.

    uv run pytest tests/api/test_req10_single_worker_fail_closed.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from musubi.api.app import create_app
from musubi.settings import Settings

_SERVICE_FILE = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "musubi-api.service"
_WORKER_ENV = "WEB_CONCURRENCY"  # the standard uvicorn/gunicorn worker-count signal


def _execstart() -> str:
    text = _SERVICE_FILE.read_text()
    line = next((ln for ln in text.splitlines() if ln.strip().startswith("ExecStart=")), None)
    assert line is not None, "no ExecStart in the service file"
    return line


def _workers_in(execstart: str) -> int | None:
    """The N in `--workers N` (or `--workers=N`), or None if the flag is absent."""
    m = re.search(r"--workers[=\s]+(\d+)", execstart)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# reference prototype of the guard (green) — the spec the fix should add
# --------------------------------------------------------------------------- #


def _reject_multi_worker(worker_count: int) -> None:
    if worker_count > 1:
        raise RuntimeError(
            f"idempotency cache is process-local; {worker_count} workers would tear it — "
            f"pin a single worker or move to a shared backend (fail-closed)"
        )


def test_guard_prototype_rejects_multi_worker() -> None:
    _reject_multi_worker(1)
    for n in (2, 4, 8):
        with pytest.raises(RuntimeError, match="process-local"):
            _reject_multi_worker(n)


# --------------------------------------------------------------------------- #
# DEPLOYMENT contract
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="REQ-10: systemd ExecStart does not explicitly pin --workers 1 yet — deferred; closed by slice-auth-boundary-phase-a (PR #403, Issue #410)",
)
def test_systemd_must_pin_single_worker() -> None:
    """The runtime must PIN one worker, not rely on uvicorn's implicit default."""
    assert _workers_in(_execstart()) == 1, (
        "ExecStart must explicitly pin --workers 1 so the single-worker invariant is declared, "
        "not left to uvicorn's default"
    )


def test_systemd_never_drifts_above_one_worker() -> None:
    """DRIFT GUARD (must always hold): the ExecStart may run one worker or leave it implicit,
    but it may NEVER pin more than one. If someone edits it to --workers 4, this fails loudly —
    the invariant cannot silently drift above 1."""
    n = _workers_in(_execstart())
    assert n is None or n == 1, (
        f"ExecStart pins --workers {n} > 1 while the idempotency cache is process-local — "
        f"REQ-10 hole (each worker gets its own cache)"
    )


# --------------------------------------------------------------------------- #
# APP-CONFIG contract
# --------------------------------------------------------------------------- #


def test_create_app_has_no_web_concurrency_guard_today_control(
    api_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TODAY-REALITY control (not xfail): create_app builds under WEB_CONCURRENCY=4, proving the
    guard is absent so the red below fails for the right reason."""
    monkeypatch.setenv(_WORKER_ENV, "4")
    assert create_app(settings=api_settings) is not None


@pytest.mark.xfail(
    strict=True,
    reason="REQ-10: create_app does not fail closed on WEB_CONCURRENCY>1 yet — deferred; closed by slice-auth-boundary-phase-a (PR #403, Issue #410)",
)
def test_create_app_must_fail_closed_on_web_concurrency(
    api_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_WORKER_ENV, "4")
    with pytest.raises((RuntimeError, ValueError)):
        create_app(settings=api_settings)


@pytest.mark.xfail(
    strict=True, reason="REQ-10: Settings has no api_workers field rejecting >1 yet — deferred; closed by slice-auth-boundary-phase-a (PR #403, Issue #410)"
)
def test_settings_must_reject_api_workers_gt_1(api_settings: Settings) -> None:
    """Settings must carry an api_workers field that REJECTS >1 (not merely store it). The
    field-presence assert fails today (no field); once the field exists, the behavioural check
    proves it actually rejects 2 — a bare unconstrained int leaves this xfailing."""
    assert "api_workers" in Settings.model_fields, "Settings has no api_workers field yet"
    # reached only once the field exists: constructing with 2 must be rejected.
    with pytest.raises(Exception):
        Settings.model_validate({**api_settings.model_dump(mode="python"), "api_workers": 2})


def test_single_worker_config_still_boots(
    api_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature preservation: a legitimate single-worker signal must still boot. Green before and
    after the fix."""
    monkeypatch.setenv(_WORKER_ENV, "1")
    assert create_app(settings=api_settings) is not None
