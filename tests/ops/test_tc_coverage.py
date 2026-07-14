import ast
from typing import cast

from docs.Musubi._tools.tc_coverage import _extract_skip_reason, _module_string_constants


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
# ---------------------------------------------------------------------------


def test_extract_skip_reason_variable_resolves_to_string() -> None:
    """GREEN: ``reason=_VAR`` resolves to the module-level string constant."""
    code = """
_R1_REASON = "variable reason text"

@pytest.mark.xfail(strict=True, reason=_R1_REASON)
def test_with_var_reason(): pass
"""
    tree = ast.parse(code)
    names = _module_string_constants(tree)
    func = cast(ast.FunctionDef, tree.body[1])
    assert (
        _extract_skip_reason(func.decorator_list[0], module_names=names) == "variable reason text"
    )


def test_extract_skip_reason_variable_annotated_assignment() -> None:
    """GREEN: ``reason=_VAR`` resolves when the name is declared via AnnAssign."""
    code = """
_R2_REASON: str = "annotated reason"

@pytest.mark.xfail(strict=True, reason=_R2_REASON)
def test_with_ann_var_reason(): pass
"""
    tree = ast.parse(code)
    names = _module_string_constants(tree)
    func = cast(ast.FunctionDef, tree.body[1])
    assert _extract_skip_reason(func.decorator_list[0], module_names=names) == "annotated reason"


def test_extract_skip_reason_variable_unresolved_returns_none() -> None:
    """RED: an unresolved module-level name returns ``None`` (no fabrication)."""
    code = """
@pytest.mark.xfail(strict=True, reason=_DOES_NOT_EXIST)
def test_with_missing_var_reason(): pass
"""
    tree = ast.parse(code)
    names = _module_string_constants(tree)  # empty
    func = cast(ast.FunctionDef, tree.body[0])
    assert _extract_skip_reason(func.decorator_list[0], module_names=names) is None


def test_extract_skip_reason_string_literal_still_works() -> None:
    """RED: existing string-literal reason path must not regress (Issue #457)."""
    code = """
@pytest.mark.xfail(reason="literal reason")
def test_literal_reason(): pass
"""
    tree = ast.parse(code)
    func = cast(ast.FunctionDef, tree.body[0])
    # Pass module_names=None to confirm the original signature still works.
    assert _extract_skip_reason(func.decorator_list[0], module_names=None) == "literal reason"


def test_extract_skip_reason_variable_non_string_returns_none() -> None:
    """RED: a module-level name bound to a non-string value returns ``None``."""
    code = """
_R3_VALUE = 42

@pytest.mark.xfail(strict=True, reason=_R3_VALUE)
def test_with_int_var_reason(): pass
"""
    tree = ast.parse(code)
    names = _module_string_constants(tree)
    func = cast(ast.FunctionDef, tree.body[1])
    assert _extract_skip_reason(func.decorator_list[0], module_names=names) is None


def test_module_string_constants_skips_later_reassignments() -> None:
    """RED: only the first string-constant assignment per name is kept."""
    code = """
_X = "first"
_X = 99
def test_does_not_matter(): pass
"""
    tree = ast.parse(code)
    names = _module_string_constants(tree)
    # First wins; second is ignored.
    assert names == {"_X": "first"}


def test_module_string_constants_ignores_non_constant_value() -> None:
    """RED: f-strings, calls, and concatenations are NOT captured."""
    code = """
_Y = f"dynamic {_X}"
_Z = "x" + "y"
def test_does_not_matter(): pass
"""
    tree = ast.parse(code)
    names = _module_string_constants(tree)
    assert names == {}
