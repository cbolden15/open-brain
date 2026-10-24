"""Closed, noncreating selected-source metadata for the W5 owner guide.

This reader is a projection boundary over existing collector files. It never
constructs a manager, store, lock, credential helper, transport or engine. Every
file is opened through no-follow directory descriptors, validated with the
existing format validators against preloaded bytes, and observed a second time
so concurrent replacement is reported instead of merged into one claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, cast

from open_brain_engine.engine import PHASE1_STATE_SCHEMA_VERSION

from open_brain_collector.custody import CustodyStore
from open_brain_collector.live_capture import (
    LiveCaptureService,
    _binding,
    _intake_from_dict,
    _selection,
    _source_name,
)
from open_brain_collector.live_manager import LiveSourceManager
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.slack_auth import (
    _account_name,
    _metadata_account,
)
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = ["GUIDE_CAPABILITIES", "HealthV1", "read_source_metadata"]

HealthV1 = dict[str, object]

# Runtime session and Portable metadata coordinates are owned by the app; the
# collector repeats them only as inert compatibility evidence for the guide.
_RUNTIME_COMPATIBILITY: Final = 16
_PORTABLE_COMPATIBILITY: Final = 18
GUIDE_CAPABILITIES: Final[dict[str, object]] = {
    "schema_version": 1,
    "package": "open-brain-collector",
    "guide_protocols": [1],
    "metadata_protocols": [1],
    "lifetime_guard": "inherited-flock-v1",
    "operations": [
        "sources.accounts",
        "sources.resources",
        "sources.configure",
        "sources.preview",
        "sources.import",
        "sources.custody_inspect",
        "sources.guide_metadata",
    ],
    "compatibility": {
        "schema": PHASE1_STATE_SCHEMA_VERSION,
        "runtime": _RUNTIME_COMPATIBILITY,
        "portable": _PORTABLE_COMPATIBILITY,
    },
}

_MAX_FILE: Final = 4_194_304
_MAX_MANIFEST: Final = 16_384
_MAX_PASS: Final = 16_777_216
_MAX_OUTPUT: Final = 4_194_304
_MAX_BINDINGS: Final = 768
_MAX_REASONS: Final = 16
_FAILURES: Final = {
    "not_allowed": "access_denied",
    "auth_required": "credentials_unavailable",
    "credential_unavailable": "credentials_unavailable",
    "rate_limited": "rate_limited",
}
_ACCESS_FAILURES: Final = frozenset({"access_denied", "credentials_unavailable"})
_COVERAGE_ORDER: Final = (
    "partial_page",
    "thread_partial",
    "upstream_unknown",
    "provider_limit",
    "unsupported_coverage",
    "legacy_unknown",
)
_NOTICES: Final = {"partial_thread_coverage": "thread_partial"}
_RUN_COMPLETED: Final = frozenset(
    {
        "preview_id",
        "outcome",
        "captured_count",
        "duplicate_count",
        "quarantined_count",
        "receipt_ids",
        "finished_epoch",
        "has_more",
        "notices",
        "failure_code",
    }
)
_RUN_LEGACY_OPTIONAL: Final = frozenset({"has_more", "notices"})
_RUN_FAILED: Final = frozenset({"outcome", "failure_code", "finished_epoch"})


class _Refusal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Absent(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Observation:
    store: str
    directory: Path
    name: str
    private_from: int
    limit: int
    identity: tuple[int, int, int, int, int] | None
    sha256: str | None

    def token(self) -> dict[str, str]:
        commitment = bounded_json([self.store, self.identity, self.sha256], 4096)
        return {"store": self.store, "token": hashlib.sha256(commitment).hexdigest()}


class _Pass:
    """One bounded read pass over explicitly named existing files."""

    def __init__(self) -> None:
        self.total = 0

    def read(
        self, directory: Path, name: str, *, private_from: int, limit: int
    ) -> tuple[bytes, tuple[int, int, int, int, int]]:
        directory_fd = _open_directory(directory, private_from=private_from)
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except FileNotFoundError:
                raise _Absent from None
            except OSError:
                raise _Refusal("unsafe_path") from None
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                _require_private_file(before)
                if before.st_size > limit or self.total + before.st_size > _MAX_PASS:
                    raise _Refusal("size_limit")
                raw = handle.read(limit + 1)
                after = os.fstat(handle.fileno())
            if len(raw) > limit:
                raise _Refusal("size_limit")
            self.total += len(raw)
            if self.total > _MAX_PASS:
                raise _Refusal("size_limit")
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                raise _Refusal("changed_during_read") from None
        finally:
            os.close(directory_fd)
        identity = _identity(before)
        if identity != _identity(after) or identity != _identity(current):
            raise _Refusal("changed_during_read")
        return raw, identity


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _require_private_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise _Refusal("unsafe_path")


def _open_directory(path: Path, *, private_from: int) -> int:
    """Open an existing directory without following any component or creating one."""
    if not path.is_absolute() or ".." in path.parts:
        raise _Refusal("unsafe_path")
    try:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise _Refusal("unsafe_path") from None
    try:
        for index, part in enumerate(path.parts[1:], start=1):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                raise _Absent from None
            except OSError:
                raise _Refusal("unsafe_path") from None
            os.close(fd)
            fd = child
            if index >= private_from:
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise _Refusal("unsafe_path")
        return fd
    except BaseException:
        os.close(fd)
        raise


class _Preloaded:
    """Read-only stand-in that lets existing validators parse observed bytes."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self._files = files

    def read(self, name: str) -> object | None:
        raw = self._files.get(name)
        if raw is None:
            return None
        try:
            return cast(object, json.loads(raw))
        except ValueError, UnicodeError, RecursionError:
            raise LiveSourceError("source_invalid_state") from None


class _CaptureView:
    """Expose only the attributes the existing source/preview validators read."""

    def __init__(self, files: Mapping[str, bytes], binding: str) -> None:
        self._store = _Preloaded(files)
        self._brain_binding = binding

    def _check_brain(self) -> None:
        # Binding is checked against the observed marker before validation.
        return None


class _Reader:
    def __init__(self, live_root: Path, brain_root: Path) -> None:
        self.live_root = live_root
        self.brain_root = brain_root
        self.capture = live_root / "capture"
        self.accounts = live_root / "accounts" / "slack"
        self.live_private = len(live_root.parts) - 1
        self.brain_private = len(brain_root.parts) - 1
        self.observations: list[_Observation] = []
        self.pass_ = _Pass()

    def read(self, store: str, directory: Path, name: str, *, private_from: int,
             limit: int = _MAX_FILE) -> bytes | None:
        try:
            raw, identity = self.pass_.read(
                directory, name, private_from=private_from, limit=limit
            )
        except _Absent:
            self.observations.append(
                _Observation(store, directory, name, private_from, limit, None, None)
            )
            return None
        self.observations.append(
            _Observation(
                store,
                directory,
                name,
                private_from,
                limit,
                identity,
                hashlib.sha256(raw).hexdigest(),
            )
        )
        return raw


def _verify_pass(observations: list[_Observation]) -> bool:
    """Re-observe every participating file once; any difference is a concurrent change."""
    second = _Pass()
    for item in observations:
        try:
            raw, identity = second.read(
                item.directory, item.name, private_from=item.private_from, limit=item.limit
            )
        except _Absent:
            if item.identity is not None:
                return False
            continue
        except _Refusal:
            return False
        if item.identity != identity or item.sha256 != hashlib.sha256(raw).hexdigest():
            return False
    return True


def _reasons(*values: str) -> list[str]:
    return list(dict.fromkeys(values))[:_MAX_REASONS]


def _base(
    source_id: str,
    consistency: str,
    reasons: list[str],
    *,
    retryable: bool = False,
) -> HealthV1:
    return {
        "schema_version": 1,
        "snapshot": {
            "consistency": consistency,
            "global_consistency": "unknown",
            "retryable": retryable,
            "reason_codes": _reasons(*reasons),
            "evidence_tokens": [],
        },
        "account": {
            "registration": "unavailable",
            "connection_id": None,
            "display_name": None,
            "credential_status": "unknown",
            "last_access_failure_code": None,
            "last_access_failure_epoch": None,
        },
        "source": {
            "source_id": source_id,
            "generation": None,
            "control_epoch": None,
            "configured_status": None,
            "activity": "unknown",
            "schedule_running": "unknown",
            "stored_next_run_epoch": None,
        },
        "pending_preview": {
            "status": "unavailable",
            "preview_id": None,
            "count": None,
            "has_more": None,
            "coverage_codes": [],
        },
        "execution": {
            "outcome": "unknown",
            "finished_epoch": None,
            "captured_count": None,
            "duplicate_count": None,
            "quarantined_count": None,
            "last_successful_completion_epoch": None,
            "failure_code": None,
        },
        "coverage": {"status": "unknown", "has_more": None, "codes": []},
        "recovery": {
            "retry_deadline_epoch": None,
            "retry_deadline_proven": False,
            "unresolved_receipt_count": None,
        },
        "custody": {
            "scope": "selected_source",
            "status": "unavailable",
            "counts": None,
            "retained_items": None,
        },
        "receipt_bindings": [],
        "processing_backlog": {
            "scope": "global",
            "status": "unavailable",
            "reason_code": "not_read_by_v1",
        },
        "release": {"status": "unavailable", "reason_code": "app_workflow_owned"},
    }


def _coverage(notices: object, has_more: object) -> list[str]:
    codes: set[str] = set()
    if has_more is True:
        codes.add("partial_page")
    if not isinstance(notices, list) or isinstance(has_more, bool) is False:
        codes.add("legacy_unknown")
    if isinstance(notices, list):
        codes.update(
            _NOTICES.get(item, "upstream_unknown") if type(item) is str else "upstream_unknown"
            for item in notices
        )
    return [code for code in _COVERAGE_ORDER if code in codes]


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _run(last_run: object) -> tuple[str, dict[str, object] | None]:
    """Classify a source last_run as completed, failed or unknown legacy evidence."""
    if not isinstance(last_run, dict):
        return "unknown", None
    keys = set(last_run)
    if (
        last_run.get("outcome") == "completed"
        and _RUN_COMPLETED - _RUN_LEGACY_OPTIONAL <= keys <= _RUN_COMPLETED
        and all(
            _count(last_run[key])
            for key in ("captured_count", "duplicate_count", "quarantined_count", "finished_epoch")
        )
        and last_run["failure_code"] is None
        and isinstance(last_run["receipt_ids"], list)
        and ("has_more" not in last_run or type(last_run["has_more"]) is bool)
        and ("notices" not in last_run or isinstance(last_run["notices"], list))
    ):
        return "completed", cast(dict[str, object], last_run)
    if (
        keys == _RUN_FAILED
        and last_run["outcome"] == "failed"
        and type(last_run["failure_code"]) is str
        and _count(last_run["finished_epoch"])
    ):
        return "failed", cast(dict[str, object], last_run)
    return "unknown", None


def _source_locator(
    source_id: str | None, selection: SourceResourceSelection | None
) -> str:
    if (source_id is None) == (selection is None):
        raise ValueError("exactly one source locator is required")
    if selection is not None:
        try:
            selected = _selection(asdict(selection))
        except (LiveSourceError, TypeError) as error:
            raise ValueError("invalid selection") from error
        if selected.connector_name != "slack":
            raise ValueError("guide metadata supports Slack selections only")
        return LiveSourceManager._source_id(selected)
    try:
        _source_name(cast(str, source_id))
    except LiveSourceError as error:
        raise ValueError("invalid source id") from error
    return cast(str, source_id)


def read_source_metadata(
    *,
    live_root: Path,
    brain_root: Path,
    source_id: str | None = None,
    selection: SourceResourceSelection | None = None,
) -> HealthV1:
    """Return HealthV1 for one selected source without creating or repairing state."""
    selected_id = _source_locator(source_id, selection)
    if not isinstance(live_root, Path) or not isinstance(brain_root, Path):
        raise ValueError("explicit paths are required")
    reader = _Reader(live_root, brain_root)
    try:
        report = _project(reader, selected_id, selection)
    except _Refusal as refusal:
        if refusal.reason == "changed_during_read":
            return _base(selected_id, "inconsistent", [refusal.reason], retryable=True)
        return _base(selected_id, "unavailable", [refusal.reason])
    if not _verify_pass(reader.observations):
        return _base(selected_id, "inconsistent", ["changed_during_read"], retryable=True)
    snapshot = cast(dict[str, object], report["snapshot"])
    if snapshot["consistency"] in {"per_store", "inconsistent"}:
        snapshot["evidence_tokens"] = [
            item.token() for item in reader.observations if item.identity is not None
        ]
    bounded_json(report, _MAX_OUTPUT)
    return report


def _project(
    reader: _Reader, source_id: str, selection: SourceResourceSelection | None
) -> HealthV1:
    manifest = reader.read(
        "brain",
        reader.brain_root,
        "brain.toml",
        private_from=reader.brain_private,
        limit=_MAX_MANIFEST,
    )
    if manifest is None:
        raise _Refusal("state_absent")
    try:
        binding = _binding(reader.brain_root)
    except LiveSourceError:
        raise _Refusal("invalid_state") from None
    marker = reader.read("marker", reader.capture, "brain.json", private_from=reader.live_private)
    if marker is None:
        report = _base(source_id, "uninitialized", ["state_absent", "binding_absent"])
        if selection is not None:
            _project_account(reader, report, selection.connection_id)
        return report
    try:
        marker_value = json.loads(marker)
    except ValueError, UnicodeError, RecursionError:
        raise _Refusal("invalid_state") from None
    if marker_value != {"schema_version": 1, "binding": binding}:
        raise _Refusal("binding_mismatch")
    source_name = _source_name(source_id)
    raw_source = reader.read(
        "source", reader.capture, source_name, private_from=reader.live_private
    )
    if raw_source is None:
        report = _base(source_id, "uninitialized", ["state_absent"])
        if selection is not None:
            _project_account(reader, report, selection.connection_id)
        return report
    files = {source_name: raw_source}
    try:
        entry = LiveCaptureService._load(
            cast(LiveCaptureService, _CaptureView(files, binding)), source_id
        )
    except LiveSourceError:
        raise _Refusal("invalid_state") from None
    selected = _selection(entry["selection"])
    report = _base(source_id, "per_store", [])
    reasons: list[str] = []
    source = cast(dict[str, object], report["source"])
    source.update(
        generation=entry["generation"],
        control_epoch=entry["control_epoch"],
        configured_status=entry["status"],
        stored_next_run_epoch=entry["next_run_epoch"],
    )
    if selected.connector_name == "slack":
        _project_account(reader, report, selected.connection_id)
    pending_present = _project_pending(reader, report, entry, files, binding, reasons)
    quarantined_current = _project_custody(reader, report, entry, binding, reasons)
    _project_execution(report, entry["last_run"], reasons)
    status = entry["status"]
    source["activity"] = (
        "paused"
        if status == "paused"
        else "awaiting_inspection"
        if pending_present
        else "blocked"
        if quarantined_current
        else "manual_idle"
        if status == "disabled"
        else "unknown"
    )
    snapshot = cast(dict[str, object], report["snapshot"])
    if "relation_mismatch" in reasons:
        snapshot["consistency"] = "inconsistent"
    snapshot["reason_codes"] = _reasons(*reasons)
    return report


def _project_account(reader: _Reader, report: HealthV1, connection_id: str) -> None:
    account = cast(dict[str, object], report["account"])
    try:
        name = _account_name(connection_id)
    except LiveSourceError:
        return
    try:
        raw = reader.read("account", reader.accounts, name, private_from=reader.live_private)
    except _Refusal as refusal:
        if refusal.reason == "changed_during_read":
            raise
        return
    if raw is None:
        account["registration"] = "absent"
        return
    try:
        # Registration only: the credential reference is never read or compared.
        parsed = _metadata_account(json.loads(raw), name)
    except (ValueError, UnicodeError, RecursionError, LiveSourceError):
        return
    account.update(
        registration="registered",
        connection_id=parsed.connection_id,
        display_name=parsed.display_name,
    )


def _project_pending(
    reader: _Reader,
    report: HealthV1,
    entry: dict[str, object],
    files: dict[str, bytes],
    binding: str,
    reasons: list[str],
) -> bool:
    pending = cast(dict[str, object], report["pending_preview"])
    reference = entry["pending"]
    if reference is None:
        pending["status"] = "absent"
        return False
    name = cast(str, reference).replace(":", "-") + ".json"
    raw = reader.read("preview", reader.capture, name, private_from=reader.live_private)
    if raw is None:
        reasons.append("state_absent")
        return False
    files[name] = raw
    try:
        value = LiveCaptureService._pending(
            cast(LiveCaptureService, _CaptureView(files, binding)), entry
        )
        if value is None:
            raise LiveSourceError("source_invalid_preview")
        intakes = cast(list[object], value["intakes"])
        for item in intakes:
            _intake_from_dict(item)
        has_more = value["has_more"]
        if type(has_more) is not bool or not isinstance(value["notices"], list):
            raise LiveSourceError("source_invalid_preview")
    except (LiveSourceError, KeyError, TypeError) as error:
        # A stable stale or malformed pair is never repaired or merged.
        pending["status"] = "inconsistent"
        if not (isinstance(error, LiveSourceError) and error.code == "source_stale_preview"):
            reasons.append("invalid_state")
        reasons.append("relation_mismatch")
        return False
    pending.update(
        status="present",
        preview_id=value["preview_id"],
        count=len(intakes),
        has_more=has_more,
        coverage_codes=_coverage(value["notices"], has_more),
    )
    return True


def _supported_slack_observation(receipt: Mapping[str, object]) -> bool:
    intake = receipt["intake"]
    return (
        receipt["outcome"] in {"captured", "duplicate"}
        and receipt["evidence"] == "capture_id"
        and isinstance(receipt["capture_id"], str)
        and receipt["capture_id"].startswith("capture_")
        and isinstance(intake, dict)
        and "observation" in intake
        and isinstance(intake.get("key"), dict)
        and intake["key"].get("connector_name") == "slack"
    )


def _project_custody(
    reader: _Reader,
    report: HealthV1,
    entry: dict[str, object],
    binding: str,
    reasons: list[str],
) -> bool:
    raw = reader.read("custody", reader.capture, "custody.json", private_from=reader.live_private)
    if raw is None:
        reasons.append("state_absent")
        return False
    try:
        # CustodyStore construction only retains the store; the preloaded view
        # lets the existing full receipt validator run without its lock.
        state = CustodyStore(cast(PrivateJsonStore, _Preloaded({"custody.json": raw})))._load()
    except LiveSourceError:
        cast(dict[str, object], report["custody"])["status"] = "inconsistent"
        reasons.append("invalid_state")
        return False
    receipts = [
        cast(dict[str, object], item)
        for item in cast(dict[str, object], state["receipts"]).values()
        if isinstance(item, dict)
        and item["source_id"] == entry["source_id"]
        and item["binding"] == binding
    ]
    if len(receipts) > _MAX_BINDINGS:
        raise _Refusal("size_limit")
    receipts.sort(key=lambda item: cast(int, item["sequence"]))
    counts = {
        outcome: sum(item["outcome"] == outcome for item in receipts)
        for outcome in ("pending", "captured", "duplicate", "quarantined", "history_only")
    }
    cast(dict[str, object], report["custody"]).update(
        status="available",
        counts=counts,
        retained_items=sum(item["intake"] is not None for item in receipts),
    )
    bindings: list[dict[str, object]] = []
    quarantined_current = False
    for item in receipts:
        current = (
            item["generation"] == entry["generation"]
            and item["control_epoch"] == entry["control_epoch"]
        )
        supported = _supported_slack_observation(item)
        quarantined_current = quarantined_current or (
            current and item["outcome"] == "quarantined"
        )
        bindings.append(
            {
                "receipt_id": item["receipt_id"],
                "source_id": item["source_id"],
                "generation": item["generation"],
                "control_epoch": item["control_epoch"],
                "binding_verified": True,
                "current_generation": current,
                "supported_slack_observation": supported,
                "outcome": item["outcome"],
                "capture_id": item["capture_id"] if supported else None,
            }
        )
    report["receipt_bindings"] = bindings
    cast(dict[str, object], report["recovery"])["unresolved_receipt_count"] = (
        counts["pending"] + counts["quarantined"]
    )
    return quarantined_current


def _project_execution(report: HealthV1, last_run: object, reasons: list[str]) -> None:
    kind, run = _run(last_run)
    execution = cast(dict[str, object], report["execution"])
    coverage = cast(dict[str, object], report["coverage"])
    account = cast(dict[str, object], report["account"])
    if run is None:
        if last_run is not None:
            reasons.append("legacy_unknown")
        return
    execution.update(outcome=kind, finished_epoch=run["finished_epoch"])
    if kind == "failed":
        failure = _FAILURES.get(cast(str, run["failure_code"]), "operation_failed")
        execution["failure_code"] = failure
        if failure in _ACCESS_FAILURES:
            account.update(
                last_access_failure_code=failure,
                last_access_failure_epoch=run["finished_epoch"],
            )
        return
    execution.update(
        captured_count=run["captured_count"],
        duplicate_count=run["duplicate_count"],
        quarantined_count=run["quarantined_count"],
        last_successful_completion_epoch=run["finished_epoch"],
    )
    has_more = run.get("has_more")
    codes = _coverage(run.get("notices"), has_more)
    coverage.update(
        status="partial"
        if {"partial_page", "thread_partial"} & set(codes)
        else "unsupported"
        if "unsupported_coverage" in codes
        else "unknown",
        has_more=has_more if type(has_more) is bool else None,
        codes=codes,
    )
