"""Owner-selected foreground adapter for independent receipt protection."""

from __future__ import annotations

import json
import os
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import (
    ProtectionAcknowledgement,
    ReceiptProtectionError,
    ReceiptProtectionPort,
    ReceiptProtectionRequest,
)

_CONFIG_VERSION = "receipt-protection-command.v1"
_CONFIG_KEYS = frozenset({"contract_version", "argv", "timeout_seconds"})
_MAX_CONFIG_BYTES = 65_536
_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_STDOUT_BYTES = 65_536
_MAX_STDERR_BYTES = 8_192
_READ_SIZE = 65_536


class ReceiptProtectionConfigurationError(ValueError):
    """An owner-selected protection command is not safe to execute."""


@dataclass(frozen=True, slots=True)
class ReceiptProtectionConfiguration:
    port: ReceiptProtectionPort
    timeout_seconds: float


class SubprocessReceiptProtectionPort:
    """Run one bounded protector process for each exact replay commitment."""

    def __init__(self, argv: Sequence[str]) -> None:
        if (
            not isinstance(argv, Sequence)
            or not argv
            or not all(isinstance(part, str) and part for part in argv)
            or not Path(argv[0]).is_absolute()
        ):
            raise ReceiptProtectionConfigurationError("invalid receipt protection command")
        self._argv = tuple(argv)

    def protect(
        self,
        request: ReceiptProtectionRequest,
        *,
        timeout_seconds: float,
    ) -> ProtectionAcknowledgement:
        if not isinstance(request, ReceiptProtectionRequest):
            raise ReceiptProtectionError("protection_unavailable")
        if type(timeout_seconds) not in (float, int) or timeout_seconds <= 0:
            raise ReceiptProtectionError("protection_unavailable")
        body = portable_canonical_json_bytes(request.to_dict()) + b"\n"
        if len(body) > _MAX_REQUEST_BYTES:
            raise ReceiptProtectionError("protection_unavailable")
        with TemporaryDirectory(prefix="open-brain-receipt-protection-") as working_directory:
            try:
                process = subprocess.Popen[bytes](
                    self._argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=working_directory,
                    env={},
                    close_fds=True,
                    start_new_session=True,
                    text=False,
                )
            except OSError, subprocess.SubprocessError:
                raise ReceiptProtectionError("protection_unavailable") from None
            stdout, returncode, timed_out = _exchange(
                process,
                body,
                timeout_seconds=float(timeout_seconds),
            )
        if timed_out:
            raise ReceiptProtectionError("protection_timeout")
        if returncode != 0:
            raise ReceiptProtectionError("protection_unavailable")
        try:
            document = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object)
            return ProtectionAcknowledgement.from_dict(document)
        except UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError:
            raise ReceiptProtectionError("protection_invalid_acknowledgement") from None


def load_receipt_protection_configuration(path: str | Path) -> ReceiptProtectionConfiguration:
    location = Path(path)
    if not location.is_absolute():
        raise ReceiptProtectionConfigurationError(
            "receipt protection configuration path must be absolute"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(location, flags)
    except OSError:
        raise ReceiptProtectionConfigurationError(
            "receipt protection configuration is unreadable"
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _MAX_CONFIG_BYTES
        ):
            raise ReceiptProtectionConfigurationError(
                "receipt protection configuration must be an owner-only regular file"
            )
        chunks: list[bytes] = []
        remaining = _MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, _READ_SIZE))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if not raw or len(raw) > _MAX_CONFIG_BYTES:
            raise ReceiptProtectionConfigurationError(
                "receipt protection configuration is invalid"
            )
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise ReceiptProtectionConfigurationError(
            "receipt protection configuration is invalid"
        ) from None
    if (
        not isinstance(document, dict)
        or set(document) != _CONFIG_KEYS
        or document["contract_version"] != _CONFIG_VERSION
        or not isinstance(document["argv"], list)
        or not document["argv"]
        or not all(isinstance(part, str) and part for part in document["argv"])
        or not Path(document["argv"][0]).is_absolute()
        or type(document["timeout_seconds"]) not in (int, float)
        or not 0 < document["timeout_seconds"] <= 60
    ):
        raise ReceiptProtectionConfigurationError("receipt protection configuration is invalid")
    return ReceiptProtectionConfiguration(
        port=SubprocessReceiptProtectionPort(document["argv"]),
        timeout_seconds=float(document["timeout_seconds"]),
    )


def _exchange(
    process: subprocess.Popen[bytes],
    request: bytes,
    *,
    timeout_seconds: float,
) -> tuple[bytes, int | None, bool]:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        return b"", None, False
    streams = selectors.DefaultSelector()
    for stream in (process.stdin, process.stdout, process.stderr):
        os.set_blocking(stream.fileno(), False)
    streams.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    bounds = {"stdout": _MAX_STDOUT_BYTES, "stderr": _MAX_STDERR_BYTES}
    request_offset = 0
    deadline = time.monotonic() + timeout_seconds
    breached = False
    timed_out = False
    try:
        while streams.get_map() and not breached:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, mask in streams.select(min(remaining, 0.05)):
                name = key.data
                if name == "stdin" and mask & selectors.EVENT_WRITE:
                    try:
                        written = os.write(
                            key.fd,
                            request[request_offset : request_offset + _READ_SIZE],
                        )
                    except BrokenPipeError, OSError:
                        written = 0
                        request_offset = len(request)
                    else:
                        request_offset += written
                    if request_offset >= len(request):
                        streams.unregister(key.fileobj)
                        process.stdin.close()
                    continue
                if name not in output or not mask & selectors.EVENT_READ:
                    breached = True
                    break
                try:
                    chunk = os.read(key.fd, _READ_SIZE)
                except OSError:
                    chunk = b""
                if not chunk:
                    streams.unregister(key.fileobj)
                    continue
                output[name].extend(chunk)
                if len(output[name]) > bounds[name]:
                    breached = True
                    break
        if breached or timed_out:
            _kill_process_group(process)
            return b"", None, timed_out
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(process)
            return b"", None, True
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            return b"", None, True
    finally:
        streams.close()
    return bytes(output["stdout"]), process.returncode, False


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


__all__ = [
    "ReceiptProtectionConfiguration",
    "ReceiptProtectionConfigurationError",
    "SubprocessReceiptProtectionPort",
    "load_receipt_protection_configuration",
]
