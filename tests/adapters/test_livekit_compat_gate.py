"""Red-proof the compatibility lane even if its pip/import workflow steps disappear."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ABSENT_PLUGIN = """
import importlib.abc, sys
import pytest
class MissingPlugin(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "musubi_livekit" or fullname.startswith("musubi_livekit."):
            raise ModuleNotFoundError("test plugin deliberately absent", name=fullname)
        return None
for name in tuple(sys.modules):
    if name == "musubi_livekit" or name.startswith("musubi_livekit."):
        del sys.modules[name]
sys.meta_path.insert(0, MissingPlugin())
raise SystemExit(pytest.main(["-q", "tests/adapters/test_livekit_compat.py", "-o", "addopts="]))
"""


@pytest.mark.parametrize("required", [False, True])
def test_missing_plugin_is_optional_locally_but_fails_required_lane(required: bool) -> None:
    env = {**os.environ, "MUSUBI_REQUIRE_LIVEKIT_COMPAT": "1" if required else "0"}
    result = subprocess.run(
        [sys.executable, "-c", _ABSENT_PLUGIN],
        cwd=_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if required:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "deliberately absent" in result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "8 skipped" in result.stdout
        assert "2 passed" in result.stdout
