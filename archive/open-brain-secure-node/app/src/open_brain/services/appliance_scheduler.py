"""Durable, daemon-owned scheduler for the appliance runtime."""

from __future__ import annotations

import json
import re
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, cast

from open_brain_engine.engine import LocalEngineContext, canonical_json_bytes
from open_brain_engine.storage.operational import (
    RootIdentity,
    StorageError,
    WriteState,
    atomic_replace,
    atomic_write_new,
    confined_unlink,
    read_confined,
)

APPLIANCE_SCHEDULER_DIRECTORY = PurePosixPath(".open-brain/state/appliance-scheduler")
APPLIANCE_SCHEDULER_STATE = APPLIANCE_SCHEDULER_DIRECTORY / "state.json"
_RUNS_DIRECTORY = APPLIANCE_SCHEDULER_DIRECTORY / "runs"
_SCHEMA_VERSION: Final[int] = 1
_MAXIMUM_STATE_BYTES: Final[int] = 4_096
_MAXIMUM_RECEIPT_BYTES: Final[int] = 2_048
_MAXIMUM_CONNECTORS: Final[int] = 8
MAXIMUM_RETAINED_RUN_RECEIPTS: Final[int] = 8
_MAXIMUM_PRUNE_ENTRIES: Final[int] = MAXIMUM_RETAINED_RUN_RECEIPTS * 4
_FIXED_JOB_NAMES = frozenset(
    {
        "backup-create",
        "engine-recover",
        "markdown-reconcile",
        "portable-export",
        "portable-import",
    }
)
_JOB_STATUS = frozenset({"completed", "deferred", "empty", "failed"})
_JOB_RESULT_STATUS = frozenset({"completed", "deferred", "empty"})
_CONNECTOR_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RUN_ID_PREFIX: Final[str] = "run_"


@dataclass(frozen=True, slots=True)
class ApplianceScheduledJob:
    name: str
    recurring: bool
    interval_seconds: int | None


@dataclass(frozen=True, slots=True)
class ApplianceRunContext:
    root: Path
    job_name: str
    run_id: str
    attempt: int


@dataclass(frozen=True, slots=True)
class ApplianceJobResult:
    status: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _JOB_RESULT_STATUS:
            raise ValueError("invalid appliance job result")
        _validate_reason_code(self.reason, error_message="invalid appliance job result")

    @classmethod
    def completed(cls, reason: str | None = None) -> ApplianceJobResult:
        return cls("completed", reason=reason)

    @classmethod
    def deferred(cls, reason: str | None = None) -> ApplianceJobResult:
        return cls("deferred", reason=reason)

    @classmethod
    def empty(cls, reason: str | None = None) -> ApplianceJobResult:
        return cls("empty", reason=reason)


@dataclass(frozen=True, slots=True)
class ApplianceRunReceipt:
    job_name: str
    run_id: str
    attempt: int
    status: str
    started_at: str
    finished_at: str
    next_due_at: str | None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not is_appliance_job_name(self.job_name) or self.status not in _JOB_STATUS:
            raise ValueError("invalid appliance run receipt")
        _validate_run_id(self.run_id, error_message="invalid appliance run receipt")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt <= 0:
            raise ValueError("invalid appliance run receipt")
        started_at = _canonical_timestamp(
            self.started_at,
            error_message="invalid appliance run receipt",
        )
        finished_at = _canonical_timestamp(
            self.finished_at,
            error_message="invalid appliance run receipt",
        )
        if finished_at < started_at:
            raise ValueError("invalid appliance run receipt")
        if self.next_due_at is not None:
            _canonical_timestamp(self.next_due_at, error_message="invalid appliance run receipt")
        _validate_reason_code(self.reason, error_message="invalid appliance run receipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "finished_at": self.finished_at,
            "job_name": self.job_name,
            "next_due_at": self.next_due_at,
            "reason": self.reason,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "status": self.status,
        }


class ApplianceSchedulerRetryableError(RuntimeError):
    """A job failed for a retryable reason and should become due again later."""

    def __init__(self, reason: str, *, retry_delay_seconds: int) -> None:
        _validate_reason_code(reason, error_message="invalid appliance scheduler retry")
        if (
            not isinstance(retry_delay_seconds, int)
            or isinstance(retry_delay_seconds, bool)
            or retry_delay_seconds <= 0
        ):
            raise ValueError("invalid appliance scheduler retry")
        self.reason = reason
        self.retry_delay_seconds = retry_delay_seconds
        super().__init__(reason)


class ApplianceSchedulerInterruptedError(RuntimeError):
    """A job was interrupted after claim and must replay with the same run id."""


JobHandler = Callable[[ApplianceRunContext], ApplianceJobResult]


class ApplianceScheduler:
    """Persist one daemon-owned due-job inventory and metadata-only run evidence."""

    def __init__(
        self,
        profile: LocalEngineContext,
        *,
        connector_names: tuple[str, ...] = (),
        handlers: Mapping[str, JobHandler] | None = None,
        engine_recoverer: Callable[[], int] | None = None,
        now: datetime | None = None,
    ) -> None:
        if not isinstance(profile, LocalEngineContext):
            raise ValueError("invalid appliance scheduler root")
        self._root = profile.root
        self._root_identity = profile.root_identity
        self._jobs = _inventory(connector_names)
        self._handlers = MappingProxyType(dict(handlers or {}))
        self._engine_recoverer = engine_recoverer
        self._seed_now = _normalized_now(now or datetime.now(UTC))
        self._ensure_state()

    def inventory(self) -> tuple[ApplianceScheduledJob, ...]:
        return self._jobs

    def read_state(self) -> dict[str, object]:
        payload = _read_scheduler_bytes(
            self._root,
            relative=APPLIANCE_SCHEDULER_STATE,
            root_identity=self._root_identity,
            maximum_bytes=_MAXIMUM_STATE_BYTES,
        )
        if payload is None:
            raise ValueError("invalid appliance scheduler state")
        return _load_state(payload, self._jobs)

    def request(self, job_name: str, *, now: datetime | None = None) -> None:
        selected = _job_by_name(self._jobs, job_name)
        state = self.read_state()
        jobs = cast(dict[str, dict[str, object]], state["jobs"])
        row = dict(jobs[selected.name])
        row["next_due_at"] = _isoformat(_normalized_now(now or self._seed_now))
        jobs[selected.name] = _validated_job_row(selected, row)
        self._write_state(state)

    def run_due(self, *, now: datetime) -> tuple[ApplianceRunReceipt, ...]:
        current = _normalized_now(now)
        state = self.read_state()
        jobs = cast(dict[str, dict[str, object]], state["jobs"])
        receipts: list[ApplianceRunReceipt] = []
        for job in self._jobs:
            row = dict(jobs[job.name])
            if not _is_due(row, current):
                continue
            persisted = self._read_run_receipt(job.name, _row_optional_str(row, "active_run_id"))
            if persisted is not None:
                _validate_persisted_receipt(job, row, persisted)
                jobs[job.name] = _row_from_receipt(job, persisted)
                self._write_state(state)
                receipts.append(persisted)
                continue
            row = _claim_job(row, now=current)
            jobs[job.name] = row
            self._write_state(state)
            context = ApplianceRunContext(
                root=self._root,
                job_name=job.name,
                run_id=_row_required_str(row, "active_run_id"),
                attempt=_row_int(row, "active_attempt"),
            )
            try:
                result = self._dispatch(job.name, context)
            except ApplianceSchedulerInterruptedError:
                raise
            except ApplianceSchedulerRetryableError as error:
                receipt = _finish_job(
                    job,
                    row,
                    now=current,
                    status="failed",
                    reason=error.reason,
                    next_due_at=current + timedelta(seconds=error.retry_delay_seconds),
                )
            except Exception:
                receipt = _finish_job(
                    job,
                    row,
                    now=current,
                    status="failed",
                    reason="job_failed",
                    next_due_at=_next_retry(job, current),
                )
            else:
                receipt = _finish_job(
                    job,
                    row,
                    now=current,
                    status=result.status,
                    reason=result.reason,
                    next_due_at=_next_due(job, current),
                )
            self._write_run_receipt(receipt)
            jobs[job.name] = _row_from_receipt(job, receipt)
            self._write_state(state)
            receipts.append(receipt)
        return tuple(receipts)

    def _dispatch(self, job_name: str, context: ApplianceRunContext) -> ApplianceJobResult:
        handler = self._handlers.get(job_name)
        if handler is not None:
            return handler(context)
        if job_name == "engine-recover":
            recovered = 0 if self._engine_recoverer is None else self._engine_recoverer()
            return ApplianceJobResult.completed() if recovered > 0 else ApplianceJobResult.empty()
        if job_name == "markdown-reconcile":
            return ApplianceJobResult.deferred("w4_owned")
        if (
            job_name in {"portable-export", "portable-import", "backup-create"}
            or job_name.startswith("connector-run:")
        ):
            return ApplianceJobResult.deferred("handler_deferred")
        raise ValueError("unknown appliance scheduler job")

    def _ensure_state(self) -> None:
        existing = _read_scheduler_bytes(
            self._root,
            relative=APPLIANCE_SCHEDULER_STATE,
            root_identity=self._root_identity,
            maximum_bytes=_MAXIMUM_STATE_BYTES,
        )
        if existing is not None:
            _load_state(existing, self._jobs)
            return
        _write_scheduler_new(
            self._root,
            relative=APPLIANCE_SCHEDULER_STATE,
            root_identity=self._root_identity,
            data=_bounded_canonical_json(
                _initial_state(self._jobs, now=self._seed_now),
                maximum_bytes=_MAXIMUM_STATE_BYTES,
            ),
        )

    def _write_run_receipt(self, receipt: ApplianceRunReceipt) -> None:
        _write_scheduler_new(
            self._root,
            relative=_run_receipt_path(receipt.job_name, receipt.run_id),
            root_identity=self._root_identity,
            data=_bounded_canonical_json(
                receipt.to_dict(),
                maximum_bytes=_MAXIMUM_RECEIPT_BYTES,
            ),
        )
        _prune_run_receipts(
            self._root,
            root_identity=self._root_identity,
            job_name=receipt.job_name,
        )

    def _read_run_receipt(
        self,
        job_name: str,
        run_id: str | None,
    ) -> ApplianceRunReceipt | None:
        if run_id is None:
            return None
        payload = _read_scheduler_bytes(
            self._root,
            relative=_run_receipt_path(job_name, run_id),
            root_identity=self._root_identity,
            maximum_bytes=_MAXIMUM_RECEIPT_BYTES,
        )
        if payload is None:
            return None
        return _load_run_receipt(payload, job_name=job_name)

    def _write_state(self, state: dict[str, object]) -> None:
        normalized = _validated_state(state, self._jobs)
        _replace_scheduler_bytes(
            self._root,
            relative=APPLIANCE_SCHEDULER_STATE,
            root_identity=self._root_identity,
            data=_bounded_canonical_json(
                normalized,
                maximum_bytes=_MAXIMUM_STATE_BYTES,
            ),
        )


def _inventory(connector_names: tuple[str, ...]) -> tuple[ApplianceScheduledJob, ...]:
    if not isinstance(connector_names, tuple) or len(connector_names) > _MAXIMUM_CONNECTORS:
        raise ValueError("invalid connector allow-list")
    seen: set[str] = set()
    for name in connector_names:
        if (
            not isinstance(name, str)
            or _CONNECTOR_NAME.fullmatch(name) is None
            or name in seen
        ):
            raise ValueError("invalid connector allow-list")
        seen.add(name)
    return (
        ApplianceScheduledJob("engine-recover", recurring=True, interval_seconds=300),
        ApplianceScheduledJob("markdown-reconcile", recurring=True, interval_seconds=300),
        *(
            ApplianceScheduledJob(
                f"connector-run:{name}",
                recurring=True,
                interval_seconds=300,
            )
            for name in connector_names
        ),
        ApplianceScheduledJob("portable-export", recurring=False, interval_seconds=None),
        ApplianceScheduledJob("portable-import", recurring=False, interval_seconds=None),
        ApplianceScheduledJob("backup-create", recurring=False, interval_seconds=None),
    )


def _job_by_name(
    inventory: tuple[ApplianceScheduledJob, ...], job_name: str
) -> ApplianceScheduledJob:
    for job in inventory:
        if job.name == job_name:
            return job
    raise ValueError("unknown appliance scheduler job")


def _initial_state(
    inventory: tuple[ApplianceScheduledJob, ...], *, now: datetime
) -> dict[str, object]:
    jobs: dict[str, object] = {}
    for job in inventory:
        jobs[job.name] = {
            "active_attempt": 0,
            "active_run_id": None,
            "active_started_at": None,
            "interval_seconds": job.interval_seconds,
            "last_status": None,
            "name": job.name,
            "next_due_at": _isoformat(now) if job.recurring else None,
            "recurring": job.recurring,
        }
    return _validated_state({"jobs": jobs, "schema_version": _SCHEMA_VERSION}, inventory)


def _validated_state(
    value: object,
    inventory: tuple[ApplianceScheduledJob, ...],
) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("invalid appliance scheduler state")
    state = cast(dict[str, object], dict(value))
    if set(state) != {"jobs", "schema_version"}:
        raise ValueError("invalid appliance scheduler state")
    schema_version = state.get("schema_version")
    if schema_version != _SCHEMA_VERSION:
        raise ValueError("invalid appliance scheduler state")
    jobs_value = state.get("jobs")
    if type(jobs_value) is not dict:
        raise ValueError("invalid appliance scheduler state")
    jobs = cast(dict[str, object], jobs_value)
    expected_names = [job.name for job in inventory]
    if sorted(jobs) != sorted(expected_names):
        raise ValueError("invalid appliance scheduler state")
    normalized_jobs: dict[str, object] = {}
    for job in inventory:
        row = jobs.get(job.name)
        normalized_jobs[job.name] = _validated_job_row(job, row)
    normalized = {"jobs": normalized_jobs, "schema_version": _SCHEMA_VERSION}
    if canonical_json_bytes(normalized) != canonical_json_bytes(value):
        raise ValueError("invalid appliance scheduler state")
    return normalized


def _validated_job_row(job: ApplianceScheduledJob, value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("invalid appliance scheduler state")
    row = cast(dict[str, object], dict(value))
    expected = {
        "active_attempt",
        "active_run_id",
        "active_started_at",
        "interval_seconds",
        "last_status",
        "name",
        "next_due_at",
        "recurring",
    }
    if set(row) != expected:
        raise ValueError("invalid appliance scheduler state")
    if row.get("name") != job.name:
        raise ValueError("invalid appliance scheduler state")
    if row.get("recurring") is not job.recurring:
        raise ValueError("invalid appliance scheduler state")
    if row.get("interval_seconds") != job.interval_seconds:
        raise ValueError("invalid appliance scheduler state")
    active_attempt = _row_int(row, "active_attempt")
    if active_attempt < 0:
        raise ValueError("invalid appliance scheduler state")
    last_status = row.get("last_status")
    if last_status is not None and last_status not in _JOB_STATUS:
        raise ValueError("invalid appliance scheduler state")
    next_due_at = row.get("next_due_at")
    if next_due_at is not None:
        if not isinstance(next_due_at, str):
            raise ValueError("invalid appliance scheduler state")
        _canonical_timestamp(next_due_at, error_message="invalid appliance scheduler state")
    active_run_id = row.get("active_run_id")
    active_started_at = row.get("active_started_at")
    if active_run_id is None or active_started_at is None:
        if active_run_id is not None or active_started_at is not None or active_attempt != 0:
            raise ValueError("invalid appliance scheduler state")
    else:
        if active_attempt <= 0:
            raise ValueError("invalid appliance scheduler state")
        if not isinstance(active_run_id, str) or not isinstance(active_started_at, str):
            raise ValueError("invalid appliance scheduler state")
        _validate_run_id(active_run_id, error_message="invalid appliance scheduler state")
        _canonical_timestamp(active_started_at, error_message="invalid appliance scheduler state")
    return {
        "active_attempt": active_attempt,
        "active_run_id": active_run_id,
        "active_started_at": active_started_at,
        "interval_seconds": job.interval_seconds,
        "last_status": last_status,
        "name": job.name,
        "next_due_at": next_due_at,
        "recurring": job.recurring,
    }


def _validate_persisted_receipt(
    job: ApplianceScheduledJob,
    row: Mapping[str, object],
    receipt: ApplianceRunReceipt,
) -> None:
    if (
        receipt.job_name != job.name
        or receipt.run_id != _row_required_str(row, "active_run_id")
        or receipt.attempt != _row_int(row, "active_attempt")
        or receipt.started_at != _row_required_str(row, "active_started_at")
    ):
        raise ValueError("invalid appliance scheduler state")


def _is_due(row: Mapping[str, object], now: datetime) -> bool:
    if row.get("active_run_id") is not None:
        return True
    next_due_at = row.get("next_due_at")
    return isinstance(next_due_at, str) and _parse_timestamp(next_due_at) <= now


def _claim_job(row: Mapping[str, object], *, now: datetime) -> dict[str, object]:
    run_id = _row_optional_str(row, "active_run_id") or f"{_RUN_ID_PREFIX}{uuid.uuid4()}"
    started_at = _row_optional_str(row, "active_started_at") or _isoformat_required(now)
    return {
        **dict(row),
        "active_attempt": _row_int(row, "active_attempt", default=0) + 1,
        "active_run_id": run_id,
        "active_started_at": started_at,
    }


def _finish_job(
    job: ApplianceScheduledJob,
    row: Mapping[str, object],
    *,
    now: datetime,
    status: str,
    reason: str | None,
    next_due_at: datetime | None,
) -> ApplianceRunReceipt:
    if status not in _JOB_STATUS:
        raise ValueError("invalid appliance scheduler status")
    return ApplianceRunReceipt(
        job_name=job.name,
        run_id=_row_required_str(row, "active_run_id"),
        attempt=_row_int(row, "active_attempt"),
        status=status,
        started_at=_row_required_str(row, "active_started_at"),
        finished_at=_isoformat_required(now),
        next_due_at=_isoformat(next_due_at) if next_due_at is not None else None,
        reason=reason,
    )


def _row_from_receipt(
    job: ApplianceScheduledJob, receipt: ApplianceRunReceipt
) -> dict[str, object]:
    return {
        "active_attempt": 0,
        "active_run_id": None,
        "active_started_at": None,
        "interval_seconds": job.interval_seconds,
        "last_status": receipt.status,
        "name": job.name,
        "next_due_at": receipt.next_due_at,
        "recurring": job.recurring,
    }


def _next_due(job: ApplianceScheduledJob, now: datetime) -> datetime | None:
    if not job.recurring or job.interval_seconds is None:
        return None
    return now + timedelta(seconds=job.interval_seconds)


def _next_retry(job: ApplianceScheduledJob, now: datetime) -> datetime | None:
    if not job.recurring:
        return None
    interval = 60 if job.name == "engine-recover" else 300
    return now + timedelta(seconds=interval)


def _load_state(
    payload: bytes,
    inventory: tuple[ApplianceScheduledJob, ...],
) -> dict[str, object]:
    value = _load_canonical_json(payload, error_message="invalid appliance scheduler state")
    return _validated_state(value, inventory)


def read_scheduler_snapshot(
    root: Path,
    root_identity: RootIdentity,
    *,
    now: datetime,
) -> dict[str, object]:
    payload = _read_scheduler_bytes(
        root,
        relative=APPLIANCE_SCHEDULER_STATE,
        root_identity=root_identity,
        maximum_bytes=_MAXIMUM_STATE_BYTES,
    )
    if payload is None:
        return {
            "active_count": 0,
            "due_count": 0,
            "jobs": [],
            "queue_age_seconds": 0,
            "state": "absent",
        }
    try:
        state = _load_state(payload, _inventory(()))
    except ValueError:
        return {
            "active_count": 0,
            "due_count": 0,
            "jobs": [],
            "queue_age_seconds": 0,
            "state": "invalid",
        }
    return _status_snapshot_from_state(state, now=now)


def _load_run_receipt(payload: bytes, *, job_name: str) -> ApplianceRunReceipt:
    value = _load_canonical_json(payload, error_message="invalid appliance scheduler state")
    if type(value) is not dict or set(value) != {
        "attempt",
        "finished_at",
        "job_name",
        "next_due_at",
        "reason",
        "run_id",
        "started_at",
        "status",
    }:
        raise ValueError("invalid appliance scheduler state")
    receipt = ApplianceRunReceipt(
        attempt=_required_int(value, "attempt"),
        finished_at=_required_str(value, "finished_at"),
        job_name=_required_str(value, "job_name"),
        next_due_at=_optional_str(value, "next_due_at"),
        reason=_optional_str(value, "reason"),
        run_id=_required_str(value, "run_id"),
        started_at=_required_str(value, "started_at"),
        status=_required_str(value, "status"),
    )
    if receipt.job_name != job_name:
        raise ValueError("invalid appliance scheduler state")
    return receipt


def _load_canonical_json(payload: bytes, *, error_message: str) -> object:
    if not isinstance(payload, bytes) or not payload:
        raise ValueError(error_message)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(error_message) from None
    if canonical_json_bytes(value) != payload:
        raise ValueError(error_message)
    return value


def _canonical_timestamp(value: str, *, error_message: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(error_message)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        raise ValueError(error_message) from None
    if _isoformat_required(parsed) != value:
        raise ValueError(error_message)
    return parsed


def _validate_run_id(value: str, *, error_message: str) -> None:
    if not isinstance(value, str) or not value.startswith(_RUN_ID_PREFIX):
        raise ValueError(error_message)
    try:
        identifier = uuid.UUID(value.removeprefix(_RUN_ID_PREFIX))
    except ValueError:
        raise ValueError(error_message) from None
    if identifier.version != 4 or value != f"{_RUN_ID_PREFIX}{identifier}":
        raise ValueError(error_message)


def _validate_reason_code(value: str | None, *, error_message: str) -> None:
    if value is not None and (not isinstance(value, str) or _REASON_CODE.fullmatch(value) is None):
        raise ValueError(error_message)


def is_appliance_job_name(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value in _FIXED_JOB_NAMES:
        return True
    prefix = "connector-run:"
    return (
        value.startswith(prefix)
        and _CONNECTOR_NAME.fullmatch(value.removeprefix(prefix)) is not None
    )


def _required_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError("invalid appliance scheduler state")
    return item


def _required_str(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError("invalid appliance scheduler state")
    return item


def _optional_str(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError("invalid appliance scheduler state")
    return item


def _run_receipt_path(job_name: str, run_id: str) -> PurePosixPath:
    return _RUNS_DIRECTORY / job_name / f"{run_id}.json"


def _bounded_canonical_json(value: object, *, maximum_bytes: int) -> bytes:
    payload = canonical_json_bytes(value)
    if len(payload) > maximum_bytes:
        raise ValueError("invalid appliance scheduler state")
    return payload


def _read_scheduler_bytes(
    root: Path,
    *,
    relative: PurePosixPath,
    root_identity: RootIdentity,
    maximum_bytes: int,
) -> bytes | None:
    try:
        return read_confined(
            root=root,
            relative=relative,
            expected_root_identity=root_identity,
            maximum_bytes=maximum_bytes,
        )
    except StorageError:
        raise ValueError("invalid appliance scheduler state") from None


def _write_scheduler_new(
    root: Path,
    *,
    relative: PurePosixPath,
    root_identity: RootIdentity,
    data: bytes,
) -> None:
    try:
        state = atomic_write_new(
            root=root,
            relative=relative,
            data=data,
            expected_root_identity=root_identity,
        )
    except StorageError:
        raise ValueError("invalid appliance scheduler state") from None
    if state not in {WriteState.CREATED, WriteState.ALREADY_EXISTS}:
        raise ValueError("invalid appliance scheduler state")


def _replace_scheduler_bytes(
    root: Path,
    *,
    relative: PurePosixPath,
    root_identity: RootIdentity,
    data: bytes,
) -> None:
    try:
        atomic_replace(
            root=root,
            relative=relative,
            data=data,
            require_existing=True,
            expected_root_identity=root_identity,
        )
    except StorageError:
        raise ValueError("invalid appliance scheduler state") from None


def _normalized_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid appliance scheduler time")
    return value.astimezone(UTC)


def _row_int(row: Mapping[str, object], key: str, *, default: int | None = None) -> int:
    value = row.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("invalid appliance scheduler state")
    return value


def _row_optional_str(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("invalid appliance scheduler state")
    return value


def _row_required_str(row: Mapping[str, object], key: str) -> str:
    value = _row_optional_str(row, key)
    if value is None:
        raise ValueError("invalid appliance scheduler state")
    return value


def _parse_timestamp(value: str) -> datetime:
    return _canonical_timestamp(value, error_message="invalid appliance scheduler state")


def _isoformat_required(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _isoformat_required(value)


def _status_snapshot_from_state(
    state: Mapping[str, object],
    *,
    now: datetime,
) -> dict[str, object]:
    jobs = cast(dict[str, dict[str, object]], state["jobs"])
    rows: list[dict[str, object]] = []
    due_at: list[datetime] = []
    active_count = 0
    for name in sorted(jobs):
        row = jobs[name]
        next_due_at = cast(str | None, row["next_due_at"])
        parsed_due = None if next_due_at is None else _parse_timestamp(next_due_at)
        if row["active_run_id"] is not None:
            active_count += 1
        if parsed_due is not None and parsed_due <= now:
            due_at.append(parsed_due)
        rows.append(
            {
                "active": row["active_run_id"] is not None,
                "attempt": row["active_attempt"],
                "last_status": row["last_status"],
                "name": name,
                "next_due_at": next_due_at,
                "recurring": row["recurring"],
            }
        )
    state_value = "running" if active_count else "due" if due_at else "idle"
    return {
        "active_count": active_count,
        "due_count": len(due_at),
        "jobs": rows,
        "queue_age_seconds": (
            0
            if not due_at
            else max(int((now - min(due_at)).total_seconds()), 0)
        ),
        "state": state_value,
    }


def _prune_run_receipts(
    root: Path,
    *,
    root_identity: RootIdentity,
    job_name: str,
) -> None:
    receipts_root = root / _RUNS_DIRECTORY / job_name
    try:
        root_metadata = receipts_root.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ValueError("invalid appliance scheduler state") from None
    if (
        not is_appliance_job_name(job_name)
        or stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
    ):
        raise ValueError("invalid appliance scheduler state")
    entries: list[tuple[int, str, PurePosixPath]] = []
    try:
        for index, entry in enumerate(receipts_root.iterdir(), start=1):
            if index > _MAXIMUM_PRUNE_ENTRIES:
                raise ValueError("invalid appliance scheduler state")
            metadata = entry.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or not entry.name.endswith(".json")
            ):
                continue
            relative = _RUNS_DIRECTORY / job_name / entry.name
            rank = -1
            payload = _read_scheduler_bytes(
                root,
                relative=relative,
                root_identity=root_identity,
                maximum_bytes=_MAXIMUM_RECEIPT_BYTES,
            )
            if payload is not None:
                try:
                    receipt = _load_run_receipt(payload, job_name=job_name)
                except ValueError:
                    receipt = None
                if receipt is not None:
                    rank = int(_parse_timestamp(receipt.finished_at).timestamp())
            entries.append((rank, entry.name, relative))
    except OSError:
        raise ValueError("invalid appliance scheduler state") from None
    entries.sort(reverse=True)
    for _, _, relative in entries[MAXIMUM_RETAINED_RUN_RECEIPTS:]:
        _unlink_scheduler_path(root, relative=relative, root_identity=root_identity)


def _unlink_scheduler_path(
    root: Path,
    *,
    relative: PurePosixPath,
    root_identity: RootIdentity,
) -> None:
    try:
        confined_unlink(
            root=root,
            relative=relative,
            expected_root_identity=root_identity,
        )
    except StorageError:
        raise ValueError("invalid appliance scheduler state") from None


__all__ = [
    "APPLIANCE_SCHEDULER_DIRECTORY",
    "MAXIMUM_RETAINED_RUN_RECEIPTS",
    "ApplianceJobResult",
    "ApplianceRunContext",
    "ApplianceRunReceipt",
    "ApplianceScheduledJob",
    "ApplianceScheduler",
    "ApplianceSchedulerInterruptedError",
    "ApplianceSchedulerRetryableError",
    "is_appliance_job_name",
]
