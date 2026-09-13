"""Build and verify the separately packaged Graphify Markdown helper."""

from __future__ import annotations

import gzip
import importlib.metadata
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Final, cast

from tools.open_brain_dev import artifact_audit, base_native

_SOURCE = Path("release/open-brain-graphify")
_SPEC = "open-brain-graphify.spec"
_EXECUTABLE = "open-brain-graphify"
_ARCHIVE_EXECUTABLE = "open-brain"
_LEGAL_FILES: Final = ("LICENSE", "LICENSE-MIT", "NOTICE", "OPEN-BRAIN-NOTICE")
_DEPENDENCIES: Final = {
    "graphifyy": "0.9.57",
    "markdown-it-py": "4.0.0",
    "mdit-py-plugins": "0.5.0",
    "mdurl": "0.1.2",
    "pyyaml": "6.0.3",
}
_GRAPHIFY_MODULES: Final = {
    "graphify",
    "graphify.discovery_rules",
    "graphify.extractors",
    "graphify.extractors.base",
    "graphify.extractors.markdown",
    "graphify.ids",
    "graphify.metadata",
}
_PROTOCOL = "open-brain-graphify-helper-v1"
_CAPABILITIES: Final = {
    "component": {
        "graphify_version": "0.9.57",
        "parser_profile": "pyyaml-6.0.3-pure-python",
        "patch_id": "ob1-graphify-markdown-1",
        "patch_sha256": "f7417ee080e1f5050f41b525f4bb0f97910c14abf6531dcf38cbd2ff28da796b",
        "upstream_commit": "3f82bf7f837a07fb0f7668fbdbd5662801906942",
    },
    "max_input_bytes": 16 * 1024,
    "max_output_bytes": 16 * 1024,
    "operations": ["extract_markdown"],
    "protocol": _PROTOCOL,
}


def build_graphify_artifact(root: Path, output: Path, *, version: str) -> tuple[Path, Path]:
    """Build one helper and its reproducible, licensed release archive."""
    if sys.version_info[:2] != (3, 14):
        raise base_native.BaseNativeError("Graphify native build requires Python 3.14")
    source = root.resolve(strict=True) / _SOURCE
    if not source.is_dir():
        raise base_native.BaseNativeError("Graphify release inputs are unavailable")
    selected_output = output.resolve()
    stage = selected_output / "graphify-stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    inputs = stage / "inputs"
    _install_inputs(source, inputs, root)
    _apply_patch(source, inputs)
    for item in sorted((source / "src").iterdir()):
        if not item.is_file() or item.suffix != ".py":
            raise base_native.BaseNativeError("Graphify helper source inventory is invalid")
        shutil.copyfile(item, stage / item.name)
    shutil.copyfile(source / _SPEC, stage / _SPEC)
    subprocess.run(
        (
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            os.fspath(selected_output / "dist"),
            "--workpath",
            os.fspath(selected_output / "work-graphify"),
            os.fspath(stage / _SPEC),
        ),
        cwd=root.resolve(strict=True),
        check=True,
        timeout=1800,
    )
    artifact = selected_output / f"dist/{_EXECUTABLE}"
    audit_graphify_artifact(artifact)
    _verify_helper_protocol(artifact)
    release = selected_output / "release"
    release.mkdir(parents=True, exist_ok=True)
    platform_tag = base_native.native_platform_tag()
    archive = release / f"open-brain-graphify-{version}-{platform_tag}.tar.gz"
    write_graphify_archive(artifact, source / "licenses", archive)
    comparison = selected_output / archive.name
    write_graphify_archive(artifact, source / "licenses", comparison)
    if _digest(archive) != _digest(comparison):
        raise base_native.BaseNativeError("Graphify archive is not reproducible")
    comparison.unlink()
    if artifact_audit.inspect_artifact(archive, ()):
        raise base_native.BaseNativeError("Graphify archive content audit failed")
    return artifact, archive


def audit_graphify_artifact(artifact: Path) -> base_native.BaseNativeAudit:
    """Verify the helper's executable shape and narrow dependency closure."""
    try:
        metadata = artifact.lstat()
        if (
            artifact.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & stat.S_IXUSR == 0
        ):
            raise base_native.BaseNativeError("Graphify native artifact is invalid")
        modules = base_native.archive_modules(artifact)
        graphify_modules = {
            name for name in modules if name == "graphify" or name.startswith("graphify.")
        }
        forbidden = (
            *base_native._FORBIDDEN_MODULE_PREFIXES,
            "_yaml",
            "frontmatter_probe",
            "networkx",
            "numpy",
            "open_brain",
            "rapidfuzz",
            "tree_sitter",
            "yaml._yaml",
            "yaml.cyaml",
        )
        if graphify_modules != _GRAPHIFY_MODULES or any(
            name == prefix or name.startswith(prefix + ".")
            for name in modules
            for prefix in forbidden
        ):
            raise base_native.BaseNativeError("Graphify native dependency closure is invalid")
        platform_tag = base_native.native_platform_tag()
        signature = base_native._validate_native_executable(artifact, platform_tag)
        encoded_modules = "\n".join(modules).encode("utf-8")
        return base_native.BaseNativeAudit(
            platform_tag=platform_tag,
            module_count=len(modules),
            modules_sha256=sha256(encoded_modules).hexdigest(),
            executable_sha256=_digest(artifact),
            signature=signature,
        )
    except base_native.BaseNativeError:
        raise
    except OSError as error:
        raise base_native.BaseNativeError("Graphify native artifact is unavailable") from error


def write_graphify_archive(artifact: Path, licenses: Path, archive: Path) -> Path:
    """Write the helper and its required notices with stable archive metadata."""
    members = {_ARCHIVE_EXECUTABLE: artifact}
    members.update({f"licenses/graphify/{name}": licenses / name for name in _LEGAL_FILES})
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle,
    ):
        for name, source in sorted(members.items()):
            with source.open("rb") as stream:
                info = tarfile.TarInfo(name)
                info.size = source.stat().st_size
                info.mode = 0o755 if name == _ARCHIVE_EXECUTABLE else 0o644
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                bundle.addfile(info, stream)
    return archive


def _install_inputs(source: Path, inputs: Path, root: Path) -> None:
    subprocess.run(
        (
            "uv",
            "--no-config",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            os.fspath(inputs),
            "--no-deps",
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--index-url",
            "https://pypi.org/simple",
            "-r",
            os.fspath(source / "requirements.txt"),
        ),
        cwd=root.resolve(strict=True),
        check=True,
        timeout=600,
    )
    actual = {
        distribution.metadata["Name"].casefold(): distribution.version
        for distribution in importlib.metadata.distributions(path=[os.fspath(inputs)])
    }
    if actual != _DEPENDENCIES:
        raise base_native.BaseNativeError("Graphify dependency versions are invalid")


def _apply_patch(source: Path, inputs: Path) -> None:
    try:
        rows = json.loads((source / "patch-manifest.json").read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise base_native.BaseNativeError("Graphify patch manifest is invalid") from error
    if not isinstance(rows, list) or not rows:
        raise base_native.BaseNativeError("Graphify patch manifest is invalid")
    for value in rows:
        if not isinstance(value, dict):
            raise base_native.BaseNativeError("Graphify patch manifest is invalid")
        row = cast(dict[str, object], value)
        path_value = row.get("path")
        original = row.get("original_sha256")
        candidate = row.get("candidate_sha256")
        changed = row.get("changed")
        if (
            not isinstance(path_value, str)
            or not isinstance(candidate, str)
            or len(candidate) != 64
            or original is not None and not isinstance(original, str)
            or type(changed) is not bool
        ):
            raise base_native.BaseNativeError("Graphify patch manifest is invalid")
        target = inputs / path_value
        before = _digest(target) if target.is_file() else None
        if before != original:
            raise base_native.BaseNativeError("Graphify upstream source preimage changed")
        if changed:
            replacement = source / "overrides" / path_value
            if not replacement.is_file() or _digest(replacement) != candidate:
                raise base_native.BaseNativeError("Graphify maintained patch input changed")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(replacement, target)
        if not target.is_file() or _digest(target) != candidate:
            raise base_native.BaseNativeError("Graphify staged source digest changed")


def _verify_helper_protocol(artifact: Path) -> None:
    environment = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    capabilities = _run_helper(artifact, ("--capabilities",), b"", environment)
    if capabilities != _CAPABILITIES:
        raise base_native.BaseNativeError("Graphify helper capabilities are invalid")
    first = "page_00000000-0000-4000-8000-000000000101"
    second = "page_00000000-0000-4000-8000-000000000102"
    request = _encode(
        {
            "notes": [
                {"body": "# First\n[Second](second.md)\n", "id": first, "path": "first.md"},
                {"body": "# Second\n", "id": second, "path": "second.md"},
            ],
            "operation": "extract_markdown",
            "protocol": _PROTOCOL,
        }
    )
    result = _run_helper(artifact, ("--extract-markdown",), request, environment)
    if result != {
        "diagnostics": [],
        "links": [{"kind": "explicit_reference", "source": first, "target": second}],
        "pages": [first, second],
        "protocol": _PROTOCOL,
        "status": "ok",
    }:
        raise base_native.BaseNativeError("Graphify helper extraction failed")


def _run_helper(
    artifact: Path,
    arguments: tuple[str, ...],
    request: bytes,
    environment: Mapping[str, str],
) -> object:
    try:
        result = subprocess.run(
            (os.fspath(artifact.resolve(strict=True)), *arguments),
            input=request,
            env=dict(environment),
            check=True,
            capture_output=True,
            timeout=60,
        )
        if result.stderr or len(result.stdout) > 16 * 1024:
            raise base_native.BaseNativeError("Graphify helper response is invalid")
        value = json.loads(result.stdout)
        if _encode(value) != result.stdout:
            raise base_native.BaseNativeError("Graphify helper response is invalid")
        return value
    except base_native.BaseNativeError:
        raise
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise base_native.BaseNativeError("Graphify helper execution failed") from error


def _encode(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return sha256(stream.read()).hexdigest()


__all__ = [
    "audit_graphify_artifact",
    "build_graphify_artifact",
    "write_graphify_archive",
]
