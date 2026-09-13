"""Install the packaged Obsidian plugin into the managed workspace."""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, cast

from open_brain_engine import __version__
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    StorageError,
    atomic_replace,
    capture_root_identity,
    open_root_descriptor,
    read_confined,
    read_confined_tree,
)

PLUGIN_ID: Final = "open-brain"
PLUGIN_DIRECTORY: Final = ".obsidian/plugins/open-brain"
PLUGIN_MARKER: Final = f"{PLUGIN_DIRECTORY}/.open-brain-owned.json"
PLUGIN_ASSETS: Final = ("main.js", "manifest.json", "styles.css")
_MAX_ASSET_BYTES: Final = {
    "main.js": 2 * 1024 * 1024,
    "manifest.json": 16 * 1024,
    "styles.css": 256 * 1024,
}
_DIRECTORY_FLAGS: Final = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS: Final = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class ObsidianPluginFailure(RuntimeError):
    """One bounded plugin-installation failure category."""

    def __init__(self, code: str) -> None:
        if code not in {
            "assets_unavailable",
            "foreign_plugin",
            "modified_plugin",
            "unsafe_workspace",
            "workspace_unconfigured",
        }:
            raise ValueError("invalid Obsidian plugin failure")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ObsidianPluginBundle:
    version: str
    assets: dict[str, bytes]
    digests: dict[str, str]


def discover_obsidian_plugin_assets(base_executable: Path | None = None) -> Path:
    """Find assets installed in the same immutable product prefix as the executable."""
    candidate = Path(sys.executable) if base_executable is None else base_executable
    try:
        executable = candidate.resolve(strict=True)
        metadata = executable.stat(follow_symlinks=False)
    except OSError:
        raise ObsidianPluginFailure("assets_unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
        raise ObsidianPluginFailure("assets_unavailable")
    directory = executable.parent.parent / "share/open-brain/obsidian-plugin"
    try:
        directory_metadata = directory.stat(follow_symlinks=False)
    except OSError:
        raise ObsidianPluginFailure("assets_unavailable") from None
    if not stat.S_ISDIR(directory_metadata.st_mode) or directory.is_symlink():
        raise ObsidianPluginFailure("assets_unavailable")
    return directory


def load_obsidian_plugin_bundle(directory: Path) -> ObsidianPluginBundle:
    """Load one bounded, non-symlinked bundle with the product version."""
    if not directory.is_absolute():
        raise ObsidianPluginFailure("assets_unavailable")
    try:
        metadata = directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
            raise ObsidianPluginFailure("assets_unavailable")
        assets: dict[str, bytes] = {}
        for name in PLUGIN_ASSETS:
            path = directory / name
            item = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode) or path.is_symlink() or item.st_size <= 0:
                raise ObsidianPluginFailure("assets_unavailable")
            if item.st_size > _MAX_ASSET_BYTES[name]:
                raise ObsidianPluginFailure("assets_unavailable")
            assets[name] = path.read_bytes()
    except ObsidianPluginFailure:
        raise
    except OSError:
        raise ObsidianPluginFailure("assets_unavailable") from None
    manifest = _manifest(assets["manifest.json"])
    version = manifest.get("version")
    if (
        manifest.get("id") != PLUGIN_ID
        or manifest.get("isDesktopOnly") is not True
        or not isinstance(manifest.get("minAppVersion"), str)
        or version != __version__
    ):
        raise ObsidianPluginFailure("assets_unavailable")
    return ObsidianPluginBundle(
        version=version,
        assets=assets,
        digests={name: sha256(payload).hexdigest() for name, payload in assets.items()},
    )


def install_obsidian_plugin(workspace: Path, bundle: ObsidianPluginBundle) -> dict[str, object]:
    """Install or upgrade only Open Brain-owned plugin assets."""
    root_identity = _workspace_identity(workspace)
    existing_marker = _read_marker(workspace, root_identity)
    if existing_marker is None:
        existing = read_confined_tree(
            root=workspace,
            relative=PLUGIN_DIRECTORY,
            expected_root_identity=root_identity,
            maximum_entries=16,
            maximum_file_bytes=2 * 1024 * 1024,
            maximum_total_bytes=3 * 1024 * 1024,
        )
        if existing:
            raise ObsidianPluginFailure("foreign_plugin")
        marker = _marker(bundle)
        atomic_replace(
            root=workspace,
            relative=PLUGIN_MARKER,
            data=marker,
            require_existing=False,
            expected_root_identity=root_identity,
        )
    else:
        installed = _parse_marker(existing_marker)
        _require_unmodified_assets(workspace, root_identity, installed, bundle)

    for name in PLUGIN_ASSETS:
        relative = f"{PLUGIN_DIRECTORY}/{name}"
        current = read_confined(
            root=workspace,
            relative=relative,
            expected_root_identity=root_identity,
            maximum_bytes=_MAX_ASSET_BYTES[name],
        )
        atomic_replace(
            root=workspace,
            relative=relative,
            data=bundle.assets[name],
            require_existing=current is not None,
            expected_existing_sha256=(sha256(current).hexdigest() if current is not None else None),
            expected_root_identity=root_identity,
        )

    current_marker = read_confined(
        root=workspace,
        relative=PLUGIN_MARKER,
        expected_root_identity=root_identity,
        maximum_bytes=16 * 1024,
    )
    if current_marker is None:
        raise ObsidianPluginFailure("modified_plugin")
    updated_marker = _marker(bundle)
    atomic_replace(
        root=workspace,
        relative=PLUGIN_MARKER,
        data=updated_marker,
        require_existing=True,
        expected_existing_sha256=sha256(current_marker).hexdigest(),
        expected_root_identity=root_identity,
    )
    return {
        "assets": list(PLUGIN_ASSETS),
        "plugin_id": PLUGIN_ID,
        "status": "installed",
        "version": bundle.version,
    }


def obsidian_plugin_status(workspace: Path, bundle: ObsidianPluginBundle) -> dict[str, object]:
    """Report whether the installed owned files match the packaged bundle."""
    root_identity = _workspace_identity(workspace)
    payload = _read_marker(workspace, root_identity)
    if payload is None:
        existing = read_confined_tree(
            root=workspace,
            relative=PLUGIN_DIRECTORY,
            expected_root_identity=root_identity,
            maximum_entries=16,
            maximum_file_bytes=2 * 1024 * 1024,
            maximum_total_bytes=3 * 1024 * 1024,
        )
        return {
            "plugin_id": PLUGIN_ID,
            "status": "foreign" if existing else "not_installed",
        }
    installed = _parse_marker(payload)
    state = "current"
    for name in PLUGIN_ASSETS:
        current = read_confined(
            root=workspace,
            relative=f"{PLUGIN_DIRECTORY}/{name}",
            expected_root_identity=root_identity,
            maximum_bytes=_MAX_ASSET_BYTES[name],
        )
        if current is None or sha256(current).hexdigest() != installed[name]:
            state = "modified"
            break
        if installed[name] != bundle.digests[name]:
            state = "update_available"
    return {
        "installed_version": cast(str, _marker_value(payload)["version"]),
        "packaged_version": bundle.version,
        "plugin_id": PLUGIN_ID,
        "status": state,
    }


def remove_obsidian_plugin(workspace: Path) -> dict[str, object]:
    """Remove only unchanged owned assets and the ownership marker."""
    root_identity = _workspace_identity(workspace)
    payload = _read_marker(workspace, root_identity)
    if payload is None:
        raise ObsidianPluginFailure("foreign_plugin")
    installed = _parse_marker(payload)
    for name in PLUGIN_ASSETS:
        current = read_confined(
            root=workspace,
            relative=f"{PLUGIN_DIRECTORY}/{name}",
            expected_root_identity=root_identity,
            maximum_bytes=_MAX_ASSET_BYTES[name],
        )
        if current is not None and sha256(current).hexdigest() != installed[name]:
            raise ObsidianPluginFailure("modified_plugin")
    for name in PLUGIN_ASSETS:
        _unlink_exact(
            workspace,
            root_identity,
            f"{PLUGIN_DIRECTORY}/{name}",
            expected_sha256=installed[name],
            missing_ok=True,
        )
    _unlink_exact(
        workspace,
        root_identity,
        PLUGIN_MARKER,
        expected_sha256=sha256(payload).hexdigest(),
        missing_ok=False,
    )
    return {
        "assets": list(PLUGIN_ASSETS),
        "plugin_id": PLUGIN_ID,
        "status": "removed",
    }


def _workspace_identity(workspace: Path) -> tuple[int, int]:
    if not workspace.is_absolute():
        raise ObsidianPluginFailure("unsafe_workspace")
    try:
        metadata = workspace.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or workspace.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise ObsidianPluginFailure("unsafe_workspace")
        return capture_root_identity(workspace)
    except ObsidianPluginFailure:
        raise
    except OSError, RootConfinementError, StorageError:
        raise ObsidianPluginFailure("unsafe_workspace") from None


def _read_marker(workspace: Path, root_identity: tuple[int, int]) -> bytes | None:
    try:
        payload = read_confined(
            root=workspace,
            relative=PLUGIN_MARKER,
            expected_root_identity=root_identity,
            maximum_bytes=16 * 1024,
        )
        if payload is not None:
            _parse_marker(payload)
        return payload
    except ObsidianPluginFailure:
        raise
    except RootConfinementError, StorageError, ValueError:
        raise ObsidianPluginFailure("modified_plugin") from None


def _marker(bundle: ObsidianPluginBundle) -> bytes:
    return portable_canonical_json_bytes(
        {
            "asset_sha256": bundle.digests,
            "plugin_id": PLUGIN_ID,
            "schema_version": 1,
            "version": bundle.version,
        }
    )


def _marker_value(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise ObsidianPluginFailure("modified_plugin") from None
    if not isinstance(value, dict):
        raise ObsidianPluginFailure("modified_plugin")
    return value


def _parse_marker(payload: bytes) -> dict[str, str]:
    value = _marker_value(payload)
    digests = value.get("asset_sha256")
    if (
        set(value) != {"asset_sha256", "plugin_id", "schema_version", "version"}
        or value.get("plugin_id") != PLUGIN_ID
        or value.get("schema_version") != 1
        or not isinstance(value.get("version"), str)
        or not isinstance(digests, dict)
        or set(digests) != set(PLUGIN_ASSETS)
        or not all(
            isinstance(item, str)
            and len(item) == 64
            and all(character in "0123456789abcdef" for character in item)
            for item in digests.values()
        )
    ):
        raise ObsidianPluginFailure("modified_plugin")
    return cast(dict[str, str], digests)


def _manifest(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise ObsidianPluginFailure("assets_unavailable") from None
    if not isinstance(value, dict):
        raise ObsidianPluginFailure("assets_unavailable")
    return value


def _require_unmodified_assets(
    workspace: Path,
    root_identity: tuple[int, int],
    installed: dict[str, str],
    bundle: ObsidianPluginBundle,
) -> None:
    for name in PLUGIN_ASSETS:
        current = read_confined(
            root=workspace,
            relative=f"{PLUGIN_DIRECTORY}/{name}",
            expected_root_identity=root_identity,
            maximum_bytes=_MAX_ASSET_BYTES[name],
        )
        if current is None:
            continue
        digest = sha256(current).hexdigest()
        if digest not in {installed[name], bundle.digests[name]}:
            raise ObsidianPluginFailure("modified_plugin")


def _unlink_exact(
    root: Path,
    root_identity: tuple[int, int],
    relative: str,
    *,
    expected_sha256: str,
    missing_ok: bool,
) -> None:
    parts = relative.split("/")
    root_fd = -1
    parent_fd = -1
    file_fd = -1
    try:
        root_fd = open_root_descriptor(root, root_identity)
        parent_fd = os.dup(root_fd)
        for part in parts[:-1]:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            file_fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            if missing_ok:
                return
            raise ObsidianPluginFailure("modified_plugin") from None
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 2 * 1024 * 1024:
            raise ObsidianPluginFailure("modified_plugin")
        with os.fdopen(os.dup(file_fd), "rb") as stream:
            digest = sha256(stream.read(2 * 1024 * 1024 + 1)).hexdigest()
        if digest != expected_sha256:
            raise ObsidianPluginFailure("modified_plugin")
        os.unlink(parts[-1], dir_fd=parent_fd)
        os.fsync(parent_fd)
    except ObsidianPluginFailure:
        raise
    except OSError, RootConfinementError, StorageError:
        raise ObsidianPluginFailure("modified_plugin") from None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        if root_fd >= 0:
            os.close(root_fd)


__all__ = [
    "PLUGIN_ASSETS",
    "PLUGIN_DIRECTORY",
    "PLUGIN_ID",
    "ObsidianPluginBundle",
    "ObsidianPluginFailure",
    "discover_obsidian_plugin_assets",
    "install_obsidian_plugin",
    "load_obsidian_plugin_bundle",
    "obsidian_plugin_status",
    "remove_obsidian_plugin",
]
