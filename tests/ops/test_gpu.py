from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from tests.conftest import require_gpu


def test_gpu_fixture_skips_when_env_unset() -> None:
    with patch.dict(os.environ, clear=True), pytest.raises(pytest.skip.Exception):
        require_gpu.__wrapped__()  # type: ignore[attr-defined]


def test_gpu_fixture_passes_through_when_env_set() -> None:
    with patch.dict(os.environ, {"MUSUBI_GPU_AVAILABLE": "1"}):
        require_gpu.__wrapped__()  # type: ignore[attr-defined]  # Should not raise
