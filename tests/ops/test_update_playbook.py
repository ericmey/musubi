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

import yaml

ROOT = Path(__file__).resolve().parents[2]
UPDATE_PLAYBOOK = ROOT / "deploy" / "ansible" / "update.yml"
RUNBOOK = ROOT / "deploy" / "runbooks" / "upgrade.md"
DEPLOY_PLAYBOOK = ROOT / "deploy" / "ansible" / "deploy.yml"
DEPLOY_WRAPPER = ROOT / "scripts" / "musubi-deploy"
PREFLIGHT_MANIFEST = ROOT / "deploy" / "credential-preflight.json"
ANSIBLE_README = ROOT / "deploy" / "ansible" / "README.md"
AUTO_DIGEST_WORKFLOW = ROOT / ".github" / "workflows" / "auto-digest-bump.yml"


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
        command = task.get("ansible.builtin.command") or task.get("ansible.builtin.shell")
        if command and " compose " in str(command) and " pull" in str(command):
            assert "/usr/bin/op run" in str(command)
            assert "--policy always" in str(command)
            return
    raise AssertionError("update.yml has no op-run compose pull task")


def test_update_compose_up_uses_pull_never_and_recreate_always() -> None:
    for task in _tasks(_play()):
        command = task.get("ansible.builtin.command") or task.get("ansible.builtin.shell")
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
    assert "{{ changed_services_list | join(' ') }}" in text, (
        "compose up must reference the normalised changed_services_list"
    )


def test_update_never_consumes_raw_changed_services() -> None:
    """Every consumer must read `changed_services_list`, never `changed_services`.

    Ansible's `-e key=value` extra-vars are ALWAYS strings, so
    `-e 'changed_services=["core"]'` arrives as literal text. Consuming it
    directly makes `join(' ')` concatenate its CHARACTERS and `loop:` iterate
    them one at a time — and both consumers sit inside `no_log: true` tasks,
    so the operator sees only a bare non-zero exit (#665).
    """
    text = UPDATE_PLAYBOOK.read_text()
    forbidden = (
        "{{ changed_services | join(' ') }}",
        "{{ changed_services | to_json }}",
        'loop: "{{ changed_services }}"',
    )
    offenders = [f for f in forbidden if f in text]
    assert not offenders, (
        "these consume the un-normalised variable and will silently receive a "
        f"string from `-e key=value`: {offenders}"
    )


def test_update_normalises_and_asserts_changed_services() -> None:
    """The normalisation exists AND is guarded before anything is recreated."""
    play = _play()
    vars_ = play.get("vars") or {}
    assert "changed_services_list" in vars_, (
        "update.yml must define changed_services_list to normalise both spellings"
    )

    pre = play.get("pre_tasks") or []
    guard = []
    for task in pre:
        for key in ("assert", "ansible.builtin.assert"):
            body = task.get(key)
            if body and "changed_services_list" in str(body):
                guard.append(body)
    assert guard, (
        "a pre_task must assert changed_services_list is a non-empty list of "
        "strings, so a malformed value fails before any pull or recreate"
    )
    that = str(guard[0].get("that", ""))
    assert "is not string" in that, (
        "the guard must reject a string; that is the exact failure mode of "
        "`-e key=value` extra-vars"
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
        if "--force-recreate"
        in str(task.get("ansible.builtin.command") or task.get("ansible.builtin.shell") or "")
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
    assert not missing, f"post-recreate digest guard missing: {missing!r}"


def test_core_update_runs_candidate_image_credential_preflight_intrinsically() -> None:
    play = _play()
    pre_tasks = play.get("pre_tasks") or []
    names = [task.get("name") for task in pre_tasks]
    preflight_index = names.index("Validate every live credential inside the exact candidate image")

    command = pre_tasks[preflight_index]["ansible.builtin.command"]
    argv = command["argv"]
    assert "{{ musubi_core_image }}" in argv
    assert "musubi.auth.credential_preflight" in argv
    assert "--user" in argv
    user_index = argv.index("--user")
    assert argv[user_index + 1] == (
        "{{ lookup('pipe', 'id -u') }}:{{ lookup('pipe', 'id -g') }}"
    )
    assert pre_tasks[preflight_index]["delegate_to"] == "localhost"
    assert pre_tasks[preflight_index]["become"] is False
    assert pre_tasks[preflight_index].get("no_log") is not True

    assert pre_tasks[preflight_index] in pre_tasks
    assert any("--policy always" in str(task) for task in _tasks(play))


def test_core_update_verifies_candidate_signature_before_exposing_secrets() -> None:
    pre_tasks = _play().get("pre_tasks") or []
    names = [task.get("name") for task in pre_tasks]
    verify_index = names.index("Verify the exact Core candidate image signature")
    preflight_index = names.index("Validate every live credential inside the exact candidate image")
    verify_argv = pre_tasks[verify_index]["ansible.builtin.command"]["argv"]

    assert verify_index < preflight_index
    assert verify_argv[0:2] == ["cosign", "verify"]
    assert "{{ musubi_core_image }}" in verify_argv
    assert "--env-file" not in verify_argv
    assert "/credentials" not in str(verify_argv)


def test_auto_digest_pin_requires_human_preflight_before_merge() -> None:
    text = AUTO_DIGEST_WORKFLOW.read_text()
    before_merge = text.index("## Before merge")
    cosign = text.index("cosign verify", before_merge)
    candidate_run = text.index("musubi.auth.credential_preflight", before_merge)

    assert "gh pr merge" not in text
    assert cosign < candidate_run
    assert "--user" in text[candidate_run - 1000 : candidate_run]
    assert "MUSUBI_PREFLIGHT_AUTHORITY_ENV" in text[before_merge:candidate_run]


def test_core_update_preflight_cannot_be_satisfied_by_caller_attestation_vars() -> None:
    text = UPDATE_PLAYBOOK.read_text()
    assert "candidate_credential_preflight_passed" not in text
    assert "candidate_credential_preflight_image" not in text
    assert "docker" in text
    assert "musubi.auth.credential_preflight" in text


def test_apply_wrapper_requires_explicit_preflight_authority_env() -> None:
    text = DEPLOY_WRAPPER.read_text()
    assert "MUSUBI_PREFLIGHT_AUTHORITY_ENV" in text
    assert "musubi-mcp-aoi.env" not in text
    assert 'exec "${cmd[@]}"' in text


def test_every_documented_core_update_entrypoint_names_preflight_authority_env() -> None:
    for path in (RUNBOOK, ANSIBLE_README, AUTO_DIGEST_WORKFLOW):
        text = path.read_text()
        assert "MUSUBI_PREFLIGHT_AUTHORITY_ENV" in text, (
            f"{path} documents Core updates without the required preflight authority env"
        )


def test_candidate_preflight_manifest_declares_twelve_live_and_one_template() -> None:
    manifest = yaml.safe_load(PREFLIGHT_MANIFEST.read_text())
    assert len(manifest["live"]) == 12
    assert len(manifest["templates"]) == 1
    assert manifest["templates"][0]["file"] == "musubi-mcp.env"
    assert manifest["templates"][0]["classification"] == "non-consumed-template"


def test_update_writes_upgrade_history() -> None:
    text = UPDATE_PLAYBOOK.read_text()
    assert "/var/log/musubi/upgrade-history.jsonl" in text
    # Line contents must carry the core_image + service list for later forensics.
    assert "core_image" in text
    assert "services" in text
    for task in _tasks(_play()):
        command = task.get("ansible.builtin.command") or task.get("ansible.builtin.shell")
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
