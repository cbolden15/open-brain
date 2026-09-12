"""Bounded process protocol for provisional connector conformance."""

from __future__ import annotations

import json
import os
import re
import resource
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from importlib.metadata import entry_points
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, cast

from open_brain_connectors.runtime.connectors import (
    CONNECTOR_ENTRY_POINT_GROUP,
    ConnectorBudgetLimits,
    ConnectorCapabilityPolicy,
    ConnectorEntryPoint,
    ConnectorEntryPointMetadata,
    ConnectorManifest,
    ConnectorProfile,
    ConnectorRegistry,
    ConnectorRunReceipt,
)

WORKER_PROTOCOL_VERSION = 1

_DIGEST = re.compile(r"[0-9a-f]{64}")
_ENTRY_POINT_VALUE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.]*(?::[A-Za-z_][A-Za-z0-9_]*)?"
)
_INVOCATION_ID = re.compile(r"inv_[0-9a-f]{64}")
_MAX_MESSAGE_BYTES = 64 * 1024
_READ_SIZE = 16 * 1024
_DIRECT_NETWORK_ATTEMPTS = 0

__all__ = [
    "WORKER_PROTOCOL_VERSION",
    "ConnectorConformancePlugin",
    "ConnectorNetworkMode",
    "ConnectorWorkerError",
    "ConnectorWorkerFailureCode",
    "ConnectorWorkerHost",
    "ConnectorWorkerLimits",
    "ConnectorWorkerProtocolError",
    "ConnectorWorkerReceipt",
    "ConnectorWorkerRequest",
    "connector_manifest_sha256",
]


class ConnectorWorkerProtocolError(ValueError):
    """A worker message does not match the strict versioned schema."""


class ConnectorWorkerFailureCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    NOT_ALLOWED = "not_allowed"
    NOT_DISCOVERED = "not_discovered"
    MANIFEST_MISMATCH = "manifest_mismatch"
    CAPABILITY_DENIED = "capability_denied"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    PROCESS_FAILED = "process_failed"
    INVALID_RECEIPT = "invalid_receipt"


class ConnectorWorkerError(RuntimeError):
    """Bounded worker failure with no child payload or raw error text."""

    def __init__(self, code: ConnectorWorkerFailureCode) -> None:
        self.code = ConnectorWorkerFailureCode(code)
        super().__init__(self.code.value)


class ConnectorNetworkMode(StrEnum):
    NONE = "none"
    HOST_MEDIATED = "host_mediated"


@dataclass(frozen=True, slots=True)
class ConnectorWorkerLimits:
    wall_seconds: float = 10.0
    cpu_seconds: int = 5
    memory_bytes: int = 512 * 1024 * 1024
    max_processes: int = 4
    max_stdout_bytes: int = _MAX_MESSAGE_BYTES
    max_stderr_bytes: int = 8 * 1024

    def __post_init__(self) -> None:
        integer_limits = (
            self.cpu_seconds,
            self.memory_bytes,
            self.max_processes,
            self.max_stdout_bytes,
            self.max_stderr_bytes,
        )
        if (
            not isinstance(self.wall_seconds, int | float)
            or isinstance(self.wall_seconds, bool)
            or not 0 < self.wall_seconds <= 60
            or any(type(value) is not int or value < 1 for value in integer_limits)
            or self.max_stdout_bytes > _MAX_MESSAGE_BYTES
            or self.max_stderr_bytes > _MAX_MESSAGE_BYTES
        ):
            raise ConnectorWorkerProtocolError("invalid worker limits")


@dataclass(frozen=True, slots=True)
class ConnectorWorkerRequest:
    schema_version: int
    invocation_id: str
    connector_name: str
    entry_point_value: str
    manifest: ConnectorManifest
    budget_limits: ConnectorBudgetLimits
    network_mode: ConnectorNetworkMode

    def __post_init__(self) -> None:
        if (
            type(self.manifest) is not ConnectorManifest
            or type(self.budget_limits) is not ConnectorBudgetLimits
        ):
            raise ConnectorWorkerProtocolError("invalid worker request")
        try:
            network_mode = ConnectorNetworkMode(self.network_mode)
            manifest = ConnectorManifest.from_dict(self.manifest.to_dict())
            budget_limits = ConnectorBudgetLimits(
                max_discoveries=self.budget_limits.max_discoveries,
                max_fetches=self.budget_limits.max_fetches,
                max_extractions=self.budget_limits.max_extractions,
                max_submissions=self.budget_limits.max_submissions,
            )
        except (TypeError, ValueError) as error:
            raise ConnectorWorkerProtocolError("invalid worker request") from error
        if (
            type(self.schema_version) is not int
            or self.schema_version != WORKER_PROTOCOL_VERSION
            or not isinstance(self.invocation_id, str)
            or _INVOCATION_ID.fullmatch(self.invocation_id) is None
            or not isinstance(self.connector_name, str)
            or self.connector_name != manifest.name
            or not isinstance(self.entry_point_value, str)
            or _ENTRY_POINT_VALUE.fullmatch(self.entry_point_value) is None
            or len(self.entry_point_value) > 512
            or (
                manifest.external_egress
                and network_mode is not ConnectorNetworkMode.HOST_MEDIATED
            )
            or (
                not manifest.external_egress
                and network_mode is not ConnectorNetworkMode.NONE
            )
        ):
            raise ConnectorWorkerProtocolError("invalid worker request")
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "budget_limits", budget_limits)
        object.__setattr__(self, "network_mode", network_mode)

    @property
    def manifest_sha256(self) -> str:
        return connector_manifest_sha256(self.manifest)

    def to_dict(self) -> dict[str, object]:
        return {
            "budget_limits": {
                "max_discoveries": self.budget_limits.max_discoveries,
                "max_extractions": self.budget_limits.max_extractions,
                "max_fetches": self.budget_limits.max_fetches,
                "max_submissions": self.budget_limits.max_submissions,
            },
            "connector_name": self.connector_name,
            "entry_point_value": self.entry_point_value,
            "invocation_id": self.invocation_id,
            "manifest": self.manifest.to_dict(),
            "network_mode": self.network_mode.value,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> ConnectorWorkerRequest:
        expected = {
            "budget_limits",
            "connector_name",
            "entry_point_value",
            "invocation_id",
            "manifest",
            "network_mode",
            "schema_version",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ConnectorWorkerProtocolError("invalid worker request")
        budget = value["budget_limits"]
        if not isinstance(budget, dict) or set(budget) != {
            "max_discoveries",
            "max_extractions",
            "max_fetches",
            "max_submissions",
        }:
            raise ConnectorWorkerProtocolError("invalid worker request")
        try:
            return cls(
                schema_version=cast(int, value["schema_version"]),
                invocation_id=cast(str, value["invocation_id"]),
                connector_name=cast(str, value["connector_name"]),
                entry_point_value=cast(str, value["entry_point_value"]),
                manifest=ConnectorManifest.from_dict(value["manifest"]),
                budget_limits=ConnectorBudgetLimits(
                    max_discoveries=cast(int, budget["max_discoveries"]),
                    max_fetches=cast(int, budget["max_fetches"]),
                    max_extractions=cast(int, budget["max_extractions"]),
                    max_submissions=cast(int, budget["max_submissions"]),
                ),
                network_mode=cast(ConnectorNetworkMode, value["network_mode"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ConnectorWorkerProtocolError("invalid worker request") from error


@dataclass(frozen=True, slots=True)
class ConnectorWorkerReceipt:
    schema_version: int
    invocation_id: str
    connector_name: str
    manifest_sha256: str
    first_run: ConnectorRunReceipt
    replay_run: ConnectorRunReceipt
    checkpoint_receipt_sha256: str
    capture_count: int
    direct_network_attempts: int

    def __post_init__(self) -> None:
        if (
            type(self.first_run) is not ConnectorRunReceipt
            or type(self.replay_run) is not ConnectorRunReceipt
        ):
            raise ConnectorWorkerProtocolError("invalid worker receipt")
        try:
            first_run = ConnectorRunReceipt.from_dict(self.first_run.to_dict())
            replay_run = ConnectorRunReceipt.from_dict(self.replay_run.to_dict())
        except (TypeError, ValueError) as error:
            raise ConnectorWorkerProtocolError("invalid worker receipt") from error
        if (
            type(self.schema_version) is not int
            or self.schema_version != WORKER_PROTOCOL_VERSION
            or not isinstance(self.invocation_id, str)
            or _INVOCATION_ID.fullmatch(self.invocation_id) is None
            or not isinstance(self.connector_name, str)
            or not isinstance(self.manifest_sha256, str)
            or _DIGEST.fullmatch(self.manifest_sha256) is None
            or first_run.connector_name != self.connector_name
            or replay_run.connector_name != self.connector_name
            or not isinstance(self.checkpoint_receipt_sha256, str)
            or _DIGEST.fullmatch(self.checkpoint_receipt_sha256) is None
            or type(self.capture_count) is not int
            or not 0 <= self.capture_count <= 1_000
            or type(self.direct_network_attempts) is not int
            or self.direct_network_attempts != 0
            or first_run.stubbed_count > first_run.extracted_count
            or replay_run.stubbed_count > replay_run.extracted_count
            or replay_run.submitted_count != 0
            or self.capture_count
            != first_run.created_count + replay_run.created_count
        ):
            raise ConnectorWorkerProtocolError("invalid worker receipt")
        object.__setattr__(self, "first_run", first_run)
        object.__setattr__(self, "replay_run", replay_run)

    def to_dict(self) -> dict[str, object]:
        return {
            "capture_count": self.capture_count,
            "checkpoint_receipt_sha256": self.checkpoint_receipt_sha256,
            "connector_name": self.connector_name,
            "direct_network_attempts": self.direct_network_attempts,
            "first_run": self.first_run.to_dict(),
            "invocation_id": self.invocation_id,
            "manifest_sha256": self.manifest_sha256,
            "replay_run": self.replay_run.to_dict(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> ConnectorWorkerReceipt:
        expected = {
            "capture_count",
            "checkpoint_receipt_sha256",
            "connector_name",
            "direct_network_attempts",
            "first_run",
            "invocation_id",
            "manifest_sha256",
            "replay_run",
            "schema_version",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ConnectorWorkerProtocolError("invalid worker receipt")
        try:
            return cls(
                schema_version=cast(int, value["schema_version"]),
                invocation_id=cast(str, value["invocation_id"]),
                connector_name=cast(str, value["connector_name"]),
                manifest_sha256=cast(str, value["manifest_sha256"]),
                first_run=ConnectorRunReceipt.from_dict(value["first_run"]),
                replay_run=ConnectorRunReceipt.from_dict(value["replay_run"]),
                checkpoint_receipt_sha256=cast(
                    str, value["checkpoint_receipt_sha256"]
                ),
                capture_count=cast(int, value["capture_count"]),
                direct_network_attempts=cast(int, value["direct_network_attempts"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ConnectorWorkerProtocolError("invalid worker receipt") from error


class ConnectorConformancePlugin(Protocol):
    @property
    def manifest(self) -> ConnectorManifest: ...

    def conformance(self, request: ConnectorWorkerRequest) -> ConnectorWorkerReceipt: ...


class ConnectorWorkerHost:
    """Load connector code only in a bounded child after explicit approval."""

    def __init__(
        self,
        *,
        capability_policy: ConnectorCapabilityPolicy | None = None,
    ) -> None:
        policy = ConnectorCapabilityPolicy() if capability_policy is None else capability_policy
        if type(policy) is not ConnectorCapabilityPolicy:
            raise ConnectorWorkerProtocolError("invalid worker host")
        try:
            self._capability_policy = ConnectorCapabilityPolicy(
                payloads=frozenset(policy.payloads),
                schedules=frozenset(policy.schedules),
                secrets=frozenset(policy.secrets),
                action_authorities=frozenset(policy.action_authorities),
                external_egress=policy.external_egress,
            )
        except (TypeError, ValueError) as error:
            raise ConnectorWorkerProtocolError("invalid worker host") from error

    def discover(self, profile: ConnectorProfile) -> tuple[ConnectorEntryPointMetadata, ...]:
        return ConnectorRegistry().discover(profile)

    def run_conformance(
        self,
        connector_name: str,
        *,
        profile: ConnectorProfile,
        expected_manifest: ConnectorManifest,
        limits: ConnectorWorkerLimits | None = None,
    ) -> ConnectorWorkerReceipt:
        if (
            type(profile) is not ConnectorProfile
            or type(expected_manifest) is not ConnectorManifest
        ):
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_ALLOWED)
        raw_limits = ConnectorWorkerLimits() if limits is None else limits
        if type(raw_limits) is not ConnectorWorkerLimits:
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_ALLOWED)
        try:
            selected_profile = ConnectorProfile(
                allow_list=tuple(profile.allow_list),
                egress_enabled=profile.egress_enabled,
                budget_limits=ConnectorBudgetLimits(
                    max_discoveries=profile.budget_limits.max_discoveries,
                    max_fetches=profile.budget_limits.max_fetches,
                    max_extractions=profile.budget_limits.max_extractions,
                    max_submissions=profile.budget_limits.max_submissions,
                ),
            )
            selected_manifest = ConnectorManifest.from_dict(expected_manifest.to_dict())
            selected_limits = ConnectorWorkerLimits(
                wall_seconds=raw_limits.wall_seconds,
                cpu_seconds=raw_limits.cpu_seconds,
                memory_bytes=raw_limits.memory_bytes,
                max_processes=raw_limits.max_processes,
                max_stdout_bytes=raw_limits.max_stdout_bytes,
                max_stderr_bytes=raw_limits.max_stderr_bytes,
            )
        except (TypeError, ValueError) as error:
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_ALLOWED) from error
        if not selected_profile.allows(connector_name):
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_ALLOWED)
        if not selected_profile.egress_enabled:
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.CAPABILITY_DENIED)
        try:
            self._capability_policy.validate(selected_manifest)
            discovered = self.discover(selected_profile)
        except Exception as error:
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.CAPABILITY_DENIED) from error
        selected = next((item for item in discovered if item.name == connector_name), None)
        if selected is None:
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_DISCOVERED)
        seed = _canonical_json_bytes(
            {
                "budget_limits": {
                    "max_discoveries": selected_profile.budget_limits.max_discoveries,
                    "max_extractions": selected_profile.budget_limits.max_extractions,
                    "max_fetches": selected_profile.budget_limits.max_fetches,
                    "max_submissions": selected_profile.budget_limits.max_submissions,
                },
                "connector_name": connector_name,
                "entry_point_value": selected.value,
                "manifest": selected_manifest.to_dict(),
            }
        )
        request = ConnectorWorkerRequest(
            schema_version=WORKER_PROTOCOL_VERSION,
            invocation_id="inv_" + sha256(seed).hexdigest(),
            connector_name=connector_name,
            entry_point_value=selected.value,
            manifest=selected_manifest,
            budget_limits=selected_profile.budget_limits,
            network_mode=(
                ConnectorNetworkMode.HOST_MEDIATED
                if selected_manifest.external_egress
                else ConnectorNetworkMode.NONE
            ),
        )
        receipt = _run_worker_process(request, selected_limits)
        if (
            receipt.invocation_id != request.invocation_id
            or receipt.connector_name != request.connector_name
            or receipt.manifest_sha256 != request.manifest_sha256
            or not _receipt_within_budget(receipt, request.budget_limits)
        ):
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.INVALID_RECEIPT)
        return receipt


def connector_manifest_sha256(manifest: ConnectorManifest) -> str:
    if type(manifest) is not ConnectorManifest:
        raise ConnectorWorkerProtocolError("invalid connector manifest")
    try:
        validated = ConnectorManifest.from_dict(manifest.to_dict())
    except (TypeError, ValueError) as error:
        raise ConnectorWorkerProtocolError("invalid connector manifest") from error
    return sha256(_canonical_json_bytes(validated.to_dict())).hexdigest()


def _receipt_within_budget(
    receipt: ConnectorWorkerReceipt,
    limits: ConnectorBudgetLimits,
) -> bool:
    return all(
        run.discovered_count <= limits.max_discoveries
        and run.fetched_count <= limits.max_fetches
        and run.extracted_count <= limits.max_extractions
        and run.submitted_count <= limits.max_submissions
        for run in (receipt.first_run, receipt.replay_run)
    )


def _run_worker_process(
    request: ConnectorWorkerRequest,
    limits: ConnectorWorkerLimits,
) -> ConnectorWorkerReceipt:
    payload = _canonical_json_bytes(request.to_dict()) + b"\n"
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.OUTPUT_LIMIT)
    with TemporaryDirectory(prefix="open-brain-connector-worker-") as raw_root:
        try:
            process = subprocess.Popen[bytes](
                _worker_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=Path(raw_root),
                env={},
                close_fds=True,
                start_new_session=True,
                preexec_fn=_apply_limits(limits),
                text=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.PROCESS_FAILED) from error
        stdout, stderr = _bounded_exchange(process, payload, limits)
    if stderr or process.returncode != 0:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.PROCESS_FAILED)
    try:
        decoded = json.loads(stdout, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ConnectorWorkerProtocolError) as error:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.INVALID_RECEIPT) from error
    if isinstance(decoded, dict) and set(decoded) == {"failure_code", "schema_version"}:
        try:
            code = ConnectorWorkerFailureCode(decoded["failure_code"])
        except (TypeError, ValueError) as error:
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.INVALID_RECEIPT) from error
        raise ConnectorWorkerError(code)
    try:
        return ConnectorWorkerReceipt.from_dict(decoded)
    except (TypeError, ValueError) as error:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.INVALID_RECEIPT) from error


def _worker_command() -> tuple[str, ...]:
    if getattr(sys, "frozen", False):
        return (sys.executable, "__connector-worker")
    return (
        sys.executable,
        "-I",
        "-m",
        "open_brain_connectors.runtime.connector_worker_child",
    )


def _bounded_exchange(
    process: subprocess.Popen[bytes],
    payload: bytes,
    limits: ConnectorWorkerLimits,
) -> tuple[bytes, bytes]:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.PROCESS_FAILED)
    try:
        process.stdin.write(payload)
        process.stdin.close()
    except (BrokenPipeError, OSError) as error:
        _kill_process_group(process)
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.PROCESS_FAILED) from error
    streams = selectors.DefaultSelector()
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    bounds = {"stdout": limits.max_stdout_bytes, "stderr": limits.max_stderr_bytes}
    deadline = time.monotonic() + limits.wall_seconds
    try:
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                raise ConnectorWorkerError(ConnectorWorkerFailureCode.TIMEOUT)
            for key, _mask in streams.select(min(remaining, 0.05)):
                chunk = os.read(key.fd, _READ_SIZE)
                if not chunk:
                    streams.unregister(key.fileobj)
                    continue
                name = key.data
                if not isinstance(name, str):
                    _kill_process_group(process)
                    raise ConnectorWorkerError(ConnectorWorkerFailureCode.PROCESS_FAILED)
                output[name].extend(chunk)
                if len(output[name]) > bounds[name]:
                    _kill_process_group(process)
                    raise ConnectorWorkerError(ConnectorWorkerFailureCode.OUTPUT_LIMIT)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(process)
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.TIMEOUT)
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        _kill_process_group(process)
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.TIMEOUT) from error
    finally:
        streams.close()
    return bytes(output["stdout"]), bytes(output["stderr"])


def _apply_limits(limits: ConnectorWorkerLimits) -> Callable[[], None]:
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        if sys.platform != "darwin":
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_processes, limits.max_processes))

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


def _child_main() -> int:
    global _DIRECT_NETWORK_ATTEMPTS
    try:
        payload = sys.stdin.buffer.read(_MAX_MESSAGE_BYTES + 1)
        if len(payload) > _MAX_MESSAGE_BYTES:
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.INVALID_REQUEST)
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
        request = ConnectorWorkerRequest.from_dict(decoded)
        _DIRECT_NETWORK_ATTEMPTS = 0
        _disable_direct_network()
        receipt = _run_plugin(request)
        if _DIRECT_NETWORK_ATTEMPTS:
            raise ConnectorWorkerError(ConnectorWorkerFailureCode.CAPABILITY_DENIED)
        response: Mapping[str, object] = receipt.to_dict()
    except ConnectorWorkerError as error:
        response = {
            "failure_code": error.code.value,
            "schema_version": WORKER_PROTOCOL_VERSION,
        }
    except Exception:
        response = {
            "failure_code": ConnectorWorkerFailureCode.PROCESS_FAILED.value,
            "schema_version": WORKER_PROTOCOL_VERSION,
        }
    sys.stdout.buffer.write(_canonical_json_bytes(response) + b"\n")
    sys.stdout.buffer.flush()
    return 0


def _run_plugin(request: ConnectorWorkerRequest) -> ConnectorWorkerReceipt:
    try:
        installed = tuple(
            cast(
                Sequence[ConnectorEntryPoint],
                entry_points(group=CONNECTOR_ENTRY_POINT_GROUP),
            )
        )
    except Exception as error:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_DISCOVERED) from error
    if len(installed) > 32:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_DISCOVERED)
    try:
        validated = tuple(
            (ConnectorEntryPointMetadata(name=entry.name, value=entry.value), entry)
            for entry in installed
        )
    except Exception as error:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_DISCOVERED) from error
    names = [metadata.name for metadata, _entry in validated]
    if len(names) != len(set(names)):
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_DISCOVERED)
    matches = tuple(
        (metadata, entry)
        for metadata, entry in validated
        if metadata.name == request.connector_name
    )
    if len(matches) != 1 or matches[0][0].value != request.entry_point_value:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.NOT_DISCOVERED)
    try:
        plugin = matches[0][1].load()
        manifest = getattr(plugin, "manifest", None)
        conformance = getattr(plugin, "conformance", None)
    except Exception as error:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.PROCESS_FAILED) from error
    if (
        isinstance(plugin, type)
        or type(manifest) is not ConnectorManifest
        or connector_manifest_sha256(manifest) != request.manifest_sha256
    ):
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.MANIFEST_MISMATCH)
    if not callable(conformance):
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.INVALID_RECEIPT)
    try:
        receipt = conformance(request)
    except Exception as error:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.PROCESS_FAILED) from error
    if type(receipt) is not ConnectorWorkerReceipt:
        raise ConnectorWorkerError(ConnectorWorkerFailureCode.INVALID_RECEIPT)
    return receipt


def _disable_direct_network() -> None:
    def denied(*_args: object, **_kwargs: object) -> None:
        global _DIRECT_NETWORK_ATTEMPTS
        _DIRECT_NETWORK_ATTEMPTS += 1
        raise PermissionError("direct connector network access is disabled")

    socket.__dict__["socket"] = denied
    socket.__dict__["create_connection"] = denied
    socket.__dict__["getaddrinfo"] = denied


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ConnectorWorkerProtocolError("duplicate worker field")
        value[key] = item
    return value
