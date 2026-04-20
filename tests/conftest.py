"""Shared pytest fixtures.

Fixtures are added as slices land. Reference: vault's ``_slices/test-fixtures.md``.
This file starts empty so ``pytest`` runs clean against the scaffold.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_namespace() -> str:
    """A well-formed ``{tenant}/{presence}/{plane}`` namespace for tests."""
    return "eric/claude-code/episodic"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "gpu: mark test to run only when MUSUBI_GPU_AVAILABLE=1 is set"
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("gpu"):
        import os

        if os.environ.get("MUSUBI_GPU_AVAILABLE") != "1":
            pytest.skip("Test requires MUSUBI_GPU_AVAILABLE=1")


@pytest.fixture
def require_gpu() -> None:
    import os

    if os.environ.get("MUSUBI_GPU_AVAILABLE") != "1":
        pytest.skip("Test requires MUSUBI_GPU_AVAILABLE=1")
