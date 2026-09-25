"""The quickstart demo must be repeatable against its persistent Qdrant volume.

Its success check compares object_ids from the current run, so each run needs its
own namespace. Otherwise a second run can correctly retrieve the first run's
identical memory and fail.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from musubi.types.common import NAMESPACE_RE

DEMO = Path(__file__).resolve().parents[2] / "quickstart" / "demo.py"


def _load(monkeypatch: pytest.MonkeyPatch, name: str) -> ModuleType:
    monkeypatch.setenv("JWT_SIGNING_KEY", "test-key-at-least-32-bytes-long-xxxx")
    spec = importlib.util.spec_from_file_location(name, DEMO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_namespace_is_valid_and_unique_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _load(monkeypatch, "quickstart_demo_a")
    second = _load(monkeypatch, "quickstart_demo_b")

    assert NAMESPACE_RE.match(first.NS)
    assert NAMESPACE_RE.match(second.NS)
    assert first.RUN in first.NS
    assert first.NS != second.NS
