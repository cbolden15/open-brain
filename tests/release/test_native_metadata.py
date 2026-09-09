from __future__ import annotations

import pytest

from tools.open_brain_dev.native_metadata import runtime_metadata


def test_native_metadata_preserves_dependencies_entrypoints_and_licenses() -> None:
    names = (
        "sample-1.0.dist-info/METADATA",
        "sample-1.0.dist-info/WHEEL",
        "sample-1.0.dist-info/entry_points.txt",
        "sample-1.0.dist-info/top_level.txt",
        "sample-1.0.dist-info/licenses/LICENSE",
        "sample-1.0.dist-info/licenses/vendor/COPYING",
        "sample-1.0.dist-info/LICENSE.txt",
        "sample-1.0.dist-info/LICENSE-MIT",
        "sample-1.0.dist-info/NOTICE",
        "sample/data.json",
        "sample/direct_url.json",
    )
    entries = [(name, "synthetic-source", "DATA") for name in names]
    assert runtime_metadata(entries) == entries


def test_native_metadata_drops_installation_records_and_unknown_cache_files() -> None:
    names = (
        "direct_url.json",
        "RECORD",
        "INSTALLER",
        "REQUESTED",
        "uv_build.json",
        "uv_cache.json",
        "future_installer_metadata.json",
    )
    entries = [(f"sample-1.0.dist-info/{name}", "synthetic-source", "DATA") for name in names]
    assert runtime_metadata(entries) == []


def test_metadata_selection_handles_nested_and_case_varied_roots() -> None:
    entries = [
        ("vendor/sample.DIST-INFO/direct_url.json", "synthetic", "DATA"),
        ("vendor/sample.dist-info/uv_cache.json", "synthetic", "DATA"),
        ("vendor/sample.DIST-INFO/licenses/LICENSE", "synthetic", "DATA"),
    ]
    assert runtime_metadata(entries) == entries[-1:]


def test_sysconfig_relocates_complete_mapping_without_build_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import marshal
    import sys

    from tools.open_brain_dev.native_metadata import relocated_sysconfig
    from tools.open_brain_dev.release_audit import content_rule_ids

    prefix = "/" + "/".join(["home", "synthetic-builder", "python"])
    platform_prefix = prefix + "/platform"
    variables = {
        "prefix": prefix,
        "exec_prefix": platform_prefix,
        "BINDIR": prefix + "/bin",
        "LIBDIR": platform_prefix + "/lib",
        "CFLAGS": "-I" + prefix + "/include -L" + platform_prefix + "/lib",
        "SIZEOF_VOID_P": 8,
        "Py_GIL_DISABLED": 0,
        "SOABI": "cpython-314-synthetic",
        "LDLIBRARY": "libpython3.14.so",
        "unchanged": "/opt/unrelated",
    }
    code = relocated_sysconfig("build_time_vars = " + repr(variables), "_sysconfigdata_synthetic")
    assert not content_rule_ids(marshal.dumps(code), ["synthetic-builder"])
    monkeypatch.setattr(sys, "prefix", "/frozen")
    monkeypatch.setattr(sys, "exec_prefix", "/frozen-platform")
    namespace: dict[str, object] = {}
    exec(code, namespace)
    output = namespace["build_time_vars"]
    assert isinstance(output, dict)
    assert output.keys() == variables.keys()
    assert {key: type(value) for key, value in output.items()} == {
        key: type(value) for key, value in variables.items()
    }
    assert output["BINDIR"] == "/frozen/bin"
    assert output["LIBDIR"] == "/frozen-platform/lib"
    assert output["CFLAGS"] == "-I/frozen/include -L/frozen-platform/lib"
    for key in ("SIZEOF_VOID_P", "Py_GIL_DISABLED", "SOABI", "LDLIBRARY", "unchanged"):
        assert output[key] == variables[key]


def test_equal_sysconfig_prefixes_keep_paths_relative_to_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from tools.open_brain_dev.native_metadata import relocated_sysconfig

    variables = {
        "prefix": "/opt/build",
        "exec_prefix": "/opt/build",
        "LIBDIR": "/opt/build/lib",
        "other": "/opt/build-other/lib",
    }
    code = relocated_sysconfig("build_time_vars = " + repr(variables), "_sysconfigdata_synthetic")
    monkeypatch.setattr(sys, "prefix", "/frozen")
    monkeypatch.setattr(sys, "exec_prefix", "/frozen")
    namespace: dict[str, object] = {}
    exec(code, namespace)
    assert namespace["build_time_vars"] == {
        "prefix": "/frozen",
        "exec_prefix": "/frozen",
        "LIBDIR": "/frozen/lib",
        "other": "/opt/build-other/lib",
    }


def test_sysconfig_refuses_unknown_home_paths() -> None:
    import pytest

    from tools.open_brain_dev.native_metadata import relocated_sysconfig

    variables = {
        "prefix": "/opt/build",
        "exec_prefix": "/opt/build",
        "srcdir": "/" + "/".join(["home", "unexpected", "source"]),
    }
    with pytest.raises(ValueError, match="unrelocated home path"):
        relocated_sysconfig("build_time_vars = " + repr(variables), "_sysconfigdata_synthetic")


def test_sysconfig_rejects_code_instead_of_literal_metadata() -> None:
    import pytest

    from tools.open_brain_dev.native_metadata import relocated_sysconfig

    with pytest.raises(ValueError):
        relocated_sysconfig("build_time_vars = unexpected_function()", "_sysconfigdata_synthetic")
    with pytest.raises(ValueError):
        relocated_sysconfig("import os\nbuild_time_vars = {}", "_sysconfigdata_synthetic")


def test_host_sysconfig_relocation_preserves_all_keys_and_scalar_types() -> None:
    import ast
    import importlib.util
    import sysconfig
    from pathlib import Path

    from tools.open_brain_dev.native_metadata import relocated_sysconfig

    name = sysconfig._get_sysconfigdata_name()  # type: ignore[attr-defined]
    spec = importlib.util.find_spec(name)
    assert spec is not None and spec.origin is not None
    source = Path(spec.origin).read_text()
    tree = ast.parse(source)
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign))
    original = ast.literal_eval(assignment.value)
    namespace: dict[str, object] = {}
    exec(relocated_sysconfig(source, name), namespace)
    result = namespace["build_time_vars"]
    assert isinstance(result, dict)
    assert {key: type(value) for key, value in result.items()} == {
        key: type(value) for key, value in original.items()
    }
    for key, value in original.items():
        if not isinstance(value, str) or not any(
            original[p] in value for p in ("prefix", "exec_prefix")
        ):
            assert result[key] == value
