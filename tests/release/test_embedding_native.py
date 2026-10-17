"""Embedding resource build and audits, with synthetic inputs and an injected runner."""

from __future__ import annotations

import hashlib
import json
import tarfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from tools.g4_embedding_probe import stage
from tools.open_brain_dev import component_manifest_v2 as v2
from tools.open_brain_dev import embedding_native as en

ROOT_REL = "libexec/open-brain-embedding"
LICENSES = "share/open-brain/licenses/embedding"
RUNTIME_COMMIT = "a" * 40
SOURCE_COMMIT = "b" * 40
GOOD_LIBS = (
    "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
    "\t/usr/lib/libiconv.2.dylib (compatibility version 7.0.0)\n"
)
RUNTIME_LIBS = (
    "\t@rpath/libonnxruntime.1.dylib (compatibility version 1.0.0)\n"
    "\t/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation (c 1)\n"
    "\t/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation (c 1)\n"
    "\t/usr/lib/libc++.1.dylib (c 1)\n" + GOOD_LIBS
)
MINOS = "Load command 9\n      cmd LC_BUILD_VERSION\n  cmdsize 32\n platform 1\n    minos {}\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class FakeRunner:
    """Answers otool/nm/git/cargo/rustc from tables; a build writes a fake helper."""

    metadata: str
    libs: dict[str, str] = field(default_factory=dict)
    symbols: dict[str, str] = field(default_factory=dict)
    minos: dict[str, str] = field(default_factory=dict)
    runtime_commit: str = RUNTIME_COMMIT
    dirty: str = ""
    calls: list[list[str]] = field(default_factory=list)

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        self.calls.append(list(args))
        tool = args[0]
        name = Path(args[-1]).name
        if tool == "otool" and args[1] == "-L":
            return f"{args[-1]}:\n" + self.libs.get(
                name, RUNTIME_LIBS if "onnx" in name else GOOD_LIBS
            )
        if tool == "otool":
            return MINOS.format(self.minos.get(name, "11.0"))
        if tool == "nm":
            return self.symbols.get(name, "_malloc\n_free\n")
        if tool == "git" and args[3] == "rev-parse":
            return self.runtime_commit if args[2].endswith("ort-src") else SOURCE_COMMIT
        if tool == "git":
            return self.dirty
        if tool == "rustc":
            return "rustc 1.96.0\n"
        if tool == "cargo" and args[1] == "build":
            target = Path(args[args.index("--target-dir") + 1]) / "release"
            target.mkdir(parents=True, exist_ok=True)
            (target / "open-brain-embedding").write_bytes(b"synthetic helper")
            return ""
        if tool == "cargo" and args[1] == "metadata":
            return self.metadata
        raise AssertionError(f"unexpected command {args}")


@dataclass
class World:
    root: Path
    runtime_library: Path
    runtime_source: Path
    assets: Path
    pins: dict[str, object]
    runner: FakeRunner


def make_crate(base: Path, name: str, files: Mapping[str, bytes]) -> Path:
    directory = base / f"{name}-1.0.0"
    directory.mkdir(parents=True)
    (directory / "Cargo.toml").write_text("[package]\n")
    for filename, data in files.items():
        (directory / filename).write_bytes(data)
    return directory / "Cargo.toml"


def metadata_for(crates: Mapping[str, Path], extra_kinds: Mapping[str, str | None]) -> str:
    ids = {name: f"{name} 1.0.0" for name in (*crates, *extra_kinds)}
    packages = [
        {
            "id": ids[name],
            "name": name,
            "version": "0.22.1" if name == "tokenizers" else "1.0.0",
            "manifest_path": str(path),
        }
        for name, path in crates.items()
    ]
    packages.append(
        {"id": "root", "name": "open-brain-embedding", "version": "0.1.0", "manifest_path": "/x"}
    )
    deps = [{"pkg": ids[name], "dep_kinds": [{"kind": extra_kinds.get(name)}]} for name in crates]
    nodes = [{"id": "root", "deps": deps}] + [{"id": ids[name], "deps": []} for name in crates]
    return json.dumps({"packages": packages, "resolve": {"root": "root", "nodes": nodes}})


def make_world(
    tmp_path: Path, *, crate_files: Mapping[str, Mapping[str, bytes]] | None = None
) -> World:
    root = tmp_path / "repo"
    (root / "release/open-brain-embedding").mkdir(parents=True)
    (root / "release/open-brain-embedding/Cargo.toml").write_text("[package]\n")
    (root / "release/open-brain-embedding/Cargo.lock").write_text("lock\n")
    (root / "tools/g4_embedding_probe").mkdir(parents=True)
    (root / "tools/g4_embedding_probe/ort-build-recipe.json").write_text(
        json.dumps({"commit": RUNTIME_COMMIT})
    )
    (root / "LICENSE").write_bytes(b"Apache License\nVersion 2.0\n")
    source = tmp_path / "ort-src"
    source.mkdir()
    (source / "LICENSE").write_bytes(b"MIT synthetic\n")
    (source / "ThirdPartyNotices.txt").write_bytes(b"third party\n")
    build = tmp_path / "ort-build/Release"
    build.mkdir(parents=True)
    library = build / "libonnxruntime.1.31.0.dylib"
    library.write_bytes(b"synthetic runtime")
    (build / "CMakeCache.txt").write_text("a:BOOL=ON\nonnxruntime_USE_TELEMETRY:BOOL=OFF\n")
    assets = tmp_path / "assets"
    (assets / "onnx").mkdir(parents=True)
    contents = {
        "onnx/model.onnx": b"synthetic graph",
        "tokenizer.json": b"{}",
        "config.json": b'{"k":1}',
        "vocab.txt": b"[PAD]\n",
    }
    pin_files = []
    for relative, data in contents.items():
        (assets / relative).write_bytes(data)
        pin: dict[str, object] = {"path": relative, "bytes": len(data)}
        if relative.startswith("onnx"):
            pin["lfs_sha256"] = sha(data)
        else:
            pin["git_blob_sha1"] = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        pin_files.append(pin)
    pins: dict[str, object] = {
        "repository": "example/model",
        "revision": "c" * 40,
        "files": pin_files,
    }
    files = crate_files or {
        "alpha": {"LICENSE-MIT": b"MIT alpha\n", "LICENSE-APACHE": b"Apache alpha"},
        "tokenizers": {"LICENSE": b"Apache tokenizers\n"},
    }
    registry = tmp_path / "registry"
    crates = {name: make_crate(registry, name, data) for name, data in files.items()}
    crates["devonly"] = make_crate(registry, "devonly", {})
    crates["buildonly"] = make_crate(registry, "buildonly", {})
    metadata = metadata_for(crates, {"devonly": "dev", "buildonly": "build"})
    return World(root, library, source, assets, pins, FakeRunner(metadata))


def build(world: World, tmp_path: Path, name: str = "out", **overrides: object) -> en.BuildResult:
    arguments: dict[str, object] = {
        "root": world.root,
        "version": "0.1.0",
        "platform": "macos-arm64",
        "runtime_library": world.runtime_library,
        "runtime_source": world.runtime_source,
        "assets": world.assets,
        "output": tmp_path / name,
        "runner": world.runner,
        "pins": world.pins,
        "host": "macos-arm64",
    }
    arguments.update(overrides)
    return en.build_embedding_resource(**arguments)  # type: ignore[arg-type]


def test_archive_members_modes_and_metadata(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    result = build(world, tmp_path)
    assert result.archive.name == "open-brain-components-v2-embedding-0.1.0-macos-arm64.tar.gz"
    with tarfile.open(result.archive) as tar:
        infos = {info.name: info for info in tar.getmembers()}
        names = [info.name for info in tar.getmembers()]
    files = {name for name, info in infos.items() if info.isreg()}
    assert files == {
        f"{ROOT_REL}/open-brain-embedding",
        f"{ROOT_REL}/runtime/libonnxruntime.dylib",
        f"{ROOT_REL}/model/model.onnx",
        f"{ROOT_REL}/tokenizer/tokenizer.json",
        f"{ROOT_REL}/tokenizer/config.json",
        f"{ROOT_REL}/tokenizer/vocab.txt",
        "share/open-brain/inventories/embedding.json",
        f"{LICENSES}/Apache-2.0.txt",
        f"{LICENSES}/all-MiniLM-L6-v2-NOTICE.txt",
        f"{LICENSES}/onnxruntime-LICENSE.txt",
        f"{LICENSES}/onnxruntime-ThirdPartyNotices.txt",
        f"{LICENSES}/crates/alpha-1.0.0.txt",
        f"{LICENSES}/crates/tokenizers-0.22.1.txt",
    }
    assert names == sorted(names)
    for name, info in infos.items():
        executable = name in (
            f"{ROOT_REL}/open-brain-embedding",
            f"{ROOT_REL}/runtime/libonnxruntime.dylib",
        )
        want = 0o755 if info.isdir() or executable else 0o644
        assert info.mode == want, name
        assert (info.uid, info.gid, info.mtime, info.uname, info.gname) == (0, 0, 0, "", "")
        assert not info.pax_headers
    assert result.notice_count == 6


def test_archive_is_byte_identical_across_builds(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    first = build(world, tmp_path, "one")
    second = build(world, tmp_path, "two")
    assert first.archive.read_bytes() == second.archive.read_bytes()
    assert first.record.read_bytes() == second.record.read_bytes()


def test_inventory_component_and_record_validate(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    result = build(world, tmp_path)
    destination = tmp_path / "extracted"
    v2.safe_extract(result.archive, destination, result.inventory)
    v2.verify_extracted_resource(destination, result.inventory)
    inventory = v2.parse_inventory(
        (destination / "share/open-brain/inventories/embedding.json").read_bytes()
    )
    assert inventory == result.inventory
    assert inventory.component is not None
    v2.validate_embedding_component(
        inventory.component, inventory, executable_sha256=result.resource.executable_sha256
    )
    record = json.loads(result.record.read_text())
    assert record == {
        "role": "embedding",
        "platform": "macos-arm64",
        "filename": result.archive.name,
        "archive_sha256": sha(result.archive.read_bytes()),
        "archive_bytes": result.archive.stat().st_size,
        "executable": f"{ROOT_REL}/open-brain-embedding",
        "executable_sha256": sha(b"synthetic helper"),
        "inventory": "share/open-brain/inventories/embedding.json",
        "inventory_sha256": sha(v2.render_inventory(inventory)),
    }
    component = inventory.component
    assert component["preprocess"] == {
        **stage.PREPROCESS,
        "segmenter_version": "ob.token-window.v1",
        "tokenizer_build": "tokenizers 0.22.1 fancy-regex",
        "tokenizer_json_sha256": sha(b"{}"),
        "tokenizer_config_sha256": sha(b'{"k":1}'),
        "vocab_sha256": sha(b"[PAD]\n"),
    }
    helper = cast(dict[str, object], component["helper_build"])
    assert helper["source_commit"] == SOURCE_COMMIT
    assert helper["dependency_lock_sha256"] == sha(b"lock\n")
    runtime = cast(dict[str, object], component["runtime_build"])
    assert runtime["upstream_commit"] == RUNTIME_COMMIT


def test_notices_digest_covers_sorted_paths_and_hashes(tmp_path: Path) -> None:
    notices = {f"{LICENSES}/b.txt": b"B", f"{LICENSES}/a.txt": b"A"}
    expected = sha(
        json.dumps(
            [{"path": "a.txt", "sha256": sha(b"A")}, {"path": "b.txt", "sha256": sha(b"B")}],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    assert en.notices_digest(notices) == expected


def test_crate_without_license_files_is_refused(tmp_path: Path) -> None:
    world = make_world(
        tmp_path,
        crate_files={"alpha": {"README.md": b"no license"}, "tokenizers": {"LICENSE": b"x"}},
    )
    with pytest.raises(
        en.EmbeddingNativeError, match=r"crates without license files: alpha 1\.0\.0"
    ):
        build(world, tmp_path)
    assert not list((tmp_path / "out").glob("*.tar.gz"))


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            lambda w: w.runner.libs.update(
                {"open-brain-embedding": "\t/usr/lib/libcurl.4.dylib (c 1)\n"}
            ),
            "disallowed library",
        ),
        (
            lambda w: w.runner.libs.update(
                {"open-brain-embedding": "\t@rpath/libonnxruntime.1.dylib (c 1)\n"}
            ),
            "links the ONNX Runtime",
        ),
        (
            lambda w: w.runner.libs.update(
                {w.runtime_library.name: "\t/usr/lib/libz.1.dylib (c 1)\n"}
            ),
            "disallowed library",
        ),
        (
            lambda w: w.runner.symbols.update({"open-brain-embedding": "_malloc\n_getaddrinfo\n"}),
            "network symbol",
        ),
        (
            lambda w: w.runner.symbols.update(
                {w.runtime_library.name: "_OBJC_CLASS_$_NSURLSession\n"}
            ),
            "network symbol",
        ),
        (
            lambda w: w.runner.symbols.update({w.runtime_library.name: "_socket\n"}),
            "network symbol",
        ),
        (
            lambda w: w.runtime_library.write_bytes(b"x vortex.data.microsoft.com y"),
            "telemetry string",
        ),
        (
            lambda w: w.runtime_library.write_bytes(
                b"Microsoft::Applications::Events::ILogManager"
            ),
            "telemetry string",
        ),
        (
            lambda w: (w.runtime_library.parent / "CMakeCache.txt").write_text(
                "onnxruntime_USE_TELEMETRY:BOOL=ON\n"
            ),
            "telemetry out",
        ),
        (lambda w: (w.runtime_library.parent / "CMakeCache.txt").unlink(), "cannot read"),
        (lambda w: w.runner.minos.update({w.runtime_library.name: "12.0"}), "above 11.0"),
        (lambda w: w.runner.minos.update({"open-brain-embedding": "14.0"}), "above 11.0"),
        (lambda w: setattr(w.runner, "runtime_commit", "d" * 40), "recipe commit"),
        (lambda w: setattr(w.runner, "dirty", " M src/main.rs\n"), "uncommitted"),
    ],
)
def test_audit_refusals(tmp_path: Path, edit: Callable[[World], object], message: str) -> None:
    world = make_world(tmp_path)
    edit(world)
    with pytest.raises(en.EmbeddingNativeError, match=message):
        build(world, tmp_path)
    assert not list((tmp_path / "out").glob("*.tar.gz"))


def test_telemetry_string_spanning_a_chunk_boundary_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(en, "_CHUNK", 8)
    path = tmp_path / "lib"
    path.write_bytes(b"1234567" + b"ONECOLLECTOR" + b"89")
    with pytest.raises(en.EmbeddingNativeError, match="onecollector"):
        en.audit_telemetry_strings(path)


@pytest.mark.parametrize("edit", ["pin_size", "pin_hash", "missing"])
def test_asset_pin_mismatch_is_refused(tmp_path: Path, edit: str) -> None:
    world = make_world(tmp_path)
    files = world.pins["files"]
    assert isinstance(files, list)
    if edit == "pin_size":
        files[0]["bytes"] = 1
    elif edit == "pin_hash":
        (world.assets / "vocab.txt").write_bytes(b"[PAD]!")
        files[3]["bytes"] = 6
    else:
        (world.assets / "config.json").unlink()
    with pytest.raises(en.EmbeddingNativeError):
        build(world, tmp_path)


def test_linux_linkage_uses_needed_entries() -> None:
    def runner(
        args: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        assert args[0] == "readelf"
        return (
            " 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]\n"
            " 0x0000000000000001 (NEEDED)             Shared library: [libstdc++.so.6]\n"
            " 0x000000000000000e (SONAME)             Library soname: [libonnxruntime.so.1]\n"
        )

    en.audit_linkage(Path("/x/lib"), "linux-x86_64", runner, runtime=True)

    def bad(
        args: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        return " 0x1 (NEEDED)             Shared library: [libssl.so.3]\n"

    with pytest.raises(en.EmbeddingNativeError, match="libssl"):
        en.audit_linkage(Path("/x/lib"), "linux-x86_64", bad, runtime=False)


def test_wrong_host_and_platform_are_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    with pytest.raises(en.EmbeddingNativeError, match="natively"):
        build(world, tmp_path, host="linux-x86_64")
    with pytest.raises(en.EmbeddingNativeError, match="unsupported"):
        build(world, tmp_path, platform="windows")


def test_stage_writes_the_canonical_component_for_an_inventory(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    result = build(world, tmp_path)
    inventory_file = tmp_path / "embedding.json"
    inventory_file.write_bytes(v2.render_inventory(result.inventory))
    staged = stage.stage(
        tmp_path / "staged",
        helper=world.runtime_library,
        runtime_library=world.runtime_library,
        assets=world.assets,
        inventory=inventory_file,
    )
    document = (staged / "component.json").read_bytes()
    assert not document.endswith(b"\n")
    assert result.inventory.component is not None
    assert sha(document) == v2.component_id(result.inventory.component)
    # The probe-benchmark mode is unchanged.
    plain = stage.stage(
        tmp_path / "plain",
        helper=world.runtime_library,
        runtime_library=world.runtime_library,
        assets=world.assets,
    )
    assert json.loads((plain / "component.json").read_bytes())["preprocess"] == stage.PREPROCESS


def test_profile_matches_probe_stage_profile() -> None:
    assert {k: v for k, v in en.PROFILE.items() if k != "segmenter_version"} == stage.PREPROCESS


def test_real_archive_extracts_and_verifies(tmp_path: Path) -> None:
    """Checks the archive written by ``make embedding-native``; skipped until one exists."""
    root = Path(__file__).resolve().parents[2]
    archives = sorted((root / "build/g4-embedding-archive").glob("*.tar.gz"))
    if not archives:
        pytest.skip("no real embedding archive has been built")
    archive = archives[-1]

    with tarfile.open(archive) as tar:
        member = tar.extractfile("share/open-brain/inventories/embedding.json")
        assert member is not None
        inventory = v2.parse_inventory(member.read())
    destination = tmp_path / "prefix"
    v2.safe_extract(archive, destination, inventory)
    v2.verify_extracted_resource(destination, inventory)
    assert inventory.component is not None
