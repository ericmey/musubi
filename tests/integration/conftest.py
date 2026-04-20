"""Integration-test fixtures.

Boots the docker-compose dependency stack at session scope, then
spawns an in-process uvicorn for ``musubi.api.app:create_app`` so
the suite hits the same code path as `make test` but against real
Qdrant + TEI + Ollama. Tear-down at session-end stops the
uvicorn subprocess and runs ``docker compose down -v``.

Per slice ``slice-ops-integration-harness``:

- ``live_stack`` fixture is session-scoped (compose boot is the
  expensive operation we want to amortise).
- ``api_client`` fixture is function-scoped; one fresh
  :class:`AsyncMusubiClient` per test, sharing the live stack.
- ``parallel_session_fixture`` exposes the tooling test #4 needs
  for asserting the harness supports parallel session execution
  (multiple ``api_client`` against one ``live_stack``).

The :func:`subprocess.run` calls are factored through a tiny
indirection (:func:`_run`) so harness-shape tests
(`tests/integration/test_harness.py`) can monkeypatch it and verify
fixture behaviour without docker installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

# Repo root — three parents up from this file
# (tests/integration/conftest.py → tests/integration → tests → repo root).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "deploy" / "test-env" / "docker-compose.test.yml"
_ENV_FILE = _REPO_ROOT / "deploy" / "test-env" / ".env.test"

_DEFAULT_API_PORT = 8100
_BOOT_TIMEOUT_S = 300.0
_BOOT_POLL_INTERVAL_S = 1.0


@dataclass(frozen=True)
class StackHandle:
    """Per-session live-stack handle that fixtures + tests share."""

    api_url: str
    """Base URL the in-process musubi-core uvicorn listens on
    (e.g. ``http://localhost:8100/v1``)."""

    operator_token: str
    """JWT minted against the test JWT signing key with the operator
    scope so tests can hit any namespace + endpoint."""

    qdrant_url: str
    """Qdrant HTTP URL — exposed for tests that need to reach the
    vector store directly (rare; most go through the API)."""

    compose_project: str
    """``docker compose -p <project>`` so parallel sessions don't
    collide on container names."""


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Single chokepoint for shell-outs — harness-shape tests
    monkeypatch this attribute to verify the fixture's command
    sequence without running real docker / uvicorn."""
    return subprocess.run(
        cmd,
        check=kwargs.pop("check", True),
        capture_output=kwargs.pop("capture_output", True),
        text=kwargs.pop("text", True),
        **kwargs,
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _compose_up(project: str) -> None:
    _run(
        [
            "docker",
            "compose",
            "-f",
            str(_COMPOSE_FILE),
            "-p",
            project,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            str(int(_BOOT_TIMEOUT_S)),
        ]
    )


def _compose_down(project: str) -> None:
    _run(
        [
            "docker",
            "compose",
            "-f",
            str(_COMPOSE_FILE),
            "-p",
            project,
            "down",
            "-v",
            "--remove-orphans",
        ],
        check=False,
    )


def _wait_for_api(url: str, timeout_s: float = _BOOT_TIMEOUT_S) -> None:
    """Poll ``GET {url}/v1/ops/health`` until 200 or timeout."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(f"{url}/v1/ops/health")
            if resp.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_err = exc
        time.sleep(_BOOT_POLL_INTERVAL_S)
    raise RuntimeError(
        f"musubi-core did not become healthy at {url} within {timeout_s}s "
        f"(last error: {last_err!r})"
    )


def _start_api(*, port: int, env_file: Path) -> subprocess.Popen[bytes]:
    """Spawn an in-process uvicorn for ``musubi.api.app:create_app``.

    Reads its ``Settings`` from the env-file the compose stack
    publishes ports against. Returned :class:`subprocess.Popen` is
    terminated at session teardown.
    """
    env = os.environ.copy()
    env.update(_parse_env_file(env_file))
    return subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "--factory",
            "musubi.api.app:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        cwd=_REPO_ROOT,
    )


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _mint_operator_token(jwt_signing_key: str) -> str:
    """HS256 token with the operator scope for the integration tests."""
    from datetime import UTC, datetime, timedelta

    import jwt

    now = datetime.now(UTC)
    payload = {
        "iss": "https://auth.test.local",
        "sub": "integration-test",
        "aud": "musubi",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=2)).timestamp()),
        "jti": "integration-test-token",
        "scope": "operator",
        "presence": "integration-test/harness",
    }
    return jwt.encode(payload, jwt_signing_key, algorithm="HS256")


# --------------------------------------------------------------------------
# Public fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_stack(request: pytest.FixtureRequest) -> Iterator[StackHandle]:
    """Boot the docker-compose dependency stack + an in-process
    uvicorn; yield a :class:`StackHandle`; tear down at session end.

    Skips the entire session if docker is not installed (so unit-only
    runs against the integration package don't error). Bullets 1-4
    (`tests/integration/test_harness.py`) monkeypatch
    :func:`_run` so they don't hit this fast-skip path."""
    if not _docker_available():
        pytest.skip("docker is not installed; integration smoke skipped")

    api_port = int(os.environ.get("MUSUBI_TEST_API_PORT", str(_DEFAULT_API_PORT)))
    project = os.environ.get("MUSUBI_TEST_PROJECT", "musubi-integration")
    api_url = f"http://127.0.0.1:{api_port}"

    env_kv = _parse_env_file(_ENV_FILE)
    operator_token = _mint_operator_token(env_kv["JWT_SIGNING_KEY"])

    # Boot deps + spawn API.
    _compose_up(project)
    api_proc = _start_api(port=api_port, env_file=_ENV_FILE)
    try:
        _wait_for_api(api_url)
        handle = StackHandle(
            api_url=f"{api_url}/v1",
            operator_token=operator_token,
            qdrant_url=f"http://127.0.0.1:{env_kv.get('QDRANT_PORT', '6333')}",
            compose_project=project,
        )
        yield handle
    finally:
        api_proc.terminate()
        try:
            api_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api_proc.kill()
        _compose_down(project)


@pytest.fixture
def api_client(live_stack: StackHandle) -> Iterator[Any]:
    """Per-test :class:`AsyncMusubiClient`. Closed on test exit.

    Tests run via ``asyncio.run(...)`` create + tear down their own
    event loop per call, so the loop is already closed when this
    finalizer runs. We allocate a fresh loop just for the close to
    avoid the ``RuntimeError: Event loop is closed`` chain that
    surfaces otherwise."""
    import asyncio

    from musubi.sdk import AsyncMusubiClient

    client = AsyncMusubiClient(
        base_url=live_stack.api_url,
        token=live_stack.operator_token,
    )
    try:
        yield client
    finally:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(client.close())
        finally:
            loop.close()
