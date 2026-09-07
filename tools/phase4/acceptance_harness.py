"""Reusable isolated-artifact contracts and stable Phase 4 finding codes."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, cast

EXPECTED_RED_SCHEMA: Final = 1
_DISTRIBUTION_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_FORBIDDEN_APP_IMPORT_ROOTS: Final = frozenset(
    {"open_brain_connectors", "open_brain_legacy"}
)
_PUBLIC_CONNECTOR_APP_MODULES: Final = frozenset(
    {
        "open_brain.extensions.connector_worker_v1",
        "open_brain.extensions.connectors",
    }
)
_IMPORTLIB_MODULE: Final = "importlib"
_IMPORTLIB_NAMESPACE: Final = "importlib.__dict__"
_IMPORTLIB_UTIL_MODULE: Final = "importlib.util"
_PKGUTIL_MODULE: Final = "pkgutil"
_BUILTINS_MODULE: Final = "builtins"
_BUILTINS_NAMESPACE: Final = "builtins.__dict__"
_SYS_MODULE: Final = "sys"
_SYS_MODULES: Final = "sys.modules"
_SYS_NAMESPACE: Final = "sys.__dict__"
_FRAME_OBJECT: Final = "frame"
_NAMESPACE_MAPPING: Final = "namespace"
_FUNCTION_OBJECT: Final = "function"
_OBJECT_TYPE: Final = "object"
_TYPE_CALLABLE: Final = "type"
_TYPE_OBJECT: Final = "type-object"
_IMPORT_MODULE_CALLABLE: Final = "import_module"
_BUILTIN_IMPORT_CALLABLE: Final = "__import__"
_RESOLVE_NAME_CALLABLE: Final = "resolve_name"
_GETATTR_CALLABLE: Final = "getattr"
_SYS_GETFRAME_CALLABLE: Final = "sys._getframe"
_SYS_GETATTRIBUTE_CALLABLE: Final = "sys.__getattribute__"
_IMPORTLIB_GETATTRIBUTE_CALLABLE: Final = "importlib.__getattribute__"
_BUILTINS_GETATTRIBUTE_CALLABLE: Final = "builtins.__getattribute__"
_FUNCTION_GETATTRIBUTE_CALLABLE: Final = "function.__getattribute__"
_OBJECT_GETATTRIBUTE_CALLABLE: Final = "object.__getattribute__"
_GLOBALS_CALLABLE: Final = "globals"
_LOCALS_CALLABLE: Final = "locals"
_VARS_CALLABLE: Final = "vars"
_EVAL_CALLABLE: Final = "eval"
_EXEC_CALLABLE: Final = "exec"
_DYNAMIC_IMPORT_CALLABLES: Final = frozenset(
    {_IMPORT_MODULE_CALLABLE, _BUILTIN_IMPORT_CALLABLE, _RESOLVE_NAME_CALLABLE}
)
_UNSAFE_REFLECTION_CALLABLES: Final = frozenset(
    {
        _GLOBALS_CALLABLE,
        _LOCALS_CALLABLE,
        _VARS_CALLABLE,
        _EVAL_CALLABLE,
        _EXEC_CALLABLE,
        _SYS_GETFRAME_CALLABLE,
        _SYS_GETATTRIBUTE_CALLABLE,
        _IMPORTLIB_GETATTRIBUTE_CALLABLE,
        _BUILTINS_GETATTRIBUTE_CALLABLE,
        _FUNCTION_GETATTRIBUTE_CALLABLE,
        _OBJECT_GETATTRIBUTE_CALLABLE,
    }
)
_DYNAMIC_IMPORT_CAPABILITIES: Final = frozenset(
    {
        _IMPORTLIB_MODULE,
        _IMPORTLIB_NAMESPACE,
        _IMPORTLIB_UTIL_MODULE,
        _PKGUTIL_MODULE,
        _BUILTINS_MODULE,
        _BUILTINS_NAMESPACE,
        _SYS_MODULES,
        _SYS_NAMESPACE,
        _NAMESPACE_MAPPING,
        _IMPORT_MODULE_CALLABLE,
        _BUILTIN_IMPORT_CALLABLE,
        *_UNSAFE_REFLECTION_CALLABLES,
    }
)
_BUILTINS_MEMBERS: Final[Mapping[str, str]] = {
    "__import__": _BUILTIN_IMPORT_CALLABLE,
    "__dict__": _BUILTINS_NAMESPACE,
    "__getattribute__": _BUILTINS_GETATTRIBUTE_CALLABLE,
    "getattr": _GETATTR_CALLABLE,
    "globals": _GLOBALS_CALLABLE,
    "locals": _LOCALS_CALLABLE,
    "vars": _VARS_CALLABLE,
    "eval": _EVAL_CALLABLE,
    "exec": _EXEC_CALLABLE,
    "object": _OBJECT_TYPE,
    "type": _TYPE_CALLABLE,
}


@dataclass(frozen=True, order=True, slots=True)
class Finding:
    code: str
    subject: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "subject": self.subject}


@dataclass(frozen=True, slots=True)
class ArtifactContract:
    subject: str
    required_members: tuple[str, ...]
    forbidden_patterns: tuple[str, ...]
    expected_name: str | None = None
    expected_version: str | None = None


@dataclass(frozen=True, slots=True)
class ImportProbe:
    module_paths: tuple[str, ...]
    sys_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AcceptanceContract:
    contract_id: str
    gate: str
    artifacts: tuple[str, ...]
    behaviors: tuple[str, ...]
    forbidden_imports: tuple[str, ...]


CONTRACTS: Final = (
    AcceptanceContract(
        "engine-isolation",
        "P4A",
        ("engine-wheel",),
        ("public-engine-import", "engine-unit-integration"),
        ("open_brain", "open_brain_connectors", "open_brain_legacy"),
    ),
    AcceptanceContract(
        "app-isolation",
        "P4A",
        ("app-wheel", "engine-wheel"),
        (
            "first-value-no-provider",
            "v0-gate-07-sibling-approve-reject-safe-edit-cli-ui",
            "v0-gate-13-space-create-rename-later-route-scoped-all-retrieval",
            "daemon-status-doctor",
            "backup-restore-portable-upgrade-uninstall",
        ),
        ("open_brain_connectors", "open_brain_legacy"),
    ),
    AcceptanceContract(
        "connector-isolation",
        "P4A",
        ("connector-wheel", "app-wheel", "engine-wheel"),
        ("reference-conformance", "isolated-worker", "bounded-capabilities"),
        ("open_brain_legacy",),
    ),
    AcceptanceContract(
        "artifact-membership",
        "P4A/P4B",
        ("all-python-and-native-artifacts",),
        ("required-members", "forbidden-members", "duplicates", "safe-paths"),
        (),
    ),
    AcceptanceContract(
        "identity-compatibility",
        "P4A/P4B",
        ("all-python-and-native-artifacts",),
        ("package-native-doctor-portable-schema-identity",),
        (),
    ),
    AcceptanceContract(
        "clean-host-lifecycle",
        "P4B",
        ("macos-arm64-native", "linux-x86_64-native"),
        (
            "install-init-start-status-capture-review-retrieve",
            "backup-exact-restore-portable-upgrade-rollback",
            "stop-uninstall-residue-no-source-no-system-python",
        ),
        (),
    ),
)


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return environment


def build_command(project: Path, destination: Path) -> tuple[str, ...]:
    return (
        "uv",
        "build",
        "--no-sources",
        "--project",
        os.fspath(project),
        "--out-dir",
        os.fspath(destination),
    )


def create_environment_command(
    environment: Path,
    *,
    python_version: str | None = None,
) -> tuple[str, ...]:
    selected = python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
    if not isinstance(selected, str) or re.fullmatch(r"[0-9]+\.[0-9]+", selected) is None:
        raise ValueError("invalid isolation Python version")
    return ("uv", "venv", "--python", selected, os.fspath(environment))


def install_command(python: Path, artifacts: Sequence[Path]) -> tuple[str, ...]:
    return (
        "uv",
        "pip",
        "install",
        "--python",
        os.fspath(python),
        "--link-mode",
        "copy",
        "--no-index",
        *(os.fspath(path) for path in artifacts),
    )


def export_runtime_requirements_command(package: str) -> tuple[str, ...]:
    if package not in {"open-brain", "open-brain-engine"}:
        raise ValueError("unsupported runtime requirements package")
    return (
        "uv",
        "export",
        "--locked",
        "--package",
        package,
        "--no-emit-project",
        "--no-emit-workspace",
        "--no-dev",
        "--no-annotate",
        "--no-header",
    )


def install_runtime_requirements_command(
    python: Path,
    requirements: Path,
) -> tuple[str, ...]:
    return (
        "uv",
        "pip",
        "install",
        "--python",
        os.fspath(python),
        "--only-binary=:all:",
        "--require-hashes",
        "--requirements",
        os.fspath(requirements),
    )


def export_test_requirements_command() -> tuple[str, ...]:
    return (
        "uv",
        "export",
        "--locked",
        "--only-group",
        "dev",
        "--no-emit-project",
        "--no-emit-workspace",
        "--prune",
        "build",
        "--prune",
        "mypy",
        "--prune",
        "ruff",
        "--no-annotate",
        "--no-header",
    )


def install_test_requirements_command(
    python: Path,
    requirements: Path,
) -> tuple[str, ...]:
    return (
        "uv",
        "pip",
        "install",
        "--python",
        os.fspath(python),
        "--require-hashes",
        "--requirements",
        os.fspath(requirements),
    )


def engine_test_command(
    python: Path,
    runner: Path,
    tests: Path,
    engine_site_packages: Path,
) -> tuple[str, ...]:
    return (
        os.fspath(python),
        "-I",
        os.fspath(runner),
        os.fspath(tests),
        os.fspath(engine_site_packages),
    )


def site_packages_command(python: Path) -> tuple[str, ...]:
    return (
        os.fspath(python),
        "-I",
        "-c",
        "import sysconfig; print(sysconfig.get_path('purelib'))",
    )


def run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        tuple(command),
        cwd=cwd,
        env=sanitized_environment(),
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
    )


def _archive_members(path: Path) -> tuple[list[str], Mapping[str, bytes]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            zip_payloads = {name: archive.read(name) for name in names if name.endswith("METADATA")}
            return names, zip_payloads
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            names = archive.getnames()
            tar_payloads: dict[str, bytes] = {}
            for member in archive.getmembers():
                if not member.name.endswith("PKG-INFO") or not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if stream is not None:
                    tar_payloads[member.name] = stream.read()
            return names, tar_payloads
    return [], {}


def _metadata(payloads: Mapping[str, bytes]) -> tuple[str | None, str | None]:
    for payload in payloads.values():
        name = version = None
        for line in payload.decode("utf-8", errors="replace").splitlines():
            if line.startswith("Name: "):
                name = line.removeprefix("Name: ")
            elif line.startswith("Version: "):
                version = line.removeprefix("Version: ")
        if name is not None or version is not None:
            return name, version
    return None, None


def artifact_findings(path: Path, contract: ArtifactContract) -> list[Finding]:
    if not path.is_file():
        return [Finding("P4H001", contract.subject, "artifact is missing")]
    names, payloads = _archive_members(path)
    if not names:
        return [Finding("P4H001", contract.subject, "artifact format is unsupported")]
    findings: list[Finding] = []
    if len(names) != len(set(names)):
        findings.append(Finding("P4H006", contract.subject, "artifact has duplicate members"))
    for name in names:
        member = PurePosixPath(name)
        if member.is_absolute() or ".." in member.parts or "\\" in name:
            findings.append(Finding("P4H005", contract.subject, "artifact has an unsafe member"))
        if any(fnmatch.fnmatch(name, pattern) for pattern in contract.forbidden_patterns):
            findings.append(Finding("P4H002", contract.subject, "artifact has a forbidden member"))
    for required in contract.required_members:
        if required not in names:
            findings.append(Finding("P4H001", contract.subject, "artifact lacks a required member"))
    metadata_name, metadata_version = _metadata(payloads)
    if (contract.expected_name is not None and metadata_name != contract.expected_name) or (
        contract.expected_version is not None and metadata_version != contract.expected_version
    ):
        findings.append(Finding("P4H003", contract.subject, "artifact identity is mismatched"))
    return sorted(set(findings))


def import_probe_findings(probe: ImportProbe, repository_root: Path) -> list[Finding]:
    root = repository_root.resolve()
    for raw in (*probe.module_paths, *probe.sys_path):
        if not raw:
            continue
        try:
            path = Path(raw).resolve()
        except OSError:
            continue
        if path == root or root in path.parents:
            return [
                Finding("P4H004", "isolated-import-probe", "repository source masked isolation")
            ]
    return []


def _engine_contract_source() -> str:
    return """from __future__ import annotations

import importlib.util
import json
import sys
from importlib.resources import files

import open_brain_engine
import open_brain_engine.engine as engine
from open_brain_engine.core.ids import canonical_json_bytes


def main() -> None:
    assert open_brain_engine.__version__ == "0.1.0"
    assert engine.__all__
    assert all(hasattr(engine, name) for name in engine.__all__)
    assert canonical_json_bytes({"second": 2, "first": 1}) == b'{"first":1,"second":2}'
    portable = files("open_brain_engine.portable")
    assert portable.joinpath("schemas/v1/common.json").is_file()
    assert portable.joinpath("conformance/v1/cases.json").is_file()
    forbidden = ("open_brain", "open_brain_connectors", "open_brain_legacy")
    assert all(importlib.util.find_spec(name) is None for name in forbidden)
    print(json.dumps({
        "forbidden_available": [],
        "module_paths": [open_brain_engine.__file__, engine.__file__],
        "sys_path": sys.path,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
"""


def _engine_test_runner_source() -> str:
    return """from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path


def main() -> None:
    test_root = Path(sys.argv[1]).resolve()
    engine_site_packages = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(engine_site_packages))
    sys.path.insert(0, str(test_root))
    forbidden = ("open_brain", "open_brain_connectors", "open_brain_legacy")
    assert all(importlib.util.find_spec(name) is None for name in forbidden)
    sys.argv = [
        "pytest",
        "-q",
        "-c",
        str(test_root / "pytest.ini"),
        "--rootdir",
        str(test_root),
        str(test_root / "packages/engine/tests"),
    ]
    runpy.run_module("pytest", run_name="__main__")


if __name__ == "__main__":
    main()
"""


def _copy_engine_test_contract(root: Path, destination: Path) -> None:
    shutil.copytree(
        root / "packages/engine/tests",
        destination / "packages/engine/tests",
    )
    for relative in (
        Path("tests/__init__.py"),
        Path("tests/unit/__init__.py"),
        Path("tests/unit/storage/__init__.py"),
        Path("tests/unit/storage/_factories.py"),
    ):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
    shutil.copy2(root / "tests/conftest.py", destination / "conftest.py")
    (destination / "pytest.ini").write_text(
        "[pytest]\naddopts = --import-mode=importlib\n",
        encoding="utf-8",
    )


def engine_isolation_findings(root: Path, work: Path) -> list[Finding]:
    """Build and execute the engine contract without repository source access."""

    project = root / "packages/engine"
    if not (project / "pyproject.toml").is_file():
        return [Finding("P4H007", "engine-isolation", "engine project is absent")]
    dist = work / "dist"
    environment = work / "venv"
    test_environment = work / "test-venv"
    run_root = work / "run"
    dist.mkdir(parents=True, exist_ok=True)
    run_root.mkdir(parents=True, exist_ok=True)
    try:
        run_checked(build_command(project, dist), cwd=run_root)
    except (OSError, subprocess.SubprocessError):
        return [Finding("P4H007", "engine-isolation", "engine build failed")]
    wheels = sorted(dist.glob("open_brain_engine-*.whl"))
    sdists = sorted(dist.glob("open_brain_engine-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return [Finding("P4H001", "engine-isolation", "engine artifacts are incomplete")]
    wheel = wheels[0]
    findings = artifact_findings(
        wheel,
        ArtifactContract(
            subject="engine-wheel",
            required_members=(
                "open_brain_engine/__init__.py",
                "open_brain_engine/engine/__init__.py",
                "open_brain_engine/portable/schemas/v1/common.json",
                "open_brain_engine/portable/conformance/v1/cases.json",
                "open_brain_engine/portability/schemas/v1/shared-record-envelope.json",
                "open_brain_engine/portability/conformance/v1/cases.json",
            ),
            forbidden_patterns=(
                "open_brain/**",
                "open_brain_connectors/**",
                "open_brain_legacy/**",
                "tests/**",
                "tools/**",
            ),
            expected_name="open-brain-engine",
            expected_version="0.1.0",
        ),
    )
    if findings:
        return findings
    try:
        run_checked(create_environment_command(environment), cwd=run_root)
        python = environment / "bin/python"
        requirements = run_root / "engine-runtime-requirements.txt"
        exported = run_checked(
            export_runtime_requirements_command("open-brain-engine"), cwd=root
        )
        requirements.write_text(exported.stdout, encoding="utf-8")
        run_checked(
            install_runtime_requirements_command(python, requirements),
            cwd=run_root,
        )
        run_checked(install_command(python, [wheel]), cwd=run_root)
        run_checked(create_environment_command(test_environment), cwd=run_root)
        test_python = test_environment / "bin/python"
        requirements = run_root / "test-requirements.txt"
        exported = run_checked(export_test_requirements_command(), cwd=root)
        requirements.write_text(exported.stdout, encoding="utf-8")
        run_checked(
            install_test_requirements_command(test_python, requirements),
            cwd=run_root,
        )
        engine_site_packages = Path(
            run_checked(site_packages_command(python), cwd=run_root).stdout.strip()
        )
        if not engine_site_packages.is_absolute():
            raise OSError("engine site-packages path is invalid")
        test_root = run_root / "test-contract"
        _copy_engine_test_contract(root, test_root)
        test_runner = run_root / "engine_test_runner.py"
        test_runner.write_text(_engine_test_runner_source(), encoding="utf-8")
        run_checked(
            engine_test_command(
                test_python,
                test_runner,
                test_root,
                engine_site_packages,
            ),
            cwd=run_root,
        )
        contract = run_root / "engine_contract.py"
        contract.write_text(_engine_contract_source(), encoding="utf-8")
        completed = run_checked((os.fspath(python), "-I", os.fspath(contract)), cwd=run_root)
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return [Finding("P4H007", "engine-isolation", "installed engine contract failed")]
    module_paths = payload.get("module_paths") if isinstance(payload, dict) else None
    sys_path = payload.get("sys_path") if isinstance(payload, dict) else None
    if not isinstance(module_paths, list) or not all(
        isinstance(item, str) for item in module_paths
    ):
        return [Finding("P4H007", "engine-isolation", "module origin evidence is malformed")]
    if not isinstance(sys_path, list) or not all(isinstance(item, str) for item in sys_path):
        return [Finding("P4H007", "engine-isolation", "interpreter path evidence is malformed")]
    return import_probe_findings(
        ImportProbe(tuple(cast(list[str], module_paths)), tuple(cast(list[str], sys_path))),
        root,
    )


def _legacy_contract_source() -> str:
    return """from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import sys

import open_brain_engine


def main() -> None:
    distribution = importlib.metadata.distribution("open-brain-legacy")
    assert distribution.version == "0.1.0"
    assert tuple(distribution.requires or ()) == ("open-brain-engine==0.1.0",)
    forbidden = ("open_brain", "open_brain_connectors")
    assert all(importlib.util.find_spec(name) is None for name in forbidden)
    modules = set()
    for entry in distribution.files or ():
        parts = list(entry.parts)
        if not parts or parts[0] != "open_brain_legacy" or not parts[-1].endswith(".py"):
            continue
        parts[-1] = parts[-1].removesuffix(".py")
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            modules.add(".".join(parts))
    imported = [importlib.import_module(name) for name in sorted(modules)]
    assert imported
    assert all(importlib.util.find_spec(name) is None for name in forbidden)
    ports = importlib.import_module(
        "open_brain_legacy._compat.open_brain.integrations.ports"
    )
    config_module = importlib.import_module(
        "open_brain_legacy._compat.open_brain.integrations.config"
    )
    metadata = ports.OptionalIntegrationMetadata(
        capability=ports.Capability.FINANCE,
        provider=ports.OptionalProvider.OPENAI,
    )
    config = config_module.IntegrationConfig(
        live_adapters=frozenset({ports.Capability.FINANCE})
    )
    provider_calls = []

    def load_provider(provider):
        provider_calls.append(provider)
        return object()

    outcome = metadata.load(config=config, provider_loader=load_provider)
    assert outcome == ports.IntegrationOutcome.available_for(
        capability=ports.Capability.FINANCE
    )
    assert provider_calls == [ports.OptionalProvider.OPENAI]
    assert metadata.load(config=config).reason is ports.UnavailableReason.OPTIONAL_DEPENDENCY
    representative = importlib.import_module("open_brain_legacy.services.application")
    print(json.dumps({
        "module_paths": [representative.__file__, open_brain_engine.__file__],
        "sys_path": sys.path,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
"""


def _legacy_compat_import_boundary_findings(wheel: Path) -> list[Finding]:
    """Reject imports outside stdlib, engine, and legacy from private compatibility code."""

    findings: list[Finding] = []
    try:
        with zipfile.ZipFile(wheel) as archive:
            sources = {
                name: archive.read(name)
                for name in archive.namelist()
                if name.startswith("open_brain_legacy/_compat/") and name.endswith(".py")
            }
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile):
        return [Finding("P4H007", "legacy-compat", "legacy compatibility source is unreadable")]
    allowed_roots = frozenset(
        {"__future__", "open_brain_engine", "open_brain_legacy", *sys.stdlib_module_names}
    )
    for name, payload in sorted(sources.items()):
        try:
            tree = ast.parse(payload, filename=name)
        except (SyntaxError, UnicodeError):
            findings.append(Finding("P4H007", name, "legacy compatibility source is unreadable"))
            continue
        imported, dynamic_signatures = _dynamic_imports(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        if dynamic_signatures or any(
            module.partition(".")[0] not in allowed_roots for module in imported
        ):
            findings.append(
                Finding("P4H009", name, "legacy compatibility has an undeclared import")
            )
    return sorted(set(findings))


def legacy_isolation_findings(root: Path, work: Path) -> list[Finding]:
    """Build and import legacy with only its declared engine dependency installed."""

    engine_project = root / "packages/engine"
    legacy_project = root / "packages/legacy"
    if not (legacy_project / "src/open_brain_legacy").is_dir():
        return [Finding("P4H007", "legacy-isolation", "legacy project is absent")]
    engine_dist = work / "engine-dist"
    legacy_dist = work / "legacy-dist"
    environment = work / "venv"
    run_root = work / "run"
    for path in (engine_dist, legacy_dist, run_root):
        path.mkdir(parents=True, exist_ok=True)
    try:
        run_checked(build_command(engine_project, engine_dist), cwd=run_root)
        run_checked(build_command(legacy_project, legacy_dist), cwd=run_root)
    except (OSError, subprocess.SubprocessError):
        return [Finding("P4H007", "legacy-isolation", "legacy or engine build failed")]
    engine_wheels = sorted(engine_dist.glob("open_brain_engine-*.whl"))
    legacy_wheels = sorted(legacy_dist.glob("open_brain_legacy-*.whl"))
    legacy_sdists = sorted(legacy_dist.glob("open_brain_legacy-*.tar.gz"))
    if len(engine_wheels) != 1 or len(legacy_wheels) != 1 or len(legacy_sdists) != 1:
        return [Finding("P4H001", "legacy-isolation", "legacy artifacts are incomplete")]
    legacy_wheel = legacy_wheels[0]
    findings = artifact_findings(
        legacy_wheel,
        ArtifactContract(
            subject="legacy-wheel",
            required_members=(
                "open_brain_legacy/services/application.py",
                "open_brain_legacy/_compat/open_brain/config.py",
                "open_brain_legacy/_compat/open_brain_connectors/capture/media.py",
            ),
            forbidden_patterns=(
                "open_brain/**",
                "open_brain_connectors/**",
                "open_brain_engine/**",
                "tests/**",
                "tools/**",
            ),
            expected_name="open-brain-legacy",
            expected_version="0.1.0",
        ),
    )
    if findings:
        return findings
    findings = _legacy_compat_import_boundary_findings(legacy_wheel)
    if findings:
        return findings
    stage = "create product environment"
    try:
        run_checked(create_environment_command(environment), cwd=run_root)
        python = environment / "bin/python"
        requirements = run_root / "engine-runtime-requirements.txt"
        exported = run_checked(
            export_runtime_requirements_command("open-brain-engine"), cwd=root
        )
        requirements.write_text(exported.stdout, encoding="utf-8")
        run_checked(
            install_runtime_requirements_command(python, requirements),
            cwd=run_root,
        )
        stage = "install product wheels"
        run_checked(
            install_command(python, [engine_wheels[0], legacy_wheel]),
            cwd=run_root,
        )
        contract = run_root / "legacy_contract.py"
        contract.write_text(_legacy_contract_source(), encoding="utf-8")
        stage = "run installed legacy contract"
        completed = run_checked((os.fspath(python), "-I", os.fspath(contract)), cwd=run_root)
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return [
            Finding(
                "P4H007",
                "legacy-isolation",
                f"installed legacy contract failed at {stage}",
            )
        ]
    module_paths = payload.get("module_paths") if isinstance(payload, dict) else None
    sys_path = payload.get("sys_path") if isinstance(payload, dict) else None
    if not isinstance(module_paths, list) or not all(
        isinstance(item, str) for item in module_paths
    ):
        return [Finding("P4H007", "legacy-isolation", "module origin evidence is malformed")]
    if not isinstance(sys_path, list) or not all(isinstance(item, str) for item in sys_path):
        return [Finding("P4H007", "legacy-isolation", "interpreter path evidence is malformed")]
    return import_probe_findings(
        ImportProbe(tuple(cast(list[str], module_paths)), tuple(cast(list[str], sys_path))),
        root,
    )


def _app_contract_source() -> str:
    return """from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

import open_brain.services.appliance_entrypoints as entrypoints
import open_brain.services.appliance_lifecycle as lifecycle
import open_brain_engine


def main() -> None:
    distribution = importlib.metadata.distribution("open-brain")
    assert distribution.version == "0.1.0"
    requirements = tuple(distribution.requires or ())
    assert "open-brain-engine[node]==0.1.0" in requirements
    assert "starlette<1,>=0.48" in requirements
    assert "uvicorn<1,>=0.40" in requirements
    assert not any(
        requirement.casefold().startswith(("open-brain-connectors", "open-brain-legacy"))
        for requirement in requirements
    )
    scripts = {
        item.name: item.value
        for item in distribution.entry_points
        if item.group == "console_scripts"
    }
    assert scripts == {
        "open-brain": "open_brain.services.appliance_entrypoints:run_cli",
        "open-brain-mcp": "open_brain.services.appliance_entrypoints:run_mcp",
    }
    assert callable(entrypoints.run_cli)
    assert callable(entrypoints.run_http)
    assert callable(entrypoints.run_mcp)
    resources = files("open_brain").joinpath("resources/supervisors")
    assert resources.joinpath("launchd.json").is_file()
    assert resources.joinpath("systemd.service").is_file()
    with tempfile.TemporaryDirectory() as temporary_root:
        rendered = lifecycle._supervisor(Path(temporary_root).resolve()).render()
    assert "PYTHONPATH" not in rendered
    assert "WorkingDirectory" not in rendered
    forbidden = ("open_brain_connectors", "open_brain_legacy")
    assert all(importlib.util.find_spec(name) is None for name in forbidden)
    print(json.dumps({
        "module_paths": [entrypoints.__file__, open_brain_engine.__file__],
        "sys_path": sys.path,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
"""


def _app_test_runner_source() -> str:
    return """from __future__ import annotations

import importlib.util
import os
import runpy
import sys
from pathlib import Path


def main() -> None:
    test_root = Path(sys.argv[1]).resolve()
    product_site_packages = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(product_site_packages))
    sys.path.insert(0, str(test_root))
    os.environ["PYTHONPATH"] = str(product_site_packages)
    assert importlib.util.find_spec("open_brain") is not None
    assert importlib.util.find_spec("open_brain_engine") is not None
    forbidden = ("open_brain_connectors", "open_brain_legacy")
    assert all(importlib.util.find_spec(name) is None for name in forbidden)
    sys.argv = [
        "pytest",
        "-q",
        "-c",
        str(test_root / "pytest.ini"),
        "--rootdir",
        str(test_root),
        str(test_root / "packages/app/tests"),
    ]
    runpy.run_module("pytest", run_name="__main__")


if __name__ == "__main__":
    main()
"""


def _copy_app_test_contract(root: Path, destination: Path) -> None:
    shutil.copytree(
        root / "packages/app/tests",
        destination / "packages/app/tests",
    )
    shutil.copy2(root / "tests/conftest.py", destination / "conftest.py")
    (destination / "pytest.ini").write_text(
        "[pytest]\naddopts = --import-mode=importlib\n",
        encoding="utf-8",
    )


def _engine_module_sets(root: Path) -> tuple[frozenset[str], frozenset[str]]:
    value = json.loads(
        (root / "docs/v0-package-classification.json").read_text(encoding="utf-8")
    )
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        raise ValueError("canonical manifest files are invalid")
    records = cast(dict[str, object], value["files"])
    modules: set[str] = set()
    public_modules: set[str] = set()
    prefix = PurePosixPath("packages/engine/src")
    for raw_record in records.values():
        if not isinstance(raw_record, dict):
            raise ValueError("canonical manifest record is invalid")
        record = cast(dict[str, object], raw_record)
        if record.get("target_distribution") != "engine":
            continue
        api_status = record.get("api_status")
        if api_status not in {"public", "distribution-private"}:
            raise ValueError("canonical manifest engine API status is invalid")
        target_value = record.get("target_path")
        if not isinstance(target_value, str):
            raise ValueError("canonical manifest engine path is invalid")
        target = PurePosixPath(target_value)
        if target.is_absolute() or ".." in target.parts or target.as_posix() != target_value:
            raise ValueError("canonical manifest engine path is invalid")
        if target.suffix != ".py" or tuple(target.parts[: len(prefix.parts)]) != prefix.parts:
            continue
        relative = PurePosixPath(*target.parts[len(prefix.parts) :])
        parts = (
            relative.parts[:-1]
            if relative.name == "__init__.py"
            else (*relative.parts[:-1], relative.stem)
        )
        if parts:
            module = ".".join(parts)
            modules.add(module)
            if api_status == "public":
                public_modules.add(module)
    if "open_brain_engine" not in public_modules or not public_modules <= modules:
        raise ValueError("canonical manifest public engine API is empty")
    return frozenset(modules), frozenset(public_modules)


def _declared_import_roots(payloads: Mapping[str, bytes]) -> frozenset[str]:
    if len(payloads) != 1:
        raise ValueError("app wheel metadata is invalid")
    roots = {"open_brain"}
    metadata = next(iter(payloads.values())).decode("utf-8")
    for line in metadata.splitlines():
        if not line.startswith("Requires-Dist: "):
            continue
        requirement = line.removeprefix("Requires-Dist: ")
        match = _DISTRIBUTION_NAME.match(requirement)
        if match is None:
            raise ValueError("app wheel requirement is invalid")
        roots.add(re.sub(r"[-.]+", "_", match.group().casefold()))
    return frozenset(roots)


class _ScopeNameCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        self.names.update(alias.asname or alias.name.partition(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        self.names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:  # noqa: N802
        self.nonlocal_names.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.name is not None:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:  # noqa: N802
        if node.name is not None:
            self.names.add(node.name)
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:  # noqa: N802
        if node.name is not None:
            self.names.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:  # noqa: N802
        if node.rest is not None:
            self.names.add(node.rest)
        self.generic_visit(node)

    def _visit_comprehension(
        self,
        generators: Sequence[ast.comprehension],
        values: Sequence[ast.expr],
    ) -> None:
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)

    def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.elt,))


@dataclass(slots=True)
class _ImportScope:
    kind: str
    parent: int | None
    local_names: frozenset[str]
    global_names: frozenset[str]
    nonlocal_names: frozenset[str]
    bindings: dict[str, frozenset[str]]


def _argument_names(arguments: ast.arguments) -> set[str]:
    names = {
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(element) for element in target.elts))
    return set()


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


class _DynamicImportAnalyzer(ast.NodeVisitor):
    """Detect the finite reviewed P4H009 corpus; this is not a Python sandbox."""

    def __init__(self) -> None:
        self.imported: set[str] = set()
        self.events: list[str] = []
        self._scopes = [
            _ImportScope(
                kind="module",
                parent=None,
                local_names=frozenset(),
                global_names=frozenset(),
                nonlocal_names=frozenset(),
                bindings={
                    "__builtins__": frozenset({_BUILTINS_MODULE}),
                    "__import__": frozenset({_BUILTIN_IMPORT_CALLABLE}),
                    "getattr": frozenset({_GETATTR_CALLABLE}),
                    "globals": frozenset({_GLOBALS_CALLABLE}),
                    "locals": frozenset({_LOCALS_CALLABLE}),
                    "vars": frozenset({_VARS_CALLABLE}),
                    "eval": frozenset({_EVAL_CALLABLE}),
                    "exec": frozenset({_EXEC_CALLABLE}),
                    "object": frozenset({_OBJECT_TYPE}),
                    "type": frozenset({_TYPE_CALLABLE}),
                },
            )
        ]
        self._scope_path: list[str] = []

    @property
    def _scope_index(self) -> int:
        return len(self._scopes) - 1

    def _scope_name(self) -> str:
        return ".".join(self._scope_path) if self._scope_path else "<module>"

    def _record(self, event: str) -> None:
        self.events.append(f"{self._scope_name()}:{event}")

    def _record_capabilities(self, prefix: str, provenance: frozenset[str]) -> None:
        for capability in sorted(provenance & _DYNAMIC_IMPORT_CAPABILITIES):
            self._record(f"{prefix}={capability}")

    def _resolve_from(self, scope_index: int, name: str) -> frozenset[str]:
        scope = self._scopes[scope_index]
        if name in scope.global_names:
            return self._scopes[0].bindings.get(name, frozenset())
        if name in scope.nonlocal_names:
            return (
                self._resolve_from(scope.parent, name)
                if scope.parent is not None
                else frozenset()
            )
        if name in scope.bindings:
            return scope.bindings[name]
        if name in scope.local_names:
            return frozenset()
        return (
            self._resolve_from(scope.parent, name)
            if scope.parent is not None
            else frozenset()
        )

    def _resolve(self, name: str) -> frozenset[str]:
        return self._resolve_from(self._scope_index, name)

    def _binding_scope(self, name: str, scope_index: int | None = None) -> int:
        selected = self._scope_index if scope_index is None else scope_index
        scope = self._scopes[selected]
        if name in scope.global_names:
            return 0
        if name in scope.nonlocal_names:
            parent = scope.parent
            while parent is not None:
                candidate = self._scopes[parent]
                if name in candidate.local_names or name in candidate.bindings:
                    return parent
                parent = candidate.parent
        return selected

    def _bind(
        self,
        name: str,
        provenance: frozenset[str],
        *,
        review: bool = True,
        scope_index: int | None = None,
    ) -> None:
        target = self._binding_scope(name, scope_index)
        self._scopes[target].bindings[name] = provenance
        if review:
            self._record_capabilities(f"bind:{name}", provenance)

    @staticmethod
    def _module_binding(module: str) -> frozenset[str]:
        normalized = module.removesuffix(".__init__")
        if normalized == "importlib":
            return frozenset({_IMPORTLIB_MODULE})
        if normalized == "importlib.util":
            return frozenset({_IMPORTLIB_UTIL_MODULE})
        if normalized.startswith("importlib.") and normalized not in {
            "importlib.metadata",
            "importlib.resources",
        }:
            return frozenset({_IMPORTLIB_MODULE})
        if normalized == "pkgutil" or normalized.startswith("pkgutil."):
            return frozenset({_PKGUTIL_MODULE})
        if normalized == "builtins":
            return frozenset({_BUILTINS_MODULE})
        if normalized == "sys":
            return frozenset({_SYS_MODULE})
        return frozenset()

    @staticmethod
    def _member_binding(owner: frozenset[str], member: str) -> frozenset[str]:
        provenance: set[str] = set()
        if _IMPORTLIB_MODULE in owner and member == "import_module":
            provenance.add(_IMPORT_MODULE_CALLABLE)
        elif _IMPORTLIB_MODULE in owner and member == "__dict__":
            provenance.add(_IMPORTLIB_NAMESPACE)
        elif _IMPORTLIB_MODULE in owner and member == "__getattribute__":
            provenance.add(_IMPORTLIB_GETATTRIBUTE_CALLABLE)
        elif _IMPORTLIB_MODULE in owner and member == "util":
            provenance.add(_IMPORTLIB_UTIL_MODULE)
        elif _IMPORTLIB_MODULE in owner and member not in {"metadata", "resources"}:
            provenance.add(_IMPORTLIB_MODULE)
        if _PKGUTIL_MODULE in owner and member == "resolve_name":
            provenance.add(_RESOLVE_NAME_CALLABLE)
        elif _PKGUTIL_MODULE in owner:
            provenance.add(_PKGUTIL_MODULE)
        if owner & {_BUILTINS_MODULE, _BUILTINS_NAMESPACE}:
            binding = _BUILTINS_MEMBERS.get(member)
            if binding is not None:
                provenance.add(binding)
        if _SYS_MODULE in owner and member == "modules":
            provenance.add(_SYS_MODULES)
        if _SYS_MODULE in owner and member == "__dict__":
            provenance.add(_SYS_NAMESPACE)
        if _SYS_MODULE in owner and member == "__getattribute__":
            provenance.add(_SYS_GETATTRIBUTE_CALLABLE)
        if _SYS_MODULE in owner and member == "_getframe":
            provenance.add(_SYS_GETFRAME_CALLABLE)
        if _FRAME_OBJECT in owner and member in {"f_globals", "f_locals"}:
            provenance.add(_NAMESPACE_MAPPING)
        if _FRAME_OBJECT in owner and member == "f_builtins":
            provenance.add(_BUILTINS_NAMESPACE)
        if _FUNCTION_OBJECT in owner and member == "__getattribute__":
            provenance.add(_FUNCTION_GETATTRIBUTE_CALLABLE)
        if _FUNCTION_OBJECT in owner and member == "__class__":
            provenance.add(_TYPE_OBJECT)
        if _OBJECT_TYPE in owner and member == "__getattribute__":
            provenance.add(_OBJECT_GETATTRIBUTE_CALLABLE)
        if _TYPE_OBJECT in owner and member == "__getattribute__":
            provenance.add(_OBJECT_GETATTRIBUTE_CALLABLE)
        if _SYS_NAMESPACE in owner and member == "modules":
            provenance.add(_SYS_MODULES)
        if _SYS_MODULES in owner and member == "builtins":
            provenance.add(_BUILTINS_MODULE)
        if _SYS_MODULES in owner and member == "importlib":
            provenance.add(_IMPORTLIB_MODULE)
        if _NAMESPACE_MAPPING in owner and member == "__builtins__":
            provenance.add(_BUILTINS_MODULE)
        if _IMPORTLIB_NAMESPACE in owner and member == "import_module":
            provenance.add(_IMPORT_MODULE_CALLABLE)
        if member == "__globals__":
            provenance.add(_NAMESPACE_MAPPING)
        return frozenset(provenance)

    def _binding(self, node: ast.AST) -> frozenset[str]:
        if isinstance(node, ast.Name):
            return self._resolve(node.id)
        if isinstance(node, ast.Lambda):
            return frozenset({_FUNCTION_OBJECT})
        if isinstance(node, ast.NamedExpr):
            return self._binding(node.value)
        if isinstance(node, ast.Attribute):
            return self._member_binding(self._binding(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            member = _constant_string(node.slice)
            if member is not None:
                return self._member_binding(self._binding(node.value), member)
        if isinstance(node, ast.Call):
            function = self._binding(node.func)
            if _GETATTR_CALLABLE in function and len(node.args) == 2 and not node.keywords:
                member = _constant_string(node.args[1])
                if member is not None:
                    return self._member_binding(self._binding(node.args[0]), member)
            if (
                _SYS_GETATTRIBUTE_CALLABLE in function
                and len(node.args) == 1
                and not node.keywords
            ):
                member = _constant_string(node.args[0])
                if member is not None:
                    return self._member_binding(frozenset({_SYS_MODULE}), member)
            if (
                _IMPORTLIB_GETATTRIBUTE_CALLABLE in function
                and len(node.args) == 1
                and not node.keywords
            ):
                member = _constant_string(node.args[0])
                if member is not None:
                    return self._member_binding(frozenset({_IMPORTLIB_MODULE}), member)
            if (
                _BUILTINS_GETATTRIBUTE_CALLABLE in function
                and len(node.args) == 1
                and not node.keywords
            ):
                member = _constant_string(node.args[0])
                if member is not None:
                    return self._member_binding(frozenset({_BUILTINS_MODULE}), member)
            if (
                _FUNCTION_GETATTRIBUTE_CALLABLE in function
                and len(node.args) == 1
                and not node.keywords
            ):
                member = _constant_string(node.args[0])
                if member is not None:
                    return self._member_binding(frozenset({_FUNCTION_OBJECT}), member)
            if (
                _OBJECT_GETATTRIBUTE_CALLABLE in function
                and len(node.args) == 2
                and not node.keywords
            ):
                member = _constant_string(node.args[1])
                if member is not None:
                    return self._member_binding(self._binding(node.args[0]), member)
            provenance: set[str] = set()
            if _TYPE_CALLABLE in function:
                provenance.add(_TYPE_OBJECT)
            if _SYS_GETFRAME_CALLABLE in function:
                provenance.add(_FRAME_OBJECT)
            if function & {_GLOBALS_CALLABLE, _LOCALS_CALLABLE}:
                provenance.add(_NAMESPACE_MAPPING)
            if _VARS_CALLABLE in function:
                if not node.args:
                    provenance.add(_NAMESPACE_MAPPING)
                elif len(node.args) == 1:
                    owner = self._binding(node.args[0])
                    if _BUILTINS_MODULE in owner:
                        provenance.add(_BUILTINS_NAMESPACE)
                    if _IMPORTLIB_MODULE in owner:
                        provenance.add(_IMPORTLIB_NAMESPACE)
                    if _SYS_MODULE in owner:
                        provenance.add(_SYS_NAMESPACE)
            return frozenset(provenance)
        if isinstance(node, ast.IfExp):
            return self._binding(node.body) | self._binding(node.orelse)
        if isinstance(node, ast.BoolOp):
            return frozenset().union(*(self._binding(value) for value in node.values))
        return frozenset()

    def _snapshot(self) -> tuple[dict[str, frozenset[str]], ...]:
        return tuple(dict(scope.bindings) for scope in self._scopes)

    def _restore(self, snapshot: tuple[dict[str, frozenset[str]], ...]) -> None:
        if len(snapshot) != len(self._scopes):
            raise RuntimeError("dynamic import scope snapshot is invalid")
        for scope, bindings in zip(self._scopes, snapshot, strict=True):
            scope.bindings = dict(bindings)

    @staticmethod
    def _joined(
        snapshots: Sequence[tuple[dict[str, frozenset[str]], ...]],
    ) -> tuple[dict[str, frozenset[str]], ...]:
        if not snapshots or len({len(snapshot) for snapshot in snapshots}) != 1:
            raise RuntimeError("dynamic import branch snapshots are invalid")
        joined: list[dict[str, frozenset[str]]] = []
        for index in range(len(snapshots[0])):
            names = set().union(*(snapshot[index] for snapshot in snapshots))
            joined.append(
                {
                    name: frozenset().union(
                        *(snapshot[index].get(name, frozenset()) for snapshot in snapshots)
                    )
                    for name in names
                }
            )
        return tuple(joined)

    def _branch(
        self,
        statements: Sequence[ast.stmt],
        start: tuple[dict[str, frozenset[str]], ...],
    ) -> tuple[dict[str, frozenset[str]], ...]:
        self._restore(start)
        for statement in statements:
            self.visit(statement)
        return self._snapshot()

    def _function_parent(self) -> int:
        parent = self._scope_index
        while self._scopes[parent].kind == "class":
            enclosing = self._scopes[parent].parent
            if enclosing is None:
                return 0
            parent = enclosing
        return parent

    def _push_scope(
        self,
        *,
        kind: str,
        parent: int,
        names: set[str],
        global_names: set[str],
        nonlocal_names: set[str],
        label: str,
    ) -> None:
        names.difference_update(global_names | nonlocal_names)
        self._scopes.append(
            _ImportScope(
                kind=kind,
                parent=parent,
                local_names=frozenset(names),
                global_names=frozenset(global_names),
                nonlocal_names=frozenset(nonlocal_names),
                bindings={name: frozenset() for name in names},
            )
        )
        self._scope_path.append(label)

    def _pop_scope(self) -> None:
        self._scope_path.pop()
        self._scopes.pop()

    def _bind_target(self, target: ast.expr, provenance: frozenset[str]) -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id, provenance)
            return
        if isinstance(target, ast.Starred):
            self._bind_target(target.value, provenance)
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind_target(element, frozenset())
            if provenance & _DYNAMIC_IMPORT_CAPABILITIES:
                self._record("escape:dynamic-importer")
            return
        if provenance & _DYNAMIC_IMPORT_CAPABILITIES:
            self._record("escape:dynamic-importer")

    def _bind_assignment(self, target: ast.expr, value: ast.expr) -> None:
        if (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(value, (ast.Tuple, ast.List))
            and len(target.elts) == len(value.elts)
        ):
            for child_target, child_value in zip(target.elts, value.elts, strict=True):
                self._bind_assignment(child_target, child_value)
            return
        self._bind_target(target, self._binding(value))

    def _function_scope(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> tuple[set[str], set[str], set[str]]:
        collector = _ScopeNameCollector()
        for statement in node.body:
            collector.visit(statement)
        collector.names.update(_argument_names(node.args))
        return collector.names, collector.global_names, collector.nonlocal_names

    def _visit_function_annotations(self, arguments: ast.arguments) -> None:
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if arguments.vararg is not None and arguments.vararg.annotation is not None:
            self.visit(arguments.vararg.annotation)
        if arguments.kwarg is not None and arguments.kwarg.annotation is not None:
            self.visit(arguments.kwarg.annotation)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        self._visit_function_annotations(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self._bind(node.name, frozenset({_FUNCTION_OBJECT}), review=False)
        outer = self._snapshot()
        local_names, global_names, nonlocal_names = self._function_scope(node)
        self._push_scope(
            kind="function",
            parent=self._function_parent(),
            names=local_names,
            global_names=global_names,
            nonlocal_names=nonlocal_names,
            label=node.name,
        )
        for statement in node.body:
            self.visit(statement)
        self._pop_scope()
        self._restore(outer)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        for expression in (*node.decorator_list, *node.bases):
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        outer = self._snapshot()
        self._push_scope(
            kind="class",
            parent=self._scope_index,
            names=set(),
            global_names=set(),
            nonlocal_names=set(),
            label=node.name,
        )
        for statement in node.body:
            self.visit(statement)
        self._pop_scope()
        self._restore(outer)
        self._bind(node.name, frozenset(), review=False)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        self._visit_function_annotations(node.args)
        outer = self._snapshot()
        self._push_scope(
            kind="function",
            parent=self._function_parent(),
            names=_argument_names(node.args),
            global_names=set(),
            nonlocal_names=set(),
            label=f"<lambda>@{node.lineno}",
        )
        self.visit(node.body)
        self._pop_scope()
        self._restore(outer)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            name = alias.asname or alias.name.partition(".")[0]
            binding = self._module_binding(alias.name)
            if binding:
                self._bind(name, binding, review=binding != frozenset({_SYS_MODULE}))
            else:
                self._bind(name, frozenset(), review=False)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        for alias in node.names:
            if alias.name == "*":
                if (
                    node.level == 0
                    and node.module is not None
                    and self._module_binding(node.module)
                    & {_BUILTINS_MODULE, _IMPORTLIB_MODULE, _PKGUTIL_MODULE}
                ):
                    self._record("escape:dynamic-importer")
                continue
            name = alias.asname or alias.name
            owner = (
                self._module_binding(node.module)
                if node.level == 0 and node.module is not None
                else frozenset()
            )
            if owner:
                self._bind(name, self._member_binding(owner, alias.name))
            else:
                self._bind(name, frozenset(), review=False)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        self.visit(node.value)
        for target in node.targets:
            self._bind_assignment(target, node.value)
            if isinstance(target, (ast.Attribute, ast.Subscript)):
                self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
            self._bind_assignment(node.target, node.value)
        else:
            self._bind_target(node.target, frozenset())

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:  # noqa: N802
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            original_scope = self._scope_index
            target_scope = self._scope_index
            while self._scopes[target_scope].kind == "comprehension":
                parent = self._scopes[target_scope].parent
                if parent is None:
                    break
                target_scope = parent
            provenance = self._binding(node.value)
            if target_scope != original_scope:
                provenance |= self._resolve_from(target_scope, node.target.id)
            self._bind(node.target.id, provenance, scope_index=target_scope)
        else:
            self._bind_assignment(node.target, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        self.visit(node.target)
        self.visit(node.value)
        self._bind_target(node.target, frozenset())

    def visit_Delete(self, node: ast.Delete) -> None:  # noqa: N802
        for target in node.targets:
            self._bind_target(target, frozenset())

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        self.visit(node.test)
        start = self._snapshot()
        body = self._branch(node.body, start)
        alternative = self._branch(node.orelse, start) if node.orelse else start
        self._restore(self._joined((body, alternative)))

    def _visit_loop(
        self,
        body: Sequence[ast.stmt],
        orelse: Sequence[ast.stmt],
        target: ast.expr | None,
    ) -> None:
        start = self._snapshot()
        self._restore(start)
        if target is not None:
            self._bind_target(target, frozenset())
        for statement in body:
            self.visit(statement)
        body_state = self._snapshot()
        loop_state = self._joined((start, body_state))
        alternative = self._branch(orelse, loop_state) if orelse else loop_state
        self._restore(self._joined((loop_state, alternative)))

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        self.visit(node.iter)
        self._visit_loop(node.body, node.orelse, node.target)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802
        self.visit(node.iter)
        self._visit_loop(node.body, node.orelse, node.target)

    def visit_While(self, node: ast.While) -> None:  # noqa: N802
        self.visit(node.test)
        self._visit_loop(node.body, node.orelse, None)

    def _visit_with(
        self,
        items: Sequence[ast.withitem],
        body: Sequence[ast.stmt],
    ) -> None:
        for item in items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars, frozenset())
        for statement in body:
            self.visit(statement)

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        self._visit_with(node.items, node.body)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        self._visit_with(node.items, node.body)

    def _visit_try(
        self,
        body: Sequence[ast.stmt],
        handlers: Sequence[ast.ExceptHandler],
        orelse: Sequence[ast.stmt],
        finalbody: Sequence[ast.stmt],
    ) -> None:
        start = self._snapshot()
        normal = self._branch(body, start)
        if orelse:
            normal = self._branch(orelse, normal)
        outcomes = [start, normal]
        for handler in handlers:
            self._restore(start)
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name is not None:
                self._bind(handler.name, frozenset(), review=False)
            for statement in handler.body:
                self.visit(statement)
            if handler.name is not None:
                self._bind(handler.name, frozenset(), review=False)
            outcomes.append(self._snapshot())
        self._restore(self._joined(outcomes))
        for statement in finalbody:
            self.visit(statement)

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
        self._visit_try(node.body, node.handlers, node.orelse, node.finalbody)

    def visit_TryStar(self, node: ast.TryStar) -> None:  # noqa: N802
        self._visit_try(node.body, node.handlers, node.orelse, node.finalbody)

    def visit_Match(self, node: ast.Match) -> None:  # noqa: N802
        self.visit(node.subject)
        start = self._snapshot()
        outcomes = [start]
        for case in node.cases:
            self._restore(start)
            collector = _ScopeNameCollector()
            collector.visit(case.pattern)
            for name in collector.names:
                self._bind(name, frozenset(), review=False)
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)
            outcomes.append(self._snapshot())
        self._restore(self._joined(outcomes))

    def _visit_comprehension(
        self,
        generators: Sequence[ast.comprehension],
        values: Sequence[ast.expr],
        lineno: int,
    ) -> None:
        if not generators:
            return
        self.visit(generators[0].iter)
        names = set().union(*(_target_names(generator.target) for generator in generators))
        self._push_scope(
            kind="comprehension",
            parent=self._function_parent(),
            names=names,
            global_names=set(),
            nonlocal_names=set(),
            label=f"<comprehension>@{lineno}",
        )
        for index, generator in enumerate(generators):
            if index:
                self.visit(generator.iter)
            self._bind_target(generator.target, frozenset())
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)
        self._pop_scope()

    def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.elt,), node.lineno)

    def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.elt,), node.lineno)

    def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.key, node.value), node.lineno)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.elt,), node.lineno)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self._record_capabilities(f"reference:{node.id}", self._resolve(node.id))

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        binding = self._binding(node)
        owner = self._binding(node.value)
        self._record_capabilities("reference:attribute", binding)
        if _SYS_MODULES in owner and not binding:
            self._record("reference:attribute=sys.modules")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        binding = self._binding(node)
        owner = self._binding(node.value)
        self._record_capabilities("reference:subscript", binding)
        if owner & {
            _SYS_MODULES,
            _SYS_NAMESPACE,
            _NAMESPACE_MAPPING,
            _BUILTINS_NAMESPACE,
            _IMPORTLIB_NAMESPACE,
        } and not binding:
            self._record("reference:subscript=dynamic-namespace")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        binding = self._binding(node.func)
        importers = sorted(binding & _DYNAMIC_IMPORT_CALLABLES)
        target_node = node.args[0] if node.args else next(
            (keyword.value for keyword in node.keywords if keyword.arg == "name"),
            None,
        )
        target = _constant_string(target_node) if target_node is not None else None
        if target is not None and importers:
            for importer in importers:
                self.imported.add(
                    target.partition(":")[0]
                    if importer == _RESOLVE_NAME_CALLABLE
                    else target
                )
        for importer in importers:
            if len(node.args) == 1 and not node.keywords and isinstance(node.args[0], ast.Name):
                argument = node.args[0].id
            elif target is not None:
                argument = "<literal>"
            else:
                argument = "<unresolved>"
            self._record(f"call:{importer}({argument})")
        for callable_name in sorted(binding & _UNSAFE_REFLECTION_CALLABLES):
            self._record(f"call:{callable_name}")
        if _GETATTR_CALLABLE in binding and len(node.args) >= 2:
            owner = self._binding(node.args[0])
            if owner & {
                _IMPORTLIB_MODULE,
                _BUILTINS_MODULE,
                _SYS_MODULE,
                _SYS_MODULES,
                _SYS_NAMESPACE,
                _NAMESPACE_MAPPING,
                _BUILTINS_NAMESPACE,
                _IMPORTLIB_NAMESPACE,
                _FUNCTION_OBJECT,
            } and _constant_string(node.args[1]) is None:
                self._record("reflect:dynamic-importer=<unresolved>")
        if not importers:
            self.visit(node.func)
        for child in node.args:
            self.visit(child)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
        if node.value is not None:
            self.visit(node.value)

    def visit_Yield(self, node: ast.Yield) -> None:  # noqa: N802
        if node.value is not None:
            self.visit(node.value)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:  # noqa: N802
        self.visit(node.value)


def _dynamic_imports(tree: ast.AST) -> tuple[set[str], tuple[str, ...]]:
    analyzer = _DynamicImportAnalyzer()
    analyzer.visit(tree)
    return analyzer.imported, tuple(sorted(analyzer.events))


def _app_import_boundary_findings(
    wheel: Path,
    engine_modules: frozenset[str],
    public_modules: frozenset[str],
) -> list[Finding]:
    findings: list[Finding] = []
    try:
        with zipfile.ZipFile(wheel) as archive:
            sources = {
                name: archive.read(name)
                for name in archive.namelist()
                if name.startswith("open_brain/") and name.endswith(".py")
            }
            metadata = {
                name: archive.read(name)
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            }
        declared_roots = _declared_import_roots(metadata)
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile):
        return [Finding("P4H007", "app-wheel", "app source inspection failed")]
    for name, payload in sorted(sources.items()):
        try:
            tree = ast.parse(payload, filename=name)
        except (SyntaxError, UnicodeError):
            findings.append(Finding("P4H007", name, "app source inspection failed"))
            continue
        imported: set[str] = set()
        dynamic_imported, dynamic_signatures = _dynamic_imports(tree)
        imported.update(dynamic_imported)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
                imported.add(node.module)
                if node.module == "open_brain_engine" or node.module.startswith(
                    "open_brain_engine."
                ):
                    imported.update(
                        candidate
                        for alias in node.names
                        if alias.name != "*"
                        and (candidate := f"{node.module}.{alias.name}") in engine_modules
                    )
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        if dynamic_signatures:
            findings.append(
                Finding("P4H009", name, "app artifact has an undeclared or unreviewed import")
            )
        if any(
            (module == "open_brain_engine" or module.startswith("open_brain_engine."))
            and module not in public_modules
            for module in imported
        ):
            findings.append(
                Finding("P4H008", name, "app artifact imports a private engine module")
            )
        if any(
            (root := module.partition(".")[0]) in _FORBIDDEN_APP_IMPORT_ROOTS
            or (
                root not in declared_roots
                and root not in sys.stdlib_module_names
                and root != "__future__"
            )
            for module in imported
        ):
            findings.append(
                Finding("P4H009", name, "app artifact has an undeclared or unreviewed import")
            )
    return sorted(set(findings))


def app_isolation_findings(root: Path, work: Path) -> list[Finding]:
    """Build and execute the app contract with only app and engine wheels available."""

    engine_project = root / "packages/engine"
    app_project = root / "packages/app"
    if not (app_project / "src/open_brain").is_dir():
        return [Finding("P4H007", "app-isolation", "app project is absent")]
    engine_dist = work / "engine-dist"
    app_dist = work / "app-dist"
    environment = work / "venv"
    test_environment = work / "test-venv"
    run_root = work / "run"
    for path in (engine_dist, app_dist, run_root):
        path.mkdir(parents=True, exist_ok=True)
    try:
        run_checked(build_command(engine_project, engine_dist), cwd=run_root)
        run_checked(build_command(app_project, app_dist), cwd=run_root)
    except (OSError, subprocess.SubprocessError):
        return [Finding("P4H007", "app-isolation", "app or engine build failed")]
    engine_wheels = sorted(engine_dist.glob("open_brain_engine-*.whl"))
    app_wheels = sorted(app_dist.glob("open_brain-*.whl"))
    app_sdists = sorted(app_dist.glob("open_brain-*.tar.gz"))
    if len(engine_wheels) != 1 or len(app_wheels) != 1 or len(app_sdists) != 1:
        return [Finding("P4H001", "app-isolation", "app artifacts are incomplete")]
    app_wheel = app_wheels[0]
    findings = artifact_findings(
        app_wheel,
        ArtifactContract(
            subject="app-wheel",
            required_members=(
                "open_brain/services/appliance_entrypoints.py",
                "open_brain/resources/supervisors/launchd.json",
                "open_brain/resources/supervisors/systemd.service",
            ),
            forbidden_patterns=(
                "open_brain_engine/**",
                "open_brain_connectors/**",
                "open_brain_legacy/**",
                "tests/**",
                "tools/**",
            ),
            expected_name="open-brain",
            expected_version="0.1.0",
        ),
    )
    try:
        engine_modules, public_engine_modules = _engine_module_sets(root)
    except (OSError, ValueError, json.JSONDecodeError):
        return [Finding("P4H007", "app-isolation", "public engine API is unreadable")]
    findings.extend(
        _app_import_boundary_findings(app_wheel, engine_modules, public_engine_modules)
    )
    if findings:
        return sorted(set(findings))
    stage = "create product environment"
    try:
        run_checked(create_environment_command(environment), cwd=run_root)
        python = environment / "bin/python"
        requirements = run_root / "app-runtime-requirements.txt"
        exported = run_checked(export_runtime_requirements_command("open-brain"), cwd=root)
        requirements.write_text(exported.stdout, encoding="utf-8")
        run_checked(
            install_runtime_requirements_command(python, requirements),
            cwd=run_root,
        )
        stage = "install product wheels"
        run_checked(
            install_command(python, [engine_wheels[0], app_wheel]),
            cwd=run_root,
        )
        stage = "create test environment"
        run_checked(create_environment_command(test_environment), cwd=run_root)
        test_python = test_environment / "bin/python"
        requirements = run_root / "test-requirements.txt"
        stage = "export test requirements"
        exported = run_checked(export_test_requirements_command(), cwd=root)
        requirements.write_text(exported.stdout, encoding="utf-8")
        stage = "install test requirements"
        run_checked(
            install_test_requirements_command(test_python, requirements),
            cwd=run_root,
        )
        stage = "resolve product site packages"
        product_site_packages = Path(
            run_checked(site_packages_command(python), cwd=run_root).stdout.strip()
        )
        if not product_site_packages.is_absolute():
            raise OSError("product site-packages path is invalid")
        test_root = run_root / "test-contract"
        _copy_app_test_contract(root, test_root)
        test_runner = run_root / "app_test_runner.py"
        test_runner.write_text(_app_test_runner_source(), encoding="utf-8")
        stage = "run installed app tests"
        run_checked(
            engine_test_command(
                test_python,
                test_runner,
                test_root,
                product_site_packages,
            ),
            cwd=run_root,
        )
        stage = "run installed CLI"
        version = run_checked(
            (os.fspath(environment / "bin/open-brain"), "--version"),
            cwd=run_root,
        )
        if version.stdout.strip() != "open-brain 0.1.0":
            raise ValueError("installed app version is mismatched")
        stage = "run installed CLI help"
        help_output = run_checked(
            (os.fspath(environment / "bin/open-brain"), "--help"),
            cwd=run_root,
        )
        if "daemon" not in help_output.stdout.split():
            raise ValueError("installed daemon command is absent")
        if not (environment / "bin/open-brain-mcp").is_file():
            raise OSError("installed MCP entry point is absent")
        contract = run_root / "app_contract.py"
        contract.write_text(_app_contract_source(), encoding="utf-8")
        stage = "run installed app contract"
        completed = run_checked((os.fspath(python), "-I", os.fspath(contract)), cwd=run_root)
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return [
            Finding("P4H007", "app-isolation", f"installed app contract failed at {stage}")
        ]
    module_paths = payload.get("module_paths") if isinstance(payload, dict) else None
    sys_path = payload.get("sys_path") if isinstance(payload, dict) else None
    if not isinstance(module_paths, list) or not all(
        isinstance(item, str) for item in module_paths
    ):
        return [Finding("P4H007", "app-isolation", "module origin evidence is malformed")]
    if not isinstance(sys_path, list) or not all(isinstance(item, str) for item in sys_path):
        return [Finding("P4H007", "app-isolation", "interpreter path evidence is malformed")]
    return import_probe_findings(
        ImportProbe(tuple(cast(list[str], module_paths)), tuple(cast(list[str], sys_path))),
        root,
    )


def _connector_contract_source() -> str:
    return """from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys

import open_brain.extensions.connector_worker_v1 as worker
import open_brain.extensions.connectors as extension
import open_brain_connectors
import open_brain_engine


def forbidden_context(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("installed connector context must not run in the parent")


def main() -> None:
    distribution = importlib.metadata.distribution("open-brain-connectors")
    assert distribution.version == "0.1.0"
    assert set(distribution.requires or ()) == {
        "open-brain==0.1.0",
        "open-brain-engine==0.1.0",
    }
    registrations = tuple(
        item
        for item in distribution.entry_points
        if item.group == extension.CONNECTOR_ENTRY_POINT_GROUP
    )
    assert tuple((item.name, item.value) for item in registrations) == (
        ("youtube", "open_brain_connectors.conformance:connector"),
    )
    manifest = extension.ConnectorManifest(
        schema_version=1,
        name="youtube",
        version="1",
        payloads=(extension.ConnectorPayload.REFERENCE_OR_FILE,),
        schedules=("JOB-029",),
        secrets=(),
        action_authorities=(),
        external_egress=True,
    )
    limits = extension.ConnectorBudgetLimits(
        max_discoveries=2,
        max_fetches=2,
        max_extractions=2,
        max_submissions=2,
    )
    host = worker.ConnectorWorkerHost()
    assert host.discover(extension.ConnectorProfile()) == ()
    profile = extension.ConnectorProfile(
        allow_list=("youtube",),
        egress_enabled=True,
        budget_limits=limits,
    )
    assert tuple((item.name, item.value) for item in host.discover(profile)) == (
        ("youtube", "open_brain_connectors.conformance:connector"),
    )
    assert "open_brain_connectors.conformance" not in sys.modules
    installed_registry = extension.ConnectorRegistry()
    try:
        installed_registry.resolve("youtube", profile)
    except extension.ConnectorConfigurationError as error:
        assert str(error) == "installed connector requires isolated worker"
    else:
        raise AssertionError("installed connector resolved in the parent")
    in_process = extension.ConnectorHost(installed_registry).run(
        "youtube",
        profile=profile,
        context_factory=forbidden_context,
    )
    assert in_process.outcome is extension.ConnectorOutcome.FAILED
    assert in_process.failure_code is extension.ConnectorFailureCode.INVALID_REGISTRATION
    assert "open_brain_connectors.conformance" not in sys.modules
    receipt = host.run_conformance(
        "youtube",
        profile=profile,
        expected_manifest=manifest,
    )
    assert "open_brain_connectors.conformance" not in sys.modules
    assert receipt.first_run.submitted_count == receipt.first_run.created_count == 1
    assert receipt.first_run.checkpoint_committed is True
    assert receipt.replay_run.submitted_count == receipt.replay_run.created_count == 0
    assert receipt.capture_count == 1
    assert receipt.direct_network_attempts == 0
    assert len(receipt.checkpoint_receipt_sha256) == 64
    assert importlib.util.find_spec("open_brain_legacy") is None
    request_fields = set(worker.ConnectorWorkerRequest.__dataclass_fields__)
    receipt_fields = set(worker.ConnectorWorkerReceipt.__dataclass_fields__)
    forbidden_fields = {"action", "brain_root", "database", "secret_value", "store"}
    assert not forbidden_fields & request_fields
    assert not forbidden_fields & receipt_fields
    print(json.dumps({
        "module_paths": [
            open_brain_connectors.__file__,
            open_brain_engine.__file__,
            extension.__file__,
            worker.__file__,
        ],
        "sys_path": sys.path,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
"""


def _literal_module_exports(payload: bytes, *, filename: str) -> frozenset[str]:
    tree = ast.parse(payload, filename=filename)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            break
        return frozenset(cast(list[str], value))
    raise ValueError("public extension exports are missing")


def _connector_import_boundary_findings(
    connector_wheel: Path,
    app_wheel: Path,
    engine_modules: frozenset[str],
    public_engine_modules: frozenset[str],
) -> list[Finding]:
    findings: list[Finding] = []
    try:
        with zipfile.ZipFile(connector_wheel) as archive:
            sources = {
                name: archive.read(name)
                for name in archive.namelist()
                if name.startswith("open_brain_connectors/") and name.endswith(".py")
            }
            metadata = {
                name: archive.read(name)
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            }
        with zipfile.ZipFile(app_wheel) as archive:
            app_exports = {
                module: _literal_module_exports(archive.read(path), filename=path)
                for module, path in {
                    "open_brain.extensions.connectors": (
                        "open_brain/extensions/connectors.py"
                    ),
                    "open_brain.extensions.connector_worker_v1": (
                        "open_brain/extensions/connector_worker_v1.py"
                    ),
                }.items()
            }
        declared_roots = _declared_import_roots(metadata)
    except (OSError, SyntaxError, ValueError, zipfile.BadZipFile, KeyError):
        return [Finding("P4H007", "connector-wheel", "connector source inspection failed")]
    for name, payload in sorted(sources.items()):
        try:
            tree = ast.parse(payload, filename=name)
        except (SyntaxError, UnicodeError):
            findings.append(Finding("P4H007", name, "connector source inspection failed"))
            continue
        imported: set[str] = set()
        dynamic_imported, dynamic_signatures = _dynamic_imports(tree)
        imported.update(dynamic_imported)
        if dynamic_signatures:
            findings.append(
                Finding("P4H009", name, "connector artifact has an undeclared import")
            )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
                imported.add(node.module)
                if node.module in _PUBLIC_CONNECTOR_APP_MODULES and any(
                    alias.name == "*" or alias.name not in app_exports[node.module]
                    for alias in node.names
                ):
                    findings.append(
                        Finding("P4H009", name, "connector imports an unpublished app value")
                    )
                if node.module == "open_brain_engine" or node.module.startswith(
                    "open_brain_engine."
                ):
                    imported.update(
                        candidate
                        for alias in node.names
                        if alias.name != "*"
                        and (candidate := f"{node.module}.{alias.name}") in engine_modules
                    )
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        if any(
            (module == "open_brain" or module.startswith("open_brain."))
            and module not in _PUBLIC_CONNECTOR_APP_MODULES
            for module in imported
        ):
            findings.append(
                Finding("P4H009", name, "connector imports app composition or private values")
            )
        if any(
            (module == "open_brain_engine" or module.startswith("open_brain_engine."))
            and module not in public_engine_modules
            for module in imported
        ):
            findings.append(
                Finding("P4H008", name, "connector artifact imports a private engine module")
            )
        if any(
            (root := module.partition(".")[0]) not in declared_roots
            and root not in sys.stdlib_module_names
            and root not in {"__future__", "open_brain_connectors"}
            for module in imported
        ):
            findings.append(
                Finding("P4H009", name, "connector artifact has an undeclared import")
            )
    return sorted(set(findings))


def connector_isolation_findings(root: Path, work: Path) -> list[Finding]:
    """Build and run connector conformance from only engine, app, and connector wheels."""

    engine_project = root / "packages/engine"
    app_project = root / "packages/app"
    connector_project = root / "packages/connectors"
    if not (connector_project / "src/open_brain_connectors").is_dir():
        return [Finding("P4H007", "connector-isolation", "connector project is absent")]
    engine_dist = work / "engine-dist"
    app_dist = work / "app-dist"
    connector_dist = work / "connector-dist"
    environment = work / "venv"
    run_root = work / "run"
    for path in (engine_dist, app_dist, connector_dist, run_root):
        path.mkdir(parents=True, exist_ok=True)
    try:
        run_checked(build_command(engine_project, engine_dist), cwd=run_root)
        run_checked(build_command(app_project, app_dist), cwd=run_root)
        run_checked(build_command(connector_project, connector_dist), cwd=run_root)
    except (OSError, subprocess.SubprocessError):
        return [Finding("P4H007", "connector-isolation", "connector build failed")]
    engine_wheels = sorted(engine_dist.glob("open_brain_engine-*.whl"))
    app_wheels = sorted(app_dist.glob("open_brain-*.whl"))
    connector_wheels = sorted(connector_dist.glob("open_brain_connectors-*.whl"))
    connector_sdists = sorted(connector_dist.glob("open_brain_connectors-*.tar.gz"))
    if any(
        len(items) != 1
        for items in (engine_wheels, app_wheels, connector_wheels, connector_sdists)
    ):
        return [Finding("P4H001", "connector-isolation", "connector artifacts are incomplete")]
    connector_wheel = connector_wheels[0]
    app_wheel = app_wheels[0]
    findings = artifact_findings(
        connector_wheel,
        ArtifactContract(
            subject="connector-wheel",
            required_members=(
                "open_brain_connectors/__init__.py",
                "open_brain_connectors/conformance.py",
                "open_brain_connectors/production/youtube_poll.py",
                "open_brain_connectors-0.1.0.dist-info/entry_points.txt",
            ),
            forbidden_patterns=(
                "open_brain/**",
                "open_brain_engine/**",
                "open_brain_legacy/**",
                "tests/**",
                "tools/**",
            ),
            expected_name="open-brain-connectors",
            expected_version="0.1.0",
        ),
    )
    try:
        engine_modules, public_engine_modules = _engine_module_sets(root)
    except (OSError, ValueError, json.JSONDecodeError):
        return [Finding("P4H007", "connector-isolation", "public engine API is unreadable")]
    findings.extend(
        _connector_import_boundary_findings(
            connector_wheel,
            app_wheel,
            engine_modules,
            public_engine_modules,
        )
    )
    if findings:
        return sorted(set(findings))
    stage = "create product environment"
    try:
        run_checked(create_environment_command(environment), cwd=run_root)
        python = environment / "bin/python"
        requirements = run_root / "app-runtime-requirements.txt"
        exported = run_checked(export_runtime_requirements_command("open-brain"), cwd=root)
        requirements.write_text(exported.stdout, encoding="utf-8")
        run_checked(
            install_runtime_requirements_command(python, requirements),
            cwd=run_root,
        )
        stage = "install product wheels"
        run_checked(
            install_command(python, [engine_wheels[0], app_wheel, connector_wheel]),
            cwd=run_root,
        )
        contract = run_root / "connector_contract.py"
        contract.write_text(_connector_contract_source(), encoding="utf-8")
        stage = "run installed connector contract"
        completed = run_checked((os.fspath(python), "-I", os.fspath(contract)), cwd=run_root)
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return [
            Finding(
                "P4H007",
                "connector-isolation",
                f"installed connector contract failed at {stage}",
            )
        ]
    module_paths = payload.get("module_paths") if isinstance(payload, dict) else None
    sys_path = payload.get("sys_path") if isinstance(payload, dict) else None
    if not isinstance(module_paths, list) or not all(
        isinstance(item, str) for item in module_paths
    ):
        return [Finding("P4H007", "connector-isolation", "module origin evidence is malformed")]
    if not isinstance(sys_path, list) or not all(isinstance(item, str) for item in sys_path):
        return [Finding("P4H007", "connector-isolation", "interpreter path evidence is malformed")]
    return import_probe_findings(
        ImportProbe(tuple(cast(list[str], module_paths)), tuple(cast(list[str], sys_path))),
        root,
    )


def current_layout_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for distribution in ("engine", "app", "connectors", "legacy"):
        if not (root / "packages" / distribution / "pyproject.toml").is_file():
            findings.append(Finding("P4E001", distribution, "distribution root is absent"))
    project: dict[str, object] = {}
    try:
        import tomllib

        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        pass
    tool = project.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    workspace = uv.get("workspace") if isinstance(uv, dict) else None
    if not isinstance(workspace, dict) or not workspace.get("members"):
        findings.append(Finding("P4E002", "uv-workspace", "workspace membership is inactive"))
    if (root / "src/open_brain").is_dir():
        findings.append(Finding("P4E003", "src/open_brain", "monolith source tree remains"))
    for artifact in ("engine-wheel", "app-wheel", "connector-wheel"):
        findings.append(
            Finding("P4E004", artifact, "isolated distribution artifact is unavailable")
        )
    for artifact in ("macos-arm64-native", "linux-x86_64-native"):
        findings.append(Finding("P4E005", artifact, "native artifact is unavailable"))
    return sorted(findings)


def expected_red_payload(root: Path) -> dict[str, object]:
    return {
        "schema_version": EXPECTED_RED_SCHEMA,
        "baseline": "current-monolith",
        "findings": [finding.to_dict() for finding in current_layout_findings(root)],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("list-contracts", "inspect-current", "write-expected-red")
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "list-contracts":
        print(json.dumps([contract.contract_id for contract in CONTRACTS]))
        return 0
    payload = expected_red_payload(args.root.resolve())
    if args.command == "write-expected-red":
        if args.output is None:
            raise SystemExit("write-expected-red requires --output")
        args.output.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 0
    findings = cast(list[dict[str, str]], payload["findings"])
    for finding in findings:
        print(json.dumps(finding, sort_keys=True))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
