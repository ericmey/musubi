"""The published tei-dense endpoint (bge-m3 for the house model gateway).

Its address and allowed source come from the inventory; one shared preflight
validates them on every compose-render path; and a Musubi-owned chain jumped
from DOCKER-USER admits only the allowed source. That is an IP allow-list,
not authentication. Fixtures are generic RFC1918 addresses.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deploy" / "ansible"
COMPOSE = ANSIBLE / "templates" / "docker-compose.yml.j2"
SCRIPT = ANSIBLE / "templates" / "musubi-tei-firewall.sh.j2"
UNIT = ANSIBLE / "templates" / "musubi-tei-firewall.service.j2"
PREFLIGHT = ANSIBLE / "tasks" / "tei-dense-preflight.yml"
FIREWALL_TASKS = ANSIBLE / "tasks" / "tei-dense-firewall.yml"
PLAYBOOKS = ["bootstrap", "config", "deploy", "update"]
STUB = Path(__file__).with_name("iptables_stub.py")

BIND, SOURCE, OTHER, PORT = "10.0.0.45", "10.0.0.25", "10.0.0.99", 8081
CHAIN, STAGE = "MUSUBI-TEI-DENSE", "MUSUBI-TEI-DENSE-NEW"
PRISTINE = {"DOCKER-USER": [["-j", "RETURN"]]}  # what Docker creates


# --- the address authority --------------------------------------------------


def test_the_bind_and_source_come_from_the_inventory_and_only_tei_dense_publishes() -> None:
    dense = COMPOSE.read_text().split("\n  tei-dense:\n", 1)[1].split("\n  tei-sparse:", 1)[0]
    assert re.findall(r'^\s+- "(.*):80"$', dense, re.M) == [
        "{{ musubi_lan_bind }}:{{ musubi_tei_dense_port }}"
    ]
    inventory = (ANSIBLE / "inventory.yml").read_text()
    assert re.search(r'^\s+musubi_lan_bind: "\{\{ musubi_ip \}\}"$', inventory, re.M)
    assert re.search(
        r"^\s+musubi_tei_dense_allowed_source: \"\{\{ tei_dense_allowed_source \| default\(''\) \}\}\"$",
        inventory,
        re.M,
    )
    rendered = COMPOSE.read_text()
    for token in re.findall(r"\{\{ [a-z_]+ \}\}", rendered):
        rendered = rendered.replace(
            token,
            {
                "{{ musubi_lan_bind }}": BIND,
                "{{ musubi_tei_dense_port }}": str(PORT),
                "{{ musubi_core_port }}": "8100",
            }.get(token, "x"),
        )
    services = yaml.safe_load(rendered)["services"]
    assert services["tei-dense"]["ports"] == [f"{BIND}:{PORT}:80"]
    assert sorted(n for n, s in services.items() if n.startswith("tei-") and "ports" in s) == [
        "tei-dense"
    ]


def test_no_site_address_is_committed_in_the_tei_dense_files() -> None:
    ipv4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
    for path in (
        SCRIPT,
        UNIT,
        PREFLIGHT,
        FIREWALL_TASKS,
        ANSIBLE / "group_vars" / "all.yml",
        ANSIBLE / "inventory.yml",
    ):
        found = [
            a for a in ipv4.findall(path.read_text()) if not a.startswith("127.")
        ]  # loopback is not a site
        assert not found, (path.name, found)


# --- one shared preflight on every render path ------------------------------


def _play(name: str) -> dict[str, Any]:
    plays = yaml.safe_load((ANSIBLE / f"{name}.yml").read_text())
    play: dict[str, Any] = next(
        p for p in plays if "docker-compose.yml.j2" in yaml.safe_dump(p.get("tasks", []))
    )
    return play


@pytest.mark.parametrize("playbook", PLAYBOOKS)
def test_every_render_path_validates_then_firewalls_then_renders(playbook: str) -> None:
    play = _play(playbook)
    imports = [t.get("ansible.builtin.import_tasks") for t in play["pre_tasks"]]
    assert imports.count("tasks/tei-dense-preflight.yml") == 1
    tasks = play["tasks"]
    names = [yaml.safe_dump(t) for t in tasks]
    firewall = next(
        i
        for i, t in enumerate(tasks)
        if t.get("ansible.builtin.import_tasks") == "tasks/tei-dense-firewall.yml"
    )
    render = next(i for i, n in enumerate(names) if "docker-compose.yml.j2" in n)
    assert firewall < render, (
        f"{playbook}: the allow-list must be active before Compose can publish"
    )


def _passes(bind: object, source: object, port: object) -> bool:
    import jinja2

    env = jinja2.Environment()
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    task = yaml.safe_load(PREFLIGHT.read_text())[0]
    values = {
        "musubi_lan_bind": bind,
        "musubi_tei_dense_allowed_source": source,
        "musubi_tei_dense_port": port,
    }
    return all(
        env.from_string("{% if " + c + " %}1{% endif %}").render(**values) == "1"
        for c in task["ansible.builtin.assert"]["that"]
    )


def test_the_preflight_admits_only_private_addresses_and_a_real_port() -> None:
    for bind, source, port in [
        (BIND, SOURCE, PORT),
        ("192.168.1.10", "192.168.1.2", 1),
        ("172.16.0.1", "172.31.255.255", 65535),
    ]:
        assert _passes(bind, source, port), (bind, source, port)
    bad: list[tuple[object, object, object]] = [
        ("8.8.8.8", SOURCE, PORT),
        ("203.0.113.5", SOURCE, PORT),
        ("0.0.0.0", SOURCE, PORT),
        ("::", SOURCE, PORT),
        ("172.32.0.1", SOURCE, PORT),
        ("10.0.0.256", SOURCE, PORT),
        (f"{BIND} ", SOURCE, PORT),
        ("musubi.local", SOURCE, PORT),
        (BIND, "", PORT),
        (BIND, "8.8.8.8", PORT),
        (BIND, "0.0.0.0", PORT),
        (BIND, BIND, PORT),  # source: empty/public/any/self
        (BIND, SOURCE, 0),
        (BIND, SOURCE, 65536),
        (BIND, SOURCE, True),
        (BIND, SOURCE, "8081"),
    ]
    for bad_bind, bad_source, bad_port in bad:
        assert not _passes(bad_bind, bad_source, bad_port), (bad_bind, bad_source, bad_port)


# --- the firewall itself ----------------------------------------------------


def _verdict(state: dict[str, list[list[str]]], src: str, dst: str, dport: int) -> str:
    """How DOCKER-USER treats a forwarded TCP packet (post-DNAT, matched on
    its original destination), as far as the script's rules can express."""

    def walk(chain: str) -> str:
        for rule in state[chain]:
            opts = dict(itertools.pairwise(rule))
            if "-s" in opts and opts["-s"] != src:
                continue
            if "--ctorigdst" in opts and opts["--ctorigdst"] != dst:
                continue
            if "--ctorigdstport" in opts and opts["--ctorigdstport"] != str(dport):
                continue
            target = opts["-j"]
            if target in state:
                if walk(target) == "DROP":
                    return "DROP"
                continue
            return target
        return "RETURN"

    return "DROP" if walk("DOCKER-USER") == "DROP" else "ACCEPT"


def _run(
    tmp_path: Path, state: dict[str, list[list[str]]], docker_user: bool = True
) -> tuple[int, list[dict[str, Any]]]:
    script = SCRIPT.read_text()
    for token, value in (
        ("{{ musubi_lan_bind }}", BIND),
        ("{{ musubi_tei_dense_port }}", str(PORT)),
        ("{{ musubi_tei_dense_allowed_source }}", SOURCE),
    ):
        script = script.replace(token, value)
    (tmp_path / "fw.sh").write_text(script)
    wrapper = tmp_path / "iptables"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    st, log = tmp_path / "state.json", tmp_path / "log.jsonl"
    st.write_text(json.dumps(state if docker_user else {}))
    log.write_text("")
    env = {
        **os.environ,
        "IPTABLES": str(wrapper),
        "IPT_STUB_STATE": str(st),
        "IPT_STUB_LOG": str(log),
    }
    code = subprocess.run(
        ["bash", str(tmp_path / "fw.sh")], env=env, capture_output=True, text=True
    ).returncode
    snaps = [json.loads(line) for line in log.read_text().splitlines()]
    return code, [json.loads(st.read_text()), *snaps]


def _protected(state: dict[str, list[list[str]]]) -> bool:
    return (_verdict(state, OTHER, BIND, PORT) == "DROP") and (
        _verdict(state, SOURCE, BIND, PORT) == "ACCEPT"
    )


def _jumps(state: dict[str, list[list[str]]]) -> int:
    return sum(1 for r in state["DOCKER-USER"] if r[-2:] == ["-j", CHAIN])


def test_first_apply_admits_only_the_allowed_source_to_the_exact_endpoint(tmp_path: Path) -> None:
    code, (final, *_) = _run(tmp_path, json.loads(json.dumps(PRISTINE)))
    assert code == 0
    assert _protected(final)
    assert (
        _verdict(final, OTHER, BIND, 9999) == "ACCEPT"
    )  # another port on the same address: untouched
    assert (
        _verdict(final, OTHER, "10.0.0.46", PORT) == "ACCEPT"
    )  # the same port elsewhere: untouched
    assert _jumps(final) == 1 and STAGE not in final
    assert final["DOCKER-USER"][-1] == ["-j", "RETURN"]  # Docker's own rule is left alone


def test_reapply_never_leaves_the_endpoint_unfiltered(tmp_path: Path) -> None:
    _, (applied, *_) = _run(tmp_path, json.loads(json.dumps(PRISTINE)))
    code, (final, *steps) = _run(tmp_path, applied)
    assert code == 0 and steps, "the reapply must run through the stub"
    assert all(_protected(s) for s in steps), "an intermediate state exposed the endpoint"
    assert _protected(final) and _jumps(final) == 1 and STAGE not in final


def test_an_interrupted_earlier_run_is_repaired_without_a_window(tmp_path: Path) -> None:
    _, (applied, *_) = _run(tmp_path, json.loads(json.dumps(PRISTINE)))
    broken = json.loads(json.dumps(applied))
    broken[STAGE] = [["-p", "tcp", "-j", "RETURN"]]  # a half-built staging chain...
    broken["DOCKER-USER"].insert(0, ["-j", STAGE])  # ...already jumped to
    broken["DOCKER-USER"].insert(0, ["-j", CHAIN])  # and a duplicate jump
    code, (final, *steps) = _run(tmp_path, broken)
    assert code == 0 and all(_protected(s) for s in steps)
    assert _protected(final) and _jumps(final) == 1 and STAGE not in final


def test_without_dockers_chain_the_script_fails_and_changes_nothing(tmp_path: Path) -> None:
    code, (final, *steps) = _run(tmp_path, {}, docker_user=False)
    assert code != 0 and final == {} and steps == []


def test_the_unit_reapplies_on_every_docker_start() -> None:
    unit = UNIT.read_text()
    for line in (
        "After=docker.service",
        "PartOf=docker.service",
        "Type=oneshot",
        "RemainAfterExit=yes",
        "ExecStart=/usr/local/sbin/musubi-tei-firewall",
        "WantedBy=docker.service multi-user.target",
    ):
        assert re.search(rf"^{re.escape(line)}$", unit, re.M), line
    tasks = yaml.safe_load(FIREWALL_TASKS.read_text())
    assert [t["ansible.builtin.template"]["dest"] for t in tasks[:2]] == [
        "/usr/local/sbin/musubi-tei-firewall",
        "/etc/systemd/system/musubi-tei-firewall.service",
    ]
    enable = tasks[2]["ansible.builtin.systemd_service"]
    assert enable["enabled"] is True and enable["daemon_reload"] is True
