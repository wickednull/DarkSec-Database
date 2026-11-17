"""Every third-party module imported by the backend must be installable.

This guards against requirements.txt drifting from the code (for example the
satellite views importing sgp4 while requirements.txt omitted it).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Modules provided by an explicitly listed dependency.
PROVIDED_BY = {
    "starlette": "fastapi",
}

DIST_NAMES = {
    "PIL": "pillow",
}


def _requirement_names() -> set[str]:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~\[\s]", line, maxsplit=1)[0]
        names.add(name.lower().replace("_", "-"))
    return names


def _imported_modules() -> set[str]:
    modules = set()
    for path in (ROOT / "darksec").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return modules


def test_backend_imports_are_declared_in_requirements():
    required = _requirement_names()
    missing = []
    for module in sorted(_imported_modules()):
        if module == "darksec" or module in sys.stdlib_module_names or module.startswith("_"):
            continue
        distribution = DIST_NAMES.get(module, module).lower().replace("_", "-")
        dependency = PROVIDED_BY.get(module, distribution)
        if distribution not in required and dependency not in required:
            missing.append(f"{module} (expected {distribution})")
    assert not missing, "Add to requirements.txt: " + ", ".join(missing)


def test_satellite_dependency_is_present():
    assert "sgp4" in _requirement_names()
