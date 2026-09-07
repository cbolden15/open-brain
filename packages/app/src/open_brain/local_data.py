"""Fail-closed platform-local data-directory selection for default Open Brain."""

from __future__ import annotations

import ctypes
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

RootIdentity = tuple[int, int]
FilesystemTypeProbe = Callable[[Path, str], str]

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_DARWIN_DENIED_FILESYSTEMS = frozenset({"afpfs", "nfs", "smbfs", "webdav"})
_LINUX_DENIED_FILESYSTEMS = frozenset(
    {"9p", "afs", "ceph", "cifs", "davfs", "fuse.sshfs", "nfs", "nfs4", "smb3"}
)
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


class LocalDataError(ValueError):
    """The default private data directory cannot be selected or opened safely."""


@dataclass(frozen=True, slots=True)
class LocalRootSelection:
    """The exact default-product data paths chosen before any state is created."""

    home: Path
    data_home: Path | None
    brain_root: Path
    platform_name: str

    @property
    def managed_directories(self) -> tuple[Path, ...]:
        if self.data_home is None:
            return (self.brain_root,)
        return (self.data_home, self.brain_root)


@dataclass(slots=True)
class PreparedLocalRoot:
    """A held root identity that can be revalidated at each write boundary."""

    selection: LocalRootSelection
    filesystem_type: str
    root_identity: RootIdentity
    _root_fd: int
    _effective_uid: int
    _filesystem_type_probe: FilesystemTypeProbe

    def revalidate(self) -> None:
        """Reject a path, owner, mode, mount, or identity change before a write."""
        if self._root_fd < 0:
            raise LocalDataError("private data directory is unavailable")
        observed_fd, observed_filesystem = _open_target(
            self.selection,
            effective_uid=self._effective_uid,
            create=False,
            filesystem_type_probe=self._filesystem_type_probe,
        )
        try:
            metadata = os.fstat(observed_fd)
            observed_identity = (metadata.st_dev, metadata.st_ino)
            held = os.fstat(self._root_fd)
            held_identity = (held.st_dev, held.st_ino)
            if (
                observed_identity != self.root_identity
                or held_identity != self.root_identity
                or observed_filesystem != self.filesystem_type
            ):
                raise LocalDataError("private data directory changed during bootstrap")
        finally:
            os.close(observed_fd)

    def private_file_exists(self, relative_path: str) -> bool:
        """Check a private regular file beneath the held root without following links."""
        parts = _relative_parts(relative_path)
        parent_fd = os.dup(self._root_fd)
        try:
            for part in parts[:-1]:
                next_fd = _open_existing_directory(parent_fd, part)
                os.close(parent_fd)
                parent_fd = next_fd
                _require_private_owner_directory(parent_fd, self._effective_uid)
            try:
                metadata = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self._effective_uid
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_nlink != 1
            ):
                raise LocalDataError("private data file is unsafe")
            return True
        except FileNotFoundError:
            return False
        finally:
            os.close(parent_fd)

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1


def select_local_root(
    *,
    data_dir: str | None,
    environment: Mapping[str, object],
    platform_name: str | None = None,
) -> LocalRootSelection:
    """Resolve the default data home and Brain root without creating either path."""
    selected_platform = sys.platform if platform_name is None else platform_name
    if selected_platform != "darwin" and not selected_platform.startswith("linux"):
        raise LocalDataError("Open Brain supports macOS and Linux")
    home = _absolute_environment_path(environment, "HOME")
    if data_dir is not None:
        brain_root = _absolute_path(data_dir, label="--data-dir")
        selection = LocalRootSelection(
            home=home,
            data_home=None,
            brain_root=brain_root,
            platform_name=selected_platform,
        )
    elif selected_platform == "darwin":
        data_home = home / "Library" / "Application Support" / "open-brain"
        selection = LocalRootSelection(
            home=home,
            data_home=data_home,
            brain_root=data_home / "brain",
            platform_name=selected_platform,
        )
    else:
        xdg_value = environment.get("XDG_DATA_HOME")
        if xdg_value in {None, ""}:
            xdg_data_home = home / ".local" / "share"
        elif isinstance(xdg_value, str):
            xdg_data_home = _absolute_path(xdg_value, label="XDG_DATA_HOME")
        else:
            raise LocalDataError("XDG_DATA_HOME is invalid")
        data_home = xdg_data_home / "open-brain"
        selection = LocalRootSelection(
            home=home,
            data_home=data_home,
            brain_root=data_home / "brain",
            platform_name=selected_platform,
        )
    _reject_synchronized_path(selection)
    return selection


@contextmanager
def prepare_local_root(
    selection: LocalRootSelection,
    *,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
) -> Iterator[PreparedLocalRoot]:
    """Create and hold the selected private root without following path links."""
    probe = _filesystem_type if filesystem_type_probe is None else filesystem_type_probe
    effective_uid = os.geteuid()
    previous_umask = os.umask(0o077)
    try:
        root_fd, filesystem_type = _open_target(
            selection,
            effective_uid=effective_uid,
            create=True,
            filesystem_type_probe=probe,
        )
    finally:
        os.umask(previous_umask)
    metadata = os.fstat(root_fd)
    prepared = PreparedLocalRoot(
        selection=selection,
        filesystem_type=filesystem_type,
        root_identity=(metadata.st_dev, metadata.st_ino),
        _root_fd=root_fd,
        _effective_uid=effective_uid,
        _filesystem_type_probe=probe,
    )
    try:
        yield prepared
    finally:
        prepared.close()


def _open_target(
    selection: LocalRootSelection,
    *,
    effective_uid: int,
    create: bool,
    filesystem_type_probe: FilesystemTypeProbe,
) -> tuple[int, str]:
    target = selection.brain_root
    if not target.is_absolute() or target == Path("/"):
        raise LocalDataError("private data directory must be absolute")
    managed = frozenset(selection.managed_directories)
    root_fd = -1
    current_path = Path("/")
    user_owned_anchor = effective_uid == 0
    try:
        root_fd = os.open("/", _DIRECTORY_FLAGS)
        for part in target.parts[1:]:
            next_path = current_path / part
            created = False
            try:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=root_fd)
            except FileNotFoundError:
                if not create or not user_owned_anchor:
                    raise LocalDataError(
                        "private data directory has no owner-controlled anchor"
                    ) from None
                _require_allowed_filesystem(
                    filesystem_type_probe(current_path, selection.platform_name),
                    selection.platform_name,
                )
                try:
                    os.mkdir(part, mode=0o700, dir_fd=root_fd)
                    os.fsync(root_fd)
                    created = True
                except FileExistsError:
                    created = False
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=root_fd)
            except OSError as error:
                raise LocalDataError(
                    "private data directory contains an unsafe component"
                ) from error
            metadata = os.fstat(next_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_fd)
                raise LocalDataError("private data directory contains a non-directory component")
            if created:
                os.fchmod(next_fd, 0o700)
                metadata = os.fstat(next_fd)
            if metadata.st_uid == effective_uid:
                user_owned_anchor = True
            elif user_owned_anchor or metadata.st_uid != 0:
                os.close(next_fd)
                raise LocalDataError("private data directory is not owned by the current user")
            if next_path in managed and (
                metadata.st_uid != effective_uid or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                os.close(next_fd)
                raise LocalDataError("private data directory permissions are unsafe")
            os.close(root_fd)
            root_fd = next_fd
            current_path = next_path
        metadata = os.fstat(root_fd)
        if metadata.st_uid != effective_uid or not user_owned_anchor:
            raise LocalDataError("private data directory is not owned by the current user")
        filesystem_type = _require_allowed_filesystem(
            filesystem_type_probe(target, selection.platform_name), selection.platform_name
        )
        return root_fd, filesystem_type
    except LocalDataError:
        if root_fd >= 0:
            os.close(root_fd)
        raise
    except OSError as error:
        if root_fd >= 0:
            os.close(root_fd)
        raise LocalDataError("private data directory is unavailable") from error


def _open_existing_directory(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise LocalDataError("private data directory contains an unsafe component") from error


def _require_private_owner_directory(directory_fd: int, effective_uid: int) -> None:
    metadata = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != effective_uid
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise LocalDataError("private data directory permissions are unsafe")


def _relative_parts(relative_path: str) -> tuple[str, ...]:
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise LocalDataError("private data file path is unsafe")
    parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise LocalDataError("private data file path is unsafe")
    return parts


def _absolute_environment_path(environment: Mapping[str, object], name: str) -> Path:
    value = environment.get(name)
    if not isinstance(value, str):
        raise LocalDataError(f"{name} is invalid")
    return _absolute_path(value, label=name)


def _absolute_path(value: str, *, label: str) -> Path:
    if not value or "\x00" in value:
        raise LocalDataError(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise LocalDataError(f"{label} must be an absolute normalized path")
    return path


def _reject_synchronized_path(selection: LocalRootSelection) -> None:
    home = selection.home
    roots: tuple[Path, ...]
    if selection.platform_name == "darwin":
        roots = (
            home / "Library" / "Mobile Documents",
            home / "Library" / "CloudStorage",
            home / "Dropbox",
            home / "OneDrive",
            home / "Google Drive",
        )
    else:
        roots = tuple(
            home / name
            for name in ("Dropbox", "OneDrive", "Google Drive", "Nextcloud", "Syncthing", "Sync")
        )
    if any(_is_within(selection.brain_root, root) for root in roots):
        raise LocalDataError("private data directory cannot use a synchronized location")


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _require_allowed_filesystem(filesystem_type: str, platform_name: str) -> str:
    normalized = filesystem_type.strip().casefold()
    if not normalized:
        raise LocalDataError("private data filesystem could not be classified")
    denied = (
        _DARWIN_DENIED_FILESYSTEMS
        if platform_name == "darwin"
        else _LINUX_DENIED_FILESYSTEMS
    )
    if normalized in denied:
        raise LocalDataError("private data directory cannot use a network filesystem")
    return normalized


def _filesystem_type(path: Path, platform_name: str) -> str:
    if platform_name == "darwin":
        return _darwin_filesystem_type(path)
    if platform_name.startswith("linux"):
        return _linux_filesystem_type(path)
    raise LocalDataError("Open Brain supports macOS and Linux")


class _DarwinFsid(ctypes.Structure):
    _fields_ = [("values", ctypes.c_int32 * 2)]


class _DarwinStatfs(ctypes.Structure):
    _fields_ = [
        ("block_size", ctypes.c_uint32),
        ("io_size", ctypes.c_int32),
        ("blocks", ctypes.c_uint64),
        ("blocks_free", ctypes.c_uint64),
        ("blocks_available", ctypes.c_uint64),
        ("files", ctypes.c_uint64),
        ("files_free", ctypes.c_uint64),
        ("fsid", _DarwinFsid),
        ("owner", ctypes.c_uint32),
        ("filesystem_type_number", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("subtype", ctypes.c_uint32),
        ("filesystem_type_name", ctypes.c_char * 16),
        ("mounted_on", ctypes.c_char * 1024),
        ("mounted_from", ctypes.c_char * 1024),
        ("extended_flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    ]


def _darwin_filesystem_type(path: Path) -> str:
    buffer = _DarwinStatfs()
    try:
        library = ctypes.CDLL(None, use_errno=True)
        statfs = library.statfs
        statfs.argtypes = (ctypes.c_char_p, ctypes.POINTER(_DarwinStatfs))
        statfs.restype = ctypes.c_int
        result = statfs(os.fsencode(path), ctypes.byref(buffer))
    except (AttributeError, OSError, TypeError) as error:
        raise LocalDataError("private data filesystem could not be classified") from error
    if result != 0:
        raise LocalDataError("private data filesystem could not be classified")
    return bytes(buffer.filesystem_type_name).split(b"\0", 1)[0].decode("ascii", "strict")


def _linux_filesystem_type(path: Path) -> str:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise LocalDataError("private data filesystem could not be classified") from error
    selected: tuple[int, str] | None = None
    for line in lines:
        left, marker, right = line.partition(" - ")
        left_fields = left.split()
        right_fields = right.split()
        if not marker or len(left_fields) < 5 or not right_fields:
            continue
        mount_point = Path(_unescape_mount_field(left_fields[4]))
        if _is_within(path, mount_point):
            candidate = (len(mount_point.parts), right_fields[0])
            if selected is None or candidate[0] > selected[0]:
                selected = candidate
    if selected is None:
        raise LocalDataError("private data filesystem could not be classified")
    return selected[1]


def _unescape_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


__all__ = [
    "FilesystemTypeProbe",
    "LocalDataError",
    "LocalRootSelection",
    "PreparedLocalRoot",
    "prepare_local_root",
    "select_local_root",
]
