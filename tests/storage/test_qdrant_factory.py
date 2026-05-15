"""Unit tests for :mod:`musubi.storage.qdrant_factory`.

The factory exists to silence one specific qdrant-client warning while
letting every other warning through. These tests pin both halves of
that contract.
"""

from __future__ import annotations

import warnings
from typing import Any

from musubi.storage.qdrant_factory import build_qdrant_client


def _install_stub_client(monkeypatch: Any, *, warning_text: str | None) -> None:
    """Replace the QdrantClient constructor with a stub that optionally
    emits a controlled UserWarning before returning. The stub is
    installed at the factory module's import binding so the patch
    travels through ``build_qdrant_client``."""

    class _StubClient:
        def __init__(self, **_: Any) -> None:
            if warning_text is not None:
                warnings.warn(warning_text, UserWarning, stacklevel=2)

    monkeypatch.setattr("musubi.storage.qdrant_factory.QdrantClient", _StubClient)


def test_factory_suppresses_only_the_documented_insecure_warning(
    monkeypatch: Any,
) -> None:
    _install_stub_client(
        monkeypatch,
        warning_text="Api key is used with an insecure connection.",
    )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        build_qdrant_client(host="q", port=6333, api_key="k", https=False)
    insecure = [
        w for w in captured if "Api key is used with an insecure connection" in str(w.message)
    ]
    assert insecure == [], "the documented insecure-connection warning must be suppressed"


def test_factory_lets_unrelated_qdrant_warnings_through(monkeypatch: Any) -> None:
    """A future qdrant_client warning we haven't seen yet must still
    surface — the whole point of scoping the filter is to avoid
    masking real issues."""
    _install_stub_client(
        monkeypatch,
        warning_text="Some other unrelated qdrant_client warning",
    )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        build_qdrant_client(host="q", port=6333, api_key="k", https=False)
    matched = [w for w in captured if "unrelated" in str(w.message)]
    assert matched, "unrelated qdrant_client warnings must reach the caller"


def test_factory_filter_pattern_is_anchored(monkeypatch: Any) -> None:
    """Pattern uses `\\Z` so an extended message starting with the
    documented phrase still surfaces — it could be a new upstream
    warning we'd want to see."""
    _install_stub_client(
        monkeypatch,
        warning_text=(
            "Api key is used with an insecure connection. "
            "Also your connection pool exceeded its limit."
        ),
    )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        build_qdrant_client(host="q", port=6333, api_key="k", https=False)
    leaked = [w for w in captured if "exceeded" in str(w.message)]
    assert leaked, "extended messages must not be swallowed by the anchored filter"
