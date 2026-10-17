"""Bounded transport for the installed offline embedding helper, protocol v1.

Contract: ``docs/plans/2026-10-15-competitive-gaps-g1/EMBEDDING-RESOURCE.md`` sections 4-6
with the persistent verified stage of goal decision D7. The installed manifest v2 and
embedding inventory are the trust root (D8); verified bytes are copied once per component
into a private stage below the Brain root and re-hashed before every helper launch.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import platform as host
import secrets
import selectors
import stat
import struct
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, NoReturn

from open_brain_engine.engine.embedding_backend import (
    EMBEDDING_DIMENSIONS,
    EmbeddedItem,
    EmbeddingChunk,
    EmbeddingFailure,
    Purpose,
)

from open_brain.services import component_manifest_v2 as v2
from open_brain.services import embedding_pin

PROTOCOL: Final = v2.PROTOCOL
ZERO_REQUEST_ID: Final = "0" * 32
HELPER_NAME: Final = "open-brain-embedding"
MANIFEST_PATH: Final = f"share/open-brain/{v2.MANIFEST_FILENAME}"
INVENTORY_PATH: Final = v2.inventory_path("embedding")
STAGE_PARTS: Final = (".open-brain", "cache", "embedding-v1", "stage")
LIMITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "max_request_bytes": 131072,
        "max_response_bytes": 1048576,
        "max_stderr_bytes": 4096,
        "max_items": 4,
        "max_total_chunks": 128,
        "max_document_bytes": 65536,
        "max_query_bytes": 8192,
        "dimensions": 384,
        "max_query_tokens": 254,
        "document_window_tokens": 224,
        "document_overlap_tokens": 32,
        "capabilities_deadline_ms": 2000,
        "query_deadline_ms": 10000,
        "document_deadline_ms": 60000,
    }
)
HELPER_ENV: Final[Mapping[str, str]] = MappingProxyType(
    {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "OMP_NUM_THREADS": "2",
        "ORT_DISABLE_TELEMETRY": "1",
    }
)
# Enforcement copies of the protocol deadlines, in seconds.
_DEADLINE_SECONDS: dict[str, float] = {
    "capabilities": LIMITS["capabilities_deadline_ms"] / 1000,
    "query": LIMITS["query_deadline_ms"] / 1000,
    "document": LIMITS["document_deadline_ms"] / 1000,
}
# Bounded allowance for building or rebuilding the persistent stage (decision D7).
_STAGE_BUILD_SECONDS: Final = 30.0
_REAP_SECONDS: Final = 0.5
_POLL_SECONDS: Final = 0.05
_READ_CHUNK: Final = 65536
_HASH_CHUNK: Final = 1024 * 1024
_METADATA_LIMIT: Final = 1024 * 1024
_MAX_DEPTH: Final = 8
_FLOAT32_MIN_NORMAL: Final = 2.0**-126
_RESOURCE_ROOT: Final = "libexec/open-brain-embedding"
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# Child codes are bounded to parent categories; native details never cross.
_CHILD_CODES: Final = MappingProxyType(
    {
        "protocol_unsupported": "protocol_invalid",
        "request_invalid": "protocol_invalid",
        "component_mismatch": "component_mismatch",
        "input_limit": "input_limit",
        "tokenization_failed": "worker_failed",
        "inference_failed": "worker_failed",
    }
)
# Unicode White_Space, the helper's ``char::is_whitespace``.
_WHITESPACE: Final = frozenset(
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def _fail(category: str) -> NoReturn:
    raise EmbeddingFailure(category)


def current_platform() -> str:
    machine = host.machine().lower()
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-arm64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    _fail("unsupported_platform")


# ----------------------------------------------------------------- discovery


@dataclass(frozen=True, slots=True)
class InstalledEmbedding:
    """The real versioned prefix of the running installed base."""

    prefix: Path


def discover_embedding_resource(base_executable: Path | None = None) -> InstalledEmbedding:
    """Select the resource from the same installed prefix as the running base binary."""
    if base_executable is None:
        if not bool(getattr(sys, "frozen", False)):
            _fail("resource_missing")
        base_executable = Path(sys.executable)
    if not isinstance(base_executable, Path) or not base_executable.is_absolute():
        _fail("resource_missing")
    try:
        resolved = base_executable.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
        if (
            resolved.name != "open-brain"
            or resolved.parent.name != "bin"
            or not stat.S_ISREG(metadata.st_mode)
        ):
            _fail("resource_missing")
        prefix = resolved.parent.parent
        for relative in (MANIFEST_PATH, INVENTORY_PATH, v2.EXECUTABLES["embedding"]):
            if not stat.S_ISREG((prefix / relative).lstat().st_mode):
                _fail("resource_untrusted")
        return InstalledEmbedding(prefix)
    except EmbeddingFailure:
        raise
    except OSError:
        _fail("resource_missing")


# --------------------------------------------------- no-follow descriptor reads


def _owned(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and info.st_mode & 0o022 == 0


def _open_absolute(path: Path) -> int:
    """Open an absolute directory one component at a time without following links."""
    if not path.is_absolute():
        raise OSError("relative path")
    descriptor = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            # Section 4: reject group/other-writable ancestors unless sticky (e.g. /tmp).
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise OSError("writable ancestor")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_installed(prefix_fd: int, relative: str) -> int:
    """Open one installed regular file; every directory and the file must be trusted."""
    parts = relative.split("/")
    current = os.dup(prefix_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = child
            if not _owned(os.fstat(current)):
                _fail("resource_untrusted")
        descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=current)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not _owned(info):
            os.close(descriptor)
            _fail("resource_untrusted")
        return descriptor
    finally:
        os.close(current)


def _read_installed(prefix_fd: int, relative: str) -> bytes:
    descriptor = _open_installed(prefix_fd, relative)
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(_METADATA_LIMIT + 1)
    if len(data) > _METADATA_LIMIT:
        _fail("resource_untrusted")
    return data


@contextmanager
def _installed_prefix(installed: InstalledEmbedding) -> Iterator[int]:
    try:
        descriptor = _open_absolute(installed.prefix)
    except OSError:
        _fail("resource_missing")
    try:
        if not _owned(os.fstat(descriptor)):
            _fail("resource_untrusted")
        yield descriptor
    finally:
        os.close(descriptor)


# -------------------------------------------------------------- verification


@dataclass(frozen=True, slots=True)
class VerifiedEmbedding:
    """Installed manifest/inventory metadata accepted for the current platform."""

    installed: InstalledEmbedding
    platform: str
    inventory: v2.Inventory
    inventory_sha256: str
    component_bytes: bytes
    component_id: str


def _component_bytes(component: Mapping[str, object]) -> bytes:
    # Canonical component object without LF (D9); its hash is the manifest component ID.
    text = json.dumps(dict(component), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def verify_embedding_metadata(
    installed: InstalledEmbedding, *, expected_inventory_sha256: str | None = None
) -> VerifiedEmbedding:
    """Accept the installed manifest v2 and embedding inventory, or fail closed.

    ``expected_inventory_sha256`` overrides the base-build pin (D8). A frozen base uses its
    pinned value for this platform and fails closed without one; source execution has an
    empty pin and keeps the installed manifest's value as the trust root.
    """
    if expected_inventory_sha256 is not None and (
        not isinstance(expected_inventory_sha256, str)
        or len(expected_inventory_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_inventory_sha256)
    ):
        raise ValueError("invalid expected inventory hash")
    if not isinstance(installed, InstalledEmbedding):
        raise ValueError("invalid installed embedding")
    platform = current_platform()
    if expected_inventory_sha256 is None:
        expected_inventory_sha256 = embedding_pin.EXPECTED_EMBEDDING_INVENTORY_SHA256.get(platform)
        if expected_inventory_sha256 is None and bool(getattr(sys, "frozen", False)):
            _fail("resource_untrusted")
    try:
        with _installed_prefix(installed) as prefix_fd:
            manifest_bytes = _read_installed(prefix_fd, MANIFEST_PATH)
            inventory_bytes = _read_installed(prefix_fd, INVENTORY_PATH)
    except FileNotFoundError:
        _fail("resource_missing")
    except OSError:
        _fail("resource_untrusted")
    digest = hashlib.sha256(inventory_bytes).hexdigest()
    try:
        manifest = v2.parse_manifest_v2(manifest_bytes)
        resource = next(
            item
            for item in manifest.resources
            if item.role == "embedding" and item.platform == platform
        )
        if digest != resource.inventory_sha256 or (
            expected_inventory_sha256 is not None and digest != expected_inventory_sha256
        ):
            _fail("resource_untrusted")
        inventory = v2.parse_inventory(inventory_bytes)
        v2.validate_resource_inventory(resource, manifest.version, inventory)
    except StopIteration:
        _fail("unsupported_platform")
    except v2.ComponentManifestError:
        _fail("resource_untrusted")
    if inventory.platform != platform or inventory.component is None:
        _fail("unsupported_platform")
    component = _component_bytes(inventory.component)
    component_id = hashlib.sha256(component).hexdigest()
    if component_id != v2.component_id(inventory.component):
        _fail("resource_untrusted")
    return VerifiedEmbedding(installed, platform, inventory, digest, component, component_id)


# ------------------------------------------------------- persistent stage (D7)


@dataclass(frozen=True, slots=True)
class _StagedFile:
    relative: str
    mode: int
    size: int
    sha256: str


def _stage_plan(verified: VerifiedEmbedding) -> tuple[tuple[str, ...], tuple[_StagedFile, ...]]:
    prefix = _RESOURCE_ROOT + "/"
    directories: list[str] = []
    files = [
        _StagedFile(
            "component.json",
            0o400,
            len(verified.component_bytes),
            hashlib.sha256(verified.component_bytes).hexdigest(),
        )
    ]
    for member in verified.inventory.members:
        if not member.path.startswith(prefix):
            continue
        relative = member.path[len(prefix) :]
        if member.kind == "directory":
            directories.append(relative)
        elif member.sha256 is not None:
            mode = 0o500 if member.mode == 0o755 else 0o400
            files.append(_StagedFile(relative, mode, member.bytes, member.sha256))
    return tuple(sorted(directories)), tuple(sorted(files, key=lambda item: item.relative))


_Identity = tuple[int, int, int, int, int, int, int, int]


def _identity(info: os.stat_result) -> _Identity:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class _Clock:
    def __init__(self, deadline: float, cancelled: Callable[[], bool]) -> None:
        self.deadline = deadline
        self.cancelled = cancelled

    def check(self) -> None:
        if self.cancelled():
            _fail("cancelled")
        if time.monotonic() >= self.deadline:
            _fail("deadline")


class _StageMismatch(Exception):
    """The persistent stage differs from the verified inventory."""


def _remove_entry(parent_fd: int, name: str) -> None:
    """Remove one entry without traversing symbolic links."""
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        os.fchmod(descriptor, 0o700)
        for child in os.listdir(descriptor):
            _remove_entry(descriptor, child)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)


class _Stage:
    """One persistent verified stage per component below the admitted Brain root."""

    def __init__(self, parent_fd: int, parent: Path, verified: VerifiedEmbedding) -> None:
        self._parent_fd = parent_fd
        self._verified = verified
        self.name = verified.component_id
        self.path = parent / self.name
        self.directories, self.files = _stage_plan(verified)

    def _expected_names(self) -> dict[str, set[str]]:
        names: dict[str, set[str]] = {"": set()}
        for directory in self.directories:
            names[directory] = set()
        for relative in (*self.directories, *(item.relative for item in self.files)):
            parent, _, leaf = relative.rpartition("/")
            names.setdefault(parent, set()).add(leaf)
        return names

    @contextmanager
    def _directories(self) -> Iterator[dict[str, int]]:
        opened: dict[str, int] = {}
        try:
            opened[""] = os.open(self.name, _DIR_FLAGS, dir_fd=self._parent_fd)
            for directory in self.directories:
                parent, _, leaf = directory.rpartition("/")
                opened[directory] = os.open(leaf, _DIR_FLAGS, dir_fd=opened[parent])
            yield opened
        finally:
            for descriptor in opened.values():
                os.close(descriptor)

    def verify(self, clock: _Clock) -> dict[str, _Identity]:
        """Re-hash every staged byte through no-follow descriptors; mismatch raises."""
        identities: dict[str, _Identity] = {}
        try:
            with self._directories() as opened:
                expected = self._expected_names()
                for relative, descriptor in opened.items():
                    info = os.fstat(descriptor)
                    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise _StageMismatch
                    if set(os.listdir(descriptor)) != expected.get(relative, set()):
                        raise _StageMismatch
                    identities[relative + "/"] = _identity(info)
                for item in self.files:
                    clock.check()
                    parent, _, leaf = item.relative.rpartition("/")
                    identities[item.relative] = self._verify_file(opened[parent], leaf, item)
        except FileNotFoundError, NotADirectoryError:
            raise _StageMismatch from None
        except OSError:
            raise _StageMismatch from None
        return identities

    def _verify_file(self, directory_fd: int, leaf: str, item: _StagedFile) -> _Identity:
        descriptor = os.open(leaf, _FILE_FLAGS, dir_fd=directory_fd)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != item.mode
                or before.st_nlink != 1
                or before.st_size != item.size
            ):
                raise _StageMismatch
            digest = hashlib.sha256()
            seen = 0
            while chunk := handle.read(_HASH_CHUNK):
                seen += len(chunk)
                if seen > item.size:
                    raise _StageMismatch
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        named = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        if (
            seen != item.size
            or digest.hexdigest() != item.sha256
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(named)
        ):
            raise _StageMismatch
        return _identity(after)

    def unchanged(self, identities: Mapping[str, _Identity]) -> bool:
        """Recheck staged stat identities immediately before launch."""
        try:
            with self._directories() as opened:
                expected = self._expected_names()
                for relative, descriptor in opened.items():
                    if _identity(os.fstat(descriptor)) != identities.get(relative + "/"):
                        return False
                    if set(os.listdir(descriptor)) != expected.get(relative, set()):
                        return False
                for item in self.files:
                    parent, _, leaf = item.relative.rpartition("/")
                    named = os.stat(leaf, dir_fd=opened[parent], follow_symlinks=False)
                    if _identity(named) != identities.get(item.relative):
                        return False
        except OSError:
            return False
        return True

    def remove(self) -> None:
        with suppress(FileNotFoundError):
            _remove_entry(self._parent_fd, self.name)

    def build(self, clock: _Clock) -> None:
        """Create the stage from verified installed bytes, then publish it by rename."""
        for name in os.listdir(self._parent_fd):
            if name.startswith(".build-"):
                _remove_entry(self._parent_fd, name)
        temporary = f".build-{secrets.token_hex(16)}"
        os.mkdir(temporary, 0o700, dir_fd=self._parent_fd)
        try:
            root = os.open(temporary, _DIR_FLAGS, dir_fd=self._parent_fd)
            try:
                os.fchmod(root, 0o700)
                self._populate(root, clock)
            finally:
                os.close(root)
            os.rename(temporary, self.name, src_dir_fd=self._parent_fd, dst_dir_fd=self._parent_fd)
        except BaseException:
            with suppress(OSError):
                _remove_entry(self._parent_fd, temporary)
            raise

    def _populate(self, root: int, clock: _Clock) -> None:
        opened = {"": root}
        try:
            for directory in self.directories:
                parent, _, leaf = directory.rpartition("/")
                os.mkdir(leaf, 0o700, dir_fd=opened[parent])
                opened[directory] = os.open(leaf, _DIR_FLAGS, dir_fd=opened[parent])
                os.fchmod(opened[directory], 0o700)
            targets = {_RESOURCE_ROOT + "/" + item.relative: item for item in self.files}
            with _installed_prefix(self._verified.installed) as prefix_fd:
                for member in self._verified.inventory.members:
                    clock.check()
                    if member.kind != "file" or member.sha256 is None:
                        continue
                    target = targets.get(member.path)
                    destination = None
                    if target is not None:
                        parent, _, leaf = target.relative.rpartition("/")
                        destination = (opened[parent], leaf, target.mode)
                    self._copy_verified(prefix_fd, member, destination)
            descriptor = os.open("component.json", _CREATE_FLAGS, 0o600, dir_fd=root)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(self._verified.component_bytes)
                os.fchmod(handle.fileno(), 0o400)
        finally:
            for relative, descriptor in opened.items():
                if relative:
                    os.close(descriptor)

    @staticmethod
    def _copy_verified(
        prefix_fd: int, member: v2.Member, destination: tuple[int, str, int] | None
    ) -> None:
        """Hash one installed member; copy exactly those descriptor bytes when staged."""
        source = _open_installed(prefix_fd, member.path)
        with os.fdopen(source, "rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size != member.bytes:
                _fail("resource_untrusted")
            output = None
            if destination is not None:
                directory_fd, leaf, _ = destination
                output = os.fdopen(os.open(leaf, _CREATE_FLAGS, 0o600, dir_fd=directory_fd), "wb")
            try:
                digest = hashlib.sha256()
                seen = 0
                while chunk := handle.read(_HASH_CHUNK):
                    seen += len(chunk)
                    if seen > member.bytes:
                        _fail("resource_untrusted")
                    digest.update(chunk)
                    if output is not None:
                        output.write(chunk)
                if (
                    seen != member.bytes
                    or digest.hexdigest() != member.sha256
                    or _identity(before) != _identity(os.fstat(handle.fileno()))
                ):
                    _fail("resource_untrusted")
                if output is not None and destination is not None:
                    output.flush()
                    os.fchmod(output.fileno(), destination[2])
            finally:
                if output is not None:
                    output.close()

    def prepare(self, clock: _Clock) -> dict[str, _Identity]:
        """Return verified identities, recreating the stage once on any mismatch."""
        try:
            return self.verify(clock)
        except _StageMismatch:
            pass
        return self.rebuild(clock)

    def rebuild(self, clock: _Clock) -> dict[str, _Identity]:
        try:
            self.remove()
            self.build(clock)
            return self.verify(clock)
        except EmbeddingFailure as failure:
            if failure.category in ("cancelled", "deadline"):
                raise
            _fail("resource_untrusted")
        except _StageMismatch, OSError:
            _fail("resource_untrusted")


@contextmanager
def _locked_stage_parent(brain_root: Path, clock: _Clock) -> Iterator[int]:
    """Open (creating 0700) the stage parent and hold its exclusive lock.

    The lock is polled so a competing holder cannot outlast the caller's deadline or
    cancellation.
    """
    opened: list[int] = []
    try:
        try:
            opened.append(_open_absolute(brain_root))
            for name in STAGE_PARTS:
                with suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=opened[-1])
                opened.append(os.open(name, _DIR_FLAGS, dir_fd=opened[-1]))
                info = os.fstat(opened[-1])
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    _fail("resource_untrusted")
            lock = os.open(
                ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=opened[-1],
            )
            opened.append(lock)
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    clock.check()
                    time.sleep(0.01)
        except OSError:
            _fail("resource_untrusted")
        yield opened[-2]
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def stage_bytes(brain_root: Path) -> int:
    """Bytes held by every persistent embedding stage, for cache disk accounting."""
    try:
        root = _open_absolute(brain_root)
    except OSError:
        return 0
    descriptors = [root]
    try:
        for name in STAGE_PARTS:
            try:
                descriptors.append(os.open(name, _DIR_FLAGS, dir_fd=descriptors[-1]))
            except FileNotFoundError:
                return 0
        total = 0
        for _, _, files, directory_fd in os.fwalk(
            ".", dir_fd=descriptors[-1], follow_symlinks=False
        ):
            for name in files:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                total += info.st_size
        return total
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


# ------------------------------------------------------------------ exchange


def _reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_REAP_SECONDS)
    except OSError, subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=_REAP_SECONDS)
        except OSError, subprocess.TimeoutExpired:
            _fail("cleanup_failed")


def _run_child(stage: Path, request: bytes, clock: _Clock) -> tuple[int, bytes]:
    """Run one helper child; drain its pipes concurrently under fixed byte caps."""
    clock.check()
    try:
        process = subprocess.Popen(
            (str(stage / HELPER_NAME), "--stdio-v1"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(stage),
            env=dict(HELPER_ENV),
            close_fds=True,
        )
    except OSError, subprocess.SubprocessError:
        _fail("worker_failed")
    streams = (process.stdin, process.stdout, process.stderr)
    try:
        stdin, stdout, stderr = streams
        if stdin is None or stdout is None or stderr is None:
            _fail("worker_failed")
        response = bytearray()
        diagnostics = 0
        pending = memoryview(request)
        with selectors.DefaultSelector() as selector:
            os.set_blocking(stdin.fileno(), False)
            selector.register(stdin, selectors.EVENT_WRITE)
            selector.register(stdout, selectors.EVENT_READ)
            selector.register(stderr, selectors.EVENT_READ)
            while selector.get_map():
                clock.check()
                timeout = min(_POLL_SECONDS, max(0.0, clock.deadline - time.monotonic()))
                for key, _ in selector.select(timeout):
                    if key.fileobj is stdin:
                        try:
                            written = os.write(stdin.fileno(), pending[:_READ_CHUNK])
                        except BlockingIOError:
                            continue
                        except OSError:
                            written = len(pending)
                        pending = pending[written:]
                        if not pending:
                            selector.unregister(stdin)
                            stdin.close()
                        continue
                    chunk = os.read(key.fd, _READ_CHUNK)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is stdout:
                        response += chunk
                        if len(response) > LIMITS["max_response_bytes"]:
                            _fail("protocol_invalid")
                    else:
                        diagnostics += len(chunk)
                        if diagnostics > LIMITS["max_stderr_bytes"]:
                            _fail("worker_failed")
        while True:
            clock.check()
            try:
                returncode = process.wait(
                    timeout=min(_POLL_SECONDS, max(0.0, clock.deadline - time.monotonic()))
                )
                break
            except subprocess.TimeoutExpired:
                continue
        return returncode, bytes(response)
    finally:
        for stream in streams:
            if stream is not None:
                with suppress(OSError):
                    stream.close()
        _reap(process)


# ------------------------------------------------------- response validation


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            _fail("protocol_invalid")
        result[key] = value
    return result


def _reject_constant(_name: str) -> NoReturn:
    _fail("protocol_invalid")


def _depth(value: object, level: int = 1) -> int:
    if isinstance(value, dict):
        return max((_depth(item, level + 1) for item in value.values()), default=level)
    if isinstance(value, list):
        return max((_depth(item, level + 1) for item in value), default=level)
    return level - 1


def _frame(raw: bytes) -> dict[str, object]:
    if raw.count(b"\n") != 1 or not raw.endswith(b"}\n") or not raw.startswith(b"{"):
        _fail("protocol_invalid")
    try:
        value = json.loads(
            raw[:-1].decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except EmbeddingFailure:
        raise
    except UnicodeDecodeError, ValueError, RecursionError:
        _fail("protocol_invalid")
    if not isinstance(value, dict) or _depth(value) > _MAX_DEPTH:
        _fail("protocol_invalid")
    return value


def _exact(value: object, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail("protocol_invalid")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        _fail("protocol_invalid")
    return value


_ERROR_KEYS: Final = frozenset({"protocol", "request_id", "status", "error"})
_CAPABILITIES_KEYS: Final = frozenset(
    {"protocol", "request_id", "status", "component_id", "limits"}
)
_EMBED_KEYS: Final = frozenset(
    {"protocol", "request_id", "status", "component_id", "purpose", "items"}
)
_ITEM_KEYS: Final = frozenset({"slot", "text_sha256", "token_count", "chunks"})
_CHUNK_KEYS: Final = frozenset(
    {"ordinal", "token_start", "token_end", "byte_start", "byte_end", "vector"}
)


def _response(returncode: int, raw: bytes, request_id: str) -> dict[str, object]:
    """Apply exit-code versus frame rules; return the success frame."""
    if returncode not in (0, 2) or not raw:
        _fail("worker_failed")
    document = _frame(raw)
    status = document.get("status")
    if returncode == 0 and status == "ok":
        return document
    if returncode == 2 and status == "error":
        error = _exact(document, _ERROR_KEYS)
        detail = _exact(error["error"], frozenset({"code", "retryable"}))
        code = detail["code"]
        if (
            error["protocol"] != PROTOCOL
            or error["request_id"] not in (request_id, ZERO_REQUEST_ID)
            or detail["retryable"] is not False
            or not isinstance(code, str)
            or code not in _CHILD_CODES
        ):
            _fail("protocol_invalid")
        _fail(_CHILD_CODES[code])
    if status in ("ok", "error"):
        _fail("worker_failed")
    _fail("protocol_invalid")


def _bindings(document: dict[str, object], request_id: str, component_id: str) -> None:
    if document["protocol"] != PROTOCOL or document["request_id"] != request_id:
        _fail("protocol_invalid")
    if document["component_id"] != component_id:
        _fail("component_mismatch")


def _validate_capabilities(returncode: int, raw: bytes, request_id: str, component_id: str) -> None:
    document = _exact(_response(returncode, raw, request_id), _CAPABILITIES_KEYS)
    _bindings(document, request_id, component_id)
    limits = _exact(document["limits"], frozenset(LIMITS))
    if any(type(limits[key]) is not int or limits[key] != value for key, value in LIMITS.items()):
        _fail("protocol_invalid")


def _windows(count: int) -> list[tuple[int, int]]:
    window = LIMITS["document_window_tokens"]
    stride = window - LIMITS["document_overlap_tokens"]
    ranges: list[tuple[int, int]] = []
    start = 0
    while True:
        end = min(start + window, count)
        ranges.append((start, end))
        if end == count:
            return ranges
        start += stride


def _characters(text: str) -> tuple[dict[int, str], dict[int, str], list[tuple[int, int]]]:
    """Byte starts and ends of every character, and spans of non-whitespace ones."""
    starts: dict[int, str] = {}
    ends: dict[int, str] = {}
    visible: list[tuple[int, int]] = []
    position = 0
    for character in text:
        width = len(character.encode("utf-8"))
        starts[position] = character
        ends[position + width] = character
        if character not in _WHITESPACE:
            visible.append((position, position + width))
        position += width
    return starts, ends, visible


def _vector(value: object) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != EMBEDDING_DIMENSIONS:
        _fail("protocol_invalid")
    numbers: list[float] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            _fail("protocol_invalid")
        if not -1.0001 <= entry <= 1.0001:
            _fail("protocol_invalid")
        number = float(entry)
        if not math.isfinite(number):
            _fail("protocol_invalid")
        numbers.append(number)
    converted = struct.unpack(
        f"<{EMBEDDING_DIMENSIONS}f", struct.pack(f"<{EMBEDDING_DIMENSIONS}f", *numbers)
    )
    canonical = tuple(0.0 if item == 0.0 else item for item in converted)
    for original, item in zip(numbers, canonical, strict=True):
        if (original != 0.0 and abs(item) < _FLOAT32_MIN_NORMAL) or abs(item) > 1.0001:
            _fail("protocol_invalid")
    norm = math.sqrt(sum(item * item for item in canonical))
    if not 0.999 <= norm <= 1.001:
        _fail("protocol_invalid")
    return canonical


def _chunks(value: object, purpose: Purpose, text: str, token_count: int) -> list[EmbeddingChunk]:
    if purpose == "query":
        if token_count > LIMITS["max_query_tokens"]:
            _fail("protocol_invalid")
        ranges = [(0, token_count)]
    else:
        ranges = _windows(token_count)
    if not isinstance(value, list) or len(value) != len(ranges):
        _fail("protocol_invalid")
    starts, ends, visible = _characters(text)
    chunks: list[EmbeddingChunk] = []
    for ordinal, (raw, (token_start, token_end)) in enumerate(zip(value, ranges, strict=True)):
        fields = _exact(raw, _CHUNK_KEYS)
        if (
            _integer(fields["ordinal"]) != ordinal
            or _integer(fields["token_start"]) != token_start
            or _integer(fields["token_end"]) != token_end
        ):
            _fail("protocol_invalid")
        byte_start = _integer(fields["byte_start"])
        byte_end = _integer(fields["byte_end"])
        first = starts.get(byte_start)
        last = ends.get(byte_end)
        if (
            byte_start >= byte_end
            or first is None
            or last is None
            or first in _WHITESPACE
            or last in _WHITESPACE
        ):
            _fail("protocol_invalid")
        if chunks and (byte_start < chunks[-1].byte_start or byte_end < chunks[-1].byte_end):
            _fail("protocol_invalid")
        chunks.append(
            EmbeddingChunk(
                ordinal=ordinal,
                token_start=token_start,
                token_end=token_end,
                byte_start=byte_start,
                byte_end=byte_end,
                vector=_vector(fields["vector"]),
            )
        )
    covered: list[list[int]] = []
    for chunk in chunks:
        if covered and chunk.byte_start <= covered[-1][1]:
            covered[-1][1] = max(covered[-1][1], chunk.byte_end)
        else:
            covered.append([chunk.byte_start, chunk.byte_end])
    index = 0
    for start, end in visible:
        while index < len(covered) and covered[index][1] < end:
            index += 1
        if index == len(covered) or covered[index][0] > start:
            _fail("protocol_invalid")
    return chunks


def _validate_embed(
    returncode: int,
    raw: bytes,
    *,
    request_id: str,
    component_id: str,
    purpose: Purpose,
    texts: Sequence[str],
) -> list[EmbeddedItem]:
    document = _exact(_response(returncode, raw, request_id), _EMBED_KEYS)
    _bindings(document, request_id, component_id)
    if document["purpose"] != purpose:
        _fail("protocol_invalid")
    entries = document["items"]
    if not isinstance(entries, list) or len(entries) != len(texts):
        _fail("protocol_invalid")
    items: list[EmbeddedItem] = []
    total = 0
    for slot, (entry, text) in enumerate(zip(entries, texts, strict=True)):
        fields = _exact(entry, _ITEM_KEYS)
        token_count = _integer(fields["token_count"])
        encoded = text.encode("utf-8")
        if (
            _integer(fields["slot"]) != slot
            or fields["text_sha256"] != hashlib.sha256(encoded).hexdigest()
            or token_count == 0
            # Every content token covers at least one input byte; this bounds the window
            # plan below against hostile helper output.
            or token_count > len(encoded)
        ):
            _fail("protocol_invalid")
        chunks = _chunks(fields["chunks"], purpose, text, token_count)
        total += len(chunks)
        if total > LIMITS["max_total_chunks"]:
            _fail("protocol_invalid")
        items.append(
            EmbeddedItem(
                slot=slot,
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                token_count=token_count,
                chunks=tuple(chunks),
            )
        )
    return items


# ------------------------------------------------------------------ backend


def _request(document: Mapping[str, object]) -> bytes:
    text = json.dumps(dict(document), separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8") + b"\n"


def _checked_texts(purpose: object, texts: object) -> tuple[str, ...]:
    if purpose not in ("query", "document"):
        raise ValueError("invalid embedding purpose")
    if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
        raise ValueError("invalid embedding texts")
    values = tuple(texts)
    if not values or (purpose == "query" and len(values) != 1):
        raise ValueError("invalid embedding item count")
    if len(values) > LIMITS["max_items"]:
        _fail("input_limit")
    cap = LIMITS["max_query_bytes"] if purpose == "query" else LIMITS["max_document_bytes"]
    for text in values:
        if not isinstance(text, str) or all(character in _WHITESPACE for character in text):
            raise ValueError("invalid embedding text")
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid embedding text") from None
        if not unicodedata.is_normalized("NFC", text):
            raise ValueError("embedding text must be NFC")
        if len(encoded) > cap:
            _fail("input_limit")
    return values


class HelperEmbeddingBackend:
    """``EmbeddingBackend`` over the installed helper: one bounded child per exchange."""

    def __init__(
        self,
        installed: InstalledEmbedding,
        brain_root: Path,
        *,
        expected_inventory_sha256: str | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        if not isinstance(brain_root, Path) or not brain_root.is_absolute():
            raise ValueError("invalid Brain root")
        if not callable(cancelled):
            raise ValueError("invalid cancellation hook")
        self._installed = installed
        self._brain_root = brain_root
        self._expected = expected_inventory_sha256
        self._cancelled = cancelled
        verified = verify_embedding_metadata(
            installed, expected_inventory_sha256=expected_inventory_sha256
        )
        self.component_id: str = verified.component_id

    def embed(self, purpose: Purpose, texts: Sequence[str]) -> list[EmbeddedItem]:
        values = _checked_texts(purpose, texts)
        embed_id = secrets.token_hex(16)
        request = _request(
            {
                "protocol": PROTOCOL,
                "request_id": embed_id,
                "operation": "embed",
                "component_id": self.component_id,
                "purpose": purpose,
                "items": [{"slot": slot, "text": text} for slot, text in enumerate(values)],
            }
        )
        if len(request) > LIMITS["max_request_bytes"]:
            _fail("input_limit")
        # The first exchange of an operation may build the persistent stage (copy and hash
        # every installed byte), so it gets a bounded stage-build allowance on top of the
        # capabilities deadline; it stays cancellable throughout.
        clock = _Clock(
            time.monotonic() + _DEADLINE_SECONDS["capabilities"] + _STAGE_BUILD_SECONDS,
            self._cancelled,
        )
        clock.check()
        verified = verify_embedding_metadata(
            self._installed, expected_inventory_sha256=self._expected
        )
        if verified.component_id != self.component_id:
            _fail("component_mismatch")
        with _locked_stage_parent(self._brain_root, clock) as parent_fd:
            stage = _Stage(parent_fd, self._brain_root.joinpath(*STAGE_PARTS), verified)
            capabilities_id = secrets.token_hex(16)
            returncode, raw = self._launch(
                stage,
                _request(
                    {
                        "protocol": PROTOCOL,
                        "request_id": capabilities_id,
                        "operation": "capabilities",
                    }
                ),
                clock,
                _DEADLINE_SECONDS["capabilities"],
            )
            _validate_capabilities(returncode, raw, capabilities_id, self.component_id)
            clock = _Clock(time.monotonic() + _DEADLINE_SECONDS[purpose], self._cancelled)
            returncode, raw = self._launch(stage, request, clock, _DEADLINE_SECONDS[purpose])
        if self._cancelled():
            _fail("cancelled")
        return _validate_embed(
            returncode,
            raw,
            request_id=embed_id,
            component_id=self.component_id,
            purpose=purpose,
            texts=values,
        )

    @staticmethod
    def _launch(
        stage: _Stage, request: bytes, clock: _Clock, child_seconds: float
    ) -> tuple[int, bytes]:
        identities = stage.prepare(clock)
        if not stage.unchanged(identities):
            identities = stage.rebuild(clock)
            if not stage.unchanged(identities):
                _fail("resource_untrusted")
        child_clock = _Clock(min(clock.deadline, time.monotonic() + child_seconds), clock.cancelled)
        return _run_child(stage.path, request, child_clock)
