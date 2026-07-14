import os
import sys


def test_uv_virtualenv_active() -> None:
    """
    Contract: The CI runner MUST execute within a properly isolated virtual environment.
    The prior broken state (curl installer + uv pip install -e .) failed to create
    or activate a venv, dropping dependencies randomly into the system or failing out.
    """
    # sys.prefix != sys.base_prefix is the canonical check for a virtual environment.
    assert sys.prefix != sys.base_prefix, "CI execution MUST be isolated in a virtual environment"

    # Additionally, ensure uv injected the VIRTUAL_ENV env var
    assert "VIRTUAL_ENV" in os.environ, (
        "VIRTUAL_ENV environment variable MUST be present (proves uv sync activated the context)"
    )
