from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.nw0_api_probe import authority, run


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree_sha(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode() + b"\0" + _sha(data).encode() + b"\n")
    return digest.hexdigest()


def _wheel_rows(target: str) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    names = ["rfc8785", *(f"package{index:02d}" for index in range(1, 14))]
    rows = []
    contents = {}
    for name in names:
        version = "0.1.4" if name == "rfc8785" else "1.0"
        filename = f"{name}-{version}-py3-none-any.whl"
        data = f"synthetic {target} {filename}\n".encode()
        contents[filename] = data
        rows.append({
            "name": name,
            "version": version,
            "filename": filename,
            "url": f"https://files.pythonhosted.org/packages/{filename}",
            "bytes": len(data),
            "sha256": _sha(data),
        })
    return rows, contents


def _fixed_inputs() -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, bytes]]]:
    platforms = {}
    contents = {}
    for target in sorted(authority.TARGETS):
        platforms[target], contents[target] = _wheel_rows(target)
    metadata = {
        "platforms": platforms,
        "dependency_tree_sha256": {"macos-arm64": "d" * 64, "linux-x86_64": "e" * 64},
    }
    manifests = {"macos-arm64": "a" * 64, "linux-x86_64": "b" * 64}
    return metadata, manifests, contents


def _bindings(profile: run.ProofProfile) -> dict[str, str]:
    result = {
        "requirements.txt": _sha(b"rfc8785==0.1.4\n"),
        "payload/run_bundle.py.txt": _sha(b"# synthetic runner\n"),
    }
    for name in profile.candidate_files:
        result[f"payload/{name}.txt"] = _sha(f"synthetic {name}\n".encode())
    return result


def _runtime_identity(target: str) -> dict[str, str]:
    system, machine = {
        "macos-arm64": ("Darwin", "arm64"),
        "linux-x86_64": ("Linux", "x86_64"),
    }[target]
    return {
        "system": system,
        "machine": machine,
        "python_implementation": "CPython",
        "python_version": "3.14",
    }


def _receipts(
    profile: run.ProofProfile,
    target: str,
    rows: list[dict[str, Any]],
    manifest: str,
    dependency: str,
    bindings: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = {
        name: bindings[f"payload/{name}.txt"] for name in profile.candidate_files
    }
    candidate["requirements.txt"] = bindings["requirements.txt"]
    candidate_digest = hashlib.sha256(b"".join(
        name.encode() + b"\0" + digest.encode() + b"\n"
        for name, digest in sorted(candidate.items())
    )).hexdigest()
    sources = {name: digest for name, _, digest in profile.base_sources}
    snapshots = {
        "candidate": candidate_digest,
        "dependencies": dependency,
        **{f"source/{name}": digest for name, digest in sources.items()},
    }
    versions = {row["name"]: row["version"] for row in rows}
    runtime_identity = _runtime_identity(target)
    suite = {
        "schema": "open-brain-native-authority-receipt-v1",
        "ok": True,
        "target": target,
        "runtime": runtime_identity,
        "manifest_sha256": manifest,
        "runner_sha256": bindings["payload/run_bundle.py.txt"],
        "candidate_files_sha256": candidate,
        "base_source_sha256": sources,
        "dependency_wheels_sha256": {row["filename"]: row["sha256"] for row in rows},
        "dependency_versions": versions,
        "snapshot_sha256": snapshots,
        "runtime_source_root": "candidate",
        "base_source_roots": {"engine": "source/engine", "app": "source/app"},
        "dependency_roots": ["dependencies"],
        "isolated_flags": ["-I", "-S", "-B"],
        "modules": authority.MODULES,
        "expected_tests_per_run": 40,
        "ephemeral_tls": True,
        "persisted_key_material": False,
        "model_calls": 0,
        "live_provider_approved": False,
        "full_nw0_approved": False,
        "dependency_tree_sha256": dependency,
        "raw_local_path": "/private/suite",
        "runs": [
            {
                "label": label,
                "tests": 40,
                "returncode": 0,
                "failure": None,
                "reaped": True,
                "direct_reaped": True,
                "group_terminated": True,
                "cleanup_known": True,
                "truncated": False,
                "elapsed_seconds": 1.0,
                "command": ["/private/child"],
            }
            for label in ("normal", "optimized")
        ],
    }
    preflight = {
        "schema": "open-brain-native-authority-preflight-v1",
        "ok": True,
        "target": target,
        "runtime": runtime_identity,
        "manifest_sha256": manifest,
        "runner_sha256": bindings["payload/run_bundle.py.txt"],
        "dependency_tree_sha256": dependency,
        "versions": versions,
        "isolated_flags": ["-I", "-S", "-B"],
        "snapshot_sha256": snapshots,
        "import_origins": {
            "open_brain.profile": "source/app",
            "open_brain_engine.core.ids": "source/engine",
            "rfc8785": "dependencies",
        },
        "raw_local_path": "/private/preflight",
    }
    return suite, preflight


def _write_bound(path: Path, data: bytes, bindings: dict[str, str], key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    bindings[key] = _sha(data)


def _execute_fixture(tmp_path: Path) -> dict[str, Any]:
    output = tmp_path / "output"
    output.mkdir()
    engine_files = {"open_brain_engine/core.py": b"ENGINE = True\n"}
    app_files = {"open_brain/profile.py": b"APP = True\n"}
    engine = tmp_path / "engine"
    app = tmp_path / "app"
    for root, files in ((engine, engine_files), (app, app_files)):
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    profile = run.ProofProfile(
        "authority",
        tmp_path / "unused-source",
        run.AUTHORITY_PROFILE.candidate_files,
        40,
        (("engine", engine, _tree_sha(engine_files)), ("app", app, _tree_sha(app_files))),
        ("expected-bundle-manifests.json",),
    )
    bindings = _bindings(profile)
    runtime = output / "runtime"
    for name in profile.candidate_files:
        _write_bound(
            runtime / "candidate" / name,
            f"synthetic {name}\n".encode(),
            bindings,
            f"payload/{name}.txt",
        )
    _write_bound(
        runtime / "candidate/requirements.txt",
        b"rfc8785==0.1.4\n",
        bindings,
        "requirements.txt",
    )
    runner_bytes = b"# synthetic runner\n"
    _write_bound(
        runtime / "runner/run_bundle.py",
        runner_bytes,
        bindings,
        "payload/run_bundle.py.txt",
    )
    metadata, manifests, contents = _fixed_inputs()
    manifest_bytes = b'{"synthetic":"fixed-manifest"}\n'
    manifests["macos-arm64"] = _sha(manifest_bytes)
    _write_bound(
        runtime / "dependency-wheels.json",
        json.dumps(metadata, sort_keys=True).encode(),
        bindings,
        "dependency-wheels.json",
    )
    _write_bound(
        runtime / "expected-bundle-manifests.json",
        json.dumps(manifests, sort_keys=True).encode(),
        bindings,
        "expected-bundle-manifests.json",
    )
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for filename, data in contents["macos-arm64"].items():
        (wheelhouse / filename).write_bytes(data)
    rows = metadata["platforms"]["macos-arm64"]
    suite, preflight = _receipts(
        profile,
        "macos-arm64",
        rows,
        manifests["macos-arm64"],
        metadata["dependency_tree_sha256"]["macos-arm64"],
        bindings,
    )
    return {
        "profile": profile,
        "output": output,
        "wheelhouse": wheelhouse,
        "bindings": bindings,
        "manifest_bytes": manifest_bytes,
        "runner_bytes": runner_bytes,
        "suite": suite,
        "preflight": preflight,
    }


def test_regular_and_bound_bytes_reject_aliases_specials_and_drift(tmp_path: Path) -> None:
    source = tmp_path / "plain.txt"
    source.write_bytes(b"fixed\n")
    assert authority.regular(source) == b"fixed\n"
    assert authority.bound_bytes(source, _sha(b"fixed\n")) == b"fixed\n"
    with pytest.raises(ValueError, match="changed"):
        authority.bound_bytes(source, "0" * 64)

    hardlink = tmp_path / "hardlink.txt"
    hardlink.hardlink_to(source)
    with pytest.raises(ValueError, match="nonregular"):
        authority.regular(source)
    hardlink.unlink()

    symlink = tmp_path / "symlink.txt"
    symlink.symlink_to(source)
    with pytest.raises(ValueError, match="linked"):
        authority.regular(symlink)

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="nonregular"):
        authority.regular(fifo)


@pytest.mark.parametrize("target", ["macos-arm64", "linux-x86_64"])
def test_target_inputs_accepts_each_complete_fixed_target(target: str) -> None:
    metadata, manifests, _ = _fixed_inputs()
    rows, manifest, dependency = authority.target_inputs(metadata, manifests, target)
    assert len(rows) == 14
    assert len({row["name"] for row in rows}) == 14
    assert manifest == manifests[target]
    assert dependency == metadata["dependency_tree_sha256"][target]


@pytest.mark.parametrize(
    "case",
    [
        "missing-target",
        "extra-target",
        "missing-wheel",
        "extra-wheel",
        "manifest-digest",
        "dependency-digest",
        "wheel-digest",
        "http",
        "foreign-host",
        "credentials",
        "port",
        "query",
        "fragment",
        "path-mismatch",
    ],
)
def test_target_inputs_rejects_incomplete_unpinned_or_unsafe_inputs(case: str) -> None:
    metadata, manifests, _ = _fixed_inputs()
    target = "macos-arm64"
    rows = metadata["platforms"][target]
    if case == "missing-target":
        del manifests["linux-x86_64"]
    elif case == "extra-target":
        metadata["platforms"]["windows-x86_64"] = []
    elif case == "missing-wheel":
        rows.pop()
    elif case == "extra-wheel":
        extra = copy.deepcopy(rows[-1])
        extra["name"] = "package-extra"
        extra["filename"] = "package-extra-1.0-py3-none-any.whl"
        extra["url"] = f"https://files.pythonhosted.org/packages/{extra['filename']}"
        rows.append(extra)
    elif case == "manifest-digest":
        manifests[target] = "A" * 64
    elif case == "dependency-digest":
        metadata["dependency_tree_sha256"][target] = "short"
    elif case == "wheel-digest":
        rows[0]["sha256"] = "g" * 64
    else:
        filename = rows[0]["filename"]
        rows[0]["url"] = {
            "http": f"http://files.pythonhosted.org/packages/{filename}",
            "foreign-host": f"https://example.invalid/packages/{filename}",
            "credentials": f"https://user@files.pythonhosted.org/packages/{filename}",
            "port": f"https://files.pythonhosted.org:444/packages/{filename}",
            "query": f"https://files.pythonhosted.org/packages/{filename}?download=1",
            "fragment": f"https://files.pythonhosted.org/packages/{filename}#wheel",
            "path-mismatch": "https://files.pythonhosted.org/packages/other.whl",
        }[case]
    with pytest.raises(ValueError):
        authority.target_inputs(metadata, manifests, target)


def test_source_snapshot_copies_bound_source_and_rejects_unsafe_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    included = {"pkg/module.py": b"VALUE = 1\n", "data/config.json": b"{}\n"}
    ignored = {"pkg/__pycache__/module.pyc": b"bytecode", "pkg/legacy.pyo": b"bytecode"}
    for name, data in {**included, **ignored}.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    destination = tmp_path / "snapshot"
    authority.source_snapshot(source, destination, _tree_sha(included))
    assert {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    } == included

    (source / "pkg/module.py").write_bytes(b"VALUE = 2\n")
    with pytest.raises(ValueError, match="snapshot mismatch"):
        authority.source_snapshot(source, tmp_path / "mutated", _tree_sha(included))
    assert not (tmp_path / "mutated").exists()

    linked_source = tmp_path / "linked-source"
    linked_source.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="linked source root"):
        authority.source_snapshot(linked_source, tmp_path / "linked-output", "0" * 64)

    special_source = tmp_path / "special-source"
    special_source.mkdir()
    os.mkfifo(special_source / "pipe")
    with pytest.raises(ValueError, match="nonregular"):
        authority.source_snapshot(special_source, tmp_path / "special-output", "0" * 64)


def test_prepare_wheels_uses_exact_offline_bytes_without_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows, contents = _wheel_rows("macos-arm64")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for filename, data in contents.items():
        (wheelhouse / filename).write_bytes(data)
    calls = []
    monkeypatch.setattr(run, "run_command", lambda *args, **kwargs: calls.append(args))
    output = tmp_path / "output"
    output.mkdir()
    destination = authority.prepare_wheels(rows, output, wheelhouse, {"PATH": "/usr/bin"})
    assert calls == []
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == contents


@pytest.mark.parametrize("case", ["extra", "missing", "size", "digest"])
def test_prepare_wheels_rejects_offline_set_or_byte_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    rows, contents = _wheel_rows("linux-x86_64")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for filename, data in contents.items():
        (wheelhouse / filename).write_bytes(data)
    selected = wheelhouse / rows[0]["filename"]
    if case == "extra":
        (wheelhouse / "unexpected.whl").write_bytes(b"extra")
    elif case == "missing":
        selected.unlink()
    elif case == "size":
        selected.write_bytes(selected.read_bytes() + b"x")
    else:
        selected.write_bytes(b"x" * len(selected.read_bytes()))
    calls = []
    monkeypatch.setattr(run, "run_command", lambda *args, **kwargs: calls.append(args))
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(ValueError):
        authority.prepare_wheels(rows, output, wheelhouse, {"PATH": "/usr/bin"})
    assert calls == []


@pytest.mark.parametrize("target", ["macos-arm64", "linux-x86_64"])
def test_public_result_accepts_complete_receipts_and_omits_private_paths(target: str) -> None:
    metadata, manifests, _ = _fixed_inputs()
    profile = run.AUTHORITY_PROFILE
    bindings = _bindings(profile)
    rows = metadata["platforms"][target]
    dependency = metadata["dependency_tree_sha256"][target]
    suite, preflight = _receipts(
        profile, target, rows, manifests[target], dependency, bindings,
    )
    result = authority.public_result(
        suite,
        preflight,
        profile=profile,
        target=target,
        rows=rows,
        manifest=manifests[target],
        dependency=dependency,
        bindings=bindings,
    )
    encoded = json.dumps(result)
    assert result["target"] == target
    assert [row["tests"] for row in result["runs"]] == [40, 40]
    assert result["metadata_preflight_passed"] is True
    assert result["synthetic_authority_proof_passed"] is True
    assert "/private/" not in encoded
    assert "command" not in encoded
    assert "raw_local_path" not in encoded


@pytest.mark.parametrize(
    "case",
    [
        "target",
        "count",
        "cleanup",
        "truncated",
        "preflight-target",
        "manifest",
        "runner",
        "wheel-anchor",
        "source-anchor",
        "versions",
        "suite-schema",
        "preflight-schema",
        "ephemeral-tls",
        "persisted-key",
        "model-calls",
        "live-provider",
        "full-nw0",
        "suite-runtime",
        "preflight-runtime",
        "suite-dependency-tree",
        "preflight-dependency-tree",
    ],
)
def test_public_result_rejects_incomplete_or_misbound_receipts(case: str) -> None:
    metadata, manifests, _ = _fixed_inputs()
    profile = run.AUTHORITY_PROFILE
    target = "macos-arm64"
    rows = metadata["platforms"][target]
    dependency = metadata["dependency_tree_sha256"][target]
    bindings = _bindings(profile)
    suite, preflight = _receipts(
        profile, target, rows, manifests[target], dependency, bindings,
    )
    if case == "target":
        suite["target"] = "linux-x86_64"
    elif case == "count":
        suite["runs"][0]["tests"] = 39
    elif case == "cleanup":
        suite["runs"][1]["cleanup_known"] = False
    elif case == "truncated":
        suite["runs"][1]["truncated"] = True
    elif case == "preflight-target":
        preflight["target"] = "linux-x86_64"
    elif case == "manifest":
        suite["manifest_sha256"] = "0" * 64
    elif case == "runner":
        suite["runner_sha256"] = "0" * 64
    elif case == "wheel-anchor":
        suite["dependency_wheels_sha256"][rows[0]["filename"]] = "0" * 64
    elif case == "source-anchor":
        suite["base_source_sha256"]["engine"] = "0" * 64
    elif case == "versions":
        suite["dependency_versions"][rows[0]["name"]] = "changed"
    elif case == "suite-schema":
        suite["schema"] = "wrong-schema"
    elif case == "preflight-schema":
        preflight["schema"] = "wrong-schema"
    elif case == "ephemeral-tls":
        suite["ephemeral_tls"] = False
    elif case == "persisted-key":
        suite["persisted_key_material"] = True
    elif case == "model-calls":
        suite["model_calls"] = False
    elif case == "live-provider":
        suite["live_provider_approved"] = True
    elif case == "full-nw0":
        suite["full_nw0_approved"] = True
    elif case == "suite-runtime":
        suite["runtime"]["machine"] = "wrong-machine"
    elif case == "preflight-runtime":
        preflight["runtime"]["python_version"] = "3.13.0"
    elif case == "suite-dependency-tree":
        suite["dependency_tree_sha256"] = "0" * 64
    else:
        preflight["dependency_tree_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        authority.public_result(
            suite,
            preflight,
            profile=profile,
            target=target,
            rows=rows,
            manifest=manifests[target],
            dependency=dependency,
            bindings=bindings,
        )


@pytest.mark.parametrize("root", ["candidate", "source/engine"])
def test_public_result_rejects_matching_preflight_and_suite_snapshot_drift(root: str) -> None:
    metadata, manifests, _ = _fixed_inputs()
    profile = run.AUTHORITY_PROFILE
    target = "macos-arm64"
    rows = metadata["platforms"][target]
    dependency = metadata["dependency_tree_sha256"][target]
    bindings = _bindings(profile)
    suite, preflight = _receipts(
        profile, target, rows, manifests[target], dependency, bindings,
    )
    suite["snapshot_sha256"][root] = "0" * 64
    preflight["snapshot_sha256"] = copy.deepcopy(suite["snapshot_sha256"])
    with pytest.raises(ValueError, match="preflight or runtime roots"):
        authority.public_result(
            suite,
            preflight,
            profile=profile,
            target=target,
            rows=rows,
            manifest=manifests[target],
            dependency=dependency,
            bindings=bindings,
        )


def test_execute_checks_fixed_boundaries_and_projects_mocked_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _execute_fixture(tmp_path)
    output = fixture["output"]
    commands = []

    def fake_command(
        command: list[str], root: Path, environment: dict[str, str], label: str,
        *, timeout: int, max_log_bytes: int,
    ) -> None:
        commands.append((label, command))
        assert root == output
        assert environment == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        assert timeout == {"build": 30, "preflight": 20, "runner": 75}[label]
        assert max_log_bytes == 262144
        if label == "build":
            bundle = output / "bundle"
            bundle.mkdir()
            (bundle / "manifest.json").write_bytes(fixture["manifest_bytes"])
            (bundle / "run_bundle.py").write_bytes(fixture["runner_bytes"])
        elif label == "preflight":
            (output / "preflight.json").write_text(json.dumps(fixture["preflight"]))
        else:
            (output / "tests").mkdir()
            (output / "tests/receipt.json").write_text(json.dumps(fixture["suite"]))

    monkeypatch.setattr(run, "run_command", fake_command)
    result = authority.execute(
        fixture["profile"],
        output,
        fixture["wheelhouse"],
        "macos-arm64",
        fixture["bindings"],
    )
    assert [label for label, _ in commands] == ["build", "preflight", "runner"]
    build = commands[0][1]
    assert build[:5] == [
        sys.executable,
        "-I",
        "-B",
        str(output / "runtime/runner/run_bundle.py"),
        "build",
    ]
    for _, command in commands[1:]:
        assert command[:3] == [sys.executable, "-I", "-B"]
        assert command[command.index("--expected-manifest-sha256") + 1] == fixture[
            "suite"
        ]["manifest_sha256"]
    assert result["target"] == "macos-arm64"
    assert [row["tests"] for row in result["runs"]] == [40, 40]
    assert "/private/" not in json.dumps(result)


@pytest.mark.parametrize(
    ("case", "expected_commands"),
    [
        ("metadata-before-build", []),
        ("manifests-before-build", []),
        ("runner-before-build", []),
        ("manifest-after-build", ["build"]),
        ("runner-after-preflight", ["build", "preflight"]),
    ],
)
def test_execute_rejects_mutation_before_any_later_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_commands: list[str],
) -> None:
    fixture = _execute_fixture(tmp_path)
    output = fixture["output"]
    if case == "metadata-before-build":
        (output / "runtime/dependency-wheels.json").write_bytes(b"{}\n")
    elif case == "manifests-before-build":
        (output / "runtime/expected-bundle-manifests.json").write_bytes(b"{}\n")
    elif case == "runner-before-build":
        (output / "runtime/runner/run_bundle.py").write_bytes(b"changed\n")
    commands = []

    def fake_command(
        command: list[str], root: Path, environment: dict[str, str], label: str,
        *, timeout: int, max_log_bytes: int,
    ) -> None:
        del command, root, environment, timeout, max_log_bytes
        commands.append(label)
        if label == "build":
            bundle = output / "bundle"
            bundle.mkdir()
            manifest = (
                b'{"wrong":"manifest"}\n'
                if case == "manifest-after-build"
                else fixture["manifest_bytes"]
            )
            (bundle / "manifest.json").write_bytes(manifest)
            (bundle / "run_bundle.py").write_bytes(fixture["runner_bytes"])
        elif label == "preflight":
            (output / "preflight.json").write_text(json.dumps(fixture["preflight"]))
            if case == "runner-after-preflight":
                (output / "bundle/run_bundle.py").write_bytes(b"changed\n")
        else:
            raise AssertionError("suite command must not run after a detected mutation")

    monkeypatch.setattr(run, "run_command", fake_command)
    with pytest.raises(ValueError):
        authority.execute(
            fixture["profile"],
            output,
            fixture["wheelhouse"],
            "macos-arm64",
            fixture["bindings"],
        )
    assert commands == expected_commands
