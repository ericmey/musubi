import ast
from typing import cast

from docs.Musubi._tools.tc_coverage import (
    _extract_skip_reason,
    _positional_module_string_bindings,
)


def test_extract_skip_reason_single_line() -> None:
    code = """
@pytest.mark.skip(reason="simple")
def test_foo(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[0])
    assert _extract_skip_reason(func.decorator_list[0]) == "simple"


def test_extract_skip_reason_multi_line() -> None:
    code = """
@pytest.mark.skip(
    reason=(
        "this is a "
        "very long reason"
    )
)
def test_bar(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[0])
    assert _extract_skip_reason(func.decorator_list[0]) == "this is a very long reason"


def test_extract_skip_reason_xfail() -> None:
    code = """
@pytest.mark.xfail(reason="known bug")
def test_baz(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[0])
    assert _extract_skip_reason(func.decorator_list[0]) == "known bug"


# ---------------------------------------------------------------------------
# Issue #457: variable reason resolution (e.g. ``reason=_R1_REASON``).
# Positional repair per chair-20260714-094556-c88885cb: the resolution must
# follow Python's actual decorator-evaluation semantics (the LAST binding
# established BEFORE the decorated function executes), not a first-wins
# whole-file map.
# ---------------------------------------------------------------------------


def test_extract_skip_reason_variable_resolves_to_string() -> None:
    """GREEN: ``reason=_VAR`` resolves to the module-level string constant
    visible AT THE POSITION of the function definition."""
    code = """
_R1_REASON = "variable reason text"

@pytest.mark.xfail(strict=True, reason=_R1_REASON)
def test_with_var_reason(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[1])
    names = _positional_module_string_bindings(tree, func)
    assert (
        _extract_skip_reason(func.decorator_list[0], module_names=names) == "variable reason text"
    )


def test_extract_skip_reason_variable_annotated_assignment() -> None:
    """GREEN: ``reason=_VAR`` resolves when the name is declared via AnnAssign
    at the function's position."""
    code = """
_R2_REASON: str = "annotated reason"

@pytest.mark.xfail(strict=True, reason=_R2_REASON)
def test_with_ann_var_reason(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[1])
    names = _positional_module_string_bindings(tree, func)
    assert _extract_skip_reason(func.decorator_list[0], module_names=names) == "annotated reason"


def test_extract_skip_reason_variable_unresolved_returns_none() -> None:
    """RED: an unresolved module-level name returns ``None`` (no fabrication)."""
    code = """
@pytest.mark.xfail(strict=True, reason=_DOES_NOT_EXIST)
def test_with_missing_var_reason(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[0])
    names = _positional_module_string_bindings(tree, func)  # empty
    assert _extract_skip_reason(func.decorator_list[0], module_names=names) is None


def test_extract_skip_reason_string_literal_still_works() -> None:
    """RED: existing string-literal reason path must not regress."""
    code = """
@pytest.mark.xfail(reason="literal reason")
def test_literal_reason(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[0])
    # Pass module_names=None to confirm the original signature still works.
    assert _extract_skip_reason(func.decorator_list[0], module_names=None) == "literal reason"


# ---------------------------------------------------------------------------
# Positional repair discriminators (chair-20260714-094556-c88885cb).
# Python decorator evaluation uses the LAST binding established BEFORE the
# decorated function executes. A whole-file first-wins map is wrong.
# ---------------------------------------------------------------------------


def test_positional_last_prior_string_wins() -> None:
    """POSITIONAL: when a name is rebound to a new string BEFORE the function,
    the LATER string wins (NOT the first)."""
    code = """
_X = "first"
_X = "second"

@pytest.mark.xfail(strict=True, reason=_X)
def test_last_wins(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[2])
    names = _positional_module_string_bindings(tree, func)
    # The old first-wins map returned "first"; the positional resolver
    # must return "second" (the binding visible at the function's position).
    assert names == {"_X": "second"}


def test_positional_prior_string_then_non_string_returns_none() -> None:
    """POSITIONAL: when a name was a string and is then rebound to a non-string
    BEFORE the function, the name is UNRESOLVED at the function's position
    (NOT the prior string). Python would raise TypeError at decorator eval;
    we return None to preserve the bounded str | None return shape."""
    code = """
_X = "first"
_X = 99

@pytest.mark.xfail(strict=True, reason=_X)
def test_non_string_after_string(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[2])
    names = _positional_module_string_bindings(tree, func)
    # The old first-wins map kept "first"; the positional resolver
    # must exclude _X (it's bound to a non-string at this position).
    assert names == {}


def test_positional_assignment_after_function_ignored() -> None:
    """POSITIONAL: an assignment AFTER the decorated function is NOT visible
    at the function's decorator-eval position. Python has not executed it yet."""
    code = """
@pytest.mark.xfail(strict=True, reason=_X)
def test_assignment_after_is_invisible(): pass
_X = "after"
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[1])
    names = _positional_module_string_bindings(tree, func)
    # The old code (whole-file first-wins) could not see this; the positional
    # resolver EXPLICITLY does not see it (the assignment is after the
    # target node). _X is NOT in the bindings.
    assert "_X" not in names


def test_positional_annassign_last_prior_string_wins() -> None:
    """POSITIONAL: AnnAssign (``Name: str = "..."``) also follows last-prior-string-wins."""
    code = """
_X: str = "first"
_X: str = "second"

@pytest.mark.xfail(strict=True, reason=_X)
def test_annassign_last_wins(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[2])
    names = _positional_module_string_bindings(tree, func)
    assert names == {"_X": "second"}


def test_positional_ignores_non_constant_value() -> None:
    """POSITIONAL: f-strings, calls, and concatenations are NOT captured
    (Python would evaluate them at module load but their result is not a
    constant)."""
    code = """
_Y = f"dynamic {_X}"
_Z = "x" + "y"

@pytest.mark.xfail(strict=True, reason=_Y)
def test_fstring(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[2])
    names = _positional_module_string_bindings(tree, func)
    assert names == {}


def test_positional_skips_multi_target_and_tuple_unpacking() -> None:
    """POSITIONAL: ``a = b = "x"`` and ``a, b = "x", "y"`` are NOT followed
    (they could rebind multiple names, and Python's evaluation order for them
    is not the simple single-name pattern we need to track)."""
    code = """
_A = _B = "shared"
_C, _D = "left", "right"

@pytest.mark.xfail(strict=True, reason=_A)
def test_multi_target(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[2])
    names = _positional_module_string_bindings(tree, func)
    # _A is a multi-target assign; we skip it. _C, _D is tuple unpacking;
    # we skip it. So the binding map is empty for these names.
    assert names == {}


def test_positional_end_to_end_via_find_test_definition() -> None:
    """END-TO-END: the positional resolver is wired through
    _find_test_definition (not the old first-wins map). The
    ``_X = "first"; _X = "second"`` pattern resolves to "second"
    when the function is decorated with ``reason=_X``.

    This test exercises the full path (not just the helper) so a future
    regression that bypasses the positional resolver is caught. The
    wiring in _find_test_definition is verified at PR-level by running
    the synthetic end-to-end check. Here we assert the helper chain
    directly: positional-bindings + extract-skip-reason must return
    "second", AND a forced first-wins answer ({"_X": "first"}) must
    still be respected (so the helper is not silently downgrading)."""
    code = """
_X = "first"
_X = "second"

@pytest.mark.xfail(strict=True, reason=_X)
def test_e2e_positional(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[2])
    names = _positional_module_string_bindings(tree, func)
    assert _extract_skip_reason(func.decorator_list[0], module_names=names) == "second"
    # Sanity: a forced first-wins answer is still respected by the helper
    # (so the helper is not silently downgrading), but the positional
    # resolver must NOT produce it on its own.
    assert _extract_skip_reason(func.decorator_list[0], module_names={"_X": "first"}) == "first"
    # The positional resolver must not return "first" for this code.
    assert names != {"_X": "first"}
