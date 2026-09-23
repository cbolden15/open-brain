"""Crash-aware lifetime registry for concurrent foreground Open Brain clients."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.engine.contracts import LocalEngineContext
from open_brain_engine.engine.runtime_admission import hold_runtime_registry
from open_brain_engine.storage.filesystem import (
    DurabilityError,
    RootConfinementError,
    RootIdentity,
    _open_child_directory,
    _open_root,
    _write_all,
)

_RUNTIME_DIRECTORY = "runtime-sessions"
_REGISTRY_LOCK = "registry.lock"
_REGISTRY_VERSION = "registry-version"
_REGISTRY_PENDING_VERSION = "registry-version.pending"
_REGISTRY_VERSION_BYTES = b"open-brain-runtime-sessions-v2\n"
_LEGACY_REGISTRY_VERSION_BYTES = b"open-brain-runtime-sessions-v1\n"
_SESSION_FILE = re.compile(r"^session-[0-9a-f]{32}\.lock$")
_REGISTRY_LOCK_TIMEOUT_SECONDS = 2.0
RUNTIME_SESSION_VERSION = 5


class LocalRuntimeSessionError(RuntimeError):
    """The foreground runtime-session registry could not fail closed."""


class LocalRuntimeCompatibilityError(LocalRuntimeSessionError):
    """A schema migration cannot proceed while another runtime is admitted."""


@dataclass(frozen=True, slots=True)
class LocalRuntimeSession:
    """Startup facts established while one session marker is held."""

    crash_recovery_required: bool
    live_peer_count: int
    stale_session_count: int


@contextmanager
def hold_local_runtime_session(
    root: Path,
    root_identity: RootIdentity,
    *,
    legacy_state_exists: bool,
    recover_abandoned_sessions: Callable[[], object],
    admit_session: Callable[[LocalRuntimeSession], object] | None = None,
    admission_profile: LocalEngineContext | None = None,
) -> Iterator[LocalRuntimeSession]:
    """Register one client and distinguish live peers from abandoned sessions."""
    root_fd = state_fd = directory_fd = registry_fd = session_fd = -1
    session_name = f"session-{uuid.uuid4().hex}.lock"
    session_created = False
    body_entered = False
    registry_locked = False
    admission_stack = ExitStack()
    try:
        root_fd = _open_root(root, root_identity)
        state_fd = _open_child_directory(root_fd, ".open-brain", create=False)
        directory_fd = _open_child_directory(state_fd, _RUNTIME_DIRECTORY, create=True)
        _require_private_directory(directory_fd)
        registry_fd, _ = _open_private_file(directory_fd, _REGISTRY_LOCK, create=True)
        if admission_profile is None:
            _acquire_registry_lock(registry_fd)
        else:
            admission_stack.enter_context(
                hold_runtime_registry(admission_profile, directory_fd, registry_fd)
            )
        registry_locked = True
        registry_initialized = _registry_initialized(directory_fd)
        session_fd, _ = _open_private_file(directory_fd, session_name, create=True)
        fcntl.flock(session_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _replace_file(
            session_fd,
            canonical_json_bytes(
                {
                    "pid": os.getpid(),
                    "session_id": session_name.removeprefix("session-").removesuffix(".lock"),
                    "version": RUNTIME_SESSION_VERSION,
                }
            ),
        )
        session_created = True
        os.fsync(directory_fd)
        live_sessions, stale_sessions = _scan_sessions(directory_fd)
        live_peers = live_sessions - 1
        crash_recovery_required = bool(stale_sessions) or (
            legacy_state_exists and not registry_initialized
        )
        runtime_session = LocalRuntimeSession(
            crash_recovery_required=crash_recovery_required,
            live_peer_count=live_peers,
            stale_session_count=len(stale_sessions),
        )
        if admit_session is not None:
            admit_session(runtime_session)
        _, stale_after_admission = _scan_sessions(directory_fd)
        if crash_recovery_required or stale_after_admission:
            recover_abandoned_sessions()
            _remove_stale_sessions(directory_fd, stale_sessions | stale_after_admission)
        if not registry_initialized:
            _create_registry_version(directory_fd)
        os.fsync(directory_fd)
        admission_stack.close()
        fcntl.flock(registry_fd, fcntl.LOCK_UN)
        registry_locked = False
        body_entered = True
        yield runtime_session
    except LocalRuntimeCompatibilityError:
        if session_created and directory_fd >= 0:
            try:
                os.unlink(session_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
                session_created = False
            except OSError:
                raise LocalRuntimeSessionError(
                    "runtime compatibility rejection cleanup failed"
                ) from None
        raise
    except LocalRuntimeSessionError:
        raise
    except RootConfinementError, DurabilityError:
        raise LocalRuntimeSessionError("runtime session registry is unavailable") from None
    except OSError:
        raise LocalRuntimeSessionError("runtime session registry operation failed") from None
    finally:
        try:
            if registry_fd >= 0 and session_created and body_entered:
                if not registry_locked:
                    _acquire_registry_lock(registry_fd)
                    registry_locked = True
                metadata = os.stat(session_name, dir_fd=directory_fd, follow_symlinks=False)
                held = os.fstat(session_fd)
                if (metadata.st_dev, metadata.st_ino) != (held.st_dev, held.st_ino):
                    raise LocalRuntimeSessionError("runtime session marker was replaced")
                live_peers, stale_sessions = _scan_sessions(directory_fd)
                if stale_sessions or live_peers == 1:
                    recover_abandoned_sessions()
                    _remove_stale_sessions(directory_fd, stale_sessions)
                os.unlink(session_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
        finally:
            admission_stack.close()
            if registry_locked:
                with suppress(OSError):
                    fcntl.flock(registry_fd, fcntl.LOCK_UN)
                registry_locked = False
            if session_fd >= 0:
                with suppress(OSError):
                    fcntl.flock(session_fd, fcntl.LOCK_UN)
                os.close(session_fd)
            for descriptor in (registry_fd, directory_fd, state_fd, root_fd):
                if descriptor >= 0:
                    os.close(descriptor)


def _registry_initialized(directory_fd: int) -> bool:
    try:
        file_fd, _ = _open_private_file(directory_fd, _REGISTRY_VERSION, create=False)
    except FileNotFoundError:
        return False
    try:
        payload = os.read(file_fd, len(_REGISTRY_VERSION_BYTES) + 1)
        if payload == _LEGACY_REGISTRY_VERSION_BYTES:
            return False
        if payload != _REGISTRY_VERSION_BYTES:
            raise LocalRuntimeSessionError("runtime session registry version is invalid")
        return True
    finally:
        os.close(file_fd)


def _create_registry_version(directory_fd: int) -> None:
    file_fd, _ = _open_private_file(directory_fd, _REGISTRY_PENDING_VERSION, create=True)
    try:
        _replace_file(file_fd, _REGISTRY_VERSION_BYTES)
    finally:
        os.close(file_fd)
    os.replace(
        _REGISTRY_PENDING_VERSION,
        _REGISTRY_VERSION,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )
    os.fsync(directory_fd)


def _scan_sessions(directory_fd: int) -> tuple[int, dict[str, tuple[int, int]]]:
    try:
        names = tuple(sorted(os.listdir(directory_fd)))
    except OSError:
        raise LocalRuntimeSessionError("runtime session registry is unreadable") from None
    allowed = {_REGISTRY_LOCK, _REGISTRY_VERSION, _REGISTRY_PENDING_VERSION}
    unexpected = [
        name for name in names if name not in allowed and _SESSION_FILE.fullmatch(name) is None
    ]
    if unexpected:
        raise LocalRuntimeSessionError("runtime session registry contains an invalid entry")
    live = 0
    stale: dict[str, tuple[int, int]] = {}
    for name in names:
        if _SESSION_FILE.fullmatch(name) is None:
            continue
        file_fd, _ = _open_private_file(directory_fd, name, create=False)
        try:
            try:
                fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                live += 1
                continue
            metadata = os.fstat(file_fd)
            stale[name] = (metadata.st_dev, metadata.st_ino)
        finally:
            os.close(file_fd)
    return live, stale


def _remove_stale_sessions(directory_fd: int, stale: dict[str, tuple[int, int]]) -> None:
    # A peer can die after journal cleanup. Remove only the earlier snapshot.
    for name, identity in stale.items():
        file_fd, _ = _open_private_file(directory_fd, name, create=False)
        try:
            metadata = os.fstat(file_fd)
            if (metadata.st_dev, metadata.st_ino) != identity:
                raise LocalRuntimeSessionError("stale runtime session marker was replaced")
            fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.unlink(name, dir_fd=directory_fd)
        finally:
            os.close(file_fd)
    if stale:
        os.fsync(directory_fd)


def _acquire_registry_lock(file_fd: int) -> None:
    deadline = time.monotonic() + _REGISTRY_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            if time.monotonic() >= deadline:
                raise LocalRuntimeSessionError("runtime session registry is busy") from None
            time.sleep(0.01)


def _open_private_file(directory_fd: int, name: str, *, create: bool) -> tuple[int, bool]:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    created = False
    if create:
        try:
            file_fd = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            created = True
        except FileExistsError:
            file_fd = os.open(name, flags, dir_fd=directory_fd)
    else:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
    metadata = os.fstat(file_fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        os.close(file_fd)
        raise LocalRuntimeSessionError("runtime session registry entry is unsafe")
    return file_fd, created


def _replace_file(file_fd: int, payload: bytes) -> None:
    os.ftruncate(file_fd, 0)
    os.lseek(file_fd, 0, os.SEEK_SET)
    _write_all(file_fd, payload)
    os.fsync(file_fd)


def _require_private_directory(directory_fd: int) -> None:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise LocalRuntimeSessionError("runtime session registry directory is unsafe")


__all__ = [
    "LocalRuntimeSession",
    "LocalRuntimeCompatibilityError",
    "LocalRuntimeSessionError",
    "RUNTIME_SESSION_VERSION",
    "hold_local_runtime_session",
]
