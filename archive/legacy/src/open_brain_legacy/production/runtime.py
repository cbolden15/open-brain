from __future__ import annotations

import math
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
from contextlib import suppress
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import IO, Protocol

from open_brain_engine.core.models import PrivacyDecision, RawAssetRef
from open_brain_engine.core.ports import StagedExecutionRequest, StagedExecutionResult

from .errors import ProductionRuntimeError, RuntimeFailureCode

_READ_SIZE = 64 * 1024


class AssetReader(Protocol):
    """Reads only the explicitly named raw assets for a staged execution."""

    def read(self, asset: RawAssetRef) -> bytes: ...


class _BoundedProcess(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdout(self) -> IO[bytes] | None: ...

    @property
    def stderr(self) -> IO[bytes] | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    wall_seconds: float = 60.0
    cpu_seconds: int = 30
    memory_bytes: int = 512 * 1024 * 1024
    max_processes: int = 4
    max_stdout_bytes: int = 64 * 1024
    max_stderr_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        integers = (
            self.cpu_seconds,
            self.memory_bytes,
            self.max_processes,
            self.max_stdout_bytes,
            self.max_stderr_bytes,
        )
        if (
            not isinstance(self.wall_seconds, int | float)
            or isinstance(self.wall_seconds, bool)
            or not math.isfinite(self.wall_seconds)
            or self.wall_seconds <= 0
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in integers
            )
        ):
            raise ValueError("invalid runtime limits")


DEFAULT_RUNTIME_LIMITS = RuntimeLimits()


class StagedLocalModelRuntime:
    """A bubblewrap-backed local model runner with no network or inherited credentials.

    The configured command must be a static executable that reads
    ``OPEN_BRAIN_PROMPT_PATH`` and ``OPEN_BRAIN_ASSET_DIR`` inside the sandbox.
    Non-empty network authority is rejected because this runtime has no verified
    host-filtering proxy; that is a deliberate narrowing of the executor contract.
    """

    def __init__(
        self,
        *,
        command: tuple[str, ...],
        asset_reader: AssetReader,
        enabled: bool = False,
        limits: RuntimeLimits = DEFAULT_RUNTIME_LIMITS,
        bubblewrap_path: str = "bwrap",
    ) -> None:
        if (
            not isinstance(command, tuple)
            or not command
            or any(not isinstance(value, str) or not value or "\x00" in value for value in command)
            or not Path(command[0]).is_absolute()
            or not isinstance(enabled, bool)
            or not isinstance(limits, RuntimeLimits)
            or not isinstance(bubblewrap_path, str)
            or not bubblewrap_path
            or "\x00" in bubblewrap_path
        ):
            raise ValueError("invalid staged runtime configuration")
        self._command = command
        self._asset_reader = asset_reader
        self._enabled = enabled
        self._limits = limits
        self._bubblewrap_path = bubblewrap_path

    def execute(
        self, request: StagedExecutionRequest, *, privacy: PrivacyDecision
    ) -> StagedExecutionResult:
        if not self._enabled:
            raise ProductionRuntimeError(RuntimeFailureCode.DISABLED)
        _validate_execution(request, privacy)
        bubblewrap = _find_bubblewrap(self._bubblewrap_path)
        if request.allowed_network_hosts or not _runtime_controls_supported() or bubblewrap is None:
            raise ProductionRuntimeError(RuntimeFailureCode.UNSUPPORTED_CONTROL)
        limits = replace(
            self._limits,
            wall_seconds=min(self._limits.wall_seconds, float(request.timeout_seconds)),
            max_stdout_bytes=min(self._limits.max_stdout_bytes, request.max_output_bytes),
        )
        with tempfile.TemporaryDirectory(prefix="open-brain-runtime-") as raw_stage:
            stage = Path(raw_stage)
            _write_stage(stage, request, self._asset_reader)
            output = _run_sandboxed(self._sandbox_command(stage, bubblewrap), stage, limits)
        try:
            text = output.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProductionRuntimeError(RuntimeFailureCode.EXECUTION_FAILED) from error
        if len(output) > request.max_output_bytes:
            raise ProductionRuntimeError(RuntimeFailureCode.OUTPUT_LIMIT)
        return StagedExecutionResult(text=text, produced_assets=())

    def _sandbox_command(self, stage: Path, bubblewrap: str | None = None) -> tuple[str, ...]:
        executable = self._command[0]
        return (
            self._bubblewrap_path if bubblewrap is None else bubblewrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--clearenv",
            "--tmpfs",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--dir",
            "/work",
            "--ro-bind",
            str(stage),
            "/input",
            "--ro-bind",
            executable,
            "/model",
            "--setenv",
            "OPEN_BRAIN_PROMPT_PATH",
            "/input/prompt.txt",
            "--setenv",
            "OPEN_BRAIN_ASSET_DIR",
            "/input/assets",
            "--chdir",
            "/work",
            "--",
            "/model",
            *self._command[1:],
        )


def _validate_execution(request: StagedExecutionRequest, privacy: PrivacyDecision) -> None:
    if not isinstance(request, StagedExecutionRequest) or not isinstance(privacy, PrivacyDecision):
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
    if (
        not privacy.authority.cloud
        or not isinstance(request.request_id, str)
        or not isinstance(request.purpose, str)
        or not isinstance(request.prompt, str)
        or not isinstance(request.readable_assets, tuple)
        or any(not isinstance(asset, RawAssetRef) for asset in request.readable_assets)
        or not isinstance(request.allowed_network_hosts, tuple)
        or any(not isinstance(host, str) or not host for host in request.allowed_network_hosts)
        or not isinstance(request.timeout_seconds, (int, float))
        or isinstance(request.timeout_seconds, bool)
        or not math.isfinite(request.timeout_seconds)
        or request.timeout_seconds <= 0
        or not isinstance(request.max_output_bytes, int)
        or isinstance(request.max_output_bytes, bool)
        or request.max_output_bytes < 1
    ):
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)


def _runtime_controls_supported() -> bool:
    return sys.platform.startswith("linux") and all(
        hasattr(resource, name)
        for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NPROC")
    )


def _write_stage(stage: Path, request: StagedExecutionRequest, reader: AssetReader) -> None:
    assets = stage / "assets"
    assets.mkdir(mode=0o700)
    _write_private_file(stage / "prompt.txt", request.prompt.encode("utf-8"))
    seen: set[str] = set()
    for asset in request.readable_assets:
        if asset.sha256 in seen:
            raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
        seen.add(asset.sha256)
        try:
            data = reader.read(asset)
        except Exception as error:
            raise ProductionRuntimeError(RuntimeFailureCode.INTEGRITY) from error
        if (
            not isinstance(data, bytes)
            or len(data) != asset.byte_length
            or sha256(data).hexdigest() != asset.sha256
        ):
            raise ProductionRuntimeError(RuntimeFailureCode.INTEGRITY)
        _write_private_file(assets / asset.sha256, data)


def _write_private_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow(), 0o400)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT)
    except Exception:
        with suppress(OSError):
            path.unlink()
        raise


def _run_sandboxed(command: tuple[str, ...], stage: Path, limits: RuntimeLimits) -> bytes:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=stage,
            env={},
            close_fds=True,
            start_new_session=True,
            preexec_fn=_apply_limits(limits),
            text=False,
        )
        stdout, failure = _bounded_output(process, limits)
        if failure is not None:
            raise ProductionRuntimeError(failure)
        if process.returncode != 0:
            raise ProductionRuntimeError(RuntimeFailureCode.EXECUTION_FAILED)
        return stdout
    except ProductionRuntimeError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise ProductionRuntimeError(RuntimeFailureCode.EXECUTION_FAILED) from error
    finally:
        if process is not None and process.poll() is None:
            _kill_process_group(process)


def _apply_limits(limits: RuntimeLimits) -> Callable[[], None]:
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_processes, limits.max_processes))
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (limits.max_stdout_bytes, limits.max_stdout_bytes),
        )

    return apply


def _bounded_output(
    process: _BoundedProcess, limits: RuntimeLimits
) -> tuple[bytes, RuntimeFailureCode | None]:
    if process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        return b"", RuntimeFailureCode.EXECUTION_FAILED
    streams = selectors.DefaultSelector()
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    limits_by_stream = {"stdout": limits.max_stdout_bytes, "stderr": limits.max_stderr_bytes}
    deadline = time.monotonic() + limits.wall_seconds
    try:
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                return b"", RuntimeFailureCode.TIMEOUT
            for key, _ in streams.select(min(remaining, 0.05)):
                chunk = os.read(key.fd, _READ_SIZE)
                if not chunk:
                    streams.unregister(key.fileobj)
                    continue
                name = key.data
                if not isinstance(name, str):
                    _kill_process_group(process)
                    return b"", RuntimeFailureCode.EXECUTION_FAILED
                output[name].extend(chunk)
                if len(output[name]) > limits_by_stream[name]:
                    _kill_process_group(process)
                    return b"", RuntimeFailureCode.OUTPUT_LIMIT
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(process)
            return b"", RuntimeFailureCode.TIMEOUT
        process.wait(timeout=remaining)
        return bytes(output["stdout"]), None
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        return b"", RuntimeFailureCode.TIMEOUT
    finally:
        streams.close()


def _kill_process_group(process: object) -> None:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        kill = getattr(process, "kill", None)
        if callable(kill):
            kill()
    wait = getattr(process, "wait", None)
    if callable(wait):
        try:
            wait(timeout=1)
        except subprocess.TimeoutExpired:
            kill = getattr(process, "kill", None)
            if callable(kill):
                kill()
                wait()


def _no_follow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _find_bubblewrap(value: str) -> str | None:
    if os.path.isabs(value):
        return value if os.path.isfile(value) and os.access(value, os.X_OK) else None
    return shutil.which(value)
