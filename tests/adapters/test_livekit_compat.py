"""The old LiveKit import path forwards to the extracted plugin when installed."""

from __future__ import annotations

import builtins
import importlib
import runpy
import sys
from pathlib import Path

import pytest

_EXPORTS = {
    "": (
        "ContextCache",
        "FastTalker",
        "LiveKitAdapter",
        "LiveKitAdapterConfig",
        "SlowThinker",
        "detect_interesting_fact",
        "redact_pii",
    ),
    "adapter": ("LiveKitAdapter",),
    "cache": ("ContextCache", "RetrievalStatus", "RETRIEVAL_UNAVAILABLE"),
    "config": ("LiveKitAdapterConfig",),
    "fast_talker": ("FastTalker",),
    "heuristics": ("detect_interesting_fact",),
    "redaction": ("redact_pii",),
    "slow_thinker": ("SlowThinker",),
}


def _shim_path(module: str) -> Path:
    import musubi

    package = Path(musubi.__file__).parent / "adapters" / "livekit"
    return package / (f"{module}.py" if module else "__init__.py")


@pytest.mark.parametrize("module,exports", _EXPORTS.items())
def test_legacy_exports_are_plugin_objects(module: str, exports: tuple[str, ...]) -> None:
    pytest.importorskip("musubi_livekit")
    legacy_name = "musubi.adapters.livekit" + (f".{module}" if module else "")
    plugin_name = "musubi_livekit" + (f".{module}" if module else "")
    legacy = importlib.import_module(legacy_name)
    plugin = importlib.import_module(plugin_name)
    for name in exports:
        assert getattr(legacy, name) is getattr(plugin, name)


def test_missing_plugin_has_explicit_install_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(sys.modules):
        if name.startswith("musubi_livekit."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "musubi_livekit", None)
    with pytest.raises(ModuleNotFoundError, match="Install musubi-livekit"):
        runpy.run_path(str(_shim_path("")))


def test_foreign_missing_dependency_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def fail_plugin_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "musubi_livekit.cache":
            raise ModuleNotFoundError("foreign dependency missing", name="foreign_dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_plugin_import)
    with pytest.raises(ModuleNotFoundError, match="foreign dependency missing") as exc:
        runpy.run_path(str(_shim_path("cache")))
    assert exc.value.name == "foreign_dependency"
