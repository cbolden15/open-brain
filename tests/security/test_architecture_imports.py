import ast
import json
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from tools.phase4.move_manifest import validate_manifest

REPOSITORY_ROOT = Path(__file__).parents[2]
CLASSIFICATION_PATH = REPOSITORY_ROOT / "docs" / "v0-package-classification.json"
MONOLITH_SOURCE_ROOT = REPOSITORY_ROOT / "src" / "open_brain"
APP_SOURCE_ROOT = REPOSITORY_ROOT / "packages" / "app" / "src" / "open_brain"
LEGACY_SOURCE_ROOT = REPOSITORY_ROOT / "packages" / "legacy" / "src" / "open_brain_legacy"
OWNERS = {"engine", "app", "connector", "hosted", "legacy", "workspace"}


@dataclass(frozen=True, slots=True)
class ImportReference:
    line: int
    module: str


@dataclass(frozen=True, slots=True)
class DynamicImportSite:
    path: str
    line: int


def _load_classification() -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate classification key: {key}")
            value[key] = item
        return value

    value = json.loads(
        CLASSIFICATION_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
    )
    if not isinstance(value, dict):
        raise TypeError("package classification must be an object")
    return cast(dict[str, object], value)


def _package_directories(source_root: Path) -> set[str]:
    return {
        path.name
        for path in source_root.iterdir()
        if path.is_dir() and path.name != "__pycache__"
    }


def _root_modules(source_root: Path) -> set[str]:
    return {path.stem for path in source_root.glob("*.py")}


def _source_files(source_root: Path) -> list[Path]:
    return sorted(path for path in source_root.rglob("*.py") if "__pycache__" not in path.parts)


def _current_open_brain_source(
    classification: dict[str, object], relative_path: str
) -> tuple[Path, Path]:
    record = _metadata(_classified_files(classification), relative_path)
    current_path = record.get("current_path")
    if not isinstance(current_path, str):
        raise TypeError("classification current path must be a string")
    path = REPOSITORY_ROOT / current_path
    for source_root in (APP_SOURCE_ROOT, LEGACY_SOURCE_ROOT):
        if path.is_relative_to(source_root):
            return path, source_root
    raise ValueError(f"classified path is outside the open_brain namespace: {current_path}")


def _current_open_brain_sources(
    classification: dict[str, object],
    *,
    source_roots: tuple[Path, ...] = (APP_SOURCE_ROOT, LEGACY_SOURCE_ROOT),
) -> list[tuple[str, Path, Path]]:
    sources: list[tuple[str, Path, Path]] = []
    for relative_path, raw_record in _classified_files(classification).items():
        if not isinstance(raw_record, dict):
            raise TypeError("classification record must be an object")
        current_path = raw_record.get("current_path")
        if not isinstance(current_path, str) or not current_path.endswith(".py"):
            continue
        path = REPOSITORY_ROOT / current_path
        if not path.is_file():
            continue
        for source_root in source_roots:
            if path.is_relative_to(source_root):
                sources.append((relative_path, path, source_root))
                break
    return sorted(sources)


def _classified_files(classification: dict[str, object]) -> dict[str, object]:
    files = classification.get("files")
    if not isinstance(files, dict):
        raise TypeError("classification files must be an object")
    return cast(dict[str, object], files)


def _planned_source_classification(classification: dict[str, object]) -> dict[str, object]:
    planned = deepcopy(classification)
    files = _classified_files(planned)
    planned["files"] = {
        path: value
        for path, value in files.items()
        if isinstance(value, dict) and value.get("movement_state") != "moved"
    }
    return planned


def _metadata(files: dict[str, object], relative_path: str) -> dict[str, object]:
    value = files[relative_path]
    if not isinstance(value, dict):
        raise TypeError(f"file classification must be an object: {relative_path}")
    return cast(dict[str, object], value)


def _module_for_path(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("open_brain", *parts))


def _package_for_path(path: Path, source_root: Path) -> str:
    module = _module_for_path(path, source_root)
    if path.name == "__init__.py":
        return module
    return module.rpartition(".")[0]


def _absolute_from_module(node: ast.ImportFrom, path: Path, source_root: Path) -> str:
    if node.level == 0:
        return node.module or ""
    package_parts = _package_for_path(path, source_root).split(".")
    keep = len(package_parts) - node.level + 1
    if keep < 1:
        return ""
    base = package_parts[:keep]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _module_target(module: str, classified_paths: set[str]) -> str | None:
    if module in {"open_brain", "open_brain_legacy"}:
        return "__init__.py" if "__init__.py" in classified_paths else None
    root = next(
        (
            candidate
            for candidate in ("open_brain", "open_brain_legacy")
            if module.startswith(f"{candidate}.")
        ),
        None,
    )
    if root is None:
        return None
    relative = module.removeprefix(f"{root}.").replace(".", "/")
    module_path = f"{relative}.py"
    package_path = f"{relative}/__init__.py"
    if module_path in classified_paths:
        return module_path
    if package_path in classified_paths:
        return package_path
    return None


def _is_dynamic_import_call(node: ast.Call) -> bool:
    function = node.func
    return (
        isinstance(function, ast.Name)
        and function.id in {"__import__", "import_module"}
    ) or (isinstance(function, ast.Attribute) and function.attr == "import_module")


def _import_references(
    path: Path,
    source_root: Path,
    classified_paths: set[str],
) -> list[ImportReference]:
    references: list[ImportReference] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            references.extend(
                ImportReference(node.lineno, alias.name)
                for alias in node.names
                if alias.name in {"open_brain", "open_brain_legacy"}
                or alias.name.startswith(("open_brain.", "open_brain_legacy."))
            )
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_from_module(node, path, source_root)
            if module not in {"open_brain", "open_brain_legacy"} and not module.startswith(
                ("open_brain.", "open_brain_legacy.")
            ):
                continue
            for alias in node.names:
                candidate = module if alias.name == "*" else f"{module}.{alias.name}"
                target_module = (
                    candidate
                    if _module_target(candidate, classified_paths) is not None
                    else module
                )
                references.append(ImportReference(node.lineno, target_module))
        elif isinstance(node, ast.Call) and _is_dynamic_import_call(node) and node.args:
            argument = node.args[0]
            if (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and (
                    argument.value in {"open_brain", "open_brain_legacy"}
                    or argument.value.startswith(("open_brain.", "open_brain_legacy."))
                )
            ):
                references.append(ImportReference(node.lineno, argument.value))
    return references


def _nonliteral_dynamic_import_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _is_dynamic_import_call(node)
            and (
                not node.args
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str)
            )
        }
    )


def _nonliteral_dynamic_import_sites(source_root: Path) -> list[DynamicImportSite]:
    sites = {
        DynamicImportSite(path.relative_to(source_root).as_posix(), line)
        for path in _source_files(source_root)
        for line in _nonliteral_dynamic_import_lines(path)
    }
    return sorted(sites, key=lambda site: (site.path, site.line))


def _selector_matches(metadata: dict[str, object], selector: object) -> bool:
    if not isinstance(selector, dict):
        raise TypeError("rule selector must be an object")
    owners = selector.get("owners")
    apis = selector.get("apis")
    roles_any = selector.get("roles_any")
    roles_none = selector.get("roles_none")
    roles = metadata.get("roles")
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise TypeError("file roles must be a string list")
    if owners is not None and metadata.get("owner") not in owners:
        return False
    if apis is not None and metadata.get("api") not in apis:
        return False
    if roles_any is not None and not set(roles).intersection(roles_any):
        return False
    return roles_none is None or not set(roles).intersection(roles_none)


def _relation_matches(
    source: dict[str, object],
    target: dict[str, object],
    relation: object,
) -> bool:
    if relation is None:
        return True
    if relation != "different-adapter-group":
        raise ValueError(f"unsupported architecture relation: {relation}")
    source_group = source.get("adapter_group")
    target_group = target.get("adapter_group")
    return (
        isinstance(source_group, str)
        and isinstance(target_group, str)
        and source_group != target_group
    )


def _architecture_violations(
    source_root: Path,
    *,
    classification: dict[str, object] | None = None,
) -> list[str]:
    active = _load_classification() if classification is None else classification
    files = _classified_files(active)
    classified_paths = set(files)
    rules = active.get("rules")
    if not isinstance(rules, list):
        raise TypeError("architecture rules must be a list")
    violations: set[str] = set()

    for path in _source_files(source_root):
        source_path = path.relative_to(source_root).as_posix()
        source = _metadata(files, source_path)
        references = _import_references(path, source_root, classified_paths)
        for reference in references:
            target_path = _module_target(reference.module, classified_paths)
            if target_path is None:
                continue
            target = _metadata(files, target_path)
            for rule in rules:
                if not isinstance(rule, dict):
                    raise TypeError("architecture rule must be an object")
                rule_id = rule.get("id")
                clauses = rule.get("violations")
                if not isinstance(rule_id, str) or not isinstance(clauses, list):
                    raise TypeError("architecture rule is malformed")
                if not _selector_matches(source, rule.get("source")):
                    continue
                for clause in clauses:
                    if not isinstance(clause, dict):
                        raise TypeError("architecture violation clause must be an object")
                    if _selector_matches(target, clause.get("target")) and _relation_matches(
                        source,
                        target,
                        clause.get("relation"),
                    ):
                        violations.add(
                            f"{rule_id}: {source_path}:{reference.line} -> {target_path}"
                        )
                        break
    return sorted(violations)


def _ownership_errors(
    source_root: Path,
    classification: dict[str, object],
) -> list[str]:
    files = _classified_files(classification)
    discovered = {
        path.relative_to(source_root).as_posix() for path in _source_files(source_root)
    }
    classified = set(files)
    errors = [f"unclassified runtime file: {path}" for path in discovered - classified]
    errors.extend(f"stale file classification: {path}" for path in classified - discovered)
    for relative_path in discovered & classified:
        metadata = _metadata(files, relative_path)
        owner = metadata.get("owner")
        api = metadata.get("api")
        roles = metadata.get("roles")
        if owner not in OWNERS:
            errors.append(f"invalid owner for {relative_path}: {owner}")
        if api not in {"public", "internal"}:
            errors.append(f"invalid api classification for {relative_path}: {api}")
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            errors.append(f"invalid roles for {relative_path}")
        elif roles != sorted(set(roles)):
            errors.append(f"roles must be sorted and unique for {relative_path}")
        if isinstance(roles, list) and "adapter" in roles:
            if not isinstance(metadata.get("adapter_group"), str):
                errors.append(f"adapter group required for {relative_path}")
        elif "adapter_group" in metadata:
            errors.append(f"adapter group without adapter role for {relative_path}")
    return sorted(errors)


def _unresolved_internal_imports(
    source_root: Path,
    classification: dict[str, object],
) -> list[str]:
    files = _classified_files(classification)
    classified_paths = set(files)
    unresolved: set[str] = set()
    for path in _source_files(source_root):
        source_path = path.relative_to(source_root).as_posix()
        for reference in _import_references(path, source_root, classified_paths):
            if _module_target(reference.module, classified_paths) is None:
                unresolved.add(f"{source_path}:{reference.line} -> {reference.module}")
    return sorted(unresolved)


def _dynamic_import_review_errors(
    source_root: Path,
    classification: dict[str, object],
) -> list[str]:
    sites = {(site.path, site.line) for site in _nonliteral_dynamic_import_sites(source_root)}
    return _dynamic_import_review_errors_for_sites(sites, classification)


def _dynamic_import_review_errors_for_sites(
    sites: set[tuple[str, int]],
    classification: dict[str, object],
) -> list[str]:
    files = _classified_files(classification)
    reviews = classification.get("dynamic_import_reviews")
    if not isinstance(reviews, list):
        raise TypeError("dynamic import reviews must be a list")
    reviewed: set[tuple[str, int]] = set()
    errors: list[str] = []
    for review in reviews:
        if not isinstance(review, dict):
            raise TypeError("dynamic import review must be an object")
        path = review.get("path")
        line = review.get("line")
        owner = review.get("owner")
        allowed_host = review.get("allowed_host")
        rationale = review.get("rationale")
        if (
            not isinstance(path, str)
            or not isinstance(line, int)
            or not isinstance(owner, str)
            or not isinstance(allowed_host, bool)
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise TypeError("dynamic import review is malformed")
        key = (path, line)
        if key in reviewed:
            errors.append(f"duplicate nonliteral dynamic import review: {path}:{line}")
            continue
        reviewed.add(key)
        if key not in sites:
            errors.append(f"stale nonliteral dynamic import review: {path}:{line}")
            continue
        metadata = _metadata(files, path)
        actual_owner = metadata.get("owner")
        roles = metadata.get("roles")
        actual_allowed = actual_owner == "app" and isinstance(roles, list) and (
            "extension_host" in roles
        )
        if owner != actual_owner:
            errors.append(f"dynamic import review owner mismatch: {path}:{line}")
        if allowed_host != actual_allowed:
            errors.append(f"dynamic import review host mismatch: {path}:{line}")
    errors.extend(
        f"unreviewed nonliteral dynamic import: {path}:{line}"
        for path, line in sites - reviewed
    )
    return sorted(errors)


def _current_dynamic_import_review_errors(
    classification: dict[str, object],
    *,
    source_roots: tuple[Path, ...] = (APP_SOURCE_ROOT, LEGACY_SOURCE_ROOT),
) -> list[str]:
    sites = {
        (relative_path, line)
        for relative_path, path, _source_root in _current_open_brain_sources(
            classification,
            source_roots=source_roots,
        )
        for line in _nonliteral_dynamic_import_lines(path)
    }
    return _dynamic_import_review_errors_for_sites(sites, classification)


def _nonliteral_dynamic_import_violations(
    source_root: Path,
    classification: dict[str, object],
) -> list[str]:
    files = _classified_files(classification)
    violations: list[str] = []
    for site in _nonliteral_dynamic_import_sites(source_root):
        metadata = _metadata(files, site.path)
        roles = metadata.get("roles")
        if metadata.get("owner") != "app" or not isinstance(roles, list) or (
            "extension_host" not in roles
        ):
            violations.append(f"nonliteral-dynamic-import: {site.path}:{site.line}")
    return violations


def _live_debt(
    source_root: Path,
    classification: dict[str, object],
) -> list[str]:
    return sorted(
        set(
            _architecture_violations(source_root, classification=classification)
            + _nonliteral_dynamic_import_violations(source_root, classification)
        )
    )


def _live_debt_errors(
    source_root: Path,
    classification: dict[str, object],
) -> list[str]:
    declared = classification.get("temporary_live_debt")
    if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
        raise TypeError("temporary live debt must be a string list")
    errors: list[str] = []
    if declared != sorted(set(declared)):
        errors.append("temporary live debt must be sorted and unique")
    observed = set(_live_debt(source_root, classification))
    expected = set(declared)
    errors.extend(f"missing temporary debt: {item}" for item in observed - expected)
    errors.extend(f"stale temporary debt: {item}" for item in expected - observed)
    return sorted(errors)


def test_every_immediate_package_has_one_explicit_classification() -> None:
    classification = _load_classification()
    packages = classification["packages"]
    files = _classified_files(classification)
    assert isinstance(packages, dict)
    origin_packages = {
        Path(path).parts[0]
        for path in files
        if len(Path(path).parts) > 1 and not path.startswith(("packages/", "tools/"))
    }
    assert set(packages) == origin_packages
    assert all(
        isinstance(package, dict)
        and all(
            isinstance(package.get(field), str)
            for field in ("classification", "target_owner", "current_scope", "phase_1_action")
        )
        for package in packages.values()
    )


def test_every_root_module_has_one_explicit_classification() -> None:
    classification = _load_classification()
    root_modules = classification["root_modules"]
    files = _classified_files(classification)
    assert isinstance(root_modules, dict)
    origin_modules = {Path(path).stem for path in files if len(Path(path).parts) == 1}
    assert set(root_modules) == origin_modules


def test_every_runtime_file_has_one_authoritative_owner() -> None:
    classification = _load_classification()
    assert validate_manifest(REPOSITORY_ROOT, classification) == []
    assert all(
        isinstance(value, dict)
        and value.get("owner") in OWNERS
        and value.get("api") in {"public", "internal"}
        and isinstance(value.get("roles"), list)
        for value in _classified_files(classification).values()
    )


def test_current_namespace_imports_have_classified_file_endpoints() -> None:
    classification = _load_classification()
    classified_paths = set(_classified_files(classification))
    unresolved: set[str] = set()
    references: list[ImportReference] = []
    for relative_path, path, source_root in _current_open_brain_sources(classification):
        current_references = _import_references(path, source_root, classified_paths)
        references.extend(current_references)
        unresolved.update(
            f"{relative_path}:{reference.line} -> {reference.module}"
            for reference in current_references
            if _module_target(reference.module, classified_paths) is None
        )
    assert references
    assert sorted(unresolved) == []


def test_nonliteral_dynamic_import_sites_are_explicitly_reviewed() -> None:
    classification = _load_classification()
    assert _current_dynamic_import_review_errors(classification) == []


def test_current_import_violations_exactly_match_temporary_live_debt() -> None:
    classification = _load_classification()
    assert not MONOLITH_SOURCE_ROOT.exists()
    assert _live_debt_errors(LEGACY_SOURCE_ROOT, classification) == []


def test_engine_distribution_imports_no_other_runtime_distribution() -> None:
    engine_root = REPOSITORY_ROOT / "packages/engine/src/open_brain_engine"
    forbidden: set[str] = set()
    for path in sorted(engine_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Call) and _is_dynamic_import_call(node) and node.args:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    modules.append(argument.value)
            for module in modules:
                if module == "open_brain" or module.startswith(
                    ("open_brain.", "open_brain_connectors", "open_brain_legacy")
                ):
                    line = cast(int, getattr(node, "lineno", 0))
                    forbidden.add(
                        f"{path.relative_to(REPOSITORY_ROOT).as_posix()}:{line} -> {module}"
                    )
    assert forbidden == set()


def test_p2_w1_owner_edges_are_closed() -> None:
    classification = _load_classification()
    classified_paths = set(_classified_files(classification))

    def targets(relative_path: str) -> set[str]:
        path, source_root = _current_open_brain_source(classification, relative_path)
        return {
            target
            for reference in _import_references(path, source_root, classified_paths)
            if (target := _module_target(reference.module, classified_paths)) is not None
        }

    production_cli = {
        (path.relative_to(LEGACY_SOURCE_ROOT).as_posix(), target)
        for path in sorted((LEGACY_SOURCE_ROOT / "production").glob("*.py"))
        for target in targets(path.relative_to(LEGACY_SOURCE_ROOT).as_posix())
        if target.startswith("cli/")
    }
    operations_cli = {
        (path.relative_to(LEGACY_SOURCE_ROOT).as_posix(), target)
        for path in sorted((LEGACY_SOURCE_ROOT / "operations").glob("*.py"))
        for target in targets(path.relative_to(LEGACY_SOURCE_ROOT).as_posix())
        if target.startswith("cli/")
    }
    storage_operations = {
        (path.relative_to(LEGACY_SOURCE_ROOT).as_posix(), target)
        for path in sorted((LEGACY_SOURCE_ROOT / "storage").glob("*.py"))
        for target in targets(path.relative_to(LEGACY_SOURCE_ROOT).as_posix())
        if target.startswith("operations/")
    }

    assert production_cli == set()
    assert operations_cli == set()
    assert not any(target.startswith("ledger/") for target in targets("config.py"))
    assert storage_operations == set()


def test_p4_w4_legacy_composition_uses_distribution_local_compatibility() -> None:
    classification = _load_classification()
    classified_paths = set(_classified_files(classification))

    def targets(relative_path: str) -> set[str]:
        path, source_root = _current_open_brain_source(classification, relative_path)
        return {
            target
            for reference in _import_references(path, source_root, classified_paths)
            if (target := _module_target(reference.module, classified_paths)) is not None
        }

    entrypoints = targets("services/entrypoints.py")
    application = targets("services/application.py")
    production_application = targets("production/application.py")
    compatibility_runtime = "_compat/open_brain/services/runtime.py"

    assert {"services/application.py", compatibility_runtime} <= entrypoints
    assert "services/capabilities.py" not in entrypoints
    assert "production/application.py" not in entrypoints
    assert {"services/capabilities.py", compatibility_runtime} <= application
    assert "services/entrypoints.py" not in application
    assert production_application == {"services/application.py"}

    from open_brain_legacy.production.application import (
        ProductionApplication as CompatibilityProductionApplication,
    )
    from open_brain_legacy.production.application import (
        compose_production_application as compatibility_factory,
    )
    from open_brain_legacy.services.application import (
        ProductionApplication as ApplicationProductionApplication,
    )
    from open_brain_legacy.services.application import compose_production_application
    from open_brain_legacy.services.capabilities import (
        ProductionApplication as CapabilityProductionApplication,
    )
    from open_brain_legacy.services.capabilities import (
        compose_production_application as capability_factory,
    )
    from open_brain_legacy.services.entrypoints import (
        compose_production_application as entrypoint_factory,
    )

    assert compose_production_application is capability_factory
    assert entrypoint_factory is compose_production_application
    assert compatibility_factory is compose_production_application
    assert ApplicationProductionApplication is CapabilityProductionApplication
    assert CompatibilityProductionApplication is ApplicationProductionApplication


def test_p4_w2_installed_entrypoints_are_legacy_writer_free() -> None:
    app_project = tomllib.loads(
        (REPOSITORY_ROOT / "packages/app/pyproject.toml").read_text(encoding="utf-8")
    )
    phase1_entrypoints = (APP_SOURCE_ROOT / "services" / "phase1_entrypoints.py").read_text(
        encoding="utf-8"
    )
    appliance_entrypoints = (
        APP_SOURCE_ROOT / "services" / "appliance_entrypoints.py"
    ).read_text(encoding="utf-8")
    scheduler = (APP_SOURCE_ROOT / "services" / "appliance_scheduler.py").read_text(
        encoding="utf-8"
    )
    classification = _load_classification()
    files = _classified_files(classification)

    assert app_project["project"]["scripts"] == {
        "open-brain": "open_brain.services.local_entrypoints:run_cli",
        "open-brain-secure-node": "open_brain.services.secure_node_entrypoints:run_cli",
        "open-brain-secure-node-mcp": "open_brain.services.secure_node_entrypoints:run_mcp",
    }
    assert "package" not in app_project["tool"]["uv"]
    assert "SingleUserLocalApplication" not in phase1_entrypoints
    assert "compose_http_from_config" not in phase1_entrypoints
    assert "compose_mcp_from_config" not in phase1_entrypoints
    assert "open_brain.production" not in phase1_entrypoints
    assert "open_brain.operations" not in phase1_entrypoints
    assert "open_brain_legacy.production" not in phase1_entrypoints
    assert "open_brain_legacy.operations" not in phase1_entrypoints
    assert "open-brain-http" not in appliance_entrypoints
    assert "JOB-00" not in appliance_entrypoints
    assert "open_brain.storage.filesystem" not in scheduler
    assert "open_brain_engine.storage.operational" in scheduler
    operational = _metadata(files, "storage/operational.py")
    assert {key: operational[key] for key in ("api", "owner", "roles")} == {
        "api": "public",
        "owner": "engine",
        "roles": [],
    }
    assert classification["temporary_live_debt"] == []


SYNTHETIC_FILES: dict[str, dict[str, object]] = {
    "app/composition.py": {
        "owner": "app",
        "api": "internal",
        "roles": ["composition"],
    },
    "app/extension_host.py": {
        "owner": "app",
        "api": "internal",
        "roles": ["extension_host"],
    },
    "app/representation.py": {
        "owner": "app",
        "api": "internal",
        "roles": ["representation"],
    },
    "connector_alpha/adapter.py": {
        "owner": "connector",
        "api": "internal",
        "roles": ["adapter"],
        "adapter_group": "alpha",
    },
    "connector_beta/adapter.py": {
        "owner": "connector",
        "api": "internal",
        "roles": ["adapter"],
        "adapter_group": "beta",
    },
    "engine/extension_contract.py": {
        "owner": "engine",
        "api": "internal",
        "roles": ["extension_contract"],
    },
    "engine/internal.py": {"owner": "engine", "api": "internal", "roles": []},
    "engine/public.py": {"owner": "engine", "api": "public", "roles": []},
    "engine/store.py": {
        "owner": "engine",
        "api": "internal",
        "roles": ["store"],
    },
    "hosted/service.py": {"owner": "hosted", "api": "internal", "roles": []},
    "legacy/migrate.py": {"owner": "legacy", "api": "internal", "roles": []},
    "workspace/tool.py": {"owner": "workspace", "api": "internal", "roles": []},
}


def _synthetic_classification() -> dict[str, object]:
    classification = _load_classification()
    return {
        **classification,
        "files": SYNTHETIC_FILES,
        "dynamic_import_reviews": [],
        "temporary_live_debt": [],
    }


def _write_synthetic_tree(source_root: Path, source_path: str, statement: str) -> None:
    for relative_path in SYNTHETIC_FILES:
        path = source_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(statement if relative_path == source_path else "", encoding="utf-8")


RULE_CASES = (
    ("engine-to-app", "engine/internal.py", "app/composition.py"),
    ("app-to-engine-internals", "app/composition.py", "engine/internal.py"),
    (
        "connector-capability-only",
        "connector_alpha/adapter.py",
        "engine/internal.py",
    ),
    ("shipping-to-legacy", "app/composition.py", "legacy/migrate.py"),
    ("hosted-to-local-internals", "hosted/service.py", "engine/internal.py"),
    ("representation-to-adapters", "app/representation.py", "engine/store.py"),
    (
        "adapter-to-adapter",
        "connector_alpha/adapter.py",
        "connector_beta/adapter.py",
    ),
    (
        "adapter-to-adapter",
        "connector_beta/adapter.py",
        "connector_alpha/adapter.py",
    ),
    ("runtime-to-workspace", "engine/internal.py", "workspace/tool.py"),
)

EXPECTED_RULE_IDS = (
    "engine-to-app",
    "app-to-engine-internals",
    "connector-capability-only",
    "shipping-to-legacy",
    "hosted-to-local-internals",
    "representation-to-adapters",
    "adapter-to-adapter",
    "runtime-to-workspace",
)


def _import_statement(form: str, target_path: str) -> str:
    module = f"open_brain.{target_path.removesuffix('.py').replace('/', '.')}"
    package, _, name = module.rpartition(".")
    if form == "import":
        return f"import {module}\n"
    if form == "from":
        return f"from {package} import {name}\n"
    if form == "import_module":
        return f'import importlib; importlib.import_module("{module}")\n'
    if form == "dunder_import":
        return f'__import__("{module}")\n'
    raise AssertionError(f"unsupported import form: {form}")


@pytest.mark.parametrize(("rule_id", "source_path", "target_path"), RULE_CASES)
@pytest.mark.parametrize("form", ("import", "from", "import_module", "dunder_import"))
def test_each_import_rule_rejects_static_and_literal_dynamic_forms(
    tmp_path: Path,
    rule_id: str,
    source_path: str,
    target_path: str,
    form: str,
) -> None:
    source_root = tmp_path / "open_brain"
    _write_synthetic_tree(source_root, source_path, _import_statement(form, target_path))

    violations = _architecture_violations(
        source_root,
        classification=_synthetic_classification(),
    )

    assert f"{rule_id}: {source_path}:1 -> {target_path}" in violations


def test_direct_literal_import_module_is_checked(tmp_path: Path) -> None:
    source_root = tmp_path / "open_brain"
    statement = (
        "from importlib import import_module; "
        'import_module("open_brain.app.composition")\n'
    )
    _write_synthetic_tree(source_root, "engine/internal.py", statement)

    assert (
        "engine-to-app: engine/internal.py:1 -> app/composition.py"
        in _architecture_violations(
            source_root,
            classification=_synthetic_classification(),
        )
    )


def test_all_eight_rules_are_machine_readable() -> None:
    rules = _load_classification()["rules"]
    assert isinstance(rules, list)
    assert tuple(rule["id"] for rule in rules if isinstance(rule, dict)) == EXPECTED_RULE_IDS


def test_synthetic_owner_coverage_rejects_missing_and_stale_files(tmp_path: Path) -> None:
    source_root = tmp_path / "open_brain"
    _write_synthetic_tree(source_root, "engine/internal.py", "")
    missing = deepcopy(_synthetic_classification())
    missing_files = cast(dict[str, object], missing["files"])
    del missing_files["engine/internal.py"]
    stale = deepcopy(_synthetic_classification())
    stale_files = cast(dict[str, object], stale["files"])
    stale_files["engine/removed.py"] = {
        "owner": "engine",
        "api": "internal",
        "roles": [],
    }

    assert _ownership_errors(source_root, missing) == [
        "unclassified runtime file: engine/internal.py"
    ]
    assert _ownership_errors(source_root, stale) == [
        "stale file classification: engine/removed.py"
    ]


def test_synthetic_live_debt_rejects_missing_and_stale_edges(tmp_path: Path) -> None:
    source_root = tmp_path / "open_brain"
    statement = "import open_brain.app.composition\n"
    _write_synthetic_tree(source_root, "engine/internal.py", statement)
    missing = _synthetic_classification()
    stale = deepcopy(missing)
    stale["temporary_live_debt"] = [
        "engine-to-app: engine/internal.py:1 -> app/composition.py",
        "engine-to-app: engine/internal.py:99 -> app/composition.py",
    ]

    assert _live_debt_errors(source_root, missing) == [
        "missing temporary debt: engine-to-app: engine/internal.py:1 -> app/composition.py"
    ]
    assert _live_debt_errors(source_root, stale) == [
        "stale temporary debt: engine-to-app: engine/internal.py:99 -> app/composition.py"
    ]


def test_reviewed_app_extension_host_nonliteral_import_is_allowed(tmp_path: Path) -> None:
    source_root = tmp_path / "open_brain"
    statement = (
        "from importlib import import_module\n"
        'module_name = "open_brain.engine.public"\n'
        "import_module(module_name)\n"
    )
    _write_synthetic_tree(source_root, "app/extension_host.py", statement)
    classification = _synthetic_classification()
    classification["dynamic_import_reviews"] = [
        {
            "path": "app/extension_host.py",
            "line": 3,
            "owner": "app",
            "allowed_host": True,
            "rationale": "Loads a named synthetic extension after allow-list validation.",
        }
    ]

    assert _dynamic_import_review_errors(source_root, classification) == []
    assert _nonliteral_dynamic_import_violations(source_root, classification) == []


def test_arbitrary_reviewed_nonliteral_import_is_temporary_debt(tmp_path: Path) -> None:
    source_root = tmp_path / "open_brain"
    statement = (
        "from importlib import import_module\n"
        'module_name = "open_brain.app.composition"\n'
        "import_module(module_name)\n"
    )
    _write_synthetic_tree(source_root, "engine/internal.py", statement)
    classification = _synthetic_classification()
    classification["dynamic_import_reviews"] = [
        {
            "path": "engine/internal.py",
            "line": 3,
            "owner": "engine",
            "allowed_host": False,
            "rationale": "Synthetic temporary debt outside the app extension host.",
        }
    ]

    assert _dynamic_import_review_errors(source_root, classification) == []
    assert _nonliteral_dynamic_import_violations(source_root, classification) == [
        "nonliteral-dynamic-import: engine/internal.py:3"
    ]


def test_nonliteral_import_review_inventory_is_exact(tmp_path: Path) -> None:
    source_root = tmp_path / "open_brain"
    statement = "from importlib import import_module\nimport_module(module_name)\n"
    _write_synthetic_tree(source_root, "app/extension_host.py", statement)
    missing = _synthetic_classification()
    stale = deepcopy(missing)
    stale["dynamic_import_reviews"] = [
        {
            "path": "app/extension_host.py",
            "line": 99,
            "owner": "app",
            "allowed_host": True,
            "rationale": "Stale synthetic review.",
        }
    ]

    assert _dynamic_import_review_errors(source_root, missing) == [
        "unreviewed nonliteral dynamic import: app/extension_host.py:2"
    ]
    assert _dynamic_import_review_errors(source_root, stale) == [
        "stale nonliteral dynamic import review: app/extension_host.py:99",
        "unreviewed nonliteral dynamic import: app/extension_host.py:2",
    ]


def test_moved_source_dynamic_import_review_is_not_filtered(tmp_path: Path) -> None:
    source_root = tmp_path / "open_brain"
    source_path = source_root / "app" / "extension_host.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "from importlib import import_module\nimport_module(module_name)\n",
        encoding="utf-8",
    )
    classification = _synthetic_classification()
    files = deepcopy(SYNTHETIC_FILES)
    record = files["app/extension_host.py"]
    record["current_path"] = str(source_path)
    record["movement_state"] = "moved"
    classification["files"] = files
    classification["dynamic_import_reviews"] = [
        {
            "path": "app/extension_host.py",
            "line": 99,
            "owner": "app",
            "allowed_host": True,
            "rationale": "Stale synthetic review for a moved source.",
        }
    ]

    assert _current_dynamic_import_review_errors(
        classification,
        source_roots=(source_root,),
    ) == [
        "stale nonliteral dynamic import review: app/extension_host.py:99",
        "unreviewed nonliteral dynamic import: app/extension_host.py:2",
    ]


def test_literal_external_dynamic_import_needs_no_internal_review(tmp_path: Path) -> None:
    source_root = tmp_path / "open_brain"
    statement = 'import importlib\nimportlib.import_module("external_sdk")\n'
    _write_synthetic_tree(source_root, "connector_alpha/adapter.py", statement)
    classification = _synthetic_classification()

    assert _dynamic_import_review_errors(source_root, classification) == []
    assert _architecture_violations(source_root, classification=classification) == []
