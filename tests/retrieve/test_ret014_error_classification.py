"""RET-014 exhaustive retrieve error-code classification contract."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from musubi.retrieve import orchestration

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRIEVE_ROOT = REPO_ROOT / "src" / "musubi" / "retrieve"
SRC_ROOT = REPO_ROOT / "src" / "musubi"

EXPECTED_CLASSIFICATIONS = {
    "all_planes_failed": "internal",
    "all_planes_timeout": "timeout",
    "dense_embedding_failed": "internal",
    "embeddings_unavailable": "internal",
    "empty_query": "bad_query",
    "fanout_mismatch": "internal",
    "index_unavailable": "internal",
    "invalid_collections": "bad_query",
    "invalid_limit": "bad_query",
    "no_query_vectors": "internal",
    "no_retrieval_channels": "bad_query",
    "plane_timeout": "timeout",
    "qdrant_query_failed": "internal",
    "qdrant_timeout": "timeout",
    "sparse_embedding_failed": "internal",
}
EXPECTED_FORWARDING_SITES = {
    ("deep.py", "DeepRetrievalError", "errors[0].code"),
    ("fast.py", "FastRetrievalError", "error.code"),
}


def _callee_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return "<dynamic>"


def _string_literals(expression: ast.expr) -> set[str]:
    return {
        node.value
        for node in ast.walk(expression)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _collect_error_code_surface(
    root: Path = RETRIEVE_ROOT,
) -> tuple[set[str], set[str], set[tuple[str, str, str]]]:
    codes: set[str] = set()
    rejected_callees: set[str] = set()
    forwarding_sites: set[tuple[str, str, str]] = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee_name(node)
            for keyword in node.keywords:
                if keyword.arg != "code":
                    continue
                literals = _string_literals(keyword.value)
                if not callee.endswith("Error"):
                    rejected_callees.add(callee)
                elif literals:
                    codes.update(literals)
                else:
                    forwarding_sites.add((path.name, callee, ast.unparse(keyword.value)))
    return codes, rejected_callees, forwarding_sites


def _classification_registry() -> dict[str, Any]:
    registry = getattr(orchestration, "ERROR_KIND_BY_CODE", None)
    assert isinstance(registry, dict), "RET-014 explicit registry is missing"
    return registry


def test_every_literal_retrieve_error_code_has_an_explicit_classification() -> None:
    codes, _, _ = _collect_error_code_surface()
    assert codes == set(_classification_registry())


def test_existing_error_code_classifications_preserve_their_semantics() -> None:
    assert _classification_registry() == EXPECTED_CLASSIFICATIONS
    assert {
        code: orchestration._kind_from_code(code) for code in EXPECTED_CLASSIFICATIONS
    } == EXPECTED_CLASSIFICATIONS


def test_unknown_retrieve_error_code_is_rejected_instead_of_implicitly_internal() -> None:
    with pytest.raises(ValueError, match="unclassified retrieve error code"):
        orchestration._kind_from_code("synthetic_unclassified_code")


def test_intentional_internal_error_codes_are_named_and_complete() -> None:
    internal_codes = getattr(orchestration, "INTERNAL_ERROR_CODES", None)
    assert isinstance(internal_codes, frozenset)
    assert internal_codes == frozenset(
        code for code, kind in EXPECTED_CLASSIFICATIONS.items() if kind == "internal"
    )


def test_error_code_collector_rejects_new_unrecognised_code_callee() -> None:
    _, rejected_callees, _ = _collect_error_code_surface()
    assert rejected_callees == {"RetrievalWarning"}


def test_error_code_collector_accounts_for_dynamic_forwarding_sites() -> None:
    _, _, forwarding_sites = _collect_error_code_surface()
    assert forwarding_sites == EXPECTED_FORWARDING_SITES


def test_error_code_collector_walks_both_conditional_expression_arms() -> None:
    expression = ast.parse(
        "Failure(code='left_code' if condition else 'right_code')",
        mode="eval",
    ).body
    assert isinstance(expression, ast.Call)
    code_keyword = next(keyword for keyword in expression.keywords if keyword.arg == "code")
    assert _string_literals(code_keyword.value) == {"left_code", "right_code"}


def test_retrieval_error_construction_remains_closed_over_retrieve_package() -> None:
    constructor_paths: set[Path] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(
            isinstance(node, ast.Call) and _callee_name(node) == "RetrievalError"
            for node in ast.walk(tree)
        ):
            constructor_paths.add(path.relative_to(SRC_ROOT))
    assert constructor_paths
    assert all(path.parts[0] == "retrieve" for path in constructor_paths)


def test_sparse_embedding_failed_remains_distinct_in_error_and_warning_taxonomies() -> None:
    codes, rejected_callees, _ = _collect_error_code_surface()
    assert "sparse_embedding_failed" in codes
    assert rejected_callees == {"RetrievalWarning"}
    warnings_source = (RETRIEVE_ROOT / "warnings.py").read_text()
    assert 'RetrievalWarning(code="sparse_embedding_failed"' in warnings_source
