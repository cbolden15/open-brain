"""Bounded child-process adapter for the separately installed optional collector.

The core never imports collector or connector code. It launches one explicitly
selected, owner-controlled executable with a finite argv allowlist, a minimal
environment and bounded stdout/stderr, then validates strict closed JSON. The
collector is a trusted same-user program: hashes and the capability handshake
are compatibility evidence, not a sandbox.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pwd
import re
import selectors
import signal
import stat
import subprocess
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

from open_brain_engine.engine import PHASE1_STATE_SCHEMA_VERSION

from open_brain.services.local_runtime_session import RUNTIME_SESSION_VERSION

__all__ = [
    "REQUIRED_OPERATIONS",
    "CapturedRecord",
    "CollectorExecutable",
    "GuideAdapterError",
    "collector_handshake",
    "list_accounts",
    "read_guide_metadata",
    "read_resource_inventory",
    "recheck_collector_executable",
    "resolve_captured_record",
    "resolve_collector_executable",
]

REQUIRED_OPERATIONS: Final = frozenset(
    {
        "sources.accounts",
        "sources.resources",
        "sources.configure",
        "sources.preview",
        "sources.import",
        "sources.custody_inspect",
        "sources.guide_metadata",
    }
)
_PORTABLE_METADATA: Final = 16
_HANDSHAKE_DEADLINE: float = 10.0
_METADATA_DEADLINE: float = 30.0
_INVENTORY_DEADLINE: float = 120.0
_CUSTODY_DEADLINE: float = 3600.0
_HANDSHAKE_STDOUT: Final = 16_384
_OPERATION_STDOUT: Final = 4_194_304
_STDERR_LIMIT: Final = 16_384
_REAP_SECONDS: Final = 2.0
_MAX_DEPTH: Final = 16
_MAX_PAGES: Final = 5
_MAX_ROWS: Final = 1000
_MAX_EXECUTABLE_BYTES: Final = 536_870_912
_FIXED_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin"
_CAPABILITY_KEYS: Final = frozenset(
    {
        "schema_version",
        "package",
        "guide_protocols",
        "metadata_protocols",
        "lifetime_guard",
        "operations",
        "compatibility",
    }
)
_ACCOUNT_KEYS: Final = frozenset(
    {"provider", "connection_id", "display_name", "scopes", "expires_at_epoch"}
)
_RESOURCE_KEYS: Final = frozenset({"resource_id", "name", "resource_type"})


class GuideAdapterError(Exception):
    """Closed guide code; any native code stays private bounded evidence."""

    def __init__(self, code: str, *, native: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.native = native


@dataclass(frozen=True, slots=True)
class CollectorExecutable:
    path: Path
    sha256: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class CapturedRecord:
    receipt_id: str
    capture_id: str
    source_id: str
    revision_id: str
    body: str
    current: bool


class _ProjectedRecord(Protocol):
    @property
    def summary(self) -> Mapping[str, object]: ...

    @property
    def text(self) -> str: ...


def _trusted_directory_chain(directory: Path) -> None:
    for item in (directory, *directory.parents):
        info = os.lstat(item)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {os.getuid(), 0}
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise GuideAdapterError("unsafe_path")


def resolve_collector_executable(path: Path) -> CollectorExecutable:
    """Resolve one explicitly selected installed collector and record its identity."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise GuideAdapterError("invalid_request")
    try:
        canonical = Path(os.path.realpath(path, strict=True))
        _trusted_directory_chain(Path(os.path.realpath(path.parent, strict=True)))
        _trusted_directory_chain(canonical.parent)
        fd = os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except GuideAdapterError:
        raise
    except OSError:
        raise GuideAdapterError("collector_unavailable") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {os.getuid(), 0}
            or stat.S_IMODE(info.st_mode) & 0o022
            or not os.access(canonical, os.X_OK)
            or info.st_size > _MAX_EXECUTABLE_BYTES
        ):
            raise GuideAdapterError("unsafe_path")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1_048_576):
            digest.update(chunk)
        current = os.stat(canonical, follow_symlinks=False)
    except OSError:
        raise GuideAdapterError("collector_unavailable") from None
    finally:
        os.close(fd)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise GuideAdapterError("collector_changed")
    return CollectorExecutable(canonical, digest.hexdigest(), info.st_dev, info.st_ino)


def recheck_collector_executable(executable: CollectorExecutable) -> None:
    """Require the recorded canonical executable immediately before each launch."""
    try:
        current = resolve_collector_executable(executable.path)
    except GuideAdapterError:
        raise GuideAdapterError("collector_changed") from None
    if current != executable:
        raise GuideAdapterError("collector_changed")


class _ChildFailure(Exception):
    pass


def _terminate(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    with suppress(ProcessLookupError, PermissionError):
        # Stop any remaining member of the child's own process group.
        os.killpg(process.pid, signal.SIGKILL)


def _run_child(
    executable: CollectorExecutable,
    argv: Sequence[str],
    *,
    deadline_seconds: float,
    stdout_limit: int,
    pass_fds: tuple[int, ...] = (),
) -> tuple[int, bytes]:
    recheck_collector_executable(executable)
    environment = {
        "HOME": pwd.getpwuid(os.getuid()).pw_dir,
        "PATH": _FIXED_PATH,
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }
    try:
        process = subprocess.Popen(
            [os.fspath(executable.path), *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
            env=environment,
            cwd="/",
            shell=False,
        )
    except OSError:
        raise GuideAdapterError("collector_unavailable") from None
    assert process.stdout is not None and process.stderr is not None
    stdout_fd = process.stdout.fileno()
    limits = {stdout_fd: stdout_limit, process.stderr.fileno(): _STDERR_LIMIT}
    received: dict[int, bytearray] = {fd: bytearray() for fd in limits}
    deadline = time.monotonic() + deadline_seconds
    selector = selectors.DefaultSelector()
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream.fileno(), selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ChildFailure
            for key, _events in selector.select(timeout=remaining):
                fd = key.fd
                try:
                    chunk = os.read(fd, 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(fd)
                    continue
                received[fd].extend(chunk)
                if len(received[fd]) > limits[fd]:
                    raise _ChildFailure
        try:
            code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise _ChildFailure from None
    except _ChildFailure:
        _terminate(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    # Raw stderr is never echoed or retained.
    return code, bytes(received[stdout_fd])


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate key")
    return value


def _reject_constant(_value: str) -> object:
    raise ValueError("nonfinite number")


def _depth(value: object, level: int = 1) -> None:
    if level > _MAX_DEPTH:
        raise ValueError("nesting")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def _strict_object(raw: bytes) -> dict[str, object]:
    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant
    )
    _depth(value)
    if not isinstance(value, dict):
        raise ValueError("object required")
    return cast(dict[str, object], value)


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        raise ValueError("integer list")
    return cast(list[int], value)


def collector_handshake(executable: CollectorExecutable) -> frozenset[str]:
    """Run the inert capability handshake and return the fixed guide allowlist."""
    try:
        code, raw = _run_child(
            executable,
            ("--guide-capabilities-json",),
            deadline_seconds=_HANDSHAKE_DEADLINE,
            stdout_limit=_HANDSHAKE_STDOUT,
        )
        if code != 0:
            raise ValueError("exit")
        value = _strict_object(raw)
        operations = value.get("operations")
        if (
            set(value) != _CAPABILITY_KEYS
            or value["schema_version"] != 1
            or type(value["schema_version"]) is not int
            or value["package"] != "open-brain-collector"
            or 1 not in _int_list(value["guide_protocols"])
            or 1 not in _int_list(value["metadata_protocols"])
            or value["lifetime_guard"] != "inherited-flock-v1"
            or not isinstance(operations, list)
            or any(type(item) is not str for item in operations)
            or not set(operations) >= REQUIRED_OPERATIONS
            or value["compatibility"]
            != {
                "schema": PHASE1_STATE_SCHEMA_VERSION,
                "runtime": RUNTIME_SESSION_VERSION,
                "portable": _PORTABLE_METADATA,
            }
            or any(
                type(item) is not int
                for item in cast(dict[str, object], value["compatibility"]).values()
            )
        ):
            raise ValueError("incompatible")
    except GuideAdapterError as error:
        if error.code in {"collector_unavailable", "collector_changed"}:
            raise
        raise GuideAdapterError("collector_incompatible") from None
    except (_ChildFailure, ValueError, UnicodeError, RecursionError, TypeError):
        raise GuideAdapterError("collector_incompatible") from None
    # Additional advertised operations never expand the fixed allowlist.
    return REQUIRED_OPERATIONS


def _identifier(value: object, *, maximum: int = 512) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or value.startswith("-")
        or any(ord(item) < 32 or 127 <= ord(item) < 160 for item in value)
    ):
        raise GuideAdapterError("invalid_request")
    return value


def _invoke(
    executable: CollectorExecutable,
    operation: str,
    *,
    state: Path,
    brain: Path,
    guard_fd: int | None = None,
    arguments: Mapping[str, str | None] | None = None,
) -> dict[str, object]:
    """Launch exactly one allowlisted read operation and parse its closed JSON object."""
    values = dict(arguments or {})
    if operation == "sources.accounts" and not values:
        tail, guarded, deadline = ["accounts", "--provider", "slack"], True, _INVENTORY_DEADLINE
    elif operation == "sources.resources" and set(values) == {"connection_id", "cursor"}:
        tail = [
            "resources",
            "--provider",
            "slack",
            "--connection-id",
            _identifier(values["connection_id"]),
        ]
        if values["cursor"] is not None:
            tail += ["--cursor", _identifier(values["cursor"], maximum=8192)]
        guarded, deadline = True, _INVENTORY_DEADLINE
    elif operation == "sources.custody_inspect" and set(values) == {"receipt_id"}:
        tail = ["custody-inspect", "--receipt-id", _identifier(values["receipt_id"])]
        guarded, deadline = True, _CUSTODY_DEADLINE
    elif operation == "sources.guide_metadata" and set(values) == {"source_id"}:
        tail = ["guide-metadata", "--source-id", _identifier(values["source_id"])]
        guarded, deadline = False, _METADATA_DEADLINE
    else:
        # configure/preview/import are guided writes owned by a later phase.
        raise GuideAdapterError("invalid_request")
    if not state.is_absolute() or not brain.is_absolute():
        raise GuideAdapterError("invalid_request")
    if guarded != (guard_fd is not None) or (
        guard_fd is not None and (type(guard_fd) is not int or guard_fd < 0)
    ):
        raise GuideAdapterError("invalid_request")
    argv = ["sources", "--state", os.fspath(state), "--brain-root", os.fspath(brain)]
    if guard_fd is not None:
        argv += ["--guide-guard-fd", str(guard_fd)]
    argv += ["--foreground", *tail]
    try:
        code, raw = _run_child(
            executable,
            argv,
            deadline_seconds=deadline,
            stdout_limit=_OPERATION_STDOUT,
            pass_fds=() if guard_fd is None else (guard_fd,),
        )
        value = _strict_object(raw)
    except _ChildFailure:
        raise GuideAdapterError("operation_unknown") from None
    except (ValueError, UnicodeError, RecursionError):
        raise GuideAdapterError("operation_unknown") from None
    if code != 0:
        failure = value.get("failure_code")
        if code == 78 and set(value) == {"outcome", "failure_code"} and type(failure) is str:
            mapped = (
                "invalid_request" if failure == "source_invalid_arguments" else "operation_failed"
            )
            raise GuideAdapterError(mapped, native=failure[:128])
        raise GuideAdapterError("operation_unknown")
    return value


def _refuse() -> GuideAdapterError:
    return GuideAdapterError("operation_failed")


def list_accounts(
    executable: CollectorExecutable, *, state: Path, brain: Path, guard_fd: int
) -> dict[str, object]:
    """Return connected Slack account rows exactly as returned; aliases are never identity."""
    value = _invoke(executable, "sources.accounts", state=state, brain=brain, guard_fd=guard_fd)
    accounts = value.get("accounts")
    if set(value) != {"accounts"} or not isinstance(accounts, list) or len(accounts) > _MAX_ROWS:
        raise _refuse()
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in accounts:
        if not isinstance(item, dict) or set(item) != _ACCOUNT_KEYS:
            raise _refuse()
        try:
            connection = _identifier(item["connection_id"])
            name = item["display_name"]
            if item["provider"] != "slack" or type(name) is not str or not 1 <= len(name) <= 512:
                raise _refuse()
        except GuideAdapterError:
            raise _refuse() from None
        if connection in seen:
            raise _refuse()
        seen.add(connection)
        rows.append({"connection_id": connection, "display_name": name, "alias": None})
    return {"kind": "accounts", "rows": rows, "has_more": False}


def read_resource_inventory(
    executable: CollectorExecutable,
    *,
    state: Path,
    brain: Path,
    guard_fd: int,
    connection_id: str,
    cursor: str | None,
) -> dict[str, object]:
    """Read at most five returned channel pages; a limit is continuation, never absence."""
    _identifier(connection_id)
    if cursor is not None:
        _identifier(cursor, maximum=8192)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    cursors: set[str | None] = {cursor}
    next_cursor = cursor
    for _page in range(_MAX_PAGES):
        value = _invoke(
            executable,
            "sources.resources",
            state=state,
            brain=brain,
            guard_fd=guard_fd,
            arguments={"connection_id": connection_id, "cursor": next_cursor},
        )
        resources = value.get("resources")
        if set(value) != {"resources", "next_cursor"} or not isinstance(resources, list):
            raise _refuse()
        for item in resources:
            if not isinstance(item, dict) or set(item) != _RESOURCE_KEYS:
                raise _refuse()
            try:
                resource_id = _identifier(item["resource_id"])
                name = _identifier(item["name"])
            except GuideAdapterError:
                raise _refuse() from None
            if item["resource_type"] != "channel" or resource_id in seen:
                raise _refuse()
            seen.add(resource_id)
            rows.append({"resource_id": resource_id, "name": name, "resource_type": "channel"})
        if len(rows) > _MAX_ROWS:
            raise GuideAdapterError("capacity_reached")
        returned = value["next_cursor"]
        if returned is not None:
            try:
                _identifier(returned, maximum=8192)
            except GuideAdapterError:
                raise _refuse() from None
            if returned in cursors:
                raise _refuse()
            cursors.add(cast(str, returned))
        next_cursor = cast(str | None, returned)
        if next_cursor is None:
            break
    return {
        "kind": "resources",
        "rows": rows,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }


_Check = Callable[[object], bool]


def _enum(*allowed: object) -> _Check:
    return lambda value: type(value) in {str, bool, type(None)} and value in allowed


def _nullable(check: _Check) -> _Check:
    return lambda value: value is None or check(value)


def _bool(value: object) -> bool:
    return type(value) is bool


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _epoch(value: object) -> bool:
    return (type(value) is int and value >= 0) or (
        type(value) is float and math.isfinite(value) and value >= 0
    )


def _text(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 512
        and not any(unicodedata.category(item) == "Cc" for item in value)
    )


def _codes(allowed: frozenset[str], maximum: int) -> _Check:
    return lambda value: (
        isinstance(value, list)
        and len(value) <= maximum
        and all(type(item) is str and item in allowed for item in value)
    )


def _token(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"store", "token"}
        and value["store"] in {"brain", "marker", "account", "source", "preview", "custody"}
        and type(value["token"]) is str
        and _HEX64.fullmatch(value["token"]) is not None
    )


def _tokens(value: object) -> bool:
    return isinstance(value, list) and len(value) <= 7 and all(_token(item) for item in value)


def _counts(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"pending", "captured", "duplicate", "quarantined", "history_only"}
        and all(_count(item) for item in value.values())
    )


_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_REASONS: Final = frozenset(
    {
        "state_absent",
        "binding_absent",
        "binding_mismatch",
        "unsafe_path",
        "invalid_state",
        "size_limit",
        "changed_during_read",
        "relation_mismatch",
        "legacy_unknown",
        "not_read_by_v1",
        "app_workflow_owned",
    }
)
_COVERAGE: Final = frozenset(
    {
        "partial_page",
        "thread_partial",
        "upstream_unknown",
        "provider_limit",
        "unsupported_coverage",
        "legacy_unknown",
    }
)
_FAILURE: Final = _nullable(
    _enum("access_denied", "credentials_unavailable", "rate_limited", "operation_failed")
)
# Closed HealthV1 per HEALTH.md: every key required, null only where unknown.
_HEALTH_SCHEMA: Final[dict[str, dict[str, _Check]]] = {
    "snapshot": {
        "consistency": _enum("per_store", "inconsistent", "unavailable", "uninitialized"),
        "global_consistency": _enum("unknown"),
        "retryable": _bool,
        "reason_codes": _codes(_REASONS, 16),
        "evidence_tokens": _tokens,
    },
    "account": {
        "registration": _enum("registered", "absent", "unavailable"),
        "connection_id": _nullable(lambda value: _identifier_ok(value)),
        "display_name": _nullable(_text),
        "credential_status": _enum("unknown"),
        "last_access_failure_code": _FAILURE,
        "last_access_failure_epoch": _nullable(_epoch),
    },
    "source": {
        "source_id": lambda value: _identifier_ok(value),
        "generation": _nullable(lambda value: _identifier_ok(value)),
        "control_epoch": _nullable(_count),
        "configured_status": _nullable(_enum("enabled", "disabled", "paused")),
        "activity": _enum("manual_idle", "awaiting_inspection", "paused", "blocked", "unknown"),
        "schedule_running": _enum("unknown"),
        "stored_next_run_epoch": _nullable(_epoch),
    },
    "pending_preview": {
        "status": _enum("present", "absent", "unavailable", "inconsistent"),
        "preview_id": _nullable(lambda value: _identifier_ok(value)),
        "count": _nullable(_count),
        "has_more": _nullable(_bool),
        "coverage_codes": _codes(_COVERAGE, 25),
    },
    "execution": {
        "outcome": _enum("completed", "failed", "unknown"),
        "finished_epoch": _nullable(_epoch),
        "captured_count": _nullable(_count),
        "duplicate_count": _nullable(_count),
        "quarantined_count": _nullable(_count),
        "last_successful_completion_epoch": _nullable(_epoch),
        "failure_code": _FAILURE,
    },
    "coverage": {
        "status": _enum("partial", "unsupported", "unknown"),
        "has_more": _nullable(_bool),
        "codes": _codes(_COVERAGE, 25),
    },
    "recovery": {
        "retry_deadline_epoch": _enum(None),
        "retry_deadline_proven": lambda value: value is False,
        "unresolved_receipt_count": _nullable(_count),
    },
    "custody": {
        "scope": _enum("selected_source"),
        "status": _enum("available", "unavailable", "inconsistent"),
        "counts": _nullable(_counts),
        "retained_items": _nullable(_count),
    },
    "processing_backlog": {
        "scope": _enum("global"),
        "status": _enum("unavailable"),
        "reason_code": _enum("not_read_by_v1"),
    },
    "release": {"status": _enum("unavailable"), "reason_code": _enum("app_workflow_owned")},
}
_BINDING_SCHEMA: Final[dict[str, _Check]] = {
    "receipt_id": lambda value: _identifier_ok(value),
    "source_id": lambda value: _identifier_ok(value),
    "generation": lambda value: _identifier_ok(value),
    "control_epoch": _count,
    "binding_verified": _bool,
    "current_generation": _bool,
    "supported_slack_observation": _bool,
    "outcome": _enum("pending", "captured", "duplicate", "quarantined", "history_only"),
    "capture_id": _nullable(lambda value: _identifier_ok(value)),
}


def _identifier_ok(value: object) -> bool:
    try:
        _identifier(value)
    except GuideAdapterError:
        return False
    return True


def _closed(value: object, schema: Mapping[str, _Check]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(schema)
        and all(check(value[key]) for key, check in schema.items())
    )


def _validate_health(value: dict[str, object]) -> dict[str, object]:
    bindings = value.get("receipt_bindings")
    if (
        set(value) != {"schema_version", "receipt_bindings", *_HEALTH_SCHEMA}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not all(_closed(value[name], schema) for name, schema in _HEALTH_SCHEMA.items())
        or not isinstance(bindings, list)
        or len(bindings) > 768
        or not all(_closed(item, _BINDING_SCHEMA) for item in bindings)
    ):
        raise _refuse()
    return value


def read_guide_metadata(
    executable: CollectorExecutable, *, state: Path, brain: Path, source_id: str
) -> dict[str, object]:
    """Read collector-owned HealthV1 through the noncreating metadata entrypoint."""
    value = _invoke(
        executable,
        "sources.guide_metadata",
        state=state,
        brain=brain,
        arguments={"source_id": source_id},
    )
    return _validate_health(value)


def resolve_captured_record(
    *,
    receipt_id: str,
    retained_receipt_ids: Sequence[str],
    selected_source_id: str,
    bindings: Sequence[Mapping[str, object]],
    read_record: Callable[[str], _ProjectedRecord],
) -> CapturedRecord:
    """Resolve one retained import receipt to its exact literal capture, or refuse.

    `read_record` must perform the owner complete read of the literal capture ID
    with that same expected revision and without history. No search, prefix
    manipulation or newest-revision substitution is attempted.
    """
    unresolved = GuideAdapterError("receipt_unresolved")
    if receipt_id not in retained_receipt_ids:
        raise unresolved
    matching = [item for item in bindings if item.get("receipt_id") == receipt_id]
    if len(matching) != 1:
        raise unresolved
    binding = matching[0]
    capture_id = binding.get("capture_id")
    if (
        binding.get("source_id") != selected_source_id
        or binding.get("binding_verified") is not True
        or binding.get("supported_slack_observation") is not True
        or binding.get("outcome") not in {"captured", "duplicate"}
        or type(capture_id) is not str
        or not capture_id
    ):
        raise unresolved
    try:
        record = read_record(capture_id)
        summary = record.summary
        body = record.text
    except Exception:
        raise unresolved from None
    source_id = summary.get("source_id")
    if (
        summary.get("record_type") != "source"
        or summary.get("record_id") != capture_id
        or summary.get("revision_id") != capture_id
        or type(source_id) is not str
        or not source_id
        or type(body) is not str
    ):
        raise unresolved
    return CapturedRecord(
        receipt_id=receipt_id,
        capture_id=capture_id,
        source_id=source_id,
        revision_id=capture_id,
        body=body,
        current=binding.get("current_generation") is True,
    )
