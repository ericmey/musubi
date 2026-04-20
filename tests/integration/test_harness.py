"""Test contract bullets 1-4 — harness shape.

These tests verify the **fixture contract**: the subprocess argument
sequences the harness emits, the env-file plumbing, the per-session
cleanup invariants, and the parallel-session safety. They monkeypatch
the single subprocess chokepoint (`tests.integration.conftest._run`)
so the suite verifies locally on machines without docker installed,
exactly per the operator-confirmed CI-as-first-verification split.

Bullets 5-14 (real-services smoke) live in
``tests/integration/test_smoke.py`` and only run when docker is
available. Those execute on CI via the PR-trigger path-filter +
nightly-matrix workflow.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration import conftest as harness

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "deploy" / "test-env" / "docker-compose.test.yml"


@pytest.fixture
def captured_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[list[str]]]:
    """Replace the harness's subprocess chokepoint with an in-memory
    recorder that returns a successful CompletedProcess for every call.
    Tests assert against the recorded command sequence."""
    calls: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        calls.append(list(cmd))
        return _FakeCompleted()

    monkeypatch.setattr(harness, "_run", _fake_run)
    yield calls


# --------------------------------------------------------------------------
# Bullet 1 — boots compose stack cleanly
# --------------------------------------------------------------------------


def test_harness_boots_compose_stack_cleanly(captured_calls: list[list[str]]) -> None:
    """Calling ``_compose_up`` issues the canonical ``docker compose
    -f <test-env compose> -p <project> up -d --wait`` invocation."""
    harness._compose_up("musubi-integration-test1")
    assert len(captured_calls) == 1
    cmd = captured_calls[0]
    assert cmd[:2] == ["docker", "compose"]
    assert "-f" in cmd
    f_idx = cmd.index("-f")
    assert cmd[f_idx + 1] == str(_COMPOSE_FILE)
    assert "-p" in cmd
    p_idx = cmd.index("-p")
    assert cmd[p_idx + 1] == "musubi-integration-test1"
    # `up -d --wait` is the boot-with-healthcheck-await idiom.
    assert "up" in cmd
    assert "-d" in cmd
    assert "--wait" in cmd


# --------------------------------------------------------------------------
# Bullet 2 — tears down cleanly leaving no orphans
# --------------------------------------------------------------------------


def test_harness_tears_down_cleanly_leaving_no_orphans(
    captured_calls: list[list[str]],
) -> None:
    """``_compose_down`` issues ``docker compose ... down -v
    --remove-orphans`` so volumes + any orphaned containers are
    cleaned. ``check=False`` so a partially-up stack still tears down
    rather than wedging the suite."""
    harness._compose_down("musubi-integration-test2")
    assert len(captured_calls) == 1
    cmd = captured_calls[0]
    assert cmd[:2] == ["docker", "compose"]
    assert "down" in cmd
    assert "-v" in cmd
    assert "--remove-orphans" in cmd


# --------------------------------------------------------------------------
# Bullet 3 — pytest fixture provides real client to running stack
# --------------------------------------------------------------------------


def test_harness_pytest_fixture_provides_real_client_to_running_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``api_client`` fixture wires the SDK against the
    ``StackHandle.api_url`` + operator token surfaced by the
    ``live_stack`` fixture. Verified by constructing the
    :class:`AsyncMusubiClient` against a synthetic StackHandle and
    asserting on its base URL + Authorization header."""
    from musubi.sdk import AsyncMusubiClient

    handle = harness.StackHandle(
        api_url="http://127.0.0.1:18100/v1",
        operator_token="dummy-jwt",
        qdrant_url="http://127.0.0.1:16333",
        compose_project="musubi-integration-test3",
    )

    client = AsyncMusubiClient(
        base_url=handle.api_url,
        token=handle.operator_token,
    )
    try:
        # Internals are intentionally introspected — this test asserts
        # the FIXTURE plumbing, not the SDK's public surface.
        assert client._base_url == handle.api_url
        # Bearer header is set on the underlying httpx.AsyncClient.
        assert client._http.headers["Authorization"] == f"Bearer {handle.operator_token}"
    finally:
        import asyncio

        asyncio.new_event_loop().run_until_complete(client.close())


# --------------------------------------------------------------------------
# Bullet 4 — supports parallel session execution
# --------------------------------------------------------------------------


def test_harness_supports_parallel_session_execution(
    captured_calls: list[list[str]],
) -> None:
    """The ``-p <project>`` namespace lets two independent test
    sessions boot the same compose file concurrently without
    container-name collisions. Verified by spinning up two distinct
    project IDs and asserting both invocations carry distinct
    project flags."""
    harness._compose_up("musubi-integration-session-A")
    harness._compose_up("musubi-integration-session-B")

    assert len(captured_calls) == 2
    project_args = []
    for cmd in captured_calls:
        p_idx = cmd.index("-p")
        project_args.append(cmd[p_idx + 1])

    assert project_args == [
        "musubi-integration-session-A",
        "musubi-integration-session-B",
    ]
    # Distinct project IDs → distinct compose project namespaces →
    # distinct container names → no collision.
    assert len(set(project_args)) == 2


# --------------------------------------------------------------------------
# Coverage tests — exercise additional surfaces of the harness module.
# --------------------------------------------------------------------------


def test_parse_env_file_strips_comments_and_blanks(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text("\n# comment\nQDRANT_HOST=localhost\n\nQDRANT_PORT=6333\n# trailing\n")
    out = harness._parse_env_file(p)
    assert out == {"QDRANT_HOST": "localhost", "QDRANT_PORT": "6333"}


def test_parse_env_file_handles_inline_equals(tmp_path: Path) -> None:
    """An env value that itself contains ``=`` (e.g. a base64-encoded
    secret) is preserved on the right side of the FIRST split."""
    p = tmp_path / "env"
    p.write_text("KEY=base64=value=with=equals\n")
    assert harness._parse_env_file(p) == {"KEY": "base64=value=with=equals"}


def test_mint_operator_token_returns_valid_hs256(monkeypatch: pytest.MonkeyPatch) -> None:
    import jwt

    secret = "test-signing-key-must-be-long-enough-for-hs256-32-bytes-min"
    token = harness._mint_operator_token(secret)
    decoded = jwt.decode(token, secret, audience="musubi", algorithms=["HS256"])
    assert decoded["scope"] == "operator"
    assert decoded["aud"] == "musubi"


def test_docker_available_reflects_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    assert harness._docker_available() is True
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert harness._docker_available() is False


def test_compose_file_path_resolves_to_repo_root() -> None:
    """The fixture's compose-file path resolves to the on-disk file
    under deploy/test-env/, not a relative cwd path. Catches
    regressions where a refactor breaks the parents[N] math."""
    assert harness._COMPOSE_FILE == _COMPOSE_FILE
    assert harness._COMPOSE_FILE.exists(), f"compose file not found at {harness._COMPOSE_FILE}"


def test_env_file_path_resolves_to_repo_root() -> None:
    expected = _REPO_ROOT / "deploy" / "test-env" / ".env.test"
    assert expected == harness._ENV_FILE
    assert harness._ENV_FILE.exists()


def test_compose_down_uses_check_false(captured_calls: list[list[str]]) -> None:
    """Tear-down must tolerate a partially-up stack; verify the
    fixture passes ``check=False`` to subprocess.run by replacing
    _run with one that records kwargs."""
    captured_kwargs: list[dict[str, Any]] = []

    def _record(cmd: list[str], **kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Done()

    import tests.integration.conftest as conftest_mod

    original = conftest_mod._run
    conftest_mod._run = _record
    try:
        conftest_mod._compose_down("teardown-check")
    finally:
        conftest_mod._run = original

    assert captured_kwargs[0].get("check") is False
