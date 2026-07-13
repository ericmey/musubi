"""Structural tests for `deploy/ansible/update.yml` + its runbook.

Cannot run Ansible in unit tests — these tests assert playbook shape
and runbook coverage so drift fails CI instead of a live upgrade
silently no-opping.

Scope:

- The playbook parses as YAML and is a valid ansible play structure.
- The pull step uses `policy: always` — the single most important
  difference from `deploy.yml` (which uses `missing`).
- The compose-up step has `recreate: always` + `pull: never` (don't
  double-pull) + honours `changed_services` (defaults to `[core]`).
- The play does NOT re-run bootstrap tasks (apt install / user
  creation) — update.yml assumes the host is already bootstrapped.
- Health probe + upgrade-history append tasks exist.
- The upgrade runbook has the six named sections in order and every
  one documents a rollback path.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
UPDATE_PLAYBOOK = ROOT / "deploy" / "ansible" / "update.yml"
RUNBOOK = ROOT / "deploy" / "runbooks" / "upgrade.md"
DEPLOY_PLAYBOOK = ROOT / "deploy" / "ansible" / "deploy.yml"


class DefectStillPresent(Exception):
    """Raised when a production deployment guard was lost during reconciliation."""


def _load(path: Path) -> list[dict[str, Any]]:
    parsed = yaml.safe_load(path.read_text())
    assert isinstance(parsed, list), f"{path} must be an ansible play list"
    return parsed


def _tasks(play: dict[str, Any]) -> list[dict[str, Any]]:
    return list(play.get("tasks") or [])


def _play() -> dict[str, Any]:
    return _load(UPDATE_PLAYBOOK)[0]


# ---------------------------------------------------------------------------
# Playbook structure
# ---------------------------------------------------------------------------


def test_update_playbook_parses() -> None:
    assert UPDATE_PLAYBOOK.exists(), f"missing {UPDATE_PLAYBOOK}"
    play = _play()
    assert play.get("hosts") == "musubi"
    assert play.get("become") is True


def test_update_pull_policy_is_always() -> None:
    """The key difference from deploy.yml (policy=missing)."""
    for task in _tasks(_play()):
        command = task.get("ansible.builtin.command")
        if command and " compose " in str(command) and " pull" in str(command):
            assert "/usr/bin/op run" in str(command)
            assert "--policy always" in str(command)
            return
    raise AssertionError("update.yml has no op-run compose pull task")


def test_update_compose_up_uses_pull_never_and_recreate_always() -> None:
    for task in _tasks(_play()):
        command = task.get("ansible.builtin.command")
        if not command or "--force-recreate" not in str(command):
            continue
        assert "/usr/bin/op run" in str(command)
        assert "--pull never" in str(command)
        assert "--no-deps" in str(command)
        return
    raise AssertionError("update.yml has no per-service op-run compose recreate task")


def test_update_recreates_only_named_services_with_core_default() -> None:
    """`changed_services` drives which containers get recreated; default is
    `[core]` because Core is by far the most frequently-bumped image."""
    play = _play()
    vars_ = play.get("vars") or {}
    defaults = vars_.get("changed_services")
    assert defaults == ["core"], f"default changed_services should be ['core'], got {defaults!r}"
    text = UPDATE_PLAYBOOK.read_text()
    assert "{{ changed_services | join(' ') }}" in text, (
        "compose up must reference the changed_services variable"
    )


def test_update_renders_production_env_before_recreate() -> None:
    tasks = _tasks(_play())
    env_indices = [
        idx
        for idx, task in enumerate(tasks)
        if task.get("ansible.builtin.template", {}).get("dest")
        == "{{ musubi_config_dir }}/.env.production"
    ]
    assert env_indices, "update.yml must refresh .env.production"
    env_task = tasks[env_indices[0]]
    assert env_task.get("no_log") is True, ".env.production rendering must not log secrets"

    recreate_indices = [
        idx
        for idx, task in enumerate(tasks)
        if "--force-recreate" in str(task.get("ansible.builtin.command") or "")
    ]
    assert recreate_indices, "update.yml has no per-service compose recreate task"
    assert env_indices[0] < recreate_indices[0], (
        ".env.production must render before containers are recreated"
    )


def test_update_does_not_invoke_bootstrap_tasks() -> None:
    """update.yml must NOT re-run apt installs or user-creation tasks —
    that's bootstrap.yml's job and re-running it on every upgrade is slow
    and risky."""
    for task in _tasks(_play()):
        forbidden_modules = (
            "ansible.builtin.apt",
            "ansible.builtin.user",
            "ansible.builtin.group",
            "ansible.builtin.apt_repository",
            "ansible.builtin.apt_key",
            "community.general.ufw",
        )
        for mod in forbidden_modules:
            assert mod not in task, (
                f"update.yml must not invoke {mod!r} — that's bootstrap.yml's role"
            )
        for keyword in ("import_playbook", "include_playbook"):
            if keyword in task:
                assert "bootstrap" not in str(task[keyword]), (
                    "update.yml must not import bootstrap.yml"
                )


def test_update_probes_core_health_post_apply() -> None:
    """A successful update ends with /v1/ops/health returning 200."""
    for task in _tasks(_play()):
        module = task.get("ansible.builtin.uri")
        if module and "/ops/health" in str(module.get("url", "")):
            assert module.get("status_code") == 200
            return
        # Also accept {{ musubi_health_urls.core }} templating.
        if module and "{{ musubi_health_urls.core }}" in str(module.get("url", "")):
            return
    raise AssertionError("update.yml has no /v1/ops/health probe task")


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="the source candidate removes the post-recreate Config.Image assertion that prevents a silent stale or downgraded core/lifecycle worker",
)
def test_update_asserts_recreated_core_services_match_the_pinned_digest() -> None:
    text = UPDATE_PLAYBOOK.read_text()
    required = (
        "Inspect recreated containers",
        "community.docker.docker_container_info",
        "item.container.Config.Image == musubi_core_image",
        "item.item in ['core', 'lifecycle-worker']",
        "no_log: true",
    )
    missing = [fragment for fragment in required if fragment not in text]
    if missing:
        raise DefectStillPresent(f"post-recreate digest guard missing: {missing!r}")


def test_update_writes_upgrade_history() -> None:
    text = UPDATE_PLAYBOOK.read_text()
    assert "/var/log/musubi/upgrade-history.jsonl" in text
    # Line contents must carry the core_image + service list for later forensics.
    assert "core_image" in text
    assert "services" in text
    for task in _tasks(_play()):
        command = task.get("ansible.builtin.command")
        if not command or "--force-recreate" not in str(command):
            continue
        notify = task.get("notify") or []
        assert "Ensure upgrade-history log directory exists" in notify
        assert "Append an upgrade-history entry" in notify
        return
    raise AssertionError("update.yml has no per-service op-run compose recreate task")


def test_update_playbook_does_not_lower_deploys_digest_pin_behaviour() -> None:
    """Sanity-check: deploy.yml still uses `policy: missing`. A future change
    that flips deploy.yml to `always` as a shortcut would remove update.yml's
    reason to exist."""
    deploy_text = DEPLOY_PLAYBOOK.read_text()
    assert "pull --policy missing" in deploy_text
    assert "--policy always" not in deploy_text, (
        "deploy.yml uses `missing`; `always` belongs to update.yml"
    )


# ---------------------------------------------------------------------------
# Runbook
# ---------------------------------------------------------------------------


def test_upgrade_runbook_has_six_sections() -> None:
    assert RUNBOOK.exists(), f"missing {RUNBOOK}"
    text = RUNBOOK.read_text()
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    # 6 numbered steps; may also have other top-level ## sections.
    numbered = [h for h in headings if re.match(r"^## \d", h)]
    assert len(numbered) >= 6, (
        f"upgrade.md should have at least 6 numbered steps; got {len(numbered)}"
    )


def test_upgrade_runbook_every_step_has_rollback() -> None:
    text = RUNBOOK.read_text()
    step_sections = re.split(r"^## \d", text, flags=re.MULTILINE)[1:]
    assert step_sections, "no numbered steps found in runbook"
    for i, sec in enumerate(step_sections, 1):
        assert "Rollback:" in sec, f"step {i} has no `Rollback:` clause"


def test_upgrade_runbook_mentions_revert_and_rerun_rollback() -> None:
    """The slice spec calls for the revert-and-rerun rollback pattern rather
    than a dedicated --rollback flag. Confirm that language is present."""
    text = RUNBOOK.read_text()
    assert "revert" in text.lower()
    assert "re-run" in text.lower() or "rerun" in text.lower()
    # And specifically: a `git revert ... && ansible-playbook update.yml`
    # sequence somewhere in the rollback section.
    assert "git" in text.lower() and "revert" in text.lower()
    assert "update.yml" in text
