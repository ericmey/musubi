"""The inline-collection-name set in `retraction_saga.py`, enumerated by MEANING.

Copilot round 30 on musubi#732 named ONE site: a hardcoded ``"musubi_episodic"`` in
the recovery path. Fixing the named site and sweeping for `collection_name=` left a
second branch-introduced literal in place, because that one passes the collection
POSITIONALLY. A grep keyed on a spelling cannot enumerate a set defined by meaning --
the same shape as `_count_content_points` being written as *not-anchor*.

This walks the AST instead, so argument position, keyword vs positional, formatting and
line numbers are all irrelevant. A string constant whose value is a real collection name
is counted wherever it appears.

**The expected mapping is an EQUALITY, not a floor.** A new inline fails it. Fixing one
of the two pre-existing sites ALSO fails it -- deliberately: that is a work order to
update this table in the same commit, not a regression. The alternative, a ``<=``
assertion, is a bound that silently tolerates exactly the drift this exists to catch.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import musubi.api.retraction_saga as saga_module
from musubi.store import COLLECTION_NAMES

#: Literals still inline in this file, by enclosing function, all PRE-EXISTING ON MAIN
#: (a72ba70b0, 2026-08-03). #732 does not own them; they route to the names.py issue
#: alongside the `lifecycle/reflection.py` sites. The two branch-introduced sites --
#: `_release_update_lease` (7d5c932b, keyword) and the committed-repair branch of
#: `execute_retraction` (4befc383e, positional) -- now call `collection_for_plane`.
EXPECTED_INLINE_SITES = Counter({"_read_original": 1, "execute_retraction": 1})


def _inline_collection_literals(path: Path) -> Counter[str]:
    """Every string constant equal to a collection name, keyed by enclosing function."""
    names = set(COLLECTION_NAMES)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    counts: Counter[str] = Counter()
    scope: list[str] = []

    class Walker(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str) and node.value in names:
                counts[scope[-1] if scope else "<module>"] += 1

    Walker().visit(tree)
    return counts


def test_inline_collection_name_sites_are_exactly_the_pre_existing_two() -> None:
    path = Path(saga_module.__file__)
    actual = _inline_collection_literals(path)
    assert actual == EXPECTED_INLINE_SITES, (
        f"inline collection-name literals in {path.name} changed.\n"
        f"  expected {dict(EXPECTED_INLINE_SITES)}\n"
        f"  actual   {dict(actual)}\n"
        "A NEW entry means a collection name was stringified inline -- use "
        "`collection_for_plane(...)`, so a rename cannot silently survive a dual-write "
        "migration. A MISSING entry means one of the two pre-existing sites was fixed: "
        "good, update this table in the same commit."
    )


def test_the_walker_actually_catches_a_positional_literal() -> None:
    """The guard's own red-proof: prove it sees an argument passed POSITIONALLY.

    Without this, a walker that only inspected `keywords` would pass the test above on
    today's source and reproduce the exact blind spot that made this file necessary.
    """
    source = (
        "def f(client):\n"
        "    return helper(client, 'musubi_episodic', namespace='ns')\n"
        "\n"
        "def g(client):\n"
        "    return helper(client, collection_name='musubi_curated')\n"
    )
    tmp = Path(__file__).with_name("_positional_probe.py")
    tmp.write_text(source, encoding="utf-8")
    try:
        counts = _inline_collection_literals(tmp)
    finally:
        tmp.unlink()
    assert counts == Counter({"f": 1, "g": 1}), (
        f"the walker missed a spelling it must catch: {dict(counts)}. "
        "`f` is positional and `g` is a keyword; both are inline literals."
    )
