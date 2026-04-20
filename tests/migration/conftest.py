"""Fixtures for testing the migration script."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from musubi.sdk import MusubiClient


@pytest.fixture
def mock_qdrant() -> Mock:
    mock = Mock()
    # Stub scroll response
    mock.scroll.return_value = ([], None)
    return mock


@pytest.fixture
def mock_musubi() -> Mock:
    return Mock(spec=MusubiClient)


@pytest.fixture
def state_file() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "state.json"
