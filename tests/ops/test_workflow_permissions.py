"""Every GitHub Actions workflow must declare an explicit permissions scope.

CodeQL opened five "Workflow does not contain permissions" alerts at once
(#4-#8, 2026-09-19). Each was real and each was fixed by hand. This cell exists
so the class cannot come back with the next workflow somebody adds.

WHY IT GLOBS INSTEAD OF LISTING FILENAMES. The alerts arrived as a roster, and a
roster cannot see a file nobody flagged. Two of us checked that roster by hand
within the same hour and both produced a wrong answer from a narrowed view:

  - a `tail -6` on a "6 files changed" diff stat printed five, and the missing
    sixth (`ci.yml`) was reported as an unfixed gap that did not exist (Shiori)
  - a `^permissions:` grep anchored to column zero could not see a JOB-scoped
    block, and reported `publish-core-image.yml` -- the one workflow that pushes
    to GHCR and does keyless signing -- as having none (Aoi)

Both were views of a file mistaken for properties of the file. Discovery from the
directory removes the roster, and accepting permissions at EITHER scope removes
the anchor.

A workflow-level block covers every job in the file. A job-level block is
narrower and therefore better where jobs differ -- `publish-core-image.yml`
needs `packages: write` for exactly one job and nothing elsewhere -- so both
shapes are accepted, and a file mixing them must cover every job it declares.

Shiori, 2026-09-19.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"

#: Discovered, never listed. A workflow added tomorrow is covered by this cell
#: the moment it lands, which is the whole point.
FILES = sorted(p for p in WORKFLOWS.glob("*.y*ml"))


def _load(path: pathlib.Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict):
        msg = f"{path.name} did not parse as a mapping"
        raise AssertionError(msg)
    return loaded


def test_the_discovery_found_workflows_at_all() -> None:
    """A control on the instrument. If the glob breaks or the directory moves,
    every cell below would pass over an empty list and report nothing wrong --
    which is exactly how an absence check goes green having examined nothing."""
    assert FILES, f"no workflows discovered under {WORKFLOWS}; this file is inert"
    names = {p.name for p in FILES}
    assert "publish-core-image.yml" in names, (
        "the publishing workflow is not in the discovered set -- discovery is "
        "looking in the wrong place"
    )


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_workflow_declares_permissions_at_some_scope(path: pathlib.Path) -> None:
    """Workflow-level, or on every job. Either satisfies CodeQL; neither is a
    default, and GitHub's default token is broadly writable."""
    doc = _load(path)
    if doc.get("permissions") is not None:
        return

    jobs = doc.get("jobs") or {}
    assert isinstance(jobs, dict)
    assert jobs, f"{path.name} declares no permissions and no jobs"
    missing = [
        name
        for name, job in jobs.items()
        if not isinstance(job, dict) or job.get("permissions") is None
    ]
    assert not missing, (
        f"{path.name} has no workflow-level `permissions:` and these jobs declare "
        f"none of their own: {missing}. GitHub's default token is broadly writable, "
        f"so an omitted scope is not 'unset' -- it is 'everything the token can do'."
    )


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_workflow_grants_blanket_write(path: pathlib.Path) -> None:
    """`write-all` satisfies the CodeQL rule while granting more than any of
    these workflows needs. Passing the check is not the same as being least
    privilege, and a cell that only pins presence would accept it."""
    doc = _load(path)
    jobs = doc.get("jobs") or {}
    assert isinstance(jobs, dict)
    scopes: list[Any] = [doc.get("permissions")]
    scopes += [job.get("permissions") for job in jobs.values() if isinstance(job, dict)]
    for scope in scopes:
        assert scope != "write-all", f"{path.name} grants write-all"
