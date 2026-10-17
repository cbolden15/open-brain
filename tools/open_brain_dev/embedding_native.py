"""Build, audit and package the optional embedding resource archive.

The archive uses the final installed prefix-relative paths of the embedding role in
component manifest v2. ONNX Runtime itself is built elsewhere from the pinned recipe
(``tools/g4_embedding_probe/ort-build-recipe.json``); this tool consumes the resulting
library, builds the Rust helper, audits linkage, imports and telemetry, gathers every
notice and writes a byte-reproducible archive plus its manifest resource record.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import platform as host_platform
import re
import stat
import subprocess
import sys
import tarfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Protocol

from tools.open_brain_dev.component_manifest_v2 import (
    EXECUTABLES,
    MODEL_PATH,
    PLATFORMS,
    PROTOCOL,
    TOKENIZER_PATHS,
    ComponentManifestError,
    Inventory,
    Member,
    ResourceV2,
    archive_filename,
    component_id,
    inventory_path,
    parse_inventory,
    render_inventory,
    safe_extract,
    validate_resource_inventory,
    verify_extracted_resource,
)

_HELPER_SOURCE: Final = Path("release/open-brain-embedding")
_HELPER_NAME: Final = "open-brain-embedding"
_RECIPE: Final = Path("tools/g4_embedding_probe/ort-build-recipe.json")
_PINS: Final = Path("tools/g4_embedding_probe/assets.json")
_MODEL_ID: Final = "minilm"
_RUNTIME_ROOT: Final = "libexec/open-brain-embedding/runtime"
_LICENSES: Final = "share/open-brain/licenses/embedding"
_LICENSE_NAME: Final = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|UNLICENSE)", re.IGNORECASE)

_TARGETS: Final = {
    "macos-arm64": ("aarch64-apple-darwin", "libonnxruntime.dylib", "11.0", ("neon",)),
    "linux-x86_64": ("x86_64-unknown-linux-gnu", "libonnxruntime.so", "glibc 2.38", ("sse2",)),
}
# Selected profile ob.minilm-wordpiece-mean.v1 (EMBEDDING-SPIKE-RESULTS.md).
PROFILE: Final[dict[str, object]] = {
    "profile_id": "ob.minilm-wordpiece-mean.v1",
    "dimensions": 384,
    "pooling": "mean",
    "max_query_tokens": 254,
    "document_window_tokens": 224,
    "document_overlap_tokens": 32,
    "cls_token_id": 101,
    "sep_token_id": 102,
    "pad_token_id": 0,
    "query_prefix": "",
    "segmenter_version": "ob.token-window.v1",
}
# Shipped asset: path below the asset directory -> installed path.
_ASSET_FILES: Final = {
    "onnx/model.onnx": MODEL_PATH,
    "tokenizer.json": TOKENIZER_PATHS["tokenizer_json_sha256"],
    "config.json": TOKENIZER_PATHS["tokenizer_config_sha256"],
    "vocab.txt": TOKENIZER_PATHS["vocab_sha256"],
}

_MACOS_LIBRARIES: Final = re.compile(
    r"^(/usr/lib/(libSystem\.B|libiconv\.2|libc\+\+\.1|libc\+\+abi)\.dylib"
    r"|/System/Library/Frameworks/(Foundation|CoreFoundation)\.framework/Versions/[A-Z]/\3)$"
)
_LINUX_LIBRARIES: Final = frozenset(
    {
        "libc.so.6",
        "libm.so.6",
        "libgcc_s.so.1",
        "libstdc++.so.6",
        "ld-linux-x86-64.so.2",
        "libdl.so.2",
        "libpthread.so.0",
        "librt.so.1",
    }
)
_NETWORK_EXACT: Final = frozenset({"socket", "connect", "getaddrinfo", "gethostbyname"})
_NETWORK_PARTS: Final = ("nsurlsession", "cfnetwork", "curl")
_TELEMETRY: Final = (
    b"events.data.microsoft.com",
    b"onecollector",
    b"vortex.data",
    b"telemetry.microsoft",
    b"microsoft::applications::events",
    b"ilogmanager",
)
_MAX_MINOS: Final = (11, 0, 0)
_CHUNK: Final = 8 * 1024 * 1024
_NOTICE_LIMIT: Final = 16 * 1024 * 1024
EMBEDDING_FILE_LIMIT: Final = 160 * 1024 * 1024
_CRATE_LIMIT: Final = 1024 * 1024

_MODEL_NOTICE: Final = """\
all-MiniLM-L6-v2 (sentence-transformers/all-MiniLM-L6-v2)

The model graph in libexec/open-brain-embedding/model/model.onnx and the tokenizer files
under libexec/open-brain-embedding/tokenizer/ are the unmodified files onnx/model.onnx,
tokenizer.json, config.json and vocab.txt of revision {revision} of that Hugging Face
repository.

The repository contains no license file. Its model card designates the Apache License,
Version 2.0 (license: apache-2.0). The full text is in Apache-2.0.txt in this directory.
"""


class EmbeddingNativeError(RuntimeError):
    """The embedding resource cannot be built or an audit refused its inputs."""


class Runner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str: ...


def run_command(
    args: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> str:
    merged = None if env is None else {**os.environ, **env}
    try:
        done = subprocess.run(
            list(args),
            cwd=cwd,
            env=merged,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=3600,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EmbeddingNativeError(f"cannot run {args[0]}") from error
    if done.returncode != 0:
        raise EmbeddingNativeError(f"{args[0]} failed: {done.stderr.strip()[-500:]}")
    return done.stdout


def _canonical(value: object) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(path: Path, *, limit: int) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise EmbeddingNativeError(f"not a regular file: {path}")
        if info.st_size > limit:
            raise EmbeddingNativeError(f"file exceeds its size limit: {path}")
        return path.read_bytes()
    except OSError as error:
        raise EmbeddingNativeError(f"cannot read {path}") from error


# ------------------------------------------------------------------ audits


def linked_libraries(path: Path, platform: str, runner: Runner) -> list[str]:
    if platform == "macos-arm64":
        lines = runner(["otool", "-L", os.fspath(path)]).splitlines()[1:]
        return [line.strip().split(" (")[0] for line in lines if line.strip()]
    output = runner(["readelf", "-d", os.fspath(path)])
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", output)


def audit_linkage(path: Path, platform: str, runner: Runner, *, runtime: bool) -> None:
    """Allow only system libraries; a runtime may name itself, a helper never links it."""
    for library in linked_libraries(path, platform, runner):
        if os.path.basename(library).startswith("libonnxruntime"):
            if not runtime:
                raise EmbeddingNativeError(f"{path.name} links the ONNX Runtime library")
            continue
        allowed = (
            _MACOS_LIBRARIES.fullmatch(library) is not None
            if platform == "macos-arm64"
            else library in _LINUX_LIBRARIES
        )
        if not allowed:
            raise EmbeddingNativeError(f"{path.name} links a disallowed library: {library}")


def imported_symbols(path: Path, platform: str, runner: Runner) -> list[str]:
    if platform == "macos-arm64":
        output = runner(["nm", "-u", os.fspath(path)])
    else:
        output = runner(["nm", "-D", "--undefined-only", os.fspath(path)])
    names = []
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[-1].endswith(":"):
            continue
        name = parts[-1].split("@", 1)[0]
        if platform == "macos-arm64" and name.startswith("_"):
            name = name[1:]
        names.append(name)
    return names


def audit_network_symbols(path: Path, platform: str, runner: Runner) -> None:
    for name in imported_symbols(path, platform, runner):
        lowered = name.lower()
        if name in _NETWORK_EXACT or any(part in lowered for part in _NETWORK_PARTS):
            raise EmbeddingNativeError(f"{path.name} imports a network symbol: {name}")


def audit_telemetry_strings(path: Path) -> None:
    keep = max(len(item) for item in _TELEMETRY) - 1
    tail = b""
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                window = (tail + chunk).lower()
                for needle in _TELEMETRY:
                    if needle in window:
                        text = needle.decode("ascii")
                        raise EmbeddingNativeError(f"{path.name} contains telemetry string: {text}")
                tail = window[-keep:]
    except OSError as error:
        raise EmbeddingNativeError(f"cannot read {path}") from error


def audit_minimum_macos(path: Path, runner: Runner) -> None:
    output = runner(["otool", "-l", os.fspath(path)])
    found = re.search(
        r"cmd LC_BUILD_VERSION\b.*?\bminos (\d+(?:\.\d+)*)"
        r"|cmd LC_VERSION_MIN_MACOSX\b.*?\bversion (\d+(?:\.\d+)*)",
        output,
        re.DOTALL,
    )
    if found is None:
        raise EmbeddingNativeError(f"{path.name} has no minimum macOS version")
    text = found.group(1) or found.group(2)
    parts = [int(item) for item in text.split(".")]
    parts += [0] * (3 - len(parts))
    if tuple(parts) > _MAX_MINOS:
        raise EmbeddingNativeError(f"{path.name} requires macOS {text}, above 11.0")


def audit_telemetry_cache(runtime_library: Path) -> None:
    cache = runtime_library.parent / "CMakeCache.txt"
    text = _read_regular(cache, limit=_NOTICE_LIMIT).decode("utf-8", errors="replace")
    if "onnxruntime_USE_TELEMETRY:BOOL=OFF" not in text.splitlines():
        raise EmbeddingNativeError("ONNX Runtime build did not compile telemetry out")


def audit_binary(path: Path, platform: str, runner: Runner, *, runtime: bool) -> None:
    audit_linkage(path, platform, runner, runtime=runtime)
    audit_network_symbols(path, platform, runner)
    if platform == "macos-arm64":
        audit_minimum_macos(path, runner)
    if runtime:
        audit_telemetry_strings(path)
        audit_telemetry_cache(path)


# ---------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Crate:
    name: str
    version: str
    directory: Path


def crate_closure(metadata: Mapping[str, object]) -> tuple[Crate, ...]:
    """Packages reachable from the root through normal (non-dev, non-build) edges."""
    try:
        resolve = metadata["resolve"]
        assert isinstance(resolve, dict)
        root = resolve["root"]
        nodes = {node["id"]: node for node in resolve["nodes"]}
        packages = {item["id"]: item for item in metadata["packages"]}  # type: ignore[attr-defined]
        seen = {root}
        pending = [root]
        while pending:
            for dep in nodes[pending.pop()]["deps"]:
                normal = any(kind["kind"] is None for kind in dep["dep_kinds"])
                if normal and dep["pkg"] not in seen:
                    seen.add(dep["pkg"])
                    pending.append(dep["pkg"])
        seen.discard(root)
        return tuple(
            sorted(
                (
                    Crate(
                        packages[item]["name"],
                        packages[item]["version"],
                        Path(packages[item]["manifest_path"]).parent,
                    )
                    for item in seen
                ),
                key=lambda crate: (crate.name, crate.version),
            )
        )
    except (KeyError, TypeError, AssertionError) as error:
        raise EmbeddingNativeError("cargo metadata is malformed") from error


def tokenizer_build(metadata: Mapping[str, object]) -> str:
    for package in metadata["packages"]:  # type: ignore[attr-defined]
        if package["name"] == "tokenizers":
            return f"tokenizers {package['version']} fancy-regex"
    raise EmbeddingNativeError("tokenizers is not in the helper dependency closure")


def _crate_notice(crate: Crate) -> bytes | None:
    try:
        names = sorted(
            item.name
            for item in crate.directory.iterdir()
            if _LICENSE_NAME.match(item.name) and item.is_file() and not item.is_symlink()
        )
    except OSError as error:
        raise EmbeddingNativeError(f"cannot read crate {crate.name}") from error
    if not names:
        return None
    out = io.BytesIO()
    for name in names:
        data = _read_regular(crate.directory / name, limit=_CRATE_LIMIT)
        out.write(f"==== {name} ====\n".encode())
        out.write(data if data.endswith(b"\n") else data + b"\n")
    return out.getvalue()


def collect_notices(
    *, apache_text: bytes, revision: str, runtime_source: Path, crates: Iterable[Crate]
) -> dict[str, bytes]:
    """Installed path -> bytes for every notice; refuses crates without license files."""
    notices = {
        f"{_LICENSES}/Apache-2.0.txt": apache_text,
        f"{_LICENSES}/all-MiniLM-L6-v2-NOTICE.txt": _MODEL_NOTICE.format(
            revision=revision
        ).encode(),
        f"{_LICENSES}/onnxruntime-LICENSE.txt": _read_regular(
            runtime_source / "LICENSE", limit=_NOTICE_LIMIT
        ),
        f"{_LICENSES}/onnxruntime-ThirdPartyNotices.txt": _read_regular(
            runtime_source / "ThirdPartyNotices.txt", limit=_NOTICE_LIMIT
        ),
    }
    missing = []
    for crate in crates:
        text = _crate_notice(crate)
        if text is None:
            missing.append(f"{crate.name} {crate.version}")
        else:
            notices[f"{_LICENSES}/crates/{crate.name}-{crate.version}.txt"] = text
    if missing:
        raise EmbeddingNativeError("crates without license files: " + ", ".join(missing))
    return notices


def notices_digest(notices: Mapping[str, bytes], paths: Iterable[str] | None = None) -> str:
    """SHA-256 of the canonical sorted list of notice paths (below the licenses root)."""
    chosen = sorted(notices if paths is None else paths)
    rows = [
        {"path": path.removeprefix(_LICENSES + "/"), "sha256": _sha256(notices[path])}
        for path in chosen
    ]
    return _sha256(_canonical(rows))


def load_pins(root: Path) -> dict[str, object]:
    document = json.loads(_read_regular(root / _PINS, limit=_NOTICE_LIMIT))
    for candidate in document["candidates"]:
        if candidate["id"] == _MODEL_ID:
            result: dict[str, object] = candidate
            return result
    raise EmbeddingNativeError("model pins are missing")


def verify_assets(assets: Path, pins: Mapping[str, object]) -> dict[str, bytes]:
    """Re-verify the four shipped files against the pins; installed path -> bytes."""
    files = pins["files"]
    assert isinstance(files, list)
    by_path = {item["path"]: item for item in files}
    result = {}
    for relative, installed in _ASSET_FILES.items():
        pin = by_path.get(relative)
        if pin is None:
            raise EmbeddingNativeError(f"no pin for {relative}")
        data = _read_regular(assets / relative, limit=int(pin["bytes"]) + 1)
        if len(data) != pin["bytes"]:
            raise EmbeddingNativeError(f"{relative} differs from its pinned size")
        if "lfs_sha256" in pin:
            ok = _sha256(data) == pin["lfs_sha256"]
        else:
            ok = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == pin["git_blob_sha1"]
        if not ok:
            raise EmbeddingNativeError(f"{relative} differs from its pin")
        result[installed] = data
    return result


# ----------------------------------------------------------------- archive


@dataclass(frozen=True)
class _File:
    path: str
    mode: int
    data: bytes


@dataclass(frozen=True)
class BuildResult:
    archive: Path
    record: Path
    resource: ResourceV2
    inventory: Inventory
    notice_count: int


def _directories(paths: Iterable[str]) -> list[str]:
    found: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        found.update("/".join(parts[:count]) for count in range(1, len(parts) + 1))
    return sorted(found)


def _write_archive(destination: Path, files: Sequence[_File], directories: Sequence[str]) -> None:
    entries: list[tuple[str, int, bytes | None]] = [(item, 0o755, None) for item in directories]
    entries += [(item.path, item.mode, item.data) for item in files]
    entries.sort(key=lambda entry: entry[0])
    temporary = destination.with_name(destination.name + ".partial")
    try:
        with (
            open(temporary, "wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as zipped,
            tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as tar,
        ):
            for name, mode, data in entries:
                info = tarfile.TarInfo(name)
                info.mode = mode
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if data is None:
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                else:
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _host() -> str:
    machine = host_platform.machine().lower()
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-arm64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    return f"{sys.platform}-{machine}"


def build_embedding_resource(
    *,
    root: Path,
    version: str,
    platform: str,
    runtime_library: Path,
    runtime_source: Path,
    assets: Path,
    output: Path,
    runner: Runner = run_command,
    pins: Mapping[str, object] | None = None,
    host: str | None = None,
) -> BuildResult:
    """Audit inputs, build the helper and write the archive and its resource record."""
    if platform not in PLATFORMS:
        raise EmbeddingNativeError(f"unsupported platform: {platform}")
    if (host or _host()) != platform:
        raise EmbeddingNativeError("the helper is built natively; run on the target platform")
    triple, runtime_name, minimum_os, features = _TARGETS[platform]
    root = root.resolve()
    helper_dir = root / _HELPER_SOURCE
    recipe_bytes = _read_regular(root / _RECIPE, limit=_NOTICE_LIMIT)
    runtime_commit = json.loads(recipe_bytes)["commit"]

    if (
        runner(["git", "-C", os.fspath(runtime_source), "rev-parse", "HEAD"]).strip()
        != runtime_commit
    ):
        raise EmbeddingNativeError("ONNX Runtime source is not at the recipe commit")
    audit_binary(runtime_library, platform, runner, runtime=True)

    if runner(
        ["git", "-C", os.fspath(root), "status", "--porcelain", "--", _HELPER_SOURCE.as_posix()]
    ).strip():
        raise EmbeddingNativeError("helper sources have uncommitted changes")
    source_commit = runner(["git", "-C", os.fspath(root), "rev-parse", "HEAD"]).strip()

    output.mkdir(parents=True, exist_ok=True)
    manifest_path = helper_dir / "Cargo.toml"
    target_dir = output / "cargo-target"
    build_env = {"MACOSX_DEPLOYMENT_TARGET": "11.0"} if platform == "macos-arm64" else {}
    runner(
        [
            "cargo",
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            os.fspath(manifest_path),
            "--target-dir",
            os.fspath(target_dir),
        ],
        cwd=helper_dir,
        env=build_env,
    )
    helper_bytes = _read_regular(target_dir / "release" / _HELPER_NAME, limit=EMBEDDING_FILE_LIMIT)
    audit_binary(target_dir / "release" / _HELPER_NAME, platform, runner, runtime=False)

    metadata = json.loads(
        runner(
            [
                "cargo",
                "metadata",
                "--locked",
                "--offline",
                "--format-version",
                "1",
                "--filter-platform",
                triple,
                "--manifest-path",
                os.fspath(manifest_path),
            ],
            cwd=helper_dir,
        )
    )
    pinned = pins if pins is not None else load_pins(root)
    revision = str(pinned["revision"])
    asset_bytes = verify_assets(assets, pinned)
    notices = collect_notices(
        apache_text=_read_regular(root / "LICENSE", limit=_NOTICE_LIMIT),
        revision=revision,
        runtime_source=runtime_source,
        crates=crate_closure(metadata),
    )
    runtime_bytes = _read_regular(runtime_library, limit=EMBEDDING_FILE_LIMIT)

    runtime_path = f"{_RUNTIME_ROOT}/{runtime_name}"
    files = [
        _File(EXECUTABLES["embedding"], 0o755, helper_bytes),
        _File(runtime_path, 0o755, runtime_bytes),
        *(_File(path, 0o644, data) for path, data in asset_bytes.items()),
        *(_File(path, 0o644, data) for path, data in notices.items()),
    ]
    directories = _directories([*(item.path for item in files), inventory_path("embedding")])
    members = sorted(
        [
            *(Member(item, "directory", 0o755, 0, None) for item in directories),
            *(
                Member(item.path, "file", item.mode, len(item.data), _sha256(item.data))
                for item in files
            ),
        ],
        key=lambda member: member.path,
    )
    runtime_notices = [path for path in notices if "/onnxruntime-" in path]
    model_data = asset_bytes[MODEL_PATH]
    component: dict[str, object] = {
        "protocol": PROTOCOL,
        "helper_build": {
            "source_commit": source_commit,
            "recipe_sha256": _sha256(
                _canonical(
                    {
                        "command": ["cargo", "build", "--release", "--locked"],
                        "env": build_env,
                        "manifest_sha256": _sha256((helper_dir / "Cargo.toml").read_bytes()),
                        "rustc": runner(["rustc", "--version"]).strip(),
                    }
                )
            ),
            "executable_sha256": _sha256(helper_bytes),
            "target_triple": triple,
            "minimum_os": minimum_os,
            "cpu_features": sorted(features),
            "dependency_lock_sha256": _sha256((helper_dir / "Cargo.lock").read_bytes()),
        },
        "runtime_build": {
            "upstream_commit": runtime_commit,
            "recipe_sha256": _sha256(recipe_bytes),
            "library_path": runtime_path,
            "library_bytes": len(runtime_bytes),
            "library_sha256": _sha256(runtime_bytes),
            "cpu_only": True,
            "telemetry_compiled": False,
            "dependency_inventory_sha256": notices_digest(notices, runtime_notices),
        },
        "model": {
            "repository": str(pinned["repository"]),
            "revision": revision,
            "path": MODEL_PATH,
            "bytes": len(model_data),
            "sha256": _sha256(model_data),
        },
        "preprocess": {
            **PROFILE,
            "tokenizer_build": tokenizer_build(metadata),
            **{key: _sha256(asset_bytes[path]) for key, path in TOKENIZER_PATHS.items()},
        },
        "notices_sha256": notices_digest(notices),
    }
    inventory = Inventory(version, "embedding", platform, tuple(members), component)
    try:
        inventory_bytes = render_inventory(inventory)
    except ComponentManifestError as error:
        raise EmbeddingNativeError(f"inventory is invalid: {error}") from error

    archive = output / archive_filename("embedding", version, platform)
    _write_archive(
        archive,
        [*files, _File(inventory_path("embedding"), 0o644, inventory_bytes)],
        directories,
    )
    archive_bytes = archive.read_bytes()
    resource = ResourceV2(
        role="embedding",
        platform=platform,
        filename=archive.name,
        archive_sha256=_sha256(archive_bytes),
        archive_bytes=len(archive_bytes),
        executable=EXECUTABLES["embedding"],
        executable_sha256=_sha256(helper_bytes),
        inventory=inventory_path("embedding"),
        inventory_sha256=_sha256(inventory_bytes),
    )
    validate_resource_inventory(resource, version, inventory)
    record = archive.with_name(archive.name.removesuffix(".tar.gz") + ".resource.json")
    record.write_bytes(
        _canonical(
            {
                "role": resource.role,
                "platform": resource.platform,
                "filename": resource.filename,
                "archive_sha256": resource.archive_sha256,
                "archive_bytes": resource.archive_bytes,
                "executable": resource.executable,
                "executable_sha256": resource.executable_sha256,
                "inventory": resource.inventory,
                "inventory_sha256": resource.inventory_sha256,
            }
        )
        + b"\n"
    )
    return BuildResult(archive, record, resource, inventory, len(notices))


# ---------------------------------------------------------- archive audit

_RECORD_FIELDS: Final = (
    "role",
    "platform",
    "filename",
    "archive_sha256",
    "archive_bytes",
    "executable",
    "executable_sha256",
    "inventory",
    "inventory_sha256",
)
_ARCHIVE_NAME: Final = re.compile(
    r"^open-brain-components-v2-embedding-(?P<version>[0-9A-Za-z][0-9A-Za-z._-]*)-"
    r"(?P<platform>linux-x86_64|macos-arm64)\.tar\.gz$"
)
_RECORD_LIMIT: Final = 64 * 1024
_INVENTORY_LIMIT: Final = 1024 * 1024


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify_embedding_archive(archive: Path) -> tuple[str, ResourceV2, Inventory]:
    """Bind an embedding archive to its ``.resource.json`` record and embedded inventory."""
    match = _ARCHIVE_NAME.fullmatch(archive.name)
    if match is None:
        raise EmbeddingNativeError("embedding archive filename is invalid")
    version = match.group("version")
    record = archive.with_name(archive.name.removesuffix(".tar.gz") + ".resource.json")
    try:
        if archive.is_symlink() or not stat.S_ISREG(archive.lstat().st_mode):
            raise EmbeddingNativeError("embedding archive is not a regular file")
        record_bytes = _read_regular(record, limit=_RECORD_LIMIT)
        document = json.loads(record_bytes)
    except (EmbeddingNativeError, OSError, ValueError) as error:
        raise EmbeddingNativeError("embedding resource record is unavailable") from error
    if (
        not isinstance(document, dict)
        or set(document) != set(_RECORD_FIELDS)
        or _canonical(document) + b"\n" != record_bytes
        or not isinstance(document["archive_bytes"], int)
        or isinstance(document["archive_bytes"], bool)
        or not all(
            isinstance(document[key], str) for key in _RECORD_FIELDS if key != "archive_bytes"
        )
    ):
        raise EmbeddingNativeError("embedding resource record is invalid")
    resource = ResourceV2(**{key: document[key] for key in _RECORD_FIELDS})
    if (
        resource.role != "embedding"
        or resource.platform != match.group("platform")
        or resource.filename != archive.name
        or resource.filename != archive_filename("embedding", version, resource.platform)
        or resource.executable != EXECUTABLES["embedding"]
        or resource.inventory != inventory_path("embedding")
    ):
        raise EmbeddingNativeError("embedding resource record disagrees with the archive name")
    try:
        if _file_sha256(archive) != (resource.archive_sha256, resource.archive_bytes):
            raise EmbeddingNativeError("embedding archive hash disagrees with its record")
        with tarfile.open(archive, "r:gz") as tar:
            found = [info for info in tar.getmembers() if info.name == resource.inventory]
            if len(found) != 1 or not found[0].isreg() or found[0].size > _INVENTORY_LIMIT:
                raise EmbeddingNativeError("embedding archive inventory is invalid")
            stream = tar.extractfile(found[0])
            inventory_bytes = b"" if stream is None else stream.read(_INVENTORY_LIMIT + 1)
    except (OSError, tarfile.TarError, EOFError, zlib.error) as error:
        raise EmbeddingNativeError("embedding archive cannot be read") from error
    if _sha256(inventory_bytes) != resource.inventory_sha256:
        raise EmbeddingNativeError("embedding inventory hash disagrees with its record")
    try:
        inventory = parse_inventory(inventory_bytes)
        validate_resource_inventory(resource, version, inventory)
    except ComponentManifestError as error:
        raise EmbeddingNativeError(f"embedding inventory is invalid: {error}") from error
    return version, resource, inventory


def audit_embedding_archive(
    archive: Path, *, runner: Runner = run_command, host: str | None = None
) -> dict[str, object]:
    """Verify an archive, extract it exactly and re-run the binary audits on its bytes.

    The build-time ``CMakeCache.txt`` check stays with the build; the shipped runtime's
    telemetry-off build is bound by the inventory component instead.
    """
    version, resource, inventory = verify_embedding_archive(archive)
    if (host or _host()) != resource.platform:
        raise EmbeddingNativeError("the embedding archive is audited natively on its platform")
    with TemporaryDirectory(prefix="open-brain-embedding-audit-") as raw:
        root = Path(raw) / "prefix"
        try:
            safe_extract(archive, root, inventory)
            verify_extracted_resource(root, inventory)
        except ComponentManifestError as error:
            raise EmbeddingNativeError(f"embedding archive does not extract: {error}") from error
        audit_binary(root / EXECUTABLES["embedding"], resource.platform, runner, runtime=False)
        for member in inventory.members:
            if member.kind == "file" and member.path.startswith(_RUNTIME_ROOT + "/"):
                library = root / member.path
                audit_linkage(library, resource.platform, runner, runtime=True)
                audit_network_symbols(library, resource.platform, runner)
                if resource.platform == "macos-arm64":
                    audit_minimum_macos(library, runner)
                audit_telemetry_strings(library)
    return {
        "archive_sha256": resource.archive_sha256,
        "component_id": component_id(inventory.component or {}),
        "executable_sha256": resource.executable_sha256,
        "inventory_sha256": resource.inventory_sha256,
        "platform": resource.platform,
        "version": version,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--runtime-library", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    try:
        result = build_embedding_resource(
            root=args.root,
            version=args.version,
            platform=args.platform,
            runtime_library=args.runtime_library.resolve(),
            runtime_source=args.runtime_source.resolve(),
            assets=args.assets.resolve(),
            output=args.output.resolve(),
        )
    except EmbeddingNativeError as error:
        print(f"embedding-native: {error}", file=sys.stderr)
        return 2
    print(result.archive)
    print(f"archive_sha256 {result.resource.archive_sha256} bytes {result.resource.archive_bytes}")
    print(f"inventory_sha256 {result.resource.inventory_sha256} notices {result.notice_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
