from __future__ import annotations

import json
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path
from typing import Any

from tools.phase4.acceptance_harness import (
    CONTRACTS,
    ArtifactContract,
    ImportProbe,
    _app_import_boundary_findings,
    _legacy_compat_import_boundary_findings,
    artifact_findings,
    build_command,
    create_environment_command,
    engine_test_command,
    export_runtime_requirements_command,
    export_test_requirements_command,
    import_probe_findings,
    install_command,
    install_runtime_requirements_command,
    run_checked,
    sanitized_environment,
)

ROOT = Path(__file__).parents[2]
WORKSTREAM = (
    ROOT / "docs/ai/workstreams/"
    "20260901-open-brain-public-execute-goal-63-through-phase-4-p4a-p4b-and-p4c-"
    "with-independently-reviewed-pack-8a3f9b"
)
EXPECTED_RED = WORKSTREAM / "P4-W0-EXPECTED-RED.json"


def _codes(path: Path, contract: ArtifactContract) -> set[str]:
    return {finding.code for finding in artifact_findings(path, contract)}


def _wheel(path: Path, members: dict[str, bytes], *, duplicate: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
        if duplicate is not None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(duplicate, b"duplicate")


def test_all_six_acceptance_contracts_are_independently_named() -> None:
    assert [contract.contract_id for contract in CONTRACTS] == [
        "engine-isolation",
        "app-isolation",
        "connector-isolation",
        "artifact-membership",
        "identity-compatibility",
        "clean-host-lifecycle",
    ]
    app = next(contract for contract in CONTRACTS if contract.contract_id == "app-isolation")
    assert "v0-gate-07-sibling-approve-reject-safe-edit-cli-ui" in app.behaviors
    assert "v0-gate-13-space-create-rename-later-route-scoped-all-retrieval" in app.behaviors


def test_build_and_install_commands_enforce_no_sources_and_selected_python(tmp_path: Path) -> None:
    assert "--no-sources" in build_command(tmp_path / "project", tmp_path / "dist")
    assert create_environment_command(tmp_path / "venv")[:4] == (
        "uv",
        "venv",
        "--python",
        f"{sys.version_info.major}.{sys.version_info.minor}",
    )
    assert create_environment_command(tmp_path / "venv", python_version="3.14")[:4] == (
        "uv",
        "venv",
        "--python",
        "3.14",
    )
    command = install_command(tmp_path / "venv/bin/python", [tmp_path / "app.whl"])
    assert command[5:7] == ("--link-mode", "copy")
    assert "--no-index" in command
    assert "app.whl" in command[-1]
    runtime_export = export_runtime_requirements_command("open-brain")
    assert "--locked" in runtime_export
    assert "--no-emit-workspace" in runtime_export
    runtime_install = install_runtime_requirements_command(
        tmp_path / "venv/bin/python",
        tmp_path / "runtime-requirements.txt",
    )
    assert "--only-binary=:all:" in runtime_install
    assert "--require-hashes" in runtime_install
    export = export_test_requirements_command()
    assert "--locked" in export
    assert "--no-emit-workspace" in export
    assert "open-brain-engine" not in export
    assert engine_test_command(
        tmp_path / "venv/bin/python",
        tmp_path / "engine_test_runner.py",
        tmp_path / "tests",
        tmp_path / "venv/lib/python3.12/site-packages",
    ) == (
        str(tmp_path / "venv/bin/python"),
        "-I",
        str(tmp_path / "engine_test_runner.py"),
        str(tmp_path / "tests"),
        str(tmp_path / "venv/lib/python3.12/site-packages"),
    )


def test_isolated_environment_removes_source_masking_inputs() -> None:
    environment = sanitized_environment(
        {
            "PATH": "/bin",
            "PYTHONPATH": "/repo/src",
            "PYTHONHOME": "/repo/python",
            "VIRTUAL_ENV": "/repo/.venv",
            "UV_PROJECT_ENVIRONMENT": "/repo/.venv",
        }
    )

    assert environment == {
        "PATH": "/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def test_run_checked_uses_isolated_cwd_environment_and_timeout(tmp_path: Path) -> None:
    observed: dict[str, Any] = {}

    def runner(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.update({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, "ok", "")

    result = run_checked(("python", "-V"), cwd=tmp_path, runner=runner)

    assert result.returncode == 0
    assert observed["cwd"] == tmp_path
    assert observed["timeout"] == 1800
    assert "PYTHONPATH" not in observed["env"]


def test_harness_detects_missing_leaked_and_mismatched_artifacts(tmp_path: Path) -> None:
    contract = ArtifactContract(
        subject="app-wheel",
        required_members=("open_brain/__init__.py",),
        forbidden_patterns=("open_brain_legacy/**",),
        expected_name="open-brain",
        expected_version="0.1.0",
    )
    assert _codes(tmp_path / "missing.whl", contract) == {"P4H001"}

    leaked = tmp_path / "leaked.whl"
    _wheel(
        leaked,
        {
            "open_brain/__init__.py": b"",
            "open_brain_legacy/writer.py": b"",
            "open_brain-0.1.0.dist-info/METADATA": b"Name: open-brain\nVersion: 0.1.0\n",
        },
    )
    assert "P4H002" in _codes(leaked, contract)

    mismatched = tmp_path / "mismatched.whl"
    _wheel(
        mismatched,
        {
            "open_brain/__init__.py": b"",
            "wrong-9.9.9.dist-info/METADATA": b"Name: wrong\nVersion: 9.9.9\n",
        },
    )
    assert "P4H003" in _codes(mismatched, contract)


def test_harness_detects_unsafe_duplicate_and_source_path_masked_cases(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "unsafe.whl"
    metadata = b"Name: open-brain\nVersion: 0.1.0\n"
    _wheel(
        wheel,
        {
            "open_brain/__init__.py": b"",
            "../escape.py": b"",
            "open_brain-0.1.0.dist-info/METADATA": metadata,
        },
        duplicate="open_brain/__init__.py",
    )
    contract = ArtifactContract(
        subject="app-wheel",
        required_members=("open_brain/__init__.py",),
        forbidden_patterns=(),
        expected_name="open-brain",
        expected_version="0.1.0",
    )

    assert {"P4H005", "P4H006"} <= _codes(wheel, contract)
    probe = ImportProbe(module_paths=(str(ROOT / "src/open_brain/__init__.py"),), sys_path=())
    assert [finding.code for finding in import_probe_findings(probe, ROOT)] == ["P4H004"]


def test_app_artifact_rejects_private_engine_and_undeclared_imports(tmp_path: Path) -> None:
    wheel = tmp_path / "app.whl"
    _wheel(
        wheel,
        {
            "open_brain/public_use.py": (
                b"from open_brain_engine.storage.operational import read_confined\n"
            ),
            "open_brain/private_use.py": (
                b"from open_brain_engine.storage.filesystem import read_confined\n"
            ),
            "open_brain/private_child_use.py": b"from open_brain_engine.engine import local\n",
            "open_brain/undeclared_use.py": b"import open_brain_connectors\n",
            "open_brain/allowed_optional_use.py": b"import openai\n",
            "open_brain/dynamic_use.py": (
                b"from importlib import import_module\n"
                b"def load(target: str) -> object:\n"
                b"    return import_module(target)\n"
            ),
            "open_brain-0.1.0.dist-info/METADATA": (
                b"Name: open-brain\nVersion: 0.1.0\n"
                b"Requires-Dist: open-brain-engine==0.1.0\n"
                b"Requires-Dist: openai>=1; extra == 'cloud'\n"
            ),
        },
    )

    findings = _app_import_boundary_findings(
        wheel,
        frozenset(
            {
                "open_brain_engine",
                "open_brain_engine.engine",
                "open_brain_engine.engine.local",
                "open_brain_engine.storage.filesystem",
                "open_brain_engine.storage.operational",
            }
        ),
        frozenset(
            {
                "open_brain_engine",
                "open_brain_engine.engine",
                "open_brain_engine.storage.operational",
            }
        ),
    )

    assert {(finding.code, finding.subject) for finding in findings} == {
        ("P4H008", "open_brain/private_child_use.py"),
        ("P4H008", "open_brain/private_use.py"),
        ("P4H009", "open_brain/dynamic_use.py"),
        ("P4H009", "open_brain/undeclared_use.py"),
    }


def test_legacy_compat_rejects_static_and_dynamic_external_imports(tmp_path: Path) -> None:
    wheel = tmp_path / "legacy.whl"
    _wheel(
        wheel,
        {
            "open_brain_legacy/_compat/allowed.py": (
                b"import json\n"
                b"import open_brain_engine\n"
                b"from open_brain_legacy import services\n"
            ),
            "open_brain_legacy/_compat/static_external.py": b"import openai\n",
            "open_brain_legacy/_compat/dynamic_external.py": (
                b"from importlib import import_module as load\n"
                b'load("openai")\n'
            ),
        },
    )

    findings = _legacy_compat_import_boundary_findings(wheel)

    assert {(finding.code, finding.subject) for finding in findings} == {
        ("P4H009", "open_brain_legacy/_compat/dynamic_external.py"),
        ("P4H009", "open_brain_legacy/_compat/static_external.py"),
    }


def test_app_artifact_tracks_dynamic_import_provenance_and_shadowing(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "app.whl"
    _wheel(
        wheel,
        {
            "open_brain/builtins_use.py": (
                b"import builtins\n"
                b'builtins.__import__("open_brain_connectors")\n'
            ),
            "open_brain/builtins_alias_use.py": (
                b"from builtins import __import__ as load\n"
                b'load("open_brain_engine.engine.local")\n'
            ),
            "open_brain/assignment_alias_use.py": (
                b"from importlib import import_module\n"
                b"load = import_module\n"
                b'load("open_brain_connectors")\n'
            ),
            "open_brain/reflective_alias_use.py": (
                b"import importlib as loader\n"
                b'load = getattr(loader, "import_module")\n'
                b'load("open_brain_connectors")\n'
            ),
            "open_brain/integrations/ports.py": (
                b"from importlib import import_module\n"
                b"def _loaded_optional_module(import_path: str) -> object:\n"
                b"    return import_module(import_path)\n"
                b"IMPORTERS = (import_module,)\n"
            ),
            "open_brain/shadowed_importlib.py": (
                b"class Loader:\n"
                b"    def import_module(self, target: str) -> object:\n"
                b"        return target\n"
                b"importlib = Loader()\n"
                b'importlib.import_module("open_brain_connectors")\n'
            ),
            "open_brain-0.1.0.dist-info/METADATA": (
                b"Name: open-brain\nVersion: 0.1.0\n"
                b"Requires-Dist: open-brain-engine==0.1.0\n"
            ),
        },
    )

    findings = _app_import_boundary_findings(
        wheel,
        frozenset(
            {
                "open_brain_engine",
                "open_brain_engine.engine",
                "open_brain_engine.engine.local",
            }
        ),
        frozenset({"open_brain_engine", "open_brain_engine.engine"}),
    )

    assert {(finding.code, finding.subject) for finding in findings} == {
        ("P4H009", "open_brain/assignment_alias_use.py"),
        ("P4H008", "open_brain/builtins_alias_use.py"),
        ("P4H009", "open_brain/builtins_alias_use.py"),
        ("P4H009", "open_brain/builtins_use.py"),
        ("P4H009", "open_brain/integrations/ports.py"),
        ("P4H009", "open_brain/reflective_alias_use.py"),
    }


def test_app_artifact_rejects_reflective_builtin_namespace_access(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "app.whl"
    _wheel(
        wheel,
        {
            "open_brain/sys_modules_use.py": (
                b"import sys\n"
                b'sys.modules["builtins"].__import__('
                b'"open_brain_engine.engine.local")\n'
            ),
            "open_brain/globals_use.py": (
                b'globals()["__builtins__"]["__import__"]('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/locals_use.py": (
                b'locals()["__builtins__"]["__import__"]('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/vars_use.py": (
                b'vars(__builtins__)["__import__"]("open_brain_connectors")\n'
            ),
            "open_brain/eval_use.py": (
                b'eval("__import__(\\"open_brain_connectors\\")")\n'
            ),
            "open_brain/exec_use.py": (
                b'exec("__import__(\\"open_brain_connectors\\")")\n'
            ),
            "open_brain/eval_alias_use.py": (
                b"run = eval\n"
                b'run("__import__(\\"open_brain_connectors\\")")\n'
            ),
            "open_brain/shadowed_globals.py": (
                b"def globals() -> dict[str, object]:\n"
                b"    return {}\n"
                b"globals()\n"
            ),
            "open_brain-0.1.0.dist-info/METADATA": (
                b"Name: open-brain\nVersion: 0.1.0\n"
                b"Requires-Dist: open-brain-engine==0.1.0\n"
            ),
        },
    )

    findings = _app_import_boundary_findings(
        wheel,
        frozenset(
            {
                "open_brain_engine",
                "open_brain_engine.engine",
                "open_brain_engine.engine.local",
            }
        ),
        frozenset({"open_brain_engine", "open_brain_engine.engine"}),
    )

    assert {(finding.code, finding.subject) for finding in findings} == {
        ("P4H009", "open_brain/eval_alias_use.py"),
        ("P4H009", "open_brain/eval_use.py"),
        ("P4H009", "open_brain/exec_use.py"),
        ("P4H009", "open_brain/globals_use.py"),
        ("P4H009", "open_brain/locals_use.py"),
        ("P4H008", "open_brain/sys_modules_use.py"),
        ("P4H009", "open_brain/sys_modules_use.py"),
        ("P4H009", "open_brain/vars_use.py"),
    }


def test_app_artifact_joins_provenance_and_models_shadowing_bindings(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "app.whl"
    _wheel(
        wheel,
        {
            "open_brain/sys_dict_use.py": (
                b"import sys\n"
                b'sys.__dict__["modules"]["builtins"].__import__('
                b'"open_brain_engine.engine.local")\n'
            ),
            "open_brain/sys_getattribute_use.py": (
                b"import sys\n"
                b'sys.__getattribute__("modules")["builtins"].__import__('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/sys_dict_alias_use.py": (
                b"from sys import __dict__ as namespace\n"
                b'namespace["modules"]["builtins"].__import__('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/sys_getattribute_alias_use.py": (
                b"from sys import __getattribute__ as lookup\n"
                b'lookup("modules")["builtins"].__import__('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/importlib_dict_alias_use.py": (
                b"from importlib import __dict__ as namespace\n"
                b'namespace["import_module"]("open_brain_connectors")\n'
            ),
            "open_brain/importlib_getattribute_alias_use.py": (
                b"from importlib import __getattribute__ as lookup\n"
                b'lookup("import_module")("open_brain_connectors")\n'
            ),
            "open_brain/builtins_getattribute_alias_use.py": (
                b"from builtins import __getattribute__ as lookup\n"
                b'lookup("__import__")("open_brain_connectors")\n'
            ),
            "open_brain/function_globals_use.py": (
                b"def host() -> None:\n"
                b"    return None\n"
                b'host.__globals__["__builtins__"]["__import__"]('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/function_getattribute_use.py": (
                b"def host() -> None:\n"
                b"    return None\n"
                b'host.__getattribute__("__globals__")["__builtins__"]'
                b'["__import__"]("open_brain_connectors")\n'
            ),
            "open_brain/object_getattribute_use.py": (
                b"def host() -> None:\n"
                b"    return None\n"
                b'object.__getattribute__(host, "__globals__")["__builtins__"]'
                b'["__import__"]("open_brain_connectors")\n'
            ),
            "open_brain/object_getattribute_alias_use.py": (
                b"from builtins import object as root_object\n"
                b"def host() -> None:\n"
                b"    return None\n"
                b'root_object.__getattribute__(host, "__globals__")'
                b'["__builtins__"]["__import__"]("open_brain_connectors")\n'
            ),
            "open_brain/lambda_globals_use.py": (
                b"host = lambda: None\n"
                b'host.__globals__["__builtins__"]["__import__"]('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/dead_branch_rebinding.py": (
                b"import sys\n"
                b"if False:\n"
                b"    sys = object()\n"
                b'sys.modules["builtins"].__import__("open_brain_connectors")\n'
            ),
            "open_brain/comprehension_walrus_use.py": (
                b"import sys as real_sys\n"
                b"[(sys := real_sys) for _ in (0,)]\n"
                b'sys.modules["builtins"].__import__("open_brain_connectors")\n'
            ),
            "open_brain/comprehension_shadow.py": (
                b"import sys\n"
                b"def inspect(values: tuple[object, ...]) -> None:\n"
                b"    [(sys := value) for value in values]\n"
                b'    sys.modules["builtins"].__import__('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/normal_walrus_shadow.py": (
                b"import sys\n"
                b"(sys := object())\n"
                b'sys.modules["builtins"].__import__("open_brain_connectors")\n'
            ),
            "open_brain/integrations/ports.py": (
                b"from importlib import import_module\n"
                b"def _loaded_optional_module(import_path: str) -> object:\n"
                b"    return import_module(import_path)\n"
                b"def host() -> None:\n"
                b"    return None\n"
                b'host.__globals__["__builtins__"]["__import__"]('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/loop_shadow.py": (
                b"import sys\n"
                b"for sys in values:\n"
                b'    sys.modules["builtins"].__import__('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/with_shadow.py": (
                b"import sys\n"
                b"with context() as sys:\n"
                b'    sys.modules["builtins"].__import__('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/except_shadow.py": (
                b"import sys\n"
                b"try:\n"
                b"    action()\n"
                b"except RuntimeError as sys:\n"
                b'    sys.modules["builtins"].__import__('
                b'"open_brain_connectors")\n'
            ),
            "open_brain-0.1.0.dist-info/METADATA": (
                b"Name: open-brain\nVersion: 0.1.0\n"
                b"Requires-Dist: open-brain-engine==0.1.0\n"
            ),
        },
    )

    findings = _app_import_boundary_findings(
        wheel,
        frozenset(
            {
                "open_brain_engine",
                "open_brain_engine.engine",
                "open_brain_engine.engine.local",
            }
        ),
        frozenset({"open_brain_engine", "open_brain_engine.engine"}),
    )

    assert {(finding.code, finding.subject) for finding in findings} == {
        ("P4H009", "open_brain/builtins_getattribute_alias_use.py"),
        ("P4H009", "open_brain/comprehension_walrus_use.py"),
        ("P4H009", "open_brain/dead_branch_rebinding.py"),
        ("P4H009", "open_brain/function_getattribute_use.py"),
        ("P4H009", "open_brain/function_globals_use.py"),
        ("P4H009", "open_brain/integrations/ports.py"),
        ("P4H009", "open_brain/importlib_dict_alias_use.py"),
        ("P4H009", "open_brain/importlib_getattribute_alias_use.py"),
        ("P4H009", "open_brain/lambda_globals_use.py"),
        ("P4H009", "open_brain/object_getattribute_alias_use.py"),
        ("P4H009", "open_brain/object_getattribute_use.py"),
        ("P4H009", "open_brain/sys_dict_alias_use.py"),
        ("P4H008", "open_brain/sys_dict_use.py"),
        ("P4H009", "open_brain/sys_dict_use.py"),
        ("P4H009", "open_brain/sys_getattribute_alias_use.py"),
        ("P4H009", "open_brain/sys_getattribute_use.py"),
    }


def test_app_artifact_rejects_equivalent_import_and_reflection_spellings(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "app.whl"
    _wheel(
        wheel,
        {
            "open_brain/importlib_init_alias.py": (
                b"from importlib.__init__ import import_module as load\n"
                b'load("open_brain_engine.engine.local")\n'
            ),
            "open_brain/pkgutil_alias.py": (
                b"import pkgutil as loader\n"
                b'loader.resolve_name("open_brain_connectors")\n'
            ),
            "open_brain/pkgutil_from_alias.py": (
                b"from pkgutil import resolve_name as load\n"
                b'load("open_brain_engine.engine.local:subject")\n'
            ),
            "open_brain/importlib_util_alias.py": (
                b"import importlib.util as utilities\n"
                b'utilities.find_spec("open_brain_connectors")\n'
            ),
            "open_brain/importlib_util_from_alias.py": (
                b"from importlib import util as utilities\n"
                b'utilities.find_spec("open_brain_connectors")\n'
            ),
            "open_brain/sys_getframe.py": (
                b"import sys\n"
                b'sys._getframe().f_globals["__builtins__"]["__import__"]('
                b'"open_brain_connectors")\n'
            ),
            "open_brain/sys_getframe_alias.py": (
                b"from sys import _getframe as frame\n"
                b'frame().f_builtins["__import__"]("open_brain_connectors")\n'
            ),
            "open_brain/type_function_getattribute.py": (
                b"def host() -> None:\n"
                b"    return None\n"
                b'type(host).__getattribute__(host, "__globals__")'
                b'["__builtins__"]["__import__"]("open_brain_connectors")\n'
            ),
            "open_brain/type_alias_getattribute.py": (
                b"from builtins import type as kind\n"
                b"def host() -> None:\n"
                b"    return None\n"
                b'kind(host).__getattribute__(host, "__globals__")'
                b'["__builtins__"]["__import__"]("open_brain_connectors")\n'
            ),
            "open_brain/function_class_getattribute.py": (
                b"def host() -> None:\n"
                b"    return None\n"
                b'host.__class__.__getattribute__(host, "__globals__")'
                b'["__builtins__"]["__import__"]("open_brain_connectors")\n'
            ),
            "open_brain-0.1.0.dist-info/METADATA": (
                b"Name: open-brain\nVersion: 0.1.0\n"
                b"Requires-Dist: open-brain-engine==0.1.0\n"
            ),
        },
    )

    findings = _app_import_boundary_findings(
        wheel,
        frozenset(
            {
                "open_brain_engine",
                "open_brain_engine.engine",
                "open_brain_engine.engine.local",
            }
        ),
        frozenset({"open_brain_engine", "open_brain_engine.engine"}),
    )

    assert {(finding.code, finding.subject) for finding in findings} == {
        ("P4H008", "open_brain/importlib_init_alias.py"),
        ("P4H009", "open_brain/importlib_init_alias.py"),
        ("P4H009", "open_brain/importlib_util_alias.py"),
        ("P4H009", "open_brain/importlib_util_from_alias.py"),
        ("P4H009", "open_brain/function_class_getattribute.py"),
        ("P4H009", "open_brain/pkgutil_alias.py"),
        ("P4H008", "open_brain/pkgutil_from_alias.py"),
        ("P4H009", "open_brain/pkgutil_from_alias.py"),
        ("P4H009", "open_brain/sys_getframe.py"),
        ("P4H009", "open_brain/sys_getframe_alias.py"),
        ("P4H009", "open_brain/type_alias_getattribute.py"),
        ("P4H009", "open_brain/type_function_getattribute.py"),
    }


def test_app_artifact_rejects_nonliteral_dynamic_imports(tmp_path: Path) -> None:
    sources = (
        (
            b"from importlib import import_module\n"
            b"def _loaded_optional_module(import_path: str) -> object:\n"
            b'    import_path = "open_brain_connectors"\n'
            b"    return import_module(import_path)\n"
        ),
        (
            b"from importlib import import_module\n"
            b"def _loaded_optional_module(import_path: str) -> object:\n"
            b"    if import_path:\n"
            b'        import_path = "open_brain_engine.engine.local"\n'
            b"    return import_module(import_path)\n"
        ),
        (
            b"from importlib import import_module\n"
            b"def _loaded_optional_module(import_path: str, replacement: str) -> object:\n"
            b"    import_path = replacement\n"
            b"    return import_module(import_path)\n"
        ),
    )
    metadata = (
        b"Name: open-brain\nVersion: 0.1.0\n"
        b"Requires-Dist: open-brain-engine==0.1.0\n"
    )

    for index, source in enumerate(sources):
        wheel = tmp_path / f"app-{index}.whl"
        _wheel(
            wheel,
            {
                "open_brain/integrations/ports.py": source,
                "open_brain-0.1.0.dist-info/METADATA": metadata,
            },
        )

        findings = _app_import_boundary_findings(
            wheel,
            frozenset(
                {
                    "open_brain_engine",
                    "open_brain_engine.engine",
                    "open_brain_engine.engine.local",
                }
            ),
            frozenset({"open_brain_engine", "open_brain_engine.engine"}),
        )

        assert {(finding.code, finding.subject) for finding in findings} == {
            ("P4H009", "open_brain/integrations/ports.py")
        }


def test_expected_red_report_is_bounded_metadata_only() -> None:
    payload = json.loads(EXPECTED_RED.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["baseline"] == "current-monolith"
    assert len(payload["findings"]) == 11
    assert {finding["code"] for finding in payload["findings"]} == {
        "P4E001",
        "P4E002",
        "P4E003",
        "P4E004",
        "P4E005",
    }
    assert "/" + "Users/" not in EXPECTED_RED.read_text(encoding="utf-8")
