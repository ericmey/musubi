from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent.parent / ".operator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location(
    "handoff_audit",
    str(SCRIPTS / "handoff-audit.py"),
)
assert spec is not None
assert spec.loader is not None
handoff_audit = importlib.util.module_from_spec(spec)
sys.modules["handoff_audit"] = handoff_audit
spec.loader.exec_module(handoff_audit)


def _pr_info() -> Any:
    return handoff_audit.PRInfo(
        number=1,
        head_ref="tools/example",
        head_sha="abc123",
        base_ref="v2",
        title="tools: example",
        body="Closes #1.",
        merge_state="CLEAN",
        is_draft=False,
        state="OPEN",
        checks_pass=True,
        checks_detail="check: pass",
    )


def _slice(owns_paths: list[str]) -> Any:
    return handoff_audit.Slice(
        id="slice-example",
        path=Path("docs/Musubi/_slices/slice-example.md"),
        title="Example",
        status="in-review",
        owner="codex-gpt5",
        phase="8 Ops",
        depends_on=[],
        blocks=[],
        owns_paths=owns_paths,
        forbidden_paths=[],
        specs=[],
    )


def test_owns_paths_exist_accepts_file_paths_with_any_extension_or_no_extension() -> None:
    """Regression: .md, .toml, Makefile, and Dockerfile are files, not dirs."""
    owned_files = [
        "deploy/runbooks/first-deploy.md",
        "pyproject.toml",
        "Makefile",
        "Dockerfile",
    ]

    def object_type(_ref: str, path: str) -> str | None:
        return "blob" if path in owned_files else None

    with patch("handoff_audit.git_object_type", side_effect=object_type):
        result = handoff_audit.check_owns_paths_exist(
            _slice(owned_files),
            set(owned_files),
            "origin/pr/1-head",
        )

    assert result.ok is True


def test_owns_paths_exist_reports_missing_file_paths_as_missing_files() -> None:
    with patch("handoff_audit.git_object_type", return_value=None):
        result = handoff_audit.check_owns_paths_exist(
            _slice(["deploy/runbooks/first-deploy.md"]),
            set(),
            "origin/pr/1-head",
        )

    assert result.ok is False
    assert "deploy/runbooks/first-deploy.md" in result.detail
    assert "directory; no files under it" not in result.detail


def test_path_is_under_matches_specific_files_without_extension_guessing() -> None:
    owns_paths = ["Makefile", "pyproject.toml", "deploy/runbooks/first-deploy.md"]

    assert handoff_audit._path_is_under("Makefile", owns_paths) is True
    assert handoff_audit._path_is_under("pyproject.toml", owns_paths) is True
    assert handoff_audit._path_is_under("deploy/runbooks/first-deploy.md", owns_paths) is True
    assert handoff_audit._path_is_under("deploy/runbooks/other.md", owns_paths) is False
