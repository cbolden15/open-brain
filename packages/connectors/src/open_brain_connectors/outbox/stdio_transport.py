"""Foreground stdio process transport over a deployment-supplied command.

The adapter launches the deployment's ``argv``, which must implement the
``capture-submit --json`` contract: read exactly one JSON request document
from stdin, write exactly one JSON result document to stdout, and exit 0
for a capture, 75 for a retryable admission refusal, 65 for a terminal
admission refusal, 78 for a policy failure, and 2 for a usage error. The
request document carries the delivery's immutable binding (delivery ID,
destination Brain, issuer epoch, tenant, principal, policy reference,
requested tier, and the client's claimed request digest) plus the text
payload; both the written request and the read result are bounded by
``MAX_DOCUMENT_BYTES``, the documented byte cap.

Process mechanics follow the connector worker precedent without importing
its classes and without any thread: ``subprocess.Popen`` with pipes and
``close_fds``, a fresh temporary working directory, an empty environment
plus one explicit per-construction allowlist (nothing else is inherited --
no credentials, no ``PATH``), rlimits applied in the child, one bounded
``selectors`` exchange with per-stream byte caps and a wall-clock deadline,
and a SIGKILL to the process group on timeout or over-limit output.

Exit-code mapping: 0 with a parseable receipt is a ``TerminalReceipt``; 75
is a retryable ``DeliveryFailure`` under the reported code; 65 is a
terminal failure under the reported code; 78 is terminal
``policy_mismatch``; 2 is terminal ``transport_misuse``; a timeout, a
malformed or over-limit result, an unknown exit code, or a command that
cannot start is a retryable ``transport_error``. Every outcome reaches the
drain, which classifies it; the transport never drops a body.
"""

from __future__ import annotations

import json
import os
import resource
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from tempfile import TemporaryDirectory
from typing import Any

from .contracts import DeliveryEnvelope, OutboxContractError, TerminalReceipt
from .destination import (
    POLICY_MISMATCH,
    TRANSPORT_ERROR,
    TRANSPORT_MISUSE,
    terminal_receipt_from_result_document,
)
from .drain import DeliveryFailure
from .transport import OutboxTransportError

__all__ = [
    "MAX_DOCUMENT_BYTES",
    "OutboxTransportError",
    "StdioProcessTransport",
]

MAX_DOCUMENT_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_STDERR_BYTES = 8192
DEFAULT_CPU_SECONDS = 60
DEFAULT_MAX_PROCESSES = 64
_READ_SIZE = 65536


class StdioProcessTransport:
    """One foreground stdio destination command driven per delivery."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_stdout_bytes: int = MAX_DOCUMENT_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
        environment_allowlist: Sequence[str] = (),
        cpu_seconds: int = DEFAULT_CPU_SECONDS,
        max_processes: int = DEFAULT_MAX_PROCESSES,
    ) -> None:
        if (
            not isinstance(argv, Sequence)
            or not argv
            or not all(isinstance(part, str) and part for part in argv)
        ):
            raise OutboxTransportError("invalid stdio command")
        if not isinstance(timeout_seconds, float | int) or timeout_seconds <= 0:
            raise OutboxTransportError("invalid stdio timeout")
        if type(max_stdout_bytes) is not int or max_stdout_bytes < 1:
            raise OutboxTransportError("invalid stdio stdout bound")
        if type(max_stderr_bytes) is not int or max_stderr_bytes < 1:
            raise OutboxTransportError("invalid stdio stderr bound")
        if not all(
            isinstance(name, str)
            and name
            and "=" not in name
            and all(33 <= ord(character) <= 126 for character in name)
            for name in environment_allowlist
        ):
            raise OutboxTransportError("invalid stdio environment allowlist")
        if type(cpu_seconds) is not int or cpu_seconds < 1:
            raise OutboxTransportError("invalid stdio cpu bound")
        if type(max_processes) is not int or max_processes < 1:
            raise OutboxTransportError("invalid stdio process bound")
        self._argv = tuple(argv)
        self._timeout_seconds = float(timeout_seconds)
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._environment_allowlist = tuple(environment_allowlist)
        self._cpu_seconds = cpu_seconds
        self._max_processes = max_processes

    def __call__(self, envelope: DeliveryEnvelope, /) -> TerminalReceipt | DeliveryFailure:
        if not isinstance(envelope, DeliveryEnvelope):
            raise OutboxTransportError("invalid delivery envelope")
        request = _request_document(envelope)
        environment = {
            name: os.environ[name] for name in self._environment_allowlist if name in os.environ
        }
        with TemporaryDirectory(prefix="open-brain-outbox-stdio-") as working_directory:
            try:
                process = subprocess.Popen[bytes](
                    self._argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=working_directory,
                    env=environment,
                    close_fds=True,
                    start_new_session=True,
                    preexec_fn=_apply_limits(self._cpu_seconds, self._max_processes),
                    text=False,
                )
            except OSError, subprocess.SubprocessError:
                return DeliveryFailure(code=TRANSPORT_ERROR, retryable=True)
            stdout, returncode = self._exchange(process, request)
        return _map_exit_code(returncode, stdout)

    def _exchange(
        self, process: subprocess.Popen[bytes], request: bytes
    ) -> tuple[bytes, int | None]:
        """Drain both streams under byte caps and a deadline; kill on any breach."""
        if process.stdin is None or process.stdout is None or process.stderr is None:
            _kill_process_group(process)
            return b"", None
        try:
            process.stdin.write(request)
            process.stdin.close()
        except BrokenPipeError, OSError:
            _kill_process_group(process)
            return b"", None
        streams = selectors.DefaultSelector()
        streams.register(process.stdout, selectors.EVENT_READ, "stdout")
        streams.register(process.stderr, selectors.EVENT_READ, "stderr")
        output = {"stdout": bytearray(), "stderr": bytearray()}
        bounds = {"stdout": self._max_stdout_bytes, "stderr": self._max_stderr_bytes}
        deadline = time.monotonic() + self._timeout_seconds
        breached = False
        try:
            while streams.get_map() and not breached:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    breached = True
                    break
                for key, _mask in streams.select(min(remaining, 0.05)):
                    chunk = os.read(key.fd, _READ_SIZE)
                    if not chunk:
                        streams.unregister(key.fileobj)
                        continue
                    name = key.data
                    if not isinstance(name, str):
                        breached = True
                        break
                    output[name].extend(chunk)
                    if len(output[name]) > bounds[name]:
                        breached = True
                        break
            if breached:
                _kill_process_group(process)
                return b"", None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                return b"", None
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
                return b"", None
        finally:
            streams.close()
        return bytes(output["stdout"]), process.returncode


def _request_document(envelope: DeliveryEnvelope) -> bytes:
    """One canonical JSON request line for the capture-submit contract."""
    payload = envelope.payload
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"family", "text"}
        or payload["family"] != "text"
        or not isinstance(payload["text"], str)
    ):
        raise OutboxTransportError("invalid envelope payload family")
    document = {
        "delivery_id": envelope.delivery_id,
        "destination_brain_id": envelope.destination_brain_id,
        "issuer_epoch": envelope.expected_issuer_epoch,
        "tenant_id": envelope.tenant_id,
        "principal_id": envelope.principal_id,
        "policy_ref": envelope.policy_ref,
        "requested_tier": envelope.requested_tier.value,
        "request_digest": envelope.request_digest,
        "payload": {"family": "text", "text": payload["text"]},
    }
    data = (
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )
    if len(data) > MAX_DOCUMENT_BYTES:
        raise OutboxTransportError("stdio request exceeds the document byte cap")
    return data


def _apply_limits(cpu_seconds: int, max_processes: int) -> Callable[[], None]:
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))

    return apply


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


def _parse_success_document(stdout: bytes) -> TerminalReceipt | None:
    try:
        document = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object)
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        return None
    try:
        return terminal_receipt_from_result_document(document)
    except OutboxContractError:
        return None


def _reported_failure_code(stdout: bytes) -> str | None:
    try:
        document = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object)
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        return None
    if not isinstance(document, dict):
        return None
    error = document.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if (
        not isinstance(code, str)
        or not code
        or len(code) > 256
        or any(ord(character) < 33 or ord(character) == 127 for character in code)
    ):
        return None
    return code


def _map_exit_code(returncode: int | None, stdout: bytes) -> TerminalReceipt | DeliveryFailure:
    if returncode == 0:
        receipt = _parse_success_document(stdout)
        if receipt is not None:
            return receipt
        return DeliveryFailure(code=TRANSPORT_ERROR, retryable=True)
    if returncode in (65, 75):
        reported = _reported_failure_code(stdout)
        return DeliveryFailure(
            code=reported if reported is not None else TRANSPORT_ERROR,
            retryable=returncode == 75,
        )
    if returncode == 78:
        return DeliveryFailure(code=POLICY_MISMATCH, retryable=False)
    if returncode == 2:
        return DeliveryFailure(code=TRANSPORT_MISUSE, retryable=False)
    return DeliveryFailure(code=TRANSPORT_ERROR, retryable=True)
