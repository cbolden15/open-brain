from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import re
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.locks import LockScope

from .filesystem import (
    DurabilityError,
    RootConfinementError,
    RootIdentity,
    StorageError,
    StorageUnsupportedPlatformError,
    _open_child_directory,
    _open_root,
    _write_all,
)


class LeaseError(StorageError):
    """A filesystem lease operation failed without exposing host details."""


class LeaseFormatError(LeaseError):
    """Lease metadata is malformed or violates the closed lease inventory."""


class LockBusyError(LeaseError):
    """The requested kernel-authoritative lease is already held."""


_IDENTITY = re.compile(r"[a-z][a-z0-9-]{0,63}")
_DISCRIMINATORS = {
    LockScope.SHARED_WRITER: frozenset({"shared-writer"}),
    LockScope.INDEX: frozenset({"index"}),
    LockScope.PORTABILITY_PROMOTION: frozenset({"portability-promotion"}),
}
_DESCRIPTOR_FIELDS = frozenset(
    {"version", "scope", "discriminator", "owner_identity_id", "pid", "acquired_at"}
)
_MAX_DESCRIPTOR_BYTES = 1024
_LOCK_DIRECTORY = ".open-brain-locks"
_LOCK_FILE_NAMES = frozenset(
    {
        "lease.daemon-authority",
        "lease.appliance-lifecycle",
        "lease.shared-writer",
        "lease.index",
        "lease.portability-promotion",
    }
)
_PROCESS_HELD_LOCKS: set[tuple[int, int, str]] = set()
_PROCESS_HELD_LOCKS_GUARD = threading.Lock()
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class LeaseDescriptor:
    """Best-effort observational metadata for a kernel-authoritative lease."""

    version: Literal[1]
    scope: LockScope
    discriminator: str
    owner_identity_id: str
    pid: int
    acquired_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.acquired_at, datetime)
            or self.acquired_at.tzinfo is None
            or self.acquired_at.utcoffset() is None
        ):
            raise LeaseFormatError("invalid lease descriptor")
        object.__setattr__(self, "acquired_at", self.acquired_at.astimezone(UTC))
        self._validate()

    def _validate(self) -> None:
        if (
            type(self.version) is not int
            or self.version != 1
            or not isinstance(self.scope, LockScope)
            or self.scope not in _DISCRIMINATORS
            or not isinstance(self.discriminator, str)
            or self.discriminator not in _DISCRIMINATORS[self.scope]
            or not isinstance(self.owner_identity_id, str)
            or _IDENTITY.fullmatch(self.owner_identity_id) is None
            or type(self.pid) is not int
            or self.pid < 1
            or not isinstance(self.acquired_at, datetime)
            or self.acquired_at.tzinfo is not UTC
        ):
            raise LeaseFormatError("invalid lease descriptor")

    def to_dict(self) -> dict[str, object]:
        self._validate()
        return {
            "version": self.version,
            "scope": self.scope.value,
            "discriminator": self.discriminator,
            "owner_identity_id": self.owner_identity_id,
            "pid": self.pid,
            "acquired_at": _timestamp(self.acquired_at),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> LeaseDescriptor:
        if type(payload) is not bytes or not payload or len(payload) > _MAX_DESCRIPTOR_BYTES:
            raise LeaseFormatError("invalid lease descriptor")
        try:
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
            if type(value) is not dict or frozenset(value) != _DESCRIPTOR_FIELDS:
                raise LeaseFormatError("invalid lease descriptor")
            timestamp = value["acquired_at"]
            if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
                raise LeaseFormatError("invalid lease descriptor")
            acquired_at = datetime.fromisoformat(timestamp[:-1] + "+00:00")
            descriptor = cls(
                version=1 if value["version"] == 1 else value["version"],
                scope=LockScope(value["scope"]),
                discriminator=value["discriminator"],
                owner_identity_id=value["owner_identity_id"],
                pid=value["pid"],
                acquired_at=acquired_at,
            )
        except LeaseFormatError:
            raise
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise LeaseFormatError("invalid lease descriptor") from None
        if descriptor.to_bytes() != payload:
            raise LeaseFormatError("invalid lease descriptor")
        return descriptor


@dataclass(frozen=True, slots=True)
class HeldLeaseSnapshot:
    scope: LockScope
    discriminator: str
    acquired_at: datetime | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, LockScope)
            or self.scope not in _DISCRIMINATORS
            or not isinstance(self.discriminator, str)
            or self.discriminator not in _DISCRIMINATORS[self.scope]
        ):
            raise LeaseFormatError("invalid held lease snapshot")
        if self.acquired_at is not None:
            if (
                not isinstance(self.acquired_at, datetime)
                or self.acquired_at.tzinfo is None
                or self.acquired_at.utcoffset() is None
            ):
                raise LeaseFormatError("invalid held lease snapshot")
            object.__setattr__(self, "acquired_at", self.acquired_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class LockStateSnapshot:
    held_count: int
    malformed_count: int
    oldest_acquired_at: datetime | None
    held_leases: tuple[HeldLeaseSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.held_count) is not int
            or self.held_count < 0
            or type(self.malformed_count) is not int
            or self.malformed_count < 0
            or not isinstance(self.held_leases, tuple)
            or any(not isinstance(lease, HeldLeaseSnapshot) for lease in self.held_leases)
            or len(self.held_leases) != self.held_count
        ):
            raise LeaseFormatError("invalid lock state snapshot")
        if self.oldest_acquired_at is not None:
            if (
                not isinstance(self.oldest_acquired_at, datetime)
                or self.oldest_acquired_at.tzinfo is None
                or self.oldest_acquired_at.utcoffset() is None
                or self.held_count == 0
            ):
                raise LeaseFormatError("invalid lock state snapshot")
            object.__setattr__(
                self,
                "oldest_acquired_at",
                self.oldest_acquired_at.astimezone(UTC),
            )


class FileLease:
    """Root-confined POSIX record lock with observational descriptor metadata."""

    def __init__(
        self,
        state_root: Path,
        owner_identity_id: str,
        *,
        clock: Clock | None = None,
        validate_acquire: Callable[[], None] | None = None,
        parent_root_identity: RootIdentity | None = None,
        root_identity: RootIdentity | None = None,
        required_root_mode: int | None = None,
    ) -> None:
        if not isinstance(state_root, Path) or not state_root.is_absolute():
            raise RootConfinementError("unsafe lease root")
        if (
            not isinstance(owner_identity_id, str)
            or _IDENTITY.fullmatch(owner_identity_id) is None
            or clock is not None
            and not callable(clock)
            or validate_acquire is not None
            and not callable(validate_acquire)
            or parent_root_identity is not None
            and root_identity is not None
            or required_root_mode is not None
            and (type(required_root_mode) is not int or required_root_mode != 0o700)
        ):
            raise LeaseFormatError("invalid lease configuration")
        self._state_root = state_root
        self._owner_identity_id = owner_identity_id
        self._clock = _system_clock if clock is None else clock
        self._validate_acquire = validate_acquire
        self._parent_root_identity = parent_root_identity
        self._root_identity = root_identity
        self._required_root_mode = required_root_mode

    @contextmanager
    def acquire_shared_writer(self) -> Iterator[None]:
        """Acquire the one canonical-writer lease without exporting its lock enum."""
        with self.acquire(LockScope.SHARED_WRITER):
            yield

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        discriminator = self._discriminator(scope)
        if self._validate_acquire is not None:
            self._validate_acquire()
        _require_record_lock_support()
        if self._root_identity is not None:
            root_fd = _open_root(self._state_root, self._root_identity)
        elif self._parent_root_identity is None:
            root_fd = _open_root(self._state_root)
        else:
            parent_fd = _open_root(self._state_root.parent, self._parent_root_identity)
            try:
                root_fd = _open_child_directory(
                    parent_fd,
                    self._state_root.name,
                    create=False,
                )
            finally:
                os.close(parent_fd)
        lock_directory_fd = -1
        lock_fd = -1
        held_key: tuple[int, int, str] | None = None
        kernel_lock_acquired = False
        process_lock_registered = False
        try:
            root_metadata = os.fstat(root_fd)
            if (
                self._required_root_mode is not None
                and stat.S_IMODE(root_metadata.st_mode) != self._required_root_mode
            ):
                raise RootConfinementError("unsafe lease root mode")
            held_key = (root_metadata.st_dev, root_metadata.st_ino, discriminator)
            with _PROCESS_HELD_LOCKS_GUARD:
                if held_key in _PROCESS_HELD_LOCKS:
                    raise LockBusyError("lease already held by this process")
                lock_directory_fd = _open_child_directory(
                    root_fd,
                    _LOCK_DIRECTORY,
                    create=True,
                )
                _validate_lock_directory(lock_directory_fd)
                lock_fd, created = _open_lock_file(lock_directory_fd, discriminator)
                try:
                    fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in {errno.EACCES, errno.EAGAIN}:
                        raise LockBusyError("lease held by another process") from None
                    raise
                kernel_lock_acquired = True
                _PROCESS_HELD_LOCKS.add(held_key)
                process_lock_registered = True

            descriptor = LeaseDescriptor(
                version=1,
                scope=scope,
                discriminator=discriminator,
                owner_identity_id=self._owner_identity_id,
                pid=os.getpid(),
                acquired_at=self._clock(),
            )
            _replace_descriptor(lock_fd, lock_directory_fd, descriptor.to_bytes())
            if created:
                os.fsync(lock_directory_fd)
            if self._validate_acquire is not None:
                self._validate_acquire()
            yield
        except (LeaseError, StorageError):
            raise
        except OSError:
            raise DurabilityError("lease operation failed") from None
        finally:
            if kernel_lock_acquired and lock_fd >= 0:
                with suppress(OSError):
                    fcntl.lockf(lock_fd, fcntl.LOCK_UN)
            if process_lock_registered and held_key is not None:
                with _PROCESS_HELD_LOCKS_GUARD:
                    _PROCESS_HELD_LOCKS.discard(held_key)
            if lock_fd >= 0:
                os.close(lock_fd)
            if lock_directory_fd >= 0:
                os.close(lock_directory_fd)
            os.close(root_fd)

    def _discriminator(self, scope: LockScope) -> str:
        if not isinstance(scope, LockScope) or scope is LockScope.NONE:
            raise LeaseFormatError("invalid lease scope")
        return scope.value


def inspect_file_leases(state_root: Path) -> LockStateSnapshot:
    """Inspect record locks without acquiring, releasing, or creating a lease."""
    _require_record_lock_support()
    if not isinstance(state_root, Path) or not state_root.is_absolute():
        raise RootConfinementError("unsafe lease root")
    lock_directory = state_root / _LOCK_DIRECTORY
    try:
        directory_metadata = os.lstat(lock_directory)
    except FileNotFoundError:
        return LockStateSnapshot(0, 0, None)
    except OSError:
        raise DurabilityError("lease inspection failed") from None
    if (
        stat.S_ISLNK(directory_metadata.st_mode)
        or not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_IMODE(directory_metadata.st_mode) != 0o700
    ):
        return LockStateSnapshot(0, 1, None)

    try:
        names = frozenset(os.listdir(lock_directory))
    except OSError:
        raise DurabilityError("lease inspection failed") from None
    malformed_count = len(
        {name for name in names if name.startswith("lease.")} - _LOCK_FILE_NAMES
    )
    held_count = 0
    acquired_at: list[datetime] = []
    held_leases: list[HeldLeaseSnapshot] = []
    for name in sorted(names & _LOCK_FILE_NAMES):
        file_fd = -1
        try:
            file_fd = os.open(
                lock_directory / name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(file_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                malformed_count += 1
                continue
            held = _record_lock_is_held(file_fd)
            descriptor = _read_descriptor(file_fd, name)
            if held:
                held_count += 1
                scope, discriminator = _scope_for_file_name(name)
                if descriptor is not None:
                    acquired_at.append(descriptor.acquired_at)
                held_leases.append(
                    HeldLeaseSnapshot(
                        scope=scope,
                        discriminator=discriminator,
                        acquired_at=None if descriptor is None else descriptor.acquired_at,
                    )
                )
            elif descriptor is None:
                malformed_count += 1
        except OSError:
            malformed_count += 1
        finally:
            if file_fd >= 0:
                os.close(file_fd)
    return LockStateSnapshot(
        held_count=held_count,
        malformed_count=malformed_count,
        oldest_acquired_at=min(acquired_at) if acquired_at else None,
        held_leases=tuple(held_leases),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise LeaseFormatError("invalid lease descriptor")
        value[key] = item
    return value


def _timestamp(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _require_record_lock_support() -> None:
    if (
        os.name != "posix"
        or not hasattr(fcntl, "lockf")
        or not hasattr(fcntl, "F_GETLK")
        or not hasattr(fcntl, "LOCK_EX")
        or not hasattr(fcntl, "LOCK_NB")
    ):
        raise StorageUnsupportedPlatformError("record locks unsupported")


def _validate_lock_directory(directory_fd: int) -> None:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RootConfinementError("unsafe lease directory")


def _open_lock_file(directory_fd: int, discriminator: str) -> tuple[int, bool]:
    name = f"lease.{discriminator}"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        created = True
    except FileExistsError:
        try:
            file_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise RootConfinementError("unsafe lease file") from None
            raise
        created = False
    metadata = os.fstat(file_fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        os.close(file_fd)
        raise RootConfinementError("unsafe lease file")
    return file_fd, created


def _replace_descriptor(file_fd: int, directory_fd: int, payload: bytes) -> None:
    try:
        os.ftruncate(file_fd, 0)
        os.lseek(file_fd, 0, os.SEEK_SET)
        _write_all(file_fd, payload)
        os.fsync(file_fd)
        os.fsync(directory_fd)
    except OSError:
        raise DurabilityError("lease descriptor write failed") from None


class _DarwinFlock(ctypes.Structure):
    _fields_ = (
        ("l_start", ctypes.c_longlong),
        ("l_len", ctypes.c_longlong),
        ("l_pid", ctypes.c_int),
        ("l_type", ctypes.c_short),
        ("l_whence", ctypes.c_short),
    )


class _LinuxFlock(ctypes.Structure):
    _fields_ = (
        ("l_type", ctypes.c_short),
        ("l_whence", ctypes.c_short),
        ("l_start", ctypes.c_longlong),
        ("l_len", ctypes.c_longlong),
        ("l_pid", ctypes.c_int),
    )


def _record_lock_is_held(file_fd: int) -> bool:
    flock_type: type[_DarwinFlock] | type[_LinuxFlock]
    if sys.platform == "darwin":
        flock_type = _DarwinFlock
    elif sys.platform.startswith("linux"):
        flock_type = _LinuxFlock
    else:
        raise StorageUnsupportedPlatformError("record lock inspection unsupported")
    query = flock_type()
    query.l_type = fcntl.F_WRLCK
    query.l_whence = os.SEEK_SET
    query.l_start = 0
    query.l_len = 0
    result = fcntl.fcntl(file_fd, fcntl.F_GETLK, bytes(query))
    if not isinstance(result, bytes) or len(result) != ctypes.sizeof(flock_type):
        raise StorageUnsupportedPlatformError("record lock inspection unsupported")
    observed = flock_type.from_buffer_copy(result)
    return int(observed.l_type) != int(fcntl.F_UNLCK)


def _read_descriptor(file_fd: int, file_name: str) -> LeaseDescriptor | None:
    try:
        os.lseek(file_fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(item) for item in chunks) > _MAX_DESCRIPTOR_BYTES:
                return None
        descriptor = LeaseDescriptor.from_bytes(b"".join(chunks))
        if file_name != f"lease.{descriptor.discriminator}":
            return None
        return descriptor
    except (OSError, LeaseFormatError):
        return None


def _scope_for_file_name(file_name: str) -> tuple[LockScope, str]:
    discriminator = file_name.removeprefix("lease.")
    return LockScope(discriminator), discriminator
