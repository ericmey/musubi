import ast
from pathlib import Path

import pytest


def test_vault004_structural_callers_use_seam() -> None:
    # watcher.py and reconciler.py must explicitly call curated_knowledge_from_frontmatter
    # and MUST NOT import or call CuratedKnowledge directly anymore.
    root = Path(__file__).resolve().parents[2]

    for filename in ["src/musubi/vault/watcher.py", "src/musubi/vault/reconciler.py"]:
        path = root / filename
        with open(path) as f:
            tree = ast.parse(f.read())

        call_count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "musubi.types.curated" and any(
                    n.name == "CuratedKnowledge" for n in node.names
                ):
                    pytest.fail(
                        f"{filename} directly imports CuratedKnowledge instead of using the seam."
                    )
            elif isinstance(node, ast.Call):
                func_id = getattr(node.func, "id", "")
                if func_id == "CuratedKnowledge":
                    pytest.fail(
                        f"{filename} directly instantiates CuratedKnowledge instead of using the seam."
                    )
                if func_id == "curated_knowledge_from_frontmatter":
                    call_count += 1

        assert call_count == 1, (
            f"{filename} must call curated_knowledge_from_frontmatter exactly once (found {call_count})"
        )
