import os
import sys
from pathlib import Path


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


def _assert_workflow_contract(content: str) -> None:
    assert "astral-sh/setup-uv@v8.1.0" in content, "Must use canonical setup-uv action"
    assert "uv python install 3.12" in content, "Must use canonical uv python install"
    assert "uv sync --extra dev" in content, "Must use canonical uv sync"
    assert "curl " not in content, "Must not use curl installer"
    assert "uv pip install -e ." not in content, "Must not use raw pip install"
    assert "--system" not in content, "Must not use system python flag"


def test_evals_workflow_file_contract() -> None:
    """
    Contract: .github/workflows/evals.yml MUST use the canonical setup-uv pattern.
    """
    repo_root = Path(__file__).parent.parent.parent
    workflow_path = repo_root / ".github" / "workflows" / "evals.yml"
    assert workflow_path.exists(), "evals.yml must exist"

    content = workflow_path.read_text(encoding="utf-8")
    _assert_workflow_contract(content)


def test_evals_workflow_discriminator_prior_broken_state() -> None:
    """
    Contract: Prove that the broken prior workflow text fails the assertion.
    """
    broken_content = """
      - name: Install uv and deps
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          uv pip install -e .
    """
    import pytest

    with pytest.raises(AssertionError, match="Must use canonical setup-uv action"):
        _assert_workflow_contract(broken_content)

    # We could do more specific assertions if needed, but the first failure proves the guard.
