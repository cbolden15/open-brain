from __future__ import annotations

import base64
import errno
import fcntl
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import CaptureId, validate_identifier
from open_brain_engine.core.models import CaptureEnvelope, RawAssetBlob, RawAssetRef, RawCapture
from open_brain_engine.core.ports import PutDisposition, PutResult


class StorageError(Exception):
    """A persistence operation failed without exposing persisted content."""


class RootConfinementError(StorageError):
    """A path did not remain within the approved root capability."""


class DuplicateConflictError(StorageError):
    """An immutable identifier already exists with different bytes."""


class DurabilityError(StorageError):
    """A durable filesystem operation did not complete."""


class StorageUnsupportedPlatformError(StorageError):
    """Required POSIX storage primitives are unavailable."""


class WriteState(StrEnum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"


@dataclass(frozen=True, slots=True)
class StoredBlob:
    record_id: str
    relative_path: PurePosixPath
    digest_sha256: str
    state: WriteState


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_FILE_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
RootIdentity = tuple[int, int]


def _require_platform_support() -> None:
    required = (
        hasattr(os, "O_DIRECTORY"),
        hasattr(os, "O_NOFOLLOW"),
        os.open in os.supports_dir_fd,
        os.mkdir in os.supports_dir_fd,
        os.rename in os.supports_dir_fd,
        os.unlink in os.supports_dir_fd,
        hasattr(fcntl, "flock"),
    )
    if os.name != "posix" or not all(required):
        raise StorageUnsupportedPlatformError("storage platform unsupported")


def _validated_parts(relative: str | PurePosixPath) -> tuple[str, ...]:
    raw = str(relative)
    if (
        not raw
        or raw.startswith("/")
        or "\\" in raw
        or "\x00" in raw
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise RootConfinementError("unsafe relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts:
        raise RootConfinementError("unsafe relative path")
    return path.parts


def _open_root(root: Path, expected_identity: RootIdentity | None = None) -> int:
    _require_platform_support()
    if not root.is_absolute():
        raise RootConfinementError("unsafe storage root")
    root_fd = -1
    try:
        parts = root.parts
        root_fd = os.open(parts[0], _DIRECTORY_FLAGS)
        for part in parts[1:]:
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=root_fd)
            previous_fd = root_fd
            root_fd = child_fd
            os.close(previous_fd)
        opened = os.fstat(root_fd)
        observed = (opened.st_dev, opened.st_ino)
        if expected_identity is not None and observed != expected_identity:
            raise RootConfinementError("storage root identity changed")
        return root_fd
    except RootConfinementError:
        if root_fd >= 0:
            os.close(root_fd)
        raise
    except OSError:
        if root_fd >= 0:
            os.close(root_fd)
        raise RootConfinementError("unsafe storage root") from None


def capture_root_identity(root: Path) -> RootIdentity:
    """Capture one no-follow directory identity for later root-bound operations."""
    root_fd = _open_root(root)
    try:
        metadata = os.fstat(root_fd)
        return (metadata.st_dev, metadata.st_ino)
    finally:
        os.close(root_fd)


def assert_root_identity(root: Path, expected_identity: RootIdentity) -> None:
    """Fail closed unless ``root`` still names the captured directory."""
    root_fd = _open_root(root, expected_identity)
    os.close(root_fd)


def open_root_descriptor(
    root: Path, expected_identity: RootIdentity | None = None
) -> int:
    """Open a no-follow root descriptor, optionally bound to a captured identity."""
    return _open_root(root, expected_identity)


def _open_child_directory(parent_fd: int, name: str, *, create: bool) -> int:
    try:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
            os.close(child_fd)
            raise RootConfinementError("unsafe storage path")
        return child_fd
    except RootConfinementError:
        raise
    except FileNotFoundError:
        if not create:
            raise
        raise DurabilityError("storage directory operation failed") from None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise RootConfinementError("unsafe storage path") from None
        raise DurabilityError("storage directory operation failed") from None


def _open_parent(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = _open_child_directory(current_fd, part, create=create)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _read_fd(file_fd: int, *, maximum_bytes: int | None = None) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(file_fd, 64 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if maximum_bytes is not None and total > maximum_bytes:
            raise StorageError("stored content exceeds read limit")
        chunks.append(chunk)


def _existing_bytes(
    parent_fd: int, name: str, *, maximum_bytes: int | None = None
) -> bytes | None:
    try:
        file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise RootConfinementError("unsafe storage target") from None
        raise DurabilityError("storage read failed") from None
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise RootConfinementError("unsafe storage target")
        return _read_fd(file_fd, maximum_bytes=maximum_bytes)
    except OSError:
        raise DurabilityError("storage read failed") from None
    finally:
        os.close(file_fd)


def _write_all(file_fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(file_fd, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        remaining = remaining[written:]


def atomic_write_new(
    *,
    root: Path,
    relative: str | PurePosixPath,
    data: bytes,
    expected_root_identity: RootIdentity | None = None,
) -> WriteState:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    parts = _validated_parts(relative)
    root_fd = _open_root(root, expected_root_identity)
    lock_fd = -1
    parent_fd = -1
    temp_name: str | None = None
    try:
        try:
            lock_fd = os.open(".write.lock", _FILE_CREATE_FLAGS, 0o600, dir_fd=root_fd)
        except FileExistsError:
            try:
                lock_fd = os.open(".write.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=root_fd)
            except OSError:
                raise RootConfinementError("unsafe storage lock") from None
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        parent_fd = _open_parent(root_fd, parts[:-1], create=True)
        existing = _existing_bytes(parent_fd, parts[-1])
        if existing is not None:
            if existing == data:
                return WriteState.ALREADY_EXISTS
            raise DuplicateConflictError("immutable record conflict")

        temp_name = "." + secrets.token_hex(16) + ".tmp"
        file_fd = os.open(temp_name, _FILE_CREATE_FLAGS, 0o600, dir_fd=parent_fd)
        try:
            _write_all(file_fd, data)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        os.replace(temp_name, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temp_name = None
        os.fsync(parent_fd)
        return WriteState.CREATED
    except (StorageError, TypeError):
        raise
    except OSError:
        raise DurabilityError("durable storage write failed") from None
    finally:
        if temp_name is not None and parent_fd >= 0:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                pass
        if parent_fd >= 0:
            os.close(parent_fd)
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(root_fd)


def atomic_replace(
    *,
    root: Path,
    relative: str | PurePosixPath,
    data: bytes,
    require_existing: bool | None = None,
    expected_existing_sha256: str | None = None,
    expected_root_identity: RootIdentity | None = None,
) -> None:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if expected_existing_sha256 is not None and (
        not isinstance(expected_existing_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_existing_sha256) is None
        or require_existing is not True
    ):
        raise ValueError("invalid expected replace digest")
    parts = _validated_parts(relative)
    root_fd = _open_root(root, expected_root_identity)
    lock_fd = -1
    parent_fd = -1
    temp_name: str | None = None
    try:
        try:
            lock_fd = os.open(".write.lock", _FILE_CREATE_FLAGS, 0o600, dir_fd=root_fd)
        except FileExistsError:
            try:
                lock_fd = os.open(".write.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=root_fd)
            except OSError:
                raise RootConfinementError("unsafe storage lock") from None
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        parent_fd = _open_parent(root_fd, parts[:-1], create=True)
        existing = _existing_bytes(parent_fd, parts[-1])
        if require_existing is True and existing is None:
            raise StorageError("replace target missing")
        if require_existing is False and existing is not None:
            raise DuplicateConflictError("replace target already exists")
        if expected_existing_sha256 is not None and (
            existing is None or sha256(existing).hexdigest() != expected_existing_sha256
        ):
            raise DuplicateConflictError("replace target changed")

        temp_name = "." + secrets.token_hex(16) + ".tmp"
        file_fd = os.open(temp_name, _FILE_CREATE_FLAGS, 0o600, dir_fd=parent_fd)
        try:
            _write_all(file_fd, data)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        os.replace(temp_name, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temp_name = None
        os.fsync(parent_fd)
    except (StorageError, TypeError):
        raise
    except OSError:
        raise DurabilityError("durable storage write failed") from None
    finally:
        if temp_name is not None and parent_fd >= 0:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                pass
        if parent_fd >= 0:
            os.close(parent_fd)
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(root_fd)


def read_confined(
    *,
    root: Path,
    relative: str | PurePosixPath,
    expected_root_identity: RootIdentity | None = None,
    maximum_bytes: int | None = None,
) -> bytes | None:
    if maximum_bytes is not None and (
        not isinstance(maximum_bytes, int)
        or isinstance(maximum_bytes, bool)
        or maximum_bytes <= 0
    ):
        raise ValueError("invalid read limit")
    parts = _validated_parts(relative)
    root_fd = _open_root(root, expected_root_identity)
    parent_fd = -1
    try:
        try:
            parent_fd = _open_parent(root_fd, parts[:-1], create=False)
        except FileNotFoundError:
            return None
        return _existing_bytes(parent_fd, parts[-1], maximum_bytes=maximum_bytes)
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)


def read_confined_tree(
    *,
    root: Path,
    relative: str | PurePosixPath,
    expected_root_identity: RootIdentity | None = None,
    maximum_entries: int,
    maximum_file_bytes: int,
    maximum_total_bytes: int,
) -> tuple[tuple[PurePosixPath, bytes], ...]:
    """Read one bounded regular-file tree through pinned no-follow descriptors."""

    for value in (maximum_entries, maximum_file_bytes, maximum_total_bytes):
        if type(value) is not int or value <= 0:
            raise ValueError("invalid confined tree limit")
    parts = _validated_parts(relative)
    root_fd = _open_root(root, expected_root_identity)
    directory_fd = -1
    counters = [0, 0]
    files: list[tuple[PurePosixPath, bytes]] = []
    try:
        try:
            directory_fd = _open_parent(root_fd, parts, create=False)
        except FileNotFoundError:
            return ()
        _read_confined_tree_descriptor(
            directory_fd,
            (),
            files,
            counters,
            maximum_entries=maximum_entries,
            maximum_file_bytes=maximum_file_bytes,
            maximum_total_bytes=maximum_total_bytes,
        )
        return tuple(sorted(files, key=lambda item: item[0].as_posix()))
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(root_fd)


def _read_confined_tree_descriptor(
    directory_fd: int,
    parts: tuple[str, ...],
    files: list[tuple[PurePosixPath, bytes]],
    counters: list[int],
    *,
    maximum_entries: int,
    maximum_file_bytes: int,
    maximum_total_bytes: int,
) -> None:
    try:
        entries = os.scandir(directory_fd)
    except OSError:
        raise DurabilityError("confined tree scan failed") from None
    with entries:
        for entry in entries:
            counters[0] += 1
            if counters[0] > maximum_entries:
                raise StorageError("stored tree exceeds bounded entry limit")
            try:
                observed = entry.stat(follow_symlinks=False)
            except OSError:
                raise DurabilityError("confined tree scan failed") from None
            if stat.S_ISDIR(observed.st_mode):
                try:
                    child_fd = _open_child_directory(directory_fd, entry.name, create=False)
                except FileNotFoundError:
                    raise RootConfinementError("confined tree identity changed") from None
                try:
                    current = os.fstat(child_fd)
                    if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
                        raise RootConfinementError("confined tree identity changed")
                    _read_confined_tree_descriptor(
                        child_fd,
                        (*parts, entry.name),
                        files,
                        counters,
                        maximum_entries=maximum_entries,
                        maximum_file_bytes=maximum_file_bytes,
                        maximum_total_bytes=maximum_total_bytes,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise RootConfinementError("confined tree contains an unsafe entry")
            if observed.st_size > maximum_file_bytes:
                raise StorageError("stored tree file exceeds bounded size")
            try:
                file_fd = os.open(entry.name, _FILE_READ_FLAGS, dir_fd=directory_fd)
            except OSError:
                raise RootConfinementError("confined tree file is unsafe") from None
            try:
                before = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (observed.st_dev, observed.st_ino)
                ):
                    raise RootConfinementError("confined tree identity changed")
                payload = _read_fd(file_fd, maximum_bytes=maximum_file_bytes)
                after = os.fstat(file_fd)
                if (
                    (
                        after.st_dev,
                        after.st_ino,
                        after.st_nlink,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    )
                    != (
                        before.st_dev,
                        before.st_ino,
                        before.st_nlink,
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    )
                    or len(payload) != before.st_size
                ):
                    raise RootConfinementError("confined tree file changed during read")
            finally:
                os.close(file_fd)
            counters[1] += len(payload)
            if counters[1] > maximum_total_bytes:
                raise StorageError("stored tree exceeds bounded total size")
            files.append((PurePosixPath(*parts, entry.name), payload))


def confined_unlink(
    *,
    root: Path,
    relative: str | PurePosixPath,
    expected_root_identity: RootIdentity | None = None,
    require_existing: bool = False,
) -> bool:
    if type(require_existing) is not bool:
        raise ValueError("invalid unlink requirement")
    parts = _validated_parts(relative)
    root_fd = _open_root(root, expected_root_identity)
    lock_fd = -1
    parent_fd = -1
    try:
        try:
            lock_fd = os.open(".write.lock", _FILE_CREATE_FLAGS, 0o600, dir_fd=root_fd)
        except FileExistsError:
            try:
                lock_fd = os.open(".write.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=root_fd)
            except OSError:
                raise RootConfinementError("unsafe storage lock") from None
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            parent_fd = _open_parent(root_fd, parts[:-1], create=False)
        except FileNotFoundError:
            if require_existing:
                raise StorageError("unlink target missing") from None
            return False
        try:
            file_fd = os.open(parts[-1], _FILE_READ_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            if require_existing:
                raise StorageError("unlink target missing") from None
            return False
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise RootConfinementError("unsafe storage target") from None
            raise DurabilityError("storage unlink failed") from None
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise RootConfinementError("unsafe storage target")
            current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise RootConfinementError("storage target replaced")
            os.unlink(parts[-1], dir_fd=parent_fd)
        finally:
            os.close(file_fd)
        os.fsync(parent_fd)
        return True
    except StorageError:
        raise
    except OSError:
        raise DurabilityError("storage unlink failed") from None
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(root_fd)


def resolve_generated_path(
    root: Path,
    relative: str | PurePosixPath,
    *,
    expected_root_identity: RootIdentity | None = None,
) -> Path:
    parts = _validated_parts(relative)
    root_fd = _open_root(root, expected_root_identity)
    try:
        current_fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                next_fd = _open_child_directory(current_fd, part, create=False)
                os.close(current_fd)
                current_fd = next_fd
            try:
                final_fd = os.open(parts[-1], _FILE_READ_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise RootConfinementError("unsafe storage target") from None
                raise DurabilityError("storage target operation failed") from None
            else:
                try:
                    if not stat.S_ISREG(os.fstat(final_fd).st_mode):
                        raise RootConfinementError("unsafe storage target")
                finally:
                    os.close(final_fd)
        finally:
            os.close(current_fd)
    finally:
        os.close(root_fd)
    return root.joinpath(*parts)


def raw_relative_path(capture_id: CaptureId | str) -> PurePosixPath:
    try:
        value = validate_identifier(str(capture_id), prefix="cap_")
    except ValueError:
        raise RootConfinementError("invalid capture identifier") from None
    return PurePosixPath("raw", value[4:6], value + ".json")


def _raw_capture_bytes(capture: RawCapture) -> bytes:
    value = {
        "assets": [
            {
                "data_base64": base64.b64encode(asset.data).decode("ascii"),
                "ref": asset.ref.to_dict(),
            }
            for asset in capture.assets
        ],
        "envelope": capture.envelope.to_dict(),
    }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _raw_capture_from_bytes(payload: bytes) -> RawCapture:
    try:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"assets", "envelope"}:
            raise ValueError
        envelope_value = value["envelope"]
        assets_value = value["assets"]
        if not isinstance(envelope_value, dict) or not isinstance(assets_value, list):
            raise ValueError
        envelope = CaptureEnvelope.from_dict(envelope_value)
        assets: list[RawAssetBlob] = []
        for item in assets_value:
            if not isinstance(item, dict) or set(item) != {"data_base64", "ref"}:
                raise ValueError
            ref_value = item["ref"]
            encoded = item["data_base64"]
            if not isinstance(ref_value, dict) or not isinstance(encoded, str):
                raise ValueError
            ref = RawAssetRef.from_dict(ref_value)
            data = base64.b64decode(encoded, validate=True)
            assets.append(RawAssetBlob.create(ref=ref, data=data))
        result = RawCapture.create(envelope=envelope, assets=tuple(assets))
        if _raw_capture_bytes(result) != payload:
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise StorageError("invalid stored raw capture") from None


class AtomicFilesystemRawStore:
    def __init__(self, *, root: Path) -> None:
        root_fd = _open_root(root)
        os.close(root_fd)
        self._root = root

    def put_if_absent(self, capture: RawCapture) -> PutResult:
        if not isinstance(capture, RawCapture):
            raise StorageError("invalid raw capture")
        canonical_capture = _raw_capture_from_bytes(_raw_capture_bytes(capture))
        payload = _raw_capture_bytes(canonical_capture)
        capture_id = canonical_capture.envelope.capture_id
        state = atomic_write_new(
            root=self._root,
            relative=raw_relative_path(capture_id),
            data=payload,
        )
        return PutResult(
            disposition=(
                PutDisposition.CREATED if state is WriteState.CREATED else PutDisposition.DUPLICATE
            ),
            record_id=str(capture_id),
            digest_sha256=sha256(payload).hexdigest(),
        )

    def get(self, capture_id: CaptureId) -> RawCapture | None:
        payload = read_confined(root=self._root, relative=raw_relative_path(capture_id))
        if payload is None:
            return None
        capture = _raw_capture_from_bytes(payload)
        if capture.envelope.capture_id != capture_id:
            raise StorageError("stored raw capture identity mismatch")
        return capture
