from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
_DISTRIBUTION_FOR_IMPORT = {
    "open_brain": "app",
    "open_brain_connectors": "connectors",
    "open_brain_engine": "engine",
    "tools": "workspace",
}
_SOURCE_ROOTS = {
    "app": ROOT / "packages/app/src",
    "connectors": ROOT / "packages/connectors/src",
    "engine": ROOT / "packages/engine/src",
}
_ALLOWED_IMPORTS = {
    "app": {"engine"},
    "connectors": {"engine"},
    "engine": set(),
}
_PROJECT_FILES = {
    "app": ROOT / "packages/app/pyproject.toml",
    "connectors": ROOT / "packages/connectors/pyproject.toml",
    "engine": ROOT / "packages/engine/pyproject.toml",
}


def _runtime_distribution_imports(source_root: Path, owner: str) -> set[str]:
    imports: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = (node.module,)
            for name in names:
                dependency = _DISTRIBUTION_FOR_IMPORT.get(name.partition(".")[0])
                if dependency is not None and dependency != owner:
                    imports.add(dependency)
    return imports


def _declared_distribution_dependencies(project_file: Path) -> set[str]:
    metadata = tomllib.loads(project_file.read_text(encoding="utf-8"))
    dependencies = metadata["project"].get("dependencies", [])
    names = {
        dependency.split("[", 1)[0].split("=", 1)[0].split("<", 1)[0].split(">", 1)[0]
        for dependency in dependencies
    }
    mapping = {
        "open-brain": "app",
        "open-brain-connectors": "connectors",
        "open-brain-engine": "engine",
    }
    return {mapping[name] for name in names if name in mapping}


@pytest.mark.parametrize("owner", tuple(_SOURCE_ROOTS))
def test_runtime_distributions_follow_the_small_declared_import_graph(owner: str) -> None:
    imported = _runtime_distribution_imports(_SOURCE_ROOTS[owner], owner)

    assert imported <= _ALLOWED_IMPORTS[owner]
    assert _declared_distribution_dependencies(_PROJECT_FILES[owner]) == _ALLOWED_IMPORTS[owner]


def test_engine_never_imports_application_or_workspace_code() -> None:
    assert _runtime_distribution_imports(_SOURCE_ROOTS["engine"], "engine") == set()
