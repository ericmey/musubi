import ast
from typing import cast

from docs.architecture._tools.tc_coverage import _extract_skip_reason


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
