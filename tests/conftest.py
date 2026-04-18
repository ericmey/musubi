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
