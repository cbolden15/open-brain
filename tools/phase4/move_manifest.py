"""Validate and render the canonical Phase 4 move manifest."""

from __future__ import annotations

import argparse
import ast
import json
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, cast

SCHEMA_VERSION: Final = 3
PHASE4_SCHEMA_VERSION: Final = 1
MANIFEST_RELATIVE: Final = Path("docs/v0-package-classification.json")
COMPATIBILITY_RELATIVE: Final = Path("release/phase4-compatibility.json")
TOOLCHAIN_RELATIVE: Final = Path("release/phase4-toolchain.json")

RUNTIME_FIELDS: Final = frozenset(
    {
        "current_path",
        "movement_state",
        "target_distribution",
        "target_path",
        "runtime_namespace",
        "import_rewrite",
        "api_status",
        "artifact_disposition",
        "test_owner",
        "old_import_disposition",
        "resource_requirements",
    }
)
SUBJECT_FIELDS: Final = frozenset(
    {
        "kind",
        "current_path",
        "movement_state",
        "target_distribution",
        "target_path",
        "runtime_namespace",
        "import_rewrite",
        "api_status",
        "artifact_disposition",
        "test_owner",
        "old_import_disposition",
        "resource_requirements",
    }
)
SHIPPING_ARTIFACTS: Final = frozenset(
    {
        "engine-wheel",
        "engine-sdist",
        "app-wheel",
        "app-sdist",
        "app-native",
        "connector-wheel",
        "connector-sdist",
    }
)


class ManifestError(ValueError):
    """The canonical manifest cannot be loaded safely."""


@dataclass(frozen=True, order=True, slots=True)
class Finding:
    code: str
    subject: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "subject": self.subject}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ManifestError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError("move manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise ManifestError("move manifest must be an object")
    return cast(dict[str, object], value)


def _object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    result = cast(list[str], value)
    return result if result == sorted(set(result)) else None


def _files(root: Path, pattern: str) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.glob(pattern)
        if path.is_file() and "__pycache__" not in path.parts
    }


def discover_subject_kinds(root: Path, manifest: Mapping[str, object]) -> dict[str, str]:
    """Discover every non-runtime P4-W0 subject in the declared repository scope."""

    subjects: dict[str, str] = {}

    def add(paths: Iterable[str], kind: str) -> None:
        for path in paths:
            previous = subjects.setdefault(path, kind)
            if previous != kind:
                raise ManifestError(f"overlapping subject discovery: {path}")

    add(_files(root, "tests/**/*.py") | _files(root, "packages/*/tests/**/*.py"), "test")
    add(
        _files(root, "schemas/**/*")
        | _files(root, "packages/*/src/*/portable/schemas/**/*")
        | _files(root, "packages/*/src/*/portability/schemas/**/*")
        | _files(root, "packages/*/src/*/protocol/schemas/**/*"),
        "schema",
    )
    add(
        _files(root, "tests/fixtures/**/*")
        | _files(root, "packages/*/tests/fixtures/**/*")
        | _files(root, "packages/*/src/*/portable/conformance/**/*")
        | _files(root, "packages/*/src/*/portability/conformance/**/*")
        | _files(root, "packages/*/src/*/protocol/conformance/**/*"),
        "fixture",
    )
    parity_resources = _files(root, "packages/*/tests/parity/phase7/capture_scenarios.json")
    parity_resource = root / "tests/parity/phase7/capture_scenarios.json"
    if parity_resource.is_file():
        parity_resources.add(parity_resource.relative_to(root).as_posix())
    add(parity_resources, "test-resource")

    package_resources = {
        path
        for pattern in (
            "docs/*.md",
            "docs/architecture/*.md",
            "docs/architecture/**/*.md",
            "docs/architecture/*.json",
            "examples/**/*",
            "packages/*/src/*/resources/**/*",
            "src/open_brain/resources/**/*",
        )
        for path in _files(root, pattern)
    }
    package_resources.update(
        path for path in ("LICENSE", "NOTICE", "README.md") if (root / path).is_file()
    )
    package_resources.update(
        path
        for path in (
            "docs/acceptance/five-minute-install.md",
            "docs/plans/2026-09-07-core-w0-shared-portability.md",
            "docs/plans/product-roadmap.md",
        )
        if (root / path).is_file()
    )
    package_resources.update(_files(root, "packages/*/LICENSE"))
    package_resources.update(_files(root, "packages/*/NOTICE"))
    add(package_resources, "package-resource")

    release_tools = {
        path
        for pattern in (
            ".github/**/*.yml",
            ".github/**/*.yaml",
            "packages/*/hatch_build.py",
            "packages/*/pyproject.toml",
            "release/native/*.spec",
            "tools/m1/*.py",
            "tools/phase4/*.py",
        )
        for path in _files(root, pattern)
    }
    release_tools.update(
        path
        for path in (
            "Makefile",
            "pyproject.toml",
            "uv.lock",
            MANIFEST_RELATIVE.as_posix(),
        )
        if (root / path).is_file()
    )
    add(release_tools, "release-tool")
    add(_files(root, "release/*.json"), "release-resource")

    phase4 = _object(manifest.get("phase4"), label="phase4")
    reports = _string_list(phase4.get("generated_reports"))
    if reports is None:
        raise ManifestError("phase4 generated_reports must be sorted and unique")
    add(reports, "generated-resource")
    reserved_entry_points = _string_list(phase4.get("reserved_entry_points"))
    if reserved_entry_points is None:
        raise ManifestError("phase4 reserved_entry_points must be sorted and unique")
    add(reserved_entry_points, "entry-point")

    for path in sorted((root / "packages").glob("*/pyproject.toml")) + [
        root / "pyproject.toml"
    ]:
        if not path.is_file():
            continue
        project = tomllib.loads(path.read_text(encoding="utf-8"))
        metadata = project.get("project", {})
        scripts = metadata.get("scripts", {}) if isinstance(metadata, dict) else {}
        if not isinstance(scripts, dict):
            raise ManifestError("project scripts must be an object")
        entry_point_groups = (
            metadata.get("entry-points", {}) if isinstance(metadata, dict) else {}
        )
        if not isinstance(entry_point_groups, dict):
            raise ManifestError("project entry-point groups must be an object")
        relative = path.relative_to(root).as_posix()
        add(
            {
                f"{relative}#project.scripts.{name}"
                for name in scripts
                if isinstance(name, str)
            },
            "entry-point",
        )
        for group, registrations in entry_point_groups.items():
            if not isinstance(group, str) or not isinstance(registrations, dict):
                raise ManifestError("project entry-point group must be an object")
            add(
                {
                    f"{relative}#project.entry-points.{group}.{name}"
                    for name in registrations
                    if isinstance(name, str)
                },
                "entry-point",
            )
    return dict(sorted(subjects.items()))


def discover_runtime_paths(root: Path) -> set[str]:
    """Discover runtime Python files in the monolith and Phase 4 destinations."""

    return {
        path
        for pattern in (
            "src/open_brain/**/*.py",
            "packages/*/src/**/*.py",
            "tools/open_brain_dev/**/*.py",
            "tools/open_brain_phase4/**/*.py",
        )
        for path in _files(root, pattern)
    }


def _target_module(target_path: str, namespace: str) -> str | None:
    marker = f"/src/{namespace}/"
    if marker in target_path:
        relative = target_path.split(marker, 1)[1]
        prefix = (namespace,)
    elif target_path.startswith("tools/"):
        relative = target_path.removeprefix("tools/")
        prefix = ("tools",)
    else:
        return None
    if not relative.endswith(".py"):
        return None
    parts = PurePosixPath(relative).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((*prefix, *parts))


def _path_is_safe(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _distribution_prefix(distribution: str) -> str | None:
    return {
        "engine": "packages/engine/",
        "app": "packages/app/",
        "connectors": "packages/connectors/",
        "legacy": "packages/legacy/",
    }.get(distribution)


def _record_findings(
    *,
    key: str,
    record: Mapping[str, object],
    kind: str,
    destinations: dict[str, str],
    runtime: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    required = RUNTIME_FIELDS if runtime else SUBJECT_FIELDS
    if not required <= set(record):
        findings.append(Finding("P4M011", key, "required manifest fields are missing"))
        return findings
    movement_state = record.get("movement_state")
    if movement_state not in {"planned", "moved"}:
        findings.append(Finding("P4M011", key, "movement state is invalid"))
    if not runtime and record.get("kind") != kind:
        findings.append(Finding("P4M011", key, "subject kind does not match discovery"))

    distribution = record.get("target_distribution")
    target_path = record.get("target_path")
    if not isinstance(distribution, str) or distribution not in {
        "engine",
        "app",
        "connectors",
        "legacy",
        "workspace",
    }:
        findings.append(Finding("P4M011", key, "target distribution is invalid"))
        return findings
    if not isinstance(target_path, str) or not _path_is_safe(target_path):
        findings.append(Finding("P4M005", key, "target path is unsafe"))
    else:
        prefix = _distribution_prefix(distribution)
        if prefix is not None and not target_path.startswith(prefix):
            findings.append(Finding("P4M005", key, "target is outside its distribution"))
        previous = destinations.setdefault(target_path, key)
        if previous != key:
            findings.append(Finding("P4M004", key, f"destination also owned by {previous}"))
        expected_current = target_path if movement_state == "moved" else key
        if record.get("current_path") != expected_current:
            findings.append(
                Finding("P4M011", key, "current path disagrees with movement state")
            )

    owner = record.get("test_owner")
    if owner not in {"engine", "app", "connector", "legacy", "workspace"}:
        findings.append(Finding("P4M011", key, "test or fixture owner is invalid"))
    api_status = record.get("api_status")
    if api_status not in {"public", "distribution-private", "test-only", "workspace-only"}:
        findings.append(Finding("P4M011", key, "API status is invalid"))
    artifacts = _string_list(record.get("artifact_disposition"))
    if artifacts is None or not artifacts:
        findings.append(Finding("P4M011", key, "artifact disposition must be sorted and unique"))
        artifacts = []
    workspace_shipping_allowed = kind in {"package-resource", "release-resource"}
    if distribution == "legacy" and SHIPPING_ARTIFACTS.intersection(artifacts):
        findings.append(
            Finding("P4M009", key, "legacy or workspace content enters shipping artifacts")
        )
    if (
        distribution == "workspace"
        and not workspace_shipping_allowed
        and SHIPPING_ARTIFACTS.intersection(artifacts)
    ):
        findings.append(
            Finding("P4M009", key, "legacy or workspace content enters shipping artifacts")
        )
    if kind == "test" and artifacts != ["excluded"]:
        findings.append(Finding("P4M009", key, "tests must be excluded from shipping artifacts"))
    requirements = _string_list(record.get("resource_requirements"))
    if requirements is None:
        findings.append(Finding("P4M011", key, "resource requirements must be sorted and unique"))
    if record.get("old_import_disposition") not in {
        "legacy-private",
        "not-applicable",
        "remove",
        "retain",
    }:
        findings.append(Finding("P4M008", key, "old import disposition is unresolved"))

    namespace = record.get("runtime_namespace")
    rewrite = record.get("import_rewrite")
    if runtime or kind == "entry-point":
        if not isinstance(namespace, str) or not namespace:
            findings.append(Finding("P4M006", key, "runtime namespace is missing"))
        if not isinstance(rewrite, dict):
            findings.append(Finding("P4M006", key, "import rewrite is missing"))
        else:
            source = rewrite.get("from")
            target = rewrite.get("to")
            if (
                not isinstance(source, str)
                or not source
                or not isinstance(target, str)
                or not target
            ):
                findings.append(Finding("P4M006", key, "import rewrite has no destination symbol"))
            elif runtime and isinstance(target_path, str) and isinstance(namespace, str):
                expected = _target_module(target_path, namespace)
                if expected is None or target != expected:
                    findings.append(
                        Finding("P4M006", key, "import rewrite disagrees with target path")
                    )
    elif namespace is not None or rewrite is not None:
        findings.append(Finding("P4M011", key, "non-runtime subject declares an import rewrite"))
    return findings


def _subject_exists(root: Path, key: str, kind: str) -> bool:
    if "#" not in key:
        return (root / key).is_file()
    raw_path, symbol = key.split("#", 1)
    path = root / raw_path
    if not path.is_file():
        return False
    if kind != "entry-point" or raw_path.endswith("pyproject.toml"):
        return True
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=raw_path)
    except (OSError, UnicodeError, SyntaxError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol
        for node in tree.body
    )


def _graph_findings(phase4: Mapping[str, object]) -> list[Finding]:
    graph = phase4.get("runtime_dependency_graph")
    if not isinstance(graph, dict):
        return [Finding("P4M007", "phase4.runtime_dependency_graph", "graph is missing")]
    expected = {
        "app": ["engine"],
        "connectors": ["app", "engine"],
        "engine": [],
        "legacy": ["engine"],
    }
    if graph != expected:
        return [Finding("P4M007", "phase4.runtime_dependency_graph", "forbidden edge or cycle")]
    return []


def _identity_findings(root: Path, phase4: Mapping[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    identity = phase4.get("release_identity")
    if not isinstance(identity, dict):
        return [Finding("P4M010", "phase4.release_identity", "release identity is missing")]
    try:
        compatibility = load_manifest(root / COMPATIBILITY_RELATIVE)
        toolchain = load_manifest(root / TOOLCHAIN_RELATIVE)
    except (ManifestError, OSError, UnicodeError, tomllib.TOMLDecodeError):
        return [Finding("P4M010", "phase4.release_identity", "identity authority is unreadable")]
    version = identity.get("candidate_version")
    schema_range = identity.get("portable_schema")
    declared_versions: list[object] = []
    for path in [root / "pyproject.toml", *sorted((root / "packages").glob("*/pyproject.toml"))]:
        if not path.is_file():
            continue
        try:
            project = tomllib.loads(path.read_text(encoding="utf-8")).get("project")
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            return [
                Finding("P4M010", "phase4.release_identity", "package identity is unreadable")
            ]
        if isinstance(project, dict) and project.get("name") in {
            "open-brain",
            "open-brain-connectors",
            "open-brain-engine",
            "open-brain-legacy",
        }:
            declared_versions.append(project.get("version"))
    if (
        not declared_versions
        or any(project_version != version for project_version in declared_versions)
        or version != compatibility.get("candidate_version")
        or version != toolchain.get("candidate_version")
        or schema_range != compatibility.get("portable_schema")
        or schema_range != toolchain.get("portable_schema")
    ):
        findings.append(
            Finding("P4M010", "phase4.release_identity", "version or schema range mismatch")
        )
    return findings


def validate_manifest(root: Path, manifest: Mapping[str, object]) -> list[Finding]:
    """Return stable, metadata-only findings for one canonical manifest."""

    findings: list[Finding] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        findings.append(Finding("P4M001", "schema_version", "unsupported schema version"))
    phase4_value = manifest.get("phase4")
    if not isinstance(phase4_value, dict):
        return sorted(findings + [Finding("P4M001", "phase4", "phase4 contract is missing")])
    phase4 = cast(dict[str, object], phase4_value)
    if phase4.get("schema_version") != PHASE4_SCHEMA_VERSION:
        findings.append(Finding("P4M001", "phase4.schema_version", "unsupported phase4 schema"))
    findings.extend(_graph_findings(phase4))

    source_root_value = manifest.get("source_root")
    files_value = manifest.get("files")
    if not isinstance(source_root_value, str) or not isinstance(files_value, dict):
        return sorted(findings + [Finding("P4M001", "files", "runtime inventory is malformed")])
    runtime = cast(dict[str, object], files_value)
    discovered_runtime = discover_runtime_paths(root)
    declared_runtime: set[str] = set()
    for value in runtime.values():
        if isinstance(value, dict):
            current_path = value.get("current_path")
            if isinstance(current_path, str):
                declared_runtime.add(current_path)
    for path in sorted(discovered_runtime - declared_runtime):
        findings.append(Finding("P4M002", path, "unclassified runtime file"))
    for path in sorted(declared_runtime - discovered_runtime):
        findings.append(Finding("P4M003", path, "stale runtime subject"))

    destinations: dict[str, str] = {}
    for relative, value in sorted(runtime.items()):
        key = f"{source_root_value}/{relative}"
        if not isinstance(value, dict):
            findings.append(Finding("P4M011", key, "runtime record is not an object"))
            continue
        findings.extend(
            _record_findings(
                key=key,
                record=cast(dict[str, object], value),
                kind="runtime",
                destinations=destinations,
                runtime=True,
            )
        )

    subjects_value = phase4.get("subjects")
    if not isinstance(subjects_value, dict):
        return sorted(
            findings + [Finding("P4M001", "phase4.subjects", "subject inventory is missing")]
        )
    subjects = cast(dict[str, object], subjects_value)
    try:
        discovered_subjects = discover_subject_kinds(root, manifest)
    except (ManifestError, OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return sorted(findings + [Finding("P4M001", "phase4.subjects", str(exc))])
    declared_subjects: set[str] = set()
    for value in subjects.values():
        if isinstance(value, dict):
            current_path = value.get("current_path")
            if isinstance(current_path, str):
                declared_subjects.add(current_path)
    for path in sorted(set(discovered_subjects) - declared_subjects):
        findings.append(Finding("P4M002", path, "unclassified non-runtime subject"))
    for path in sorted(declared_subjects - set(discovered_subjects)):
        findings.append(Finding("P4M003", path, "stale non-runtime subject"))
    for key, value in sorted(subjects.items()):
        if not isinstance(value, dict):
            findings.append(Finding("P4M011", key, "subject record is not an object"))
            continue
        current_path_value = value.get("current_path")
        current_path = current_path_value if isinstance(current_path_value, str) else ""
        kind = discovered_subjects.get(current_path, "unknown")
        if (
            kind != "unknown"
            and current_path
            and not _subject_exists(root, current_path, kind)
        ):
            findings.append(Finding("P4M002", current_path, "subject source is missing"))
        findings.extend(
            _record_findings(
                key=key,
                record=cast(dict[str, object], value),
                kind=kind,
                destinations=destinations,
                runtime=False,
            )
        )
    findings.extend(_identity_findings(root, phase4))
    return sorted(set(findings))


def _rows(manifest: Mapping[str, object]) -> list[tuple[str, Mapping[str, object]]]:
    source_root = cast(str, manifest["source_root"])
    files = cast(dict[str, object], manifest["files"])
    phase4 = cast(dict[str, object], manifest["phase4"])
    subjects = cast(dict[str, object], phase4["subjects"])
    rows = [
        (f"{source_root}/{path}", cast(dict[str, object], record))
        for path, record in files.items()
        if isinstance(record, dict)
    ]
    rows.extend(
        (path, cast(dict[str, object], record))
        for path, record in subjects.items()
        if isinstance(record, dict)
    )
    return sorted(rows)


def render_move_report(manifest: Mapping[str, object]) -> str:
    rows = _rows(manifest)
    counts = Counter(str(record.get("target_distribution")) for _, record in rows)
    lines = [
        "# Phase 4 move report",
        "",
        "Generated from `docs/v0-package-classification.json`; do not edit by hand.",
        "",
        f"- Total subjects: `{len(rows)}`",
        *[f"- {name}: `{count}`" for name, count in sorted(counts.items())],
        "",
        "| Source identity | Current subject | State | Kind | Distribution | Target | Artifacts |",
        "|---|---|---|---|---|---|---|",
    ]
    for path, record in rows:
        kind = str(record.get("kind", "runtime"))
        artifacts = ", ".join(cast(list[str], record.get("artifact_disposition", [])))
        lines.append(
            f"| `{path}` | `{record.get('current_path')}` | `{record.get('movement_state')}` | "
            f"`{kind}` | `{record.get('target_distribution')}` | "
            f"`{record.get('target_path')}` | `{artifacts}` |"
        )
    return "\n".join(lines) + "\n"


def render_import_report(manifest: Mapping[str, object]) -> str:
    source_root = cast(str, manifest["source_root"])
    files = cast(dict[str, object], manifest["files"])
    lines = [
        "# Phase 4 import rewrite report",
        "",
        "Generated from `docs/v0-package-classification.json`; do not edit by hand.",
        "",
        "| Source identity | Current file | State | Current import | Target import | "
        "Old-path disposition |",
        "|---|---|---|---|---|---|",
    ]
    for relative, raw in sorted(files.items()):
        record = cast(dict[str, object], raw)
        rewrite = cast(dict[str, object], record["import_rewrite"])
        lines.append(
            f"| `{source_root}/{relative}` | `{record.get('current_path')}` | "
            f"`{record.get('movement_state')}` | `{rewrite['from']}` | `{rewrite['to']}` | "
            f"`{record['old_import_disposition']}` |"
        )
    return "\n".join(lines) + "\n"


def write_reports(manifest: Mapping[str, object], *, move: Path, imports: Path) -> None:
    move.write_text(render_move_report(manifest), encoding="utf-8")
    imports.write_text(render_import_report(manifest), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "reports"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--move-report", type=Path)
    parser.add_argument("--import-report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    manifest_path = args.manifest or root / MANIFEST_RELATIVE
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        print(json.dumps(Finding("P4M001", MANIFEST_RELATIVE.as_posix(), str(exc)).to_dict()))
        return 1
    if args.command == "validate":
        findings = validate_manifest(root, manifest)
        for finding in findings:
            print(json.dumps(finding.to_dict(), sort_keys=True))
        return 1 if findings else 0
    if args.move_report is None or args.import_report is None:
        raise SystemExit("reports requires --move-report and --import-report")
    write_reports(manifest, move=args.move_report, imports=args.import_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
