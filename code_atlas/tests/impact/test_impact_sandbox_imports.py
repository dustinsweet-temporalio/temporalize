"""Workflow-side modules import only the standard library, temporalio and each other."""
import ast
import sys
from pathlib import Path

import pytest

IMPACT = Path(__file__).resolve().parents[2] / "impact"
ALLOWED = {"temporalio", "impact"}


@pytest.mark.parametrize("name", ["workflow.py", "constants.py", "models.py"])
def test_workflow_side_imports(name):
    tree = ast.parse((IMPACT / name).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    outside = {r for r in roots if r not in ALLOWED and r not in sys.stdlib_module_names}
    assert not outside, f"{name} imports {outside}"
