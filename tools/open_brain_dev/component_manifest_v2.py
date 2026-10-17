"""Component manifest v2, resource inventory v1 and bounded exact extraction.

The legacy v1 text manifest in ``base_native`` is untouched. This module is a separately
dispatched canonical-JSON format; see ``docs/plans/2026-10-15-competitive-gaps-g1/
EMBEDDING-RESOURCE.md`` section 3.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import unicodedata
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

MANIFEST_SCHEMA: Final = "open-brain.component-manifest.v2"
INVENTORY_SCHEMA: Final = "open-brain.resource-inventory.v1"
MANIFEST_FILENAME: Final = "open-brain-component-manifest-v2.json"
PROTOCOL: Final = "open-brain.embedding-helper.v1"

PLATFORMS: Final = ("linux-x86_64", "macos-arm64")
ROLES: Final = ("base", "graphify", "harness", "embedding")
_ROLE_SETS: Final = (
    frozenset({"base", "graphify", "embedding"}),
    frozenset({"base", "graphify", "harness", "embedding"}),
)
EXECUTABLES: Final = {
    "base": "bin/open-brain",
    "graphify": "libexec/open-brain-graphify",
    "harness": "libexec/open-brain-harness/open-brain-harness",
    "embedding": "libexec/open-brain-embedding/open-brain-embedding",
}
_EMBEDDING_ROOT: Final = "libexec/open-brain-embedding"
_LICENSES_ROOT: Final = "share/open-brain/licenses/embedding"
MODEL_PATH: Final = f"{_EMBEDDING_ROOT}/model/model.onnx"
TOKENIZER_PATHS: Final = {
    "tokenizer_json_sha256": f"{_EMBEDDING_ROOT}/tokenizer/tokenizer.json",
    "tokenizer_config_sha256": f"{_EMBEDDING_ROOT}/tokenizer/config.json",
    "vocab_sha256": f"{_EMBEDDING_ROOT}/tokenizer/vocab.txt",
}
_EMBEDDING_DIRECTORIES: Final = frozenset(
    {
        "libexec",
        _EMBEDDING_ROOT,
        f"{_EMBEDDING_ROOT}/model",
        f"{_EMBEDDING_ROOT}/tokenizer",
        f"{_EMBEDDING_ROOT}/runtime",
        "share",
        "share/open-brain",
        "share/open-brain/inventories",
        "share/open-brain/licenses",
        _LICENSES_ROOT,
    }
)
_RUNTIME_NAME: Final = re.compile(r"^libonnxruntime[0-9A-Za-z._-]*$")
_TRIPLES: Final = {
    "linux-x86_64": frozenset({"x86_64-unknown-linux-gnu", "x86_64-unknown-linux-musl"}),
    "macos-arm64": frozenset({"aarch64-apple-darwin"}),
}

_MIB: Final = 1024 * 1024
EMBEDDING_MAX_MEMBERS: Final = 256
EMBEDDING_MAX_TOTAL: Final = 250 * _MIB
EMBEDDING_MAX_FILE: Final = 160 * _MIB
EMBEDDING_ARCHIVE_OVERHEAD: Final = 32 * _MIB
# Roles without an approved budget still get finite bounds.
OTHER_MAX_MEMBERS: Final = 4096
OTHER_MAX_TOTAL: Final = 1024 * _MIB
OTHER_MAX_FILE: Final = 512 * _MIB
_CHUNK: Final = 1024 * 1024

_VERSION: Final = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
_HEX64: Final = re.compile(r"^[0-9a-f]{64}$")
_HEX40: Final = re.compile(r"^[0-9a-f]{40}$")
_ROOT_FIELDS: Final = frozenset({"schema", "version", "resources"})


class ComponentManifestError(ValueError):
    """A v2 manifest, inventory, component or resource tree is invalid."""


def _fail(message: str) -> NoReturn:
    raise ComponentManifestError(message)


# ----------------------------------------------------------------- strict JSON


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            _fail("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> NoReturn:
    _fail("nonfinite JSON number")


def _reject_float(_text: str) -> NoReturn:
    _fail("fractional JSON number")


def _loads(data: bytes) -> object:
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except ComponentManifestError:
        raise
    except UnicodeDecodeError, ValueError, RecursionError:
        _fail("invalid JSON document")


def _canonical(value: object) -> bytes:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return text.encode("utf-8")
    except UnicodeEncodeError, ValueError, TypeError:
        _fail("value is not canonical JSON")


def _document(value: object) -> bytes:
    return _canonical(value) + b"\n"


def _object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail("unexpected object fields")
    return value


def _string(value: object, *, limit: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        _fail("invalid string")
    return value


def _integer(value: object, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("invalid integer")
    if maximum is not None and value > maximum:
        _fail("integer out of range")
    return value


def _hex64(value: object) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        _fail("invalid SHA-256")
    return value


def _hex40(value: object) -> str:
    if not isinstance(value, str) or _HEX40.fullmatch(value) is None:
        _fail("invalid commit identifier")
    return value


def _version(value: object) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        _fail("invalid release version")
    return value


def _platform(value: object) -> str:
    if value not in PLATFORMS:
        _fail("unknown platform")
    assert isinstance(value, str)
    return value


def _role(value: object) -> str:
    if value not in ROLES:
        _fail("unknown role")
    assert isinstance(value, str)
    return value


# ---------------------------------------------------------------- manifest v2


def archive_filename(role: str, version: str, platform: str) -> str:
    return f"open-brain-components-v2-{role}-{version}-{platform}.tar.gz"


def inventory_path(role: str) -> str:
    return f"share/open-brain/inventories/{role}.json"


@dataclass(frozen=True)
class ResourceV2:
    role: str
    platform: str
    filename: str
    archive_sha256: str
    archive_bytes: int
    executable: str
    executable_sha256: str
    inventory: str
    inventory_sha256: str


@dataclass(frozen=True)
class ManifestV2:
    version: str
    resources: tuple[ResourceV2, ...]


_RESOURCE_FIELDS: Final = frozenset(
    {
        "role",
        "platform",
        "filename",
        "archive_sha256",
        "archive_bytes",
        "executable",
        "executable_sha256",
        "inventory",
        "inventory_sha256",
    }
)


def _sort_key(resource: ResourceV2) -> tuple[str, int]:
    return resource.platform, ROLES.index(resource.role)


def _validate_manifest(manifest: ManifestV2) -> None:
    _version(manifest.version)
    ordered = sorted(manifest.resources, key=_sort_key)
    if list(manifest.resources) != ordered:
        _fail("resources are not sorted")
    by_platform: dict[str, list[ResourceV2]] = {}
    for item in manifest.resources:
        _role(item.role)
        _platform(item.platform)
        _hex64(item.archive_sha256)
        _hex64(item.executable_sha256)
        _hex64(item.inventory_sha256)
        _integer(item.archive_bytes, minimum=1)
        if item.filename != archive_filename(item.role, manifest.version, item.platform):
            _fail("filename disagrees with role, version or platform")
        if item.executable != EXECUTABLES[item.role]:
            _fail("executable path is not the fixed role destination")
        if item.inventory != inventory_path(item.role):
            _fail("inventory path is not the fixed role destination")
        by_platform.setdefault(item.platform, []).append(item)
    if set(by_platform) != set(PLATFORMS):
        _fail("both platforms are required")
    role_sets = []
    for items in by_platform.values():
        roles = [item.role for item in items]
        if len(set(roles)) != len(roles) or len({item.filename for item in items}) != len(items):
            _fail("duplicate role or filename on a platform")
        role_sets.append(frozenset(roles))
    if role_sets[0] != role_sets[1] or role_sets[0] not in _ROLE_SETS:
        _fail("role sets are unequal or unsupported")


def render_manifest_v2(manifest: ManifestV2) -> bytes:
    _validate_manifest(manifest)
    return _document(
        {
            "schema": MANIFEST_SCHEMA,
            "version": manifest.version,
            "resources": [
                {
                    "role": item.role,
                    "platform": item.platform,
                    "filename": item.filename,
                    "archive_sha256": item.archive_sha256,
                    "archive_bytes": item.archive_bytes,
                    "executable": item.executable,
                    "executable_sha256": item.executable_sha256,
                    "inventory": item.inventory,
                    "inventory_sha256": item.inventory_sha256,
                }
                for item in manifest.resources
            ],
        }
    )


def parse_manifest_v2(data: bytes) -> ManifestV2:
    root = _object(_loads(data), _ROOT_FIELDS)
    if root["schema"] != MANIFEST_SCHEMA:
        _fail("unknown manifest schema")
    raw = root["resources"]
    if not isinstance(raw, list):
        _fail("resources must be an array")
    resources = []
    for entry in raw:
        fields = _object(entry, _RESOURCE_FIELDS)
        resources.append(
            ResourceV2(
                role=_role(fields["role"]),
                platform=_platform(fields["platform"]),
                filename=_string(fields["filename"]),
                archive_sha256=_hex64(fields["archive_sha256"]),
                archive_bytes=_integer(fields["archive_bytes"], minimum=1),
                executable=_string(fields["executable"]),
                executable_sha256=_hex64(fields["executable_sha256"]),
                inventory=_string(fields["inventory"]),
                inventory_sha256=_hex64(fields["inventory_sha256"]),
            )
        )
    manifest = ManifestV2(version=_version(root["version"]), resources=tuple(resources))
    if render_manifest_v2(manifest) != data:
        _fail("manifest bytes are not canonical")
    return manifest


# ----------------------------------------------------------------- inventory


@dataclass(frozen=True)
class Member:
    path: str
    kind: str
    mode: int
    bytes: int
    sha256: str | None


@dataclass(frozen=True)
class Inventory:
    release_version: str
    role: str
    platform: str
    members: tuple[Member, ...]
    component: dict[str, object] | None


_INVENTORY_FIELDS: Final = frozenset(
    {"schema", "release_version", "role", "platform", "members", "component"}
)
_MEMBER_FIELDS: Final = frozenset({"path", "kind", "mode", "bytes", "sha256"})


def _limits(role: str) -> tuple[int, int, int]:
    if role == "embedding":
        return EMBEDDING_MAX_MEMBERS, EMBEDDING_MAX_TOTAL, EMBEDDING_MAX_FILE
    return OTHER_MAX_MEMBERS, OTHER_MAX_TOTAL, OTHER_MAX_FILE


def _check_path(path: str) -> None:
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        _fail("invalid member path")
    for part in path.split("/"):
        if part in ("", ".", ".."):
            _fail("invalid member path")


def _is_executable_location(role: str, path: str) -> bool:
    if path == EXECUTABLES[role]:
        return True
    if path.startswith("share/"):
        return False
    if role == "embedding":
        return path.startswith(f"{_EMBEDDING_ROOT}/runtime/")
    return True


def _check_embedding_path(member: Member) -> None:
    path = member.path
    if member.kind == "directory":
        if path in _EMBEDDING_DIRECTORIES or path.startswith(_LICENSES_ROOT + "/"):
            return
        _fail("directory outside the embedding resource")
    fixed = {EXECUTABLES["embedding"], MODEL_PATH, *TOKENIZER_PATHS.values()}
    if path in fixed or path.startswith(_LICENSES_ROOT + "/"):
        return
    runtime = f"{_EMBEDDING_ROOT}/runtime/"
    if path.startswith(runtime) and _RUNTIME_NAME.fullmatch(path[len(runtime) :]):
        return
    _fail("file outside the embedding resource")


def _validate_members(inventory: Inventory) -> None:
    members = inventory.members
    max_members, max_total, max_file = _limits(inventory.role)
    if not members or len(members) > max_members:
        _fail("member count out of range")
    paths = [member.path for member in members]
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        _fail("members are not sorted and unique")
    kinds = {member.path: member.kind for member in members}
    folded: set[str] = set()
    total = 0
    for member in members:
        _check_path(member.path)
        key = unicodedata.normalize("NFC", member.path).casefold()
        if key in folded:
            _fail("member paths collide")
        folded.add(key)
        parent = member.path.rpartition("/")[0]
        if parent and kinds.get(parent) != "directory":
            _fail("member parent is not a declared directory")
        if member.path == inventory_path(inventory.role):
            _fail("inventory must not list itself")
        if member.kind == "directory":
            if member.mode != 0o755 or member.bytes != 0 or member.sha256 is not None:
                _fail("invalid directory member")
        elif member.kind == "file":
            if member.mode not in (0o644, 0o755):
                _fail("invalid file mode")
            if member.mode == 0o755 and not _is_executable_location(inventory.role, member.path):
                _fail("executable bit on a data asset")
            if member.bytes > max_file:
                _fail("file exceeds the per-file limit")
            _integer(member.bytes, minimum=0)
            _hex64(member.sha256)
            total += member.bytes
        else:
            _fail("unsupported member kind")
        if inventory.role == "embedding":
            _check_embedding_path(member)
        elif member.path.startswith((_EMBEDDING_ROOT, _LICENSES_ROOT)):
            _fail("path belongs to the embedding resource")
    if total > max_total:
        _fail("inventory exceeds the total byte limit")
    if kinds.get(inventory_path(inventory.role).rpartition("/")[0]) != "directory":
        _fail("inventory directory is not declared")


def _member_dict(member: Member) -> dict[str, object]:
    return {
        "path": member.path,
        "kind": member.kind,
        "mode": member.mode,
        "bytes": member.bytes,
        "sha256": member.sha256,
    }


def render_inventory(inventory: Inventory) -> bytes:
    _validate_inventory(inventory)
    return _document(
        {
            "schema": INVENTORY_SCHEMA,
            "release_version": inventory.release_version,
            "role": inventory.role,
            "platform": inventory.platform,
            "members": [_member_dict(member) for member in inventory.members],
            "component": inventory.component,
        }
    )


def _validate_inventory(inventory: Inventory) -> None:
    _version(inventory.release_version)
    _role(inventory.role)
    _platform(inventory.platform)
    _validate_members(inventory)
    if inventory.role == "embedding":
        if inventory.component is None:
            _fail("embedding inventory requires a component")
        validate_embedding_component(inventory.component, inventory)
    elif inventory.component is not None:
        _fail("only the embedding inventory carries a component")


def parse_inventory(data: bytes) -> Inventory:
    root = _object(_loads(data), _INVENTORY_FIELDS)
    if root["schema"] != INVENTORY_SCHEMA:
        _fail("unknown inventory schema")
    raw = root["members"]
    if not isinstance(raw, list):
        _fail("members must be an array")
    members = []
    for entry in raw:
        fields = _object(entry, _MEMBER_FIELDS)
        sha = fields["sha256"]
        members.append(
            Member(
                path=_string(fields["path"], limit=1024),
                kind=_string(fields["kind"]),
                mode=_integer(fields["mode"]),
                bytes=_integer(fields["bytes"]),
                sha256=None if sha is None else _hex64(sha),
            )
        )
    component = root["component"]
    if component is not None and not isinstance(component, dict):
        _fail("component must be an object or null")
    inventory = Inventory(
        release_version=_version(root["release_version"]),
        role=_role(root["role"]),
        platform=_platform(root["platform"]),
        members=tuple(members),
        component=component,
    )
    if render_inventory(inventory) != data:
        _fail("inventory bytes are not canonical")
    return inventory


# ---------------------------------------------------------- embedding component

_HELPER_FIELDS: Final = frozenset(
    {
        "source_commit",
        "recipe_sha256",
        "executable_sha256",
        "target_triple",
        "minimum_os",
        "cpu_features",
        "dependency_lock_sha256",
    }
)
_RUNTIME_FIELDS: Final = frozenset(
    {
        "upstream_commit",
        "recipe_sha256",
        "library_path",
        "library_bytes",
        "library_sha256",
        "cpu_only",
        "telemetry_compiled",
        "dependency_inventory_sha256",
    }
)
_MODEL_FIELDS: Final = frozenset({"repository", "revision", "path", "bytes", "sha256"})
# The first seven are read by release/open-brain-embedding/src/component.rs.
_PREPROCESS_FIELDS: Final = frozenset(
    {
        "profile_id",
        "dimensions",
        "pooling",
        "max_query_tokens",
        "document_window_tokens",
        "document_overlap_tokens",
        "cls_token_id",
        "sep_token_id",
        "pad_token_id",
        "query_prefix",
        "tokenizer_build",
        "segmenter_version",
        *TOKENIZER_PATHS,
    }
)
_COMPONENT_FIELDS: Final = frozenset(
    {"protocol", "helper_build", "runtime_build", "model", "preprocess", "notices_sha256"}
)


def component_id(component: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(dict(component))).hexdigest()


def _entry(inventory: Inventory, path: str, size: int, digest: str) -> None:
    for member in inventory.members:
        if member.path == path:
            if member.kind != "file" or member.bytes != size or member.sha256 != digest:
                _fail("component declaration disagrees with the inventory")
            return
    _fail("component declaration names a missing inventory entry")


def validate_embedding_component(
    component: Mapping[str, object],
    inventory: Inventory,
    *,
    executable_sha256: str | None = None,
) -> None:
    """Check exact fields and cross-check every duplicated declaration."""
    if inventory.role != "embedding":
        _fail("component belongs to the embedding role")
    root = _object(dict(component), _COMPONENT_FIELDS)
    if root["protocol"] != PROTOCOL:
        _fail("unknown helper protocol")
    _hex64(root["notices_sha256"])

    helper = _object(root["helper_build"], _HELPER_FIELDS)
    _hex40(helper["source_commit"])
    _hex64(helper["recipe_sha256"])
    helper_hash = _hex64(helper["executable_sha256"])
    _hex64(helper["dependency_lock_sha256"])
    if helper["target_triple"] not in _TRIPLES[inventory.platform]:
        _fail("target triple disagrees with the platform")
    _string(helper["minimum_os"])
    features = helper["cpu_features"]
    if not isinstance(features, list):
        _fail("cpu_features must be an array")
    names = [_string(item, limit=64) for item in features]
    if names != sorted(set(names)):
        _fail("cpu_features must be sorted and unique")
    if executable_sha256 is not None and helper_hash != executable_sha256:
        _fail("helper executable hash disagrees with the resource")
    executable = EXECUTABLES["embedding"]
    for member in inventory.members:
        if member.path == executable and (member.sha256 != helper_hash or member.mode != 0o755):
            _fail("helper executable disagrees with the inventory")
    if not any(member.path == executable for member in inventory.members):
        _fail("helper executable is missing from the inventory")

    runtime = _object(root["runtime_build"], _RUNTIME_FIELDS)
    _hex40(runtime["upstream_commit"])
    _hex64(runtime["recipe_sha256"])
    _hex64(runtime["dependency_inventory_sha256"])
    if runtime["cpu_only"] is not True or runtime["telemetry_compiled"] is not False:
        _fail("runtime must be CPU-only and telemetry-free")
    library = _string(runtime["library_path"], limit=1024)
    prefix = f"{_EMBEDDING_ROOT}/runtime/"
    if not library.startswith(prefix) or _RUNTIME_NAME.fullmatch(library[len(prefix) :]) is None:
        _fail("runtime library path is not a pinned native library")
    _entry(
        inventory,
        library,
        _integer(runtime["library_bytes"], minimum=1),
        _hex64(runtime["library_sha256"]),
    )

    model = _object(root["model"], _MODEL_FIELDS)
    _string(model["repository"])
    _hex40(model["revision"])
    if model["path"] != MODEL_PATH:
        _fail("model path is not the fixed graph path")
    _entry(inventory, MODEL_PATH, _integer(model["bytes"], minimum=1), _hex64(model["sha256"]))

    pre = _object(root["preprocess"], _PREPROCESS_FIELDS)
    _string(pre["profile_id"])
    _string(pre["tokenizer_build"])
    _string(pre["segmenter_version"])
    if not isinstance(pre["query_prefix"], str) or len(pre["query_prefix"]) > 256:
        _fail("invalid query prefix")
    if pre["pooling"] not in ("mean", "cls"):
        _fail("unknown pooling")
    for key, value in (
        ("dimensions", 384),
        ("max_query_tokens", 254),
        ("document_window_tokens", 224),
        ("document_overlap_tokens", 32),
    ):
        if _integer(pre[key]) != value:
            _fail("preprocess limits differ from protocol v1")
    for key in ("cls_token_id", "sep_token_id", "pad_token_id"):
        _integer(pre[key], maximum=2**32 - 1)
    for key, path in TOKENIZER_PATHS.items():
        digest = _hex64(pre[key])
        for member in inventory.members:
            if member.path == path:
                if member.sha256 != digest:
                    _fail("tokenizer asset hash disagrees with the inventory")
                break
        else:
            _fail("tokenizer asset is missing from the inventory")


def validate_resource_inventory(resource: ResourceV2, version: str, inventory: Inventory) -> None:
    """Cross-check one manifest resource against its parsed inventory."""
    if (
        inventory.release_version != version
        or inventory.role != resource.role
        or inventory.platform != resource.platform
    ):
        _fail("inventory disagrees with the manifest resource")
    for member in inventory.members:
        if member.path == resource.executable:
            if (
                member.kind != "file"
                or member.mode != 0o755
                or member.sha256 != (resource.executable_sha256)
            ):
                _fail("executable disagrees with the inventory")
            break
    else:
        _fail("executable is missing from the inventory")
    if hashlib.sha256(render_inventory(inventory)).hexdigest() != resource.inventory_sha256:
        _fail("inventory hash disagrees with the manifest")
    if inventory.component is not None:
        validate_embedding_component(
            inventory.component, inventory, executable_sha256=resource.executable_sha256
        )


# ------------------------------------------------------------ tree verification


def _hash_regular(path: str, expected: Member | None, *, size: int | None = None) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("resource file cannot be opened without following links")
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("resource file is not a single-link regular file")
        declared = expected.bytes if expected is not None else size
        if declared is not None and info.st_size != declared:
            _fail("resource file size mismatch")
        digest = hashlib.sha256()
        seen = 0
        while chunk := handle.read(_CHUNK):
            seen += len(chunk)
            if declared is not None and seen > declared:
                _fail("resource file grew during verification")
            digest.update(chunk)
        if declared is not None and seen != declared:
            _fail("resource file size mismatch")
    return digest.hexdigest()


def verify_extracted_resource(root: Path, inventory: Inventory) -> None:
    """Refuse anything in ``root`` that is not exactly the inventory (no-follow)."""
    _validate_inventory(inventory)
    expected = {member.path: member for member in inventory.members}
    own = inventory_path(inventory.role)
    own_bytes = render_inventory(inventory)
    try:
        top = os.lstat(root)
    except OSError:
        _fail("resource root is unavailable")
    if not stat.S_ISDIR(top.st_mode):
        _fail("resource root is not a directory")
    seen: set[str] = set()

    def walk(directory: str, prefix: str) -> None:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            _fail("resource directory is unreadable")
        for name in names:
            relative = f"{prefix}{name}"
            full = os.path.join(directory, name)
            try:
                info = os.lstat(full)
            except OSError:
                _fail("resource entry is unreadable")
            if relative == own:
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o644:
                    _fail("inventory file is invalid")
                digest = _hash_regular(full, None, size=len(own_bytes))
                if digest != hashlib.sha256(own_bytes).hexdigest():
                    _fail("inventory file differs from the inventory")
                continue
            member = expected.get(relative)
            if member is None or relative in seen:
                _fail("resource contains an undeclared entry")
            seen.add(relative)
            if stat.S_ISDIR(info.st_mode):
                if member.kind != "directory" or stat.S_IMODE(info.st_mode) != member.mode:
                    _fail("directory disagrees with the inventory")
                walk(full, relative + "/")
            elif stat.S_ISREG(info.st_mode):
                if member.kind != "file" or stat.S_IMODE(info.st_mode) != member.mode:
                    _fail("file disagrees with the inventory")
                if _hash_regular(full, member) != member.sha256:
                    _fail("file hash disagrees with the inventory")
            else:
                _fail("resource contains a link or special file")

    walk(os.fspath(root), "")
    if seen != set(expected):
        _fail("resource is missing declared entries")
    if not (Path(root) / own).is_file():
        _fail("resource is missing its inventory file")


# ----------------------------------------------------------------- extraction


def _clear(destination: Path, created: bool) -> None:
    if created:
        shutil.rmtree(destination, ignore_errors=True)
        return
    for child in destination.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def safe_extract(archive: Path, destination: Path, inventory: Inventory) -> None:
    """Stream a tar.gz into an empty ``destination``, enforcing the inventory first."""
    _validate_inventory(inventory)
    expected = {member.path: member for member in inventory.members}
    own = inventory_path(inventory.role)
    own_bytes = render_inventory(inventory)
    max_members, max_total, _ = _limits(inventory.role)
    if inventory.role == "embedding":
        budget = EMBEDDING_MAX_TOTAL + EMBEDDING_ARCHIVE_OVERHEAD
        try:
            if archive.stat().st_size > budget:
                _fail("archive exceeds the compressed budget")
        except OSError:
            _fail("archive is unavailable")
    created = False
    try:
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            _fail("destination is invalid")
        if destination.exists():
            if any(destination.iterdir()):
                _fail("destination is not empty")
        else:
            destination.mkdir(mode=0o755, parents=True)
            created = True
    except OSError:
        _fail("destination is unavailable")
    seen: set[str] = set()
    remaining = max_total + len(own_bytes)
    try:
        with tarfile.open(archive, mode="r|gz") as stream:
            for member in stream:
                name = member.name
                if len(seen) >= max_members + 1 or member.pax_headers:
                    _fail("archive has too many members")
                if name in seen:
                    _fail("archive repeats a member")
                is_own = name == own
                record = expected.get(name)
                if record is None and not is_own:
                    _fail("archive contains an undeclared member")
                seen.add(name)
                parent = name.rpartition("/")[0]
                if parent and parent not in seen:
                    _fail("archive member precedes its parent directory")
                target = destination.joinpath(*name.split("/"))
                if member.mode & 0o7777 != (0o644 if is_own else record.mode if record else 0):
                    _fail("archive mode disagrees with the inventory")
                if member.isdir():
                    if record is None or record.kind != "directory":
                        _fail("archive directory disagrees with the inventory")
                    os.mkdir(target, 0o700)
                    os.chmod(target, record.mode)
                elif member.isreg() and not member.issparse():
                    want = len(own_bytes) if record is None else record.bytes
                    if (record is not None and record.kind != "file") or member.size != want:
                        _fail("archive file disagrees with the inventory")
                    remaining -= member.size
                    if remaining < 0:
                        _fail("archive expands beyond the declared total")
                    source = stream.extractfile(member)
                    if source is None:
                        _fail("archive file is unreadable")
                    digest = hashlib.sha256()
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                    with os.fdopen(os.open(target, flags, 0o600), "wb") as out:
                        written = 0
                        while chunk := source.read(min(_CHUNK, want - written + 1)):
                            written += len(chunk)
                            if written > want:
                                _fail("archive file exceeds its declared size")
                            digest.update(chunk)
                            out.write(chunk)
                    expected_digest = (
                        hashlib.sha256(own_bytes).hexdigest() if record is None else record.sha256
                    )
                    if written != want or digest.hexdigest() != expected_digest:
                        _fail("archive file differs from the inventory")
                    os.chmod(target, 0o644 if record is None else record.mode)
                else:
                    _fail("archive member type is not allowed")
        if seen != set(expected) | {own}:
            _fail("archive is missing declared members")
    except ComponentManifestError:
        _clear(destination, created)
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError, zlib.error) as error:
        _clear(destination, created)
        raise ComponentManifestError("archive extraction failed") from error
