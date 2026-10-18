"""Component manifest v2, inventory v1 and exact extraction, with synthetic bytes only."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import io
import json
import os
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tools.open_brain_dev import base_native
from tools.open_brain_dev import component_manifest_v2 as v2
from tools.open_brain_dev.component_manifest_v2 import ComponentManifestError

FIXTURES = Path(__file__).parent / "fixtures" / "manifest-v2"
ROOT = "libexec/open-brain-embedding"
LICENSES = "share/open-brain/licenses/embedding"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


EMBEDDING_FILES: dict[str, bytes] = {
    f"{ROOT}/open-brain-embedding": b"synthetic helper",
    f"{ROOT}/model/model.onnx": b"synthetic graph" * 3,
    f"{ROOT}/tokenizer/tokenizer.json": b"{}",
    f"{ROOT}/tokenizer/config.json": b'{"k":1}',
    f"{ROOT}/tokenizer/vocab.txt": b"[PAD]\n",
    f"{ROOT}/runtime/libonnxruntime.dylib": b"synthetic runtime",
    f"{LICENSES}/NOTICE.txt": b"synthetic notice",
}
EXECUTABLE_PATHS = {f"{ROOT}/open-brain-embedding", f"{ROOT}/runtime/libonnxruntime.dylib"}
DIRECTORIES = [
    "libexec",
    ROOT,
    f"{ROOT}/model",
    f"{ROOT}/runtime",
    f"{ROOT}/tokenizer",
    "share",
    "share/open-brain",
    "share/open-brain/inventories",
    "share/open-brain/licenses",
    LICENSES,
]


def members_for(files: dict[str, bytes], directories: list[str]) -> tuple[v2.Member, ...]:
    rows = [v2.Member(path, "directory", 0o755, 0, None) for path in directories]
    rows += [
        v2.Member(
            path,
            "file",
            0o755 if path in EXECUTABLE_PATHS else 0o644,
            len(data),
            sha(data),
        )
        for path, data in files.items()
    ]
    return tuple(sorted(rows, key=lambda row: row.path))


def component_for(platform: str = "macos-arm64") -> dict[str, object]:
    files = EMBEDDING_FILES
    runtime = f"{ROOT}/runtime/libonnxruntime.dylib"
    return {
        "protocol": v2.PROTOCOL,
        "helper_build": {
            "source_commit": "1" * 40,
            "recipe_sha256": sha(b"helper recipe"),
            "executable_sha256": sha(files[f"{ROOT}/open-brain-embedding"]),
            "target_triple": "aarch64-apple-darwin",
            "minimum_os": "14.0",
            "cpu_features": ["neon"],
            "dependency_lock_sha256": sha(b"lock"),
        },
        "runtime_build": {
            "upstream_commit": "2" * 40,
            "recipe_sha256": sha(b"runtime recipe"),
            "library_path": runtime,
            "library_bytes": len(files[runtime]),
            "library_sha256": sha(files[runtime]),
            "cpu_only": True,
            "telemetry_compiled": False,
            "dependency_inventory_sha256": sha(b"deps"),
        },
        "model": {
            "repository": "example/synthetic-model",
            "revision": "3" * 40,
            "path": v2.MODEL_PATH,
            "bytes": len(files[v2.MODEL_PATH]),
            "sha256": sha(files[v2.MODEL_PATH]),
        },
        "preprocess": {
            "profile_id": "synthetic.profile.v1",
            "dimensions": 384,
            "pooling": "mean",
            "max_query_tokens": 254,
            "document_window_tokens": 224,
            "document_overlap_tokens": 32,
            "cls_token_id": 101,
            "sep_token_id": 102,
            "pad_token_id": 0,
            "query_prefix": "",
            "tokenizer_build": "synthetic-tokenizer 0.0.0",
            "segmenter_version": "1",
            **{key: sha(files[path]) for key, path in v2.TOKENIZER_PATHS.items()},
        },
        "notices_sha256": sha(b"notices"),
    }


def embedding_inventory(
    members: tuple[v2.Member, ...] | None = None,
    component: dict[str, object] | None = None,
    *,
    drop_component: bool = False,
) -> v2.Inventory:
    base = v2.Inventory(
        "0.1.0",
        "embedding",
        "macos-arm64",
        members_for(EMBEDDING_FILES, DIRECTORIES),
        component_for(),
    )
    if members is not None:
        base = dataclasses.replace(base, members=members)
    if component is not None:
        base = dataclasses.replace(base, component=component)
    return dataclasses.replace(base, component=None) if drop_component else base


INTERNAL = "libexec/open-brain/_internal"
FRAMEWORK = f"{INTERNAL}/Python.framework"
BASE_FILES = {
    "libexec/open-brain/open-brain": b"synthetic base",
    f"{FRAMEWORK}/Versions/3.14/Python": b"synthetic interpreter",
}
# The one-folder layout: a bin link plus PyInstaller's framework links (D14 option a).
BASE_LINKS = {
    "bin/open-brain": "../libexec/open-brain/open-brain",
    f"{INTERNAL}/Python": "Python.framework/Versions/3.14/Python",
    f"{FRAMEWORK}/Python": "Versions/Current/Python",
    f"{FRAMEWORK}/Versions/Current": "3.14",
}
BASE_DIRECTORIES = [
    "bin",
    "libexec",
    "libexec/open-brain",
    INTERNAL,
    FRAMEWORK,
    f"{FRAMEWORK}/Versions",
    f"{FRAMEWORK}/Versions/3.14",
    "share",
    "share/open-brain",
    "share/open-brain/inventories",
]


def link(path: str, target: str) -> v2.Member:
    return v2.Member(path, "symlink", 0o777, 0, None, target)


def base_members(links: dict[str, str] | None = None) -> tuple[v2.Member, ...]:
    members = members_for(BASE_FILES, BASE_DIRECTORIES)
    members = tuple(
        dataclasses.replace(m, mode=0o755) if m.path in v2.EXECUTABLES.values() else m
        for m in members
    )
    rows = (*members, *(link(path, target) for path, target in (links or BASE_LINKS).items()))
    return tuple(sorted(rows, key=lambda row: row.path))


def base_inventory(platform: str = "macos-arm64") -> v2.Inventory:
    return v2.Inventory("0.1.0", "base", platform, base_members(), None)


def build_manifest(roles: tuple[str, ...] = ("base", "graphify", "embedding")) -> v2.ManifestV2:
    resources: list[v2.ResourceV2] = []
    for platform in v2.PLATFORMS:
        for role in v2.ROLES:
            if role in roles:
                resources.append(
                    v2.ResourceV2(
                        role,
                        platform,
                        v2.archive_filename(role, "0.1.0", platform),
                        sha(f"archive {role} {platform}".encode()),
                        1000 + len(resources),
                        v2.EXECUTABLES[role],
                        sha(f"exe {role}".encode()),
                        v2.inventory_path(role),
                        sha(f"inv {role} {platform}".encode()),
                    )
                )
    return v2.ManifestV2("0.1.0", tuple(resources))


def mutated(data: bytes, edit: Callable[[dict[str, Any]], object]) -> bytes:
    value = json.loads(data)
    edit(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


# ---------------------------------------------------------------- fixtures


def test_golden_fixtures_match_builders() -> None:
    assert (FIXTURES / "manifest.json").read_bytes() == v2.render_manifest_v2(build_manifest())
    assert (FIXTURES / "inventory-embedding.json").read_bytes() == v2.render_inventory(
        embedding_inventory()
    )
    assert (FIXTURES / "inventory-base.json").read_bytes() == v2.render_inventory(base_inventory())


def test_fixtures_parse_and_round_trip() -> None:
    manifest_bytes = (FIXTURES / "manifest.json").read_bytes()
    assert v2.render_manifest_v2(v2.parse_manifest_v2(manifest_bytes)) == manifest_bytes
    inventory_bytes = (FIXTURES / "inventory-embedding.json").read_bytes()
    assert v2.render_inventory(v2.parse_inventory(inventory_bytes)) == inventory_bytes
    assert v2.parse_inventory((FIXTURES / "inventory-base.json").read_bytes()).component is None


def test_four_role_manifest_is_accepted() -> None:
    manifest = build_manifest(("base", "graphify", "harness", "embedding"))
    assert len(v2.parse_manifest_v2(v2.render_manifest_v2(manifest)).resources) == 8


# ------------------------------------------------------------ manifest rejects


def manifest_bytes() -> bytes:
    return v2.render_manifest_v2(build_manifest())


def first(value: dict[str, Any]) -> dict[str, Any]:
    resources = value["resources"]
    assert isinstance(resources, list)
    item = resources[0]
    assert isinstance(item, dict)
    return item


def test_manifest_rejects_noncanonical_bytes() -> None:
    good = manifest_bytes()
    for bad in (
        good.rstrip(b"\n"),
        good + b"\n",
        good.replace(b",", b", ", 1),
        b"\xef\xbb\xbf" + good,
    ):
        with pytest.raises(ComponentManifestError):
            v2.parse_manifest_v2(bad)


def test_manifest_rejects_duplicate_key_and_unknown_field() -> None:
    good = manifest_bytes()
    with pytest.raises(ComponentManifestError):
        v2.parse_manifest_v2(good.replace(b'"version"', b'"version":"0.1.0","version"', 1))
    with pytest.raises(ComponentManifestError):
        v2.parse_manifest_v2(mutated(good, lambda v: v.update(extra=1)))
    with pytest.raises(ComponentManifestError):
        v2.parse_manifest_v2(mutated(good, lambda v: first(v).update(extra=1)))


EDITS: dict[str, Callable[[dict[str, Any]], object]] = {
    "bool-bytes": lambda v: first(v).update(archive_bytes=True),
    "zero-bytes": lambda v: first(v).update(archive_bytes=0),
    "float-bytes": lambda v: first(v).update(archive_bytes=1000.0),
    "schema": lambda v: v.update(schema="open-brain.component-manifest.v3"),
    "role": lambda v: first(v).update(role="other"),
    "platform": lambda v: first(v).update(platform="windows-x86_64"),
    "upper-hash": lambda v: first(v).update(archive_sha256="A" * 64),
    "short-hash": lambda v: first(v).update(archive_sha256="a" * 63),
    "filename": lambda v: first(v).update(filename="open-brain-0.1.0-linux-x86_64.tar.gz"),
    "version": lambda v: v.update(version="0.2.0"),
    "bad-version": lambda v: v.update(version="../x"),
    "executable": lambda v: first(v).update(executable="bin/evil"),
    "inventory": lambda v: first(v).update(inventory="elsewhere.json"),
    "unsorted": lambda v: v["resources"].reverse(),
    "missing-platform": lambda v: v.update(
        resources=[r for r in v["resources"] if r["platform"] == "macos-arm64"]
    ),
    "unequal-roles": lambda v: v.update(
        resources=[
            r
            for r in v["resources"]
            if not (r["platform"] == "linux-x86_64" and r["role"] == "graphify")
        ]
    ),
    "no-embedding": lambda v: v.update(
        resources=[r for r in v["resources"] if r["role"] != "embedding"]
    ),
    "duplicate-role": lambda v: v["resources"].append(dict(v["resources"][0])),
}


@pytest.mark.parametrize("name", sorted(EDITS))
def test_manifest_rejects(name: str) -> None:
    with pytest.raises(ComponentManifestError):
        v2.parse_manifest_v2(mutated(manifest_bytes(), EDITS[name]))


def test_only_graphify_less_or_harness_less_sets_are_not_enough() -> None:
    with pytest.raises(ComponentManifestError):
        v2.render_manifest_v2(build_manifest(("base", "embedding")))
    with pytest.raises(ComponentManifestError):
        v2.render_manifest_v2(build_manifest(("base", "graphify", "harness")))


# ---------------------------------------------------------------- inventory


def inventory_bytes() -> bytes:
    return v2.render_inventory(embedding_inventory())


def test_inventory_rejects_noncanonical_duplicate_and_unknown() -> None:
    good = inventory_bytes()
    with pytest.raises(ComponentManifestError):
        v2.parse_inventory(good.rstrip(b"\n"))
    with pytest.raises(ComponentManifestError):
        v2.parse_inventory(good.replace(b'"role"', b'"role":"embedding","role"', 1))
    with pytest.raises(ComponentManifestError):
        v2.parse_inventory(mutated(good, lambda v: v.update(extra=1)))
    with pytest.raises(ComponentManifestError):
        v2.parse_inventory(mutated(good, lambda v: v["members"][0].update(extra=1)))


def with_members(members: tuple[v2.Member, ...]) -> v2.Inventory:
    return embedding_inventory(members=members)


def extra_file(path: str, kind: str = "file", mode: int = 0o644) -> v2.Inventory:
    base = embedding_inventory().members
    data = b"x"
    extra = v2.Member(
        path,
        kind,
        mode,
        0 if kind == "directory" else 1,
        None if kind == "directory" else sha(data),
    )
    return with_members(tuple(sorted((*base, extra), key=lambda row: row.path)))


@pytest.mark.parametrize(
    "path",
    [
        "/abs",
        "",
        ".",
        "..",
        f"{LICENSES}/../x",
        f"{LICENSES}//x",
        f"{LICENSES}\\x",
        f"{LICENSES}/\0x",
        f"{LICENSES}/.",
    ],
)
def test_inventory_rejects_bad_paths(path: str) -> None:
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(extra_file(path))


def test_inventory_rejects_structural_violations() -> None:
    base = embedding_inventory().members
    notice = f"{LICENSES}/NOTICE.txt"
    cases: list[tuple[v2.Member, ...]] = [
        tuple(reversed(base)),
        (*base, base[0]),
        tuple(m for m in base if m.path != f"{ROOT}/model"),
        (*base[:0], *base, v2.Member(f"{LICENSES}/notice.txt", "file", 0o644, 1, sha(b"x"))),
        tuple(dataclasses.replace(m, mode=0o600) if m.path == notice else m for m in base),
        tuple(dataclasses.replace(m, mode=0o755) if m.path == v2.MODEL_PATH else m for m in base),
        tuple(dataclasses.replace(m, mode=0o755) if m.path == notice else m for m in base),
        tuple(dataclasses.replace(m, mode=0o700) if m.path == ROOT else m for m in base),
        tuple(
            dataclasses.replace(m, bytes=1) if m.path == ROOT + "/tokenizer" else m for m in base
        ),
        tuple(dataclasses.replace(m, sha256="G" * 64) if m.path == notice else m for m in base),
        tuple(dataclasses.replace(m, kind="symlink") if m.path == notice else m for m in base),
    ]
    for members in cases:
        with pytest.raises(ComponentManifestError):
            v2.render_inventory(with_members(tuple(members)))


def test_inventory_rejects_unlisted_paths_and_self_listing() -> None:
    for path in (
        "libexec/other",
        f"{ROOT}/extra.bin",
        "share/open-brain/inventories/embedding.json",
    ):
        with pytest.raises(ComponentManifestError):
            v2.render_inventory(extra_file(path))
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(extra_file("share/open-brain/other", "directory", 0o755))


def test_inventory_rejects_case_and_unicode_collisions() -> None:
    for name in ("notice.TXT", "NOTICE.txt"):
        with pytest.raises(ComponentManifestError):
            v2.render_inventory(extra_file(f"{LICENSES}/{name}"))
    decomposed = "café.txt"
    composed = "café.txt"
    inventory = extra_file(f"{LICENSES}/{composed}")
    both = tuple(
        sorted(
            (
                *inventory.members,
                dataclasses.replace(inventory.members[-1], path=f"{LICENSES}/{decomposed}"),
            ),
            key=lambda row: row.path,
        )
    )
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(with_members(both))


def test_embedding_limits() -> None:
    base = list(embedding_inventory().members)
    for index in range(v2.EMBEDDING_MAX_MEMBERS):
        base.append(v2.Member(f"{LICENSES}/n{index:04d}", "file", 0o644, 1, sha(b"x")))
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(with_members(tuple(sorted(base, key=lambda row: row.path))))
    model = v2.MODEL_PATH
    for size in (v2.EMBEDDING_MAX_FILE + 1,):
        members = tuple(
            dataclasses.replace(m, bytes=size) if m.path == model else m
            for m in embedding_inventory().members
        )
        with pytest.raises(ComponentManifestError):
            v2.render_inventory(with_members(members))
    big = v2.EMBEDDING_MAX_FILE
    members = tuple(
        dataclasses.replace(m, bytes=big) if m.kind == "file" else m
        for m in embedding_inventory().members
    )
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(with_members(members))


def test_nonembedding_inventory_cannot_claim_embedding_paths_or_component() -> None:
    inventory = base_inventory()
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(dataclasses.replace(inventory, component=component_for()))
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(embedding_inventory(drop_component=True))
    members = (
        *inventory.members,
        v2.Member(f"{ROOT}/x", "file", 0o644, 1, sha(b"x")),
    )
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(dataclasses.replace(inventory, members=members))


# ---------------------------------------------------------------- component


def test_component_id_is_hash_of_canonical_component() -> None:
    component = component_for()
    expected = sha(json.dumps(component, sort_keys=True, separators=(",", ":")).encode())
    assert v2.component_id(component) == expected
    changed = json.loads(json.dumps(component))
    changed["preprocess"]["pooling"] = "cls"
    assert v2.component_id(changed) != expected


def component_edit(edit: Callable[[dict[str, Any]], object]) -> v2.Inventory:
    component: dict[str, Any] = json.loads(json.dumps(component_for()))
    edit(component)
    return embedding_inventory(component=component)


COMPONENT_EDITS: dict[str, Callable[[dict[str, Any]], object]] = {
    "protocol": lambda c: c.update(protocol="other"),
    "extra-root": lambda c: c.update(extra={}),
    "missing-root": lambda c: c.pop("notices_sha256"),
    "helper-extra": lambda c: c["helper_build"].update(extra=1),
    "helper-hash": lambda c: c["helper_build"].update(executable_sha256="0" * 64),
    "helper-triple": lambda c: c["helper_build"].update(target_triple="x86_64-unknown-linux-gnu"),
    "helper-commit": lambda c: c["helper_build"].update(source_commit="abc"),
    "helper-features": lambda c: c["helper_build"].update(cpu_features=["b", "a"]),
    "runtime-telemetry": lambda c: c["runtime_build"].update(telemetry_compiled=True),
    "runtime-cpu": lambda c: c["runtime_build"].update(cpu_only=1),
    "runtime-bytes": lambda c: c["runtime_build"].update(library_bytes=1),
    "runtime-hash": lambda c: c["runtime_build"].update(library_sha256="0" * 64),
    "runtime-path": lambda c: c["runtime_build"].update(library_path=f"{ROOT}/runtime/evil.so"),
    "model-path": lambda c: c["model"].update(path=f"{ROOT}/model/other.onnx"),
    "model-bytes": lambda c: c["model"].update(bytes=1),
    "model-hash": lambda c: c["model"].update(sha256="0" * 64),
    "model-external": lambda c: c["model"].update(external_data="x"),
    "pre-extra": lambda c: c["preprocess"].update(extra=1),
    "pre-dims": lambda c: c["preprocess"].update(dimensions=768),
    "pre-bool-id": lambda c: c["preprocess"].update(cls_token_id=True),
    "pre-pooling": lambda c: c["preprocess"].update(pooling="max"),
    "pre-tokenizer-hash": lambda c: c["preprocess"].update(vocab_sha256="0" * 64),
    "pre-missing": lambda c: c["preprocess"].pop("segmenter_version"),
    "pre-window": lambda c: c["preprocess"].update(document_window_tokens=100),
    "notices": lambda c: c.update(notices_sha256="nope"),
}


@pytest.mark.parametrize("name", sorted(COMPONENT_EDITS))
def test_component_rejects(name: str) -> None:
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(component_edit(COMPONENT_EDITS[name]))


def test_resource_cross_check_binds_executable_and_inventory_hashes() -> None:
    inventory = embedding_inventory()
    resource = v2.ResourceV2(
        "embedding",
        "macos-arm64",
        v2.archive_filename("embedding", "0.1.0", "macos-arm64"),
        "a" * 64,
        10,
        v2.EXECUTABLES["embedding"],
        sha(EMBEDDING_FILES[f"{ROOT}/open-brain-embedding"]),
        v2.inventory_path("embedding"),
        sha(v2.render_inventory(inventory)),
    )
    v2.validate_resource_inventory(resource, "0.1.0", inventory)
    for changed in (
        dataclasses.replace(resource, executable_sha256="b" * 64),
        dataclasses.replace(resource, inventory_sha256="b" * 64),
        dataclasses.replace(resource, platform="linux-x86_64"),
    ):
        with pytest.raises(ComponentManifestError):
            v2.validate_resource_inventory(changed, "0.1.0", inventory)
    with pytest.raises(ComponentManifestError):
        v2.validate_resource_inventory(resource, "0.2.0", inventory)


# ------------------------------------------------- extraction and verification


def write_tree(root: Path, inventory: v2.Inventory, files: dict[str, bytes]) -> None:
    root.mkdir(exist_ok=True)
    for member in inventory.members:
        target = root / member.path
        if member.kind == "directory":
            target.mkdir()
            target.chmod(member.mode)
        elif member.kind == "symlink":
            assert member.target is not None
            target.symlink_to(member.target)
        else:
            target.write_bytes(files[member.path])
            target.chmod(member.mode)
    own = root / v2.inventory_path(inventory.role)
    own.write_bytes(v2.render_inventory(inventory))
    own.chmod(0o644)


def make_archive(
    path: Path,
    inventory: v2.Inventory,
    files: dict[str, bytes],
    *,
    extra: Callable[[tarfile.TarFile], None] | None = None,
    skip: str | None = None,
    modes: dict[str, int] | None = None,
    links: dict[str, str] | None = None,
) -> Path:
    own = v2.inventory_path(inventory.role)
    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        entries = [(m.path, m) for m in inventory.members]
        for name, record in entries:
            if name == skip:
                continue
            info = tarfile.TarInfo(name)
            info.mode = (modes or {}).get(name, record.mode)
            if record.kind == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif record.kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = (links or {}).get(name, record.target or "")
                archive.addfile(info)
            else:
                data = files[name]
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            if name == "share/open-brain/inventories" and skip != own:
                body = v2.render_inventory(inventory)
                inv = tarfile.TarInfo(own)
                inv.mode = 0o644
                inv.size = len(body)
                archive.addfile(inv, io.BytesIO(body))
        if extra is not None:
            extra(archive)
    return path


def test_extract_then_verify_round_trip(tmp_path: Path) -> None:
    inventory = embedding_inventory()
    archive = make_archive(tmp_path / "a.tar.gz", inventory, EMBEDDING_FILES)
    destination = tmp_path / "out"
    v2.safe_extract(archive, destination, inventory)
    v2.verify_extracted_resource(destination, inventory)
    assert (destination / v2.MODEL_PATH).read_bytes() == EMBEDDING_FILES[v2.MODEL_PATH]
    assert os.stat(destination / f"{ROOT}/open-brain-embedding").st_mode & 0o777 == 0o755


def test_verify_accepts_manually_written_tree(tmp_path: Path) -> None:
    inventory = embedding_inventory()
    write_tree(tmp_path / "t", inventory, EMBEDDING_FILES)
    v2.verify_extracted_resource(tmp_path / "t", inventory)


def tree(tmp_path: Path) -> tuple[Path, v2.Inventory]:
    inventory = embedding_inventory()
    write_tree(tmp_path / "t", inventory, EMBEDDING_FILES)
    return tmp_path / "t", inventory


def test_verify_rejects_extra_file_and_directory(tmp_path: Path) -> None:
    root, inventory = tree(tmp_path)
    (root / "stray").write_text("x")
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)
    (root / "stray").unlink()
    (root / LICENSES / "sub").mkdir()
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)


def test_verify_rejects_missing_member(tmp_path: Path) -> None:
    root, inventory = tree(tmp_path)
    (root / LICENSES / "NOTICE.txt").unlink()
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)


def test_verify_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    root, inventory = tree(tmp_path)
    notice = root / LICENSES / "NOTICE.txt"
    notice.unlink()
    notice.symlink_to(root / v2.MODEL_PATH)
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)
    notice.unlink()
    os.link(root / f"{ROOT}/tokenizer/vocab.txt", notice)
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)


def test_verify_rejects_fifo(tmp_path: Path) -> None:
    root, inventory = tree(tmp_path)
    notice = root / LICENSES / "NOTICE.txt"
    notice.unlink()
    os.mkfifo(notice)
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)


def test_verify_rejects_mode_size_and_hash_mismatch(tmp_path: Path) -> None:
    root, inventory = tree(tmp_path)
    model = root / v2.MODEL_PATH
    model.chmod(0o755)
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)
    model.chmod(0o644)
    model.write_bytes(EMBEDDING_FILES[v2.MODEL_PATH] + b"!")
    model.chmod(0o644)
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)
    model.write_bytes(b"X" * len(EMBEDDING_FILES[v2.MODEL_PATH]))
    model.chmod(0o644)
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)


def test_verify_rejects_tampered_inventory_file(tmp_path: Path) -> None:
    root, inventory = tree(tmp_path)
    (root / v2.inventory_path("embedding")).write_bytes(b"{}\n")
    (root / v2.inventory_path("embedding")).chmod(0o644)
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)


def test_verify_rejects_symlinked_root(tmp_path: Path) -> None:
    root, inventory = tree(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(root)
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(link, inventory)


def extract_error(tmp_path: Path, archive: Path, inventory: v2.Inventory | None = None) -> Path:
    destination = tmp_path / "out"
    with pytest.raises(ComponentManifestError):
        v2.safe_extract(archive, destination, inventory or embedding_inventory())
    assert not destination.exists()
    return destination


def add(
    name: str, *, kind: bytes = tarfile.REGTYPE, data: bytes = b"", link: str = ""
) -> Callable[[tarfile.TarFile], None]:
    def action(archive: tarfile.TarFile) -> None:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.mode = 0o644
        info.linkname = link
        info.size = len(data) if kind == tarfile.REGTYPE else 0
        archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)

    return action


@pytest.mark.parametrize(
    "extra",
    [
        add(f"{LICENSES}/undeclared", data=b"x"),
        add(f"{LICENSES}/NOTICE.txt", data=b"synthetic notice"),
        add("evil", kind=tarfile.SYMTYPE, link="/etc/passwd"),
        add("evil2", kind=tarfile.LNKTYPE, link=f"{ROOT}/model/model.onnx"),
        add("fifo", kind=tarfile.FIFOTYPE),
        add("dev", kind=tarfile.CHRTYPE),
        add("../escape", data=b"x"),
        add("/abs", data=b"x"),
    ],
)
def test_extract_refuses_unlisted_or_special_members(
    tmp_path: Path, extra: Callable[[tarfile.TarFile], None]
) -> None:
    archive = make_archive(
        tmp_path / "a.tar.gz", embedding_inventory(), EMBEDDING_FILES, extra=extra
    )
    extract_error(tmp_path, archive)


def test_extract_refuses_content_and_shape_mismatch(tmp_path: Path) -> None:
    inventory = embedding_inventory()
    bad_hash = dict(EMBEDDING_FILES)
    bad_hash[f"{LICENSES}/NOTICE.txt"] = b"synthetic noticX"
    extract_error(tmp_path, make_archive(tmp_path / "h.tar.gz", inventory, bad_hash), inventory)
    bad_size = dict(EMBEDDING_FILES)
    bad_size[v2.MODEL_PATH] = EMBEDDING_FILES[v2.MODEL_PATH] + b"extra"
    extract_error(tmp_path, make_archive(tmp_path / "s.tar.gz", inventory, bad_size), inventory)
    extract_error(
        tmp_path,
        make_archive(tmp_path / "m.tar.gz", inventory, EMBEDDING_FILES, skip=v2.MODEL_PATH),
        inventory,
    )
    extract_error(
        tmp_path,
        make_archive(
            tmp_path / "i.tar.gz", inventory, EMBEDDING_FILES, skip=v2.inventory_path("embedding")
        ),
        inventory,
    )


def test_extract_refuses_wrong_mode_and_garbage(tmp_path: Path) -> None:
    inventory = embedding_inventory()
    archive = make_archive(
        tmp_path / "a.tar.gz",
        inventory,
        EMBEDDING_FILES,
        modes={f"{ROOT}/open-brain-embedding": 0o644},
    )
    extract_error(tmp_path, archive, inventory)
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not an archive")
    extract_error(tmp_path, junk, inventory)
    truncated = tmp_path / "t.tar.gz"
    good = make_archive(tmp_path / "g.tar.gz", inventory, EMBEDDING_FILES)
    truncated.write_bytes(good.read_bytes()[:-40])
    extract_error(tmp_path, truncated, inventory)


def test_extract_refuses_nonempty_destination_and_keeps_it(tmp_path: Path) -> None:
    inventory = embedding_inventory()
    archive = make_archive(tmp_path / "a.tar.gz", inventory, EMBEDDING_FILES)
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "keep").write_text("x")
    with pytest.raises(ComponentManifestError):
        v2.safe_extract(archive, destination, inventory)
    assert (destination / "keep").read_text() == "x"


def test_extract_refuses_symlinked_destination(tmp_path: Path) -> None:
    inventory = embedding_inventory()
    archive = make_archive(tmp_path / "a.tar.gz", inventory, EMBEDDING_FILES)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ComponentManifestError):
        v2.safe_extract(archive, link, inventory)
    assert not any(real.iterdir())


def test_extract_refuses_expansion_beyond_declared_size(tmp_path: Path) -> None:
    """A tar entry larger than declared is refused before any bytes are written."""
    inventory = embedding_inventory()
    big = dict(EMBEDDING_FILES)
    big[v2.MODEL_PATH] = b"A" * (len(EMBEDDING_FILES[v2.MODEL_PATH]) + 4096)
    archive = make_archive(tmp_path / "a.tar.gz", inventory, big)
    destination = extract_error(tmp_path, archive, inventory)
    assert not destination.exists()


def test_extract_refuses_oversize_compressed_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = embedding_inventory()
    archive = make_archive(tmp_path / "a.tar.gz", inventory, EMBEDDING_FILES)
    monkeypatch.setattr(v2, "EMBEDDING_ARCHIVE_OVERHEAD", 0)
    monkeypatch.setattr(v2, "EMBEDDING_MAX_TOTAL", 1)
    with pytest.raises(ComponentManifestError):
        v2.safe_extract(archive, tmp_path / "out", embedding_inventory())


def test_nonembedding_extract_round_trip(tmp_path: Path) -> None:
    inventory = base_inventory()
    archive = make_archive(tmp_path / "b.tar.gz", inventory, BASE_FILES)
    v2.safe_extract(archive, tmp_path / "out", inventory)
    v2.verify_extracted_resource(tmp_path / "out", inventory)
    with gzip.open(archive) as handle:
        assert handle.read(1)
    out = tmp_path / "out"
    for path, target in BASE_LINKS.items():
        assert os.readlink(out / path) == target
    assert (out / "bin/open-brain").read_bytes() == b"synthetic base"
    assert (out / f"{FRAMEWORK}/Python").read_bytes() == b"synthetic interpreter"


# ------------------------------------------------------- contained symlinks


def base_with(links: dict[str, str], *, role: str = "base") -> v2.Inventory:
    return v2.Inventory("0.1.0", role, "macos-arm64", base_members(links), None)


@pytest.mark.parametrize(
    "links",
    [
        {**BASE_LINKS, "bin/open-brain": "/abs/open-brain"},
        {**BASE_LINKS, "bin/open-brain": ""},
        {**BASE_LINKS, "bin/open-brain": "../libexec/open-brain/open\0brain"},
        {**BASE_LINKS, "bin/open-brain": "..\\libexec/open-brain/open-brain"},
        {**BASE_LINKS, "bin/open-brain": "../libexec/open-brain/./open-brain"},
        {**BASE_LINKS, "bin/open-brain": "../libexec//open-brain/open-brain"},
        {**BASE_LINKS, "bin/open-brain": "../../../etc/passwd"},
        {**BASE_LINKS, "bin/open-brain": "../share/open-brain/inventories"},
        {**BASE_LINKS, "bin/open-brain": "../libexec/open-brain/missing"},
        {**BASE_LINKS, f"{INTERNAL}/Python": "Python.framework/Versions/3.14/../3.14/Python"},
        {**BASE_LINKS, f"{INTERNAL}/Python": "../../../bin/open-brain"},
        {**BASE_LINKS, f"{INTERNAL}/a": "b", f"{INTERNAL}/b": "a"},
        {
            **BASE_LINKS,
            f"{INTERNAL}/a": "b",
            f"{INTERNAL}/b": "c",
            f"{INTERNAL}/c": "d",
            f"{INTERNAL}/d": "e",
            f"{INTERNAL}/e": "Python",
        },
        {**BASE_LINKS, "share/open-brain/alias": "../../libexec/open-brain/open-brain"},
        {**BASE_LINKS, "bin/other": "../libexec/open-brain/open-brain"},
    ],
)
def test_inventory_rejects_uncontained_symlinks(links: dict[str, str]) -> None:
    inventory = base_with(links)
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(inventory)


def test_inventory_admits_contained_chains_up_to_depth_four() -> None:
    chain = {
        **BASE_LINKS,
        f"{INTERNAL}/a": "b",
        f"{INTERNAL}/b": "c",
        f"{INTERNAL}/c": "Python",
    }
    data = v2.render_inventory(base_with(chain))
    assert v2.parse_inventory(data) == base_with(chain)


def test_inventory_rejects_malformed_symlink_members() -> None:
    members = base_members()
    path = "bin/open-brain"
    for changed in (
        {"mode": 0o755},
        {"bytes": 1},
        {"sha256": sha(b"x")},
        {"target": None},
    ):
        edited = tuple(dataclasses.replace(m, **changed) if m.path == path else m for m in members)
        with pytest.raises(ComponentManifestError):
            v2.render_inventory(v2.Inventory("0.1.0", "base", "macos-arm64", edited, None))
    with_target = tuple(
        dataclasses.replace(m, target="x") if m.path == "libexec/open-brain/open-brain" else m
        for m in members
    )
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(v2.Inventory("0.1.0", "base", "macos-arm64", with_target, None))
    beneath = tuple(
        sorted(
            (*members, v2.Member(f"{FRAMEWORK}/Versions/Current/x", "file", 0o644, 1, sha(b"x"))),
            key=lambda row: row.path,
        )
    )
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(v2.Inventory("0.1.0", "base", "macos-arm64", beneath, None))


def test_symlink_members_need_an_exact_target_field() -> None:
    data = v2.render_inventory(base_inventory())
    document = json.loads(data)
    links = [row for row in document["members"] if row["kind"] == "symlink"]
    assert links and all(set(row) == {"path", "kind", "mode", "bytes", "sha256", "target"}
                         for row in links)
    assert all("target" not in row for row in document["members"] if row["kind"] != "symlink")
    with pytest.raises(ComponentManifestError):
        v2.parse_inventory(mutated(data, lambda v: [r.pop("target", None) for r in v["members"]]))
    with pytest.raises(ComponentManifestError):
        v2.parse_inventory(mutated(data, lambda v: v["members"][0].update(target="x")))


def test_graphify_and_embedding_inventories_stay_link_free() -> None:
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(base_with(BASE_LINKS, role="graphify"))
    notice = f"{LICENSES}/NOTICE.txt"
    linked = tuple(
        sorted(
            (
                *(m for m in embedding_inventory().members if m.path != notice),
                link(notice, "../../../../libexec/open-brain-embedding/model/model.onnx"),
            ),
            key=lambda row: row.path,
        )
    )
    with pytest.raises(ComponentManifestError):
        v2.render_inventory(with_members(linked))


def test_verify_checks_symlink_targets_without_following(tmp_path: Path) -> None:
    inventory = base_inventory()
    root = tmp_path / "t"
    write_tree(root, inventory, BASE_FILES)
    v2.verify_extracted_resource(root, inventory)
    entry = root / "bin/open-brain"
    entry.unlink()
    entry.symlink_to("../libexec/open-brain/_internal/Python")
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)
    entry.unlink()
    entry.write_bytes(b"synthetic base")
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)
    entry.unlink()
    entry.symlink_to("../libexec/open-brain/open-brain")
    executable = root / "libexec/open-brain/open-brain"
    executable.unlink()
    executable.symlink_to("_internal/Python")
    with pytest.raises(ComponentManifestError):
        v2.verify_extracted_resource(root, inventory)


@pytest.mark.parametrize(
    ("options", "extra"),
    [
        ({"links": {"bin/open-brain": "../libexec/open-brain/_internal/Python"}}, None),
        ({"links": {"bin/open-brain": "/etc/passwd"}}, None),
        ({"modes": {"bin/open-brain": 0o755}}, None),
        ({}, add("bin/extra", kind=tarfile.SYMTYPE, link="../libexec/open-brain/open-brain")),
        ({}, add("bin/hard", kind=tarfile.LNKTYPE, link="libexec/open-brain/open-brain")),
    ],
)
def test_extract_refuses_symlink_mismatches(
    tmp_path: Path,
    options: dict[str, Any],
    extra: Callable[[tarfile.TarFile], None] | None,
) -> None:
    inventory = base_inventory()
    archive = make_archive(tmp_path / "b.tar.gz", inventory, BASE_FILES, extra=extra, **options)
    extract_error(tmp_path, archive, inventory)


# ------------------------------------------------------------ v1 unchanged

# Re-pinned once for the one-folder base (T5.1): destination libexec/open-brain/open-brain
# and the formula's libexec.install plus bin.install_symlink.
V1_MANIFEST_SHA256 = "4e27ec1757074577148f9d3c4e13a3e831c485b60dc910b2eb448a10c895dfde"
V1_FORMULA_SHA256 = "fb54d781e91009d4362a9fd7dd31147716f739bc0960177acfe823e2bd1c9838"


def test_v1_manifest_and_formula_output_are_unchanged(tmp_path: Path) -> None:
    artifacts = [
        base_native.ReleaseArtifact(
            version="0.1.0",
            platform_tag=platform,
            filename="open-brain-"
            + ("" if role == "base" else role + "-")
            + f"0.1.0-{platform}.tar.gz",
            role=role,
            sha256="a" * 64,
            executable_sha256="b" * 64,
            destination={
                "base": "libexec/open-brain/open-brain",
                "graphify": "libexec/open-brain-graphify",
            }[role],
        )
        for platform in ("macos-arm64", "linux-x86_64")
        for role in ("base", "graphify")
    ]
    manifest = base_native.write_release_manifest(artifacts, tmp_path / "manifest")
    assert sha(manifest.read_bytes()) == V1_MANIFEST_SHA256
    formula = base_native.render_homebrew_formula(manifest, tmp_path / "formula.rb")
    assert sha(formula.read_bytes()) == V1_FORMULA_SHA256
