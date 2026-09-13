# Private legacy compatibility snapshot; excluded from every shipping artifact.
from __future__ import annotations

import ctypes
import math
import mimetypes
import os
import resource
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from open_brain_engine.engine import ExtractionFailure, RawAssetRef

_MIB = 1024 * 1024
_PARTIAL_SUFFIXES = (".part", ".partial", ".tmp", ".ytdl")
_READ_SIZE = 64 * 1024
_RUSAGE_INFO_V2 = 2


class _DarwinRUsageInfoV2(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


class MediaTool(StrEnum):
    YT_DLP = "yt-dlp"
    GALLERY_DL = "gallery-dl"


@dataclass(frozen=True, slots=True)
class MediaLimits:
    wall_seconds: float = 60.0
    cpu_seconds: int = 30
    memory_bytes: int = 512 * _MIB
    max_processes: int = 8
    max_stdout_bytes: int = 64 * 1024
    max_stderr_bytes: int = 64 * 1024
    max_single_file_bytes: int = 50 * _MIB
    max_total_bytes: int = 100 * _MIB
    max_files: int = 8
    max_videos: int = 1

    def __post_init__(self) -> None:
        values = (
            self.cpu_seconds,
            self.memory_bytes,
            self.max_processes,
            self.max_stdout_bytes,
            self.max_stderr_bytes,
            self.max_single_file_bytes,
            self.max_total_bytes,
            self.max_files,
            self.max_videos,
        )
        if (
            not isinstance(self.wall_seconds, int | float)
            or isinstance(self.wall_seconds, bool)
            or not math.isfinite(self.wall_seconds)
            or self.wall_seconds <= 0
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in values
            )
            or self.max_single_file_bytes > self.max_total_bytes
            or self.max_videos > self.max_files
        ):
            raise ValueError("invalid media limits")


DEFAULT_MEDIA_LIMITS = MediaLimits()


@dataclass(frozen=True, slots=True)
class MediaCommand:
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    limits: MediaLimits = DEFAULT_MEDIA_LIMITS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.argv, tuple)
            or not self.argv
            or any(
                not isinstance(value, str) or not value or "\x00" in value for value in self.argv
            )
            or self.environment != ()
            or not isinstance(self.limits, MediaLimits)
        ):
            raise ValueError("invalid media command")


@dataclass(frozen=True, slots=True)
class MediaRunResult:
    assets: tuple[RawAssetRef, ...]
    stdout: bytes
    stderr: bytes
    failure: ExtractionFailure | None
    reaped: bool


class MediaAssetStore(Protocol):
    def put(self, *, data: bytes, media_type: str) -> RawAssetRef: ...


class BoundedMediaRunner:
    def __init__(
        self,
        *,
        allowed_executables: tuple[str, ...],
        staging_parent: Path | None = None,
        asset_store: MediaAssetStore | None = None,
    ) -> None:
        if not isinstance(allowed_executables, tuple) or not allowed_executables:
            raise ValueError("invalid executable allowlist")
        executable_aliases: dict[str, str] = {}
        try:
            for value in allowed_executables:
                if not isinstance(value, str) or not value:
                    raise ValueError
                path = Path(value)
                if not path.is_absolute():
                    raise ValueError
                resolved = path.resolve(strict=True)
                if not resolved.is_file() or not os.access(resolved, os.X_OK):
                    raise ValueError
                for alias in {value, path.name}:
                    existing = executable_aliases.get(alias)
                    if existing is not None and existing != str(resolved):
                        raise ValueError
                    executable_aliases[alias] = str(resolved)
        except (OSError, ValueError):
            raise ValueError("invalid executable allowlist") from None
        if staging_parent is not None and (
            not isinstance(staging_parent, Path)
            or not staging_parent.is_absolute()
            or not staging_parent.is_dir()
        ):
            raise ValueError("invalid staging parent")
        if asset_store is not None and not callable(getattr(asset_store, "put", None)):
            raise ValueError("invalid media asset store")
        self._executable_aliases = executable_aliases
        self._staging_parent = staging_parent
        self._asset_store = asset_store

    def run(self, command: MediaCommand) -> MediaRunResult:
        executable = (
            self._resolve_executable(command.argv[0]) if isinstance(command, MediaCommand) else None
        )
        if (
            not isinstance(command, MediaCommand)
            or executable is None
            or command.environment
            or not _resource_limits_supported()
        ):
            return _failure(ExtractionFailure.TOOL_UNAVAILABLE)

        parent = None if self._staging_parent is None else str(self._staging_parent)
        with tempfile.TemporaryDirectory(prefix="open-brain-media-", dir=parent) as raw_root:
            root = Path(raw_root)
            process: subprocess.Popen[bytes] | None = None
            try:
                argv = (executable, *command.argv[1:])
                platform_command = _sandboxed_media_command(argv, root)
                if platform_command is None:
                    return _failure(ExtractionFailure.TOOL_UNAVAILABLE)
                empty_environment: dict[str, str] = {}
                process = subprocess.Popen[bytes](
                    platform_command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(root),
                    env=empty_environment,
                    close_fds=True,
                    start_new_session=True,
                    preexec_fn=_limit_resources(command.limits),
                    text=False,
                )
                output = _bounded_output(process, command.limits)
                if output.failure is not None:
                    return output
                if process.returncode != 0:
                    process_failure = (
                        ExtractionFailure.TOOL_RESOURCE_LIMIT
                        if process.returncode is not None and process.returncode < 0
                        else ExtractionFailure.MALFORMED_TOOL_OUTPUT
                    )
                    return MediaRunResult((), b"", b"", process_failure, True)
                assets, staged_failure = _staged_assets(
                    root,
                    command.limits,
                    asset_store=self._asset_store,
                )
                if staged_failure is not None:
                    return MediaRunResult((), b"", b"", staged_failure, True)
                return MediaRunResult(assets, output.stdout, output.stderr, None, True)
            except (OSError, subprocess.SubprocessError):
                if process is not None:
                    _kill_and_reap(process)
                return _failure(ExtractionFailure.TOOL_UNAVAILABLE)

    def _resolve_executable(self, value: str) -> str | None:
        if not isinstance(value, str):
            return None
        return self._executable_aliases.get(value)


def _resource_limits_supported() -> bool:
    limits = all(
        hasattr(resource, name)
        for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NPROC")
    )
    return (
        limits
        and sys.platform == "darwin"
        and os.path.isfile("/usr/bin/sandbox-exec")
        and os.access("/usr/bin/sandbox-exec", os.X_OK)
    )


def _sandboxed_media_command(argv: tuple[str, ...], stage: Path) -> tuple[str, ...] | None:
    if sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK):
        return (
            "/usr/bin/sandbox-exec",
            "-p",
            _darwin_sandbox_profile(stage),
            *argv,
        )
    return None


def _darwin_sandbox_profile(stage: Path) -> str:
    if not isinstance(stage, Path) or not stage.is_absolute():
        raise ValueError("invalid media stage")
    stage_value = _sandbox_literal(str(stage))
    return "".join(
        (
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow file-read*)",
            '(deny file-read* (subpath "/Users") (subpath "/Volumes") '
            '(subpath "/private/var/folders") (subpath "/private/tmp") '
            '(subpath "/tmp") (subpath "/var/tmp") (subpath "/private/var/root"))',
            f"(allow file-read* (subpath {stage_value}))",
            f"(allow file-write* (subpath {stage_value}))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow network-outbound)",
        )
    )


def _sandbox_literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def collect_staged_media(
    staging_root: Path,
    *,
    limits: MediaLimits = DEFAULT_MEDIA_LIMITS,
    asset_store: MediaAssetStore | None = None,
) -> tuple[tuple[RawAssetRef, ...], ExtractionFailure | None]:
    if (
        not isinstance(staging_root, Path)
        or not staging_root.is_absolute()
        or not staging_root.is_dir()
    ):
        return (), ExtractionFailure.MALFORMED_TOOL_OUTPUT
    if asset_store is not None and not callable(getattr(asset_store, "put", None)):
        return (), ExtractionFailure.MALFORMED_TOOL_OUTPUT
    try:
        return _staged_assets(staging_root, limits, asset_store=asset_store)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _limit_resources(limits: MediaLimits) -> Callable[[], None]:
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        if sys.platform != "darwin":
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_processes, limits.max_processes))
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (limits.max_single_file_bytes, limits.max_single_file_bytes),
        )

    return apply


def _bounded_output(process: subprocess.Popen[bytes], limits: MediaLimits) -> MediaRunResult:
    if process.stdout is None or process.stderr is None:
        _kill_and_reap(process)
        return _failure(ExtractionFailure.TOOL_UNAVAILABLE)
    streams = selectors.DefaultSelector()
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    bounds = {
        "stdout": limits.max_stdout_bytes,
        "stderr": limits.max_stderr_bytes,
    }
    deadline = time.monotonic() + limits.wall_seconds
    try:
        while streams.get_map():
            if process.poll() is None and not _memory_within_limit(
                process.pid,
                limits.memory_bytes,
            ):
                return MediaRunResult(
                    (),
                    b"",
                    b"",
                    ExtractionFailure.TOOL_RESOURCE_LIMIT,
                    _kill_and_reap(process),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return MediaRunResult(
                    (), b"", b"", ExtractionFailure.TOOL_TIMEOUT, _kill_and_reap(process)
                )
            for key, _ in streams.select(min(remaining, 0.05)):
                chunk = os.read(key.fd, _READ_SIZE)
                if not chunk:
                    streams.unregister(key.fileobj)
                    continue
                name = key.data
                if not isinstance(name, str):
                    return MediaRunResult(
                        (),
                        b"",
                        b"",
                        ExtractionFailure.TOOL_RESOURCE_LIMIT,
                        _kill_and_reap(process),
                    )
                output[name].extend(chunk)
                if len(output[name]) > bounds[name]:
                    return MediaRunResult(
                        (),
                        b"",
                        b"",
                        ExtractionFailure.TOOL_RESOURCE_LIMIT,
                        _kill_and_reap(process),
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return MediaRunResult(
                (), b"", b"", ExtractionFailure.TOOL_TIMEOUT, _kill_and_reap(process)
            )
        process.wait(timeout=remaining)
        return MediaRunResult((), bytes(output["stdout"]), bytes(output["stderr"]), None, True)
    except subprocess.TimeoutExpired:
        return MediaRunResult((), b"", b"", ExtractionFailure.TOOL_TIMEOUT, _kill_and_reap(process))
    finally:
        streams.close()


def _memory_within_limit(process_id: int, limit: int) -> bool:
    if sys.platform != "darwin":
        return True
    resident = _darwin_resident_bytes(process_id)
    return resident is not None and resident <= limit


def _darwin_resident_bytes(process_id: int) -> int | None:
    if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id < 1:
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        function = library.proc_pid_rusage
        function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        function.restype = ctypes.c_int
        info = _DarwinRUsageInfoV2()
        result = int(
            function(
                ctypes.c_int(process_id),
                ctypes.c_int(_RUSAGE_INFO_V2),
                ctypes.byref(info),
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if result != 0:
        return None
    return int(info.ri_resident_size)


def _kill_and_reap(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    return process.poll() is not None


def _staged_assets(
    root: Path,
    limits: MediaLimits,
    *,
    asset_store: MediaAssetStore | None = None,
) -> tuple[tuple[RawAssetRef, ...], ExtractionFailure | None]:
    files: list[Path] = []
    for directory, directories, names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directories:
            if (base / name).is_symlink():
                return (), ExtractionFailure.MALFORMED_TOOL_OUTPUT
        for name in names:
            path = base / name
            if name.endswith(_PARTIAL_SUFFIXES) or path.is_symlink():
                return (), ExtractionFailure.MALFORMED_TOOL_OUTPUT
            try:
                metadata = path.lstat()
            except OSError:
                return (), ExtractionFailure.MALFORMED_TOOL_OUTPUT
            if not stat.S_ISREG(metadata.st_mode) or not path.resolve().is_relative_to(root):
                return (), ExtractionFailure.MALFORMED_TOOL_OUTPUT
            files.append(path)

    if len(files) > limits.max_files:
        return (), ExtractionFailure.MEDIA_LIMIT
    refs: list[RawAssetRef] = []
    total_bytes = 0
    videos = 0
    for path in sorted(files):
        media_type = mimetypes.guess_type(path.name)[0]
        if media_type is None or not (
            media_type.startswith(("audio/", "image/", "video/")) or media_type == "text/vtt"
        ):
            return (), ExtractionFailure.MALFORMED_TOOL_OUTPUT
        if media_type.startswith("video/"):
            videos += 1
            if videos > limits.max_videos:
                return (), ExtractionFailure.MEDIA_LIMIT
        digest, byte_length, failure = _digest_file(path, limits.max_single_file_bytes)
        if failure is not None:
            return (), failure
        total_bytes += byte_length
        if total_bytes > limits.max_total_bytes:
            return (), ExtractionFailure.MEDIA_LIMIT
        expected = RawAssetRef.create(
            asset_id="asset_" + digest,
            sha256=digest,
            media_type=media_type,
            byte_length=byte_length,
        )
        if asset_store is None:
            refs.append(expected)
            continue
        data, read_failure = _read_file(path, limits.max_single_file_bytes)
        if read_failure is not None:
            return (), read_failure
        try:
            stored = asset_store.put(data=data, media_type=media_type)
        except Exception:
            return (), ExtractionFailure.MALFORMED_TOOL_OUTPUT
        if stored != expected:
            return (), ExtractionFailure.MALFORMED_TOOL_OUTPUT
        refs.append(stored)
    return tuple(sorted(refs, key=lambda ref: str(ref.asset_id))), None


def _digest_file(path: Path, max_bytes: int) -> tuple[str, int, ExtractionFailure | None]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    digest = sha256()
    total = 0
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return "", 0, ExtractionFailure.MALFORMED_TOOL_OUTPUT
            while chunk := stream.read(_READ_SIZE):
                total += len(chunk)
                if total >= max_bytes:
                    return "", 0, ExtractionFailure.MEDIA_LIMIT
                digest.update(chunk)
    except OSError:
        return "", 0, ExtractionFailure.MALFORMED_TOOL_OUTPUT
    return digest.hexdigest(), total, None


def _read_file(path: Path, max_bytes: int) -> tuple[bytes, ExtractionFailure | None]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    data = bytearray()
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return b"", ExtractionFailure.MALFORMED_TOOL_OUTPUT
            while chunk := stream.read(_READ_SIZE):
                data.extend(chunk)
                if len(data) > max_bytes:
                    return b"", ExtractionFailure.MEDIA_LIMIT
    except OSError:
        return b"", ExtractionFailure.MALFORMED_TOOL_OUTPUT
    return bytes(data), None


def _failure(failure: ExtractionFailure) -> MediaRunResult:
    return MediaRunResult((), b"", b"", failure, True)
