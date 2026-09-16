"""Durable lifecycle controls for the optional unattended collector."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, cast, runtime_checkable

from open_brain_engine.engine import PublicJobCaptureSink

from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = [
    "CollectorCommandResult",
    "CollectorController",
    "CollectorRunOutcome",
    "CollectorRunPage",
    "CollectorLease",
    "CollectorLeaseError",
    "CollectorSourceRuntime",
    "CollectorStateStore",
    "CollectorStorageError",
    "CredentialStatusProvider",
    "EngineCaptureSink",
    "MemoryCaptureSink",
]

_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_STATUS = {"disabled", "enabled", "paused"}
_OUTCOME = {"completed", "deferred", "empty", "failed", "skipped"}
_FAILURE_CODE = {
    "capture_failed",
    "credential_locked",
    "credential_missing",
    "collector_paused",
    "collector_disabled",
    "storage_unavailable",
}
_CREDENTIAL_STATUS = {"available", "locked", "missing"}
_MAX_INTERVAL = 31_536_000


class CollectorRunOutcome(StrEnum):
    """Metadata-only collector invocation outcomes."""

    COMPLETED = "completed"
    DEFERRED = "deferred"
    EMPTY = "empty"
    FAILED = "failed"
    SKIPPED = "skipped"


class CollectorLeaseError(RuntimeError):
    """Another owner already holds the collector lease."""


class CollectorStorageError(RuntimeError):
    """Collector state could not be persisted durably."""


@dataclass(frozen=True, slots=True)
class CollectorRunPage:
    """One bounded adapter page ready for collector capture."""

    selection: SourceResourceSelection
    intakes: tuple[SourceRecordIntake, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.selection) is not SourceResourceSelection
            or not isinstance(self.intakes, tuple)
            or len(self.intakes) > 25
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str
                    or _SOURCE_ID.fullmatch(self.next_cursor) is None
                )
            )
        ):
            raise ConnectorContractError("invalid collector page")
        if any(type(intake) is not SourceRecordIntake for intake in self.intakes):
            raise ConnectorContractError("invalid collector page")
        if any(
            intake.key.connector_name != self.selection.connector_name
            or intake.key.connection_id != self.selection.connection_id
            or intake.key.resource_id != self.selection.resource_id
            for intake in self.intakes
        ):
            raise ConnectorContractError("invalid collector page")


@runtime_checkable
class CollectorSourceRuntime(Protocol):
    """A selected source adapter bound by the host for one sync."""

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        """Return the next bounded page for a selected resource."""


@runtime_checkable
class _CaptureSink(Protocol):
    def submit(self, intake: SourceRecordIntake) -> None:
        """Persist one changed intake with its stable delivery key."""


@runtime_checkable
class CredentialStatusProvider(Protocol):
    """Metadata-only credential status check performed before source fetch."""

    def status_for(self, source_id: str) -> str:
        """Return available, locked, or missing without revealing credential values."""


@dataclass(frozen=True, slots=True)
class CollectorCommandResult:
    """Compact, metadata-only result for one lifecycle command."""

    source_id: str
    status: str
    outcome: CollectorRunOutcome
    captured_count: int = 0
    duplicate_count: int = 0
    next_run_epoch: int | None = None
    pause_ack_epoch: int | None = None
    next_cursor: str | None = None
    failure_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "captured_count": self.captured_count,
            "duplicate_count": self.duplicate_count,
            "next_cursor": self.next_cursor,
            "next_run_epoch": self.next_run_epoch,
            "outcome": self.outcome.value,
            "pause_ack_epoch": self.pause_ack_epoch,
            "schema_version": 1,
            "source_id": self.source_id,
            "status": self.status,
            "failure_code": self.failure_code,
        }


class CollectorStateStore:
    """Atomic JSON state for explicitly enabled collector sources."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ConnectorContractError("invalid collector state")
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, object]:
        if not self._path.exists():
            return {"schema_version": 1, "sources": {}}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid collector state") from error
        if not isinstance(loaded, dict):
            raise ConnectorContractError("invalid collector state")
        _validate_state(loaded)
        return loaded

    def save(self, state: Mapping[str, object]) -> None:
        if not isinstance(state, Mapping):
            raise ConnectorContractError("invalid collector state")
        durable = dict(state)
        _validate_state(durable)
        temp_name: str | None = None
        payload = json.dumps(durable, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with NamedTemporaryFile(
                "w",
                delete=False,
                dir=self._path.parent,
                encoding="utf-8",
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            ) as handle:
                handle.write(payload)
                temp_name = handle.name
            Path(temp_name).chmod(0o600)
            os.replace(temp_name, self._path)
        except OSError:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)
            raise CollectorStorageError("collector_storage_unavailable") from None
        try:
            self._path.chmod(0o600)
        except OSError:
            raise CollectorStorageError("collector_storage_unavailable") from None


class CollectorLease:
    """Exclusive collector ownership marker with explicit owner metadata."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ConnectorContractError("invalid collector lease")
        self._path = path
        self._held = False

    @contextmanager
    def acquire(self, *, owner: str, pid: int | None = None) -> Iterator[None]:
        _validate_source_id(owner)
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(
            {"owner": owner, "pid": os.getpid() if pid is None else pid, "schema_version": 1},
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        try:
            fd = self._create_lock_file()
        except FileExistsError as error:
            if self._remove_stale_owner():
                try:
                    fd = self._create_lock_file()
                except FileExistsError as retry_error:
                    raise CollectorLeaseError("collector_already_owned") from retry_error
            else:
                raise CollectorLeaseError("collector_already_owned") from error
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            self._held = True
            yield
        finally:
            if self._held:
                self._path.unlink(missing_ok=True)
                self._held = False

    def _create_lock_file(self) -> int:
        return os.open(
            self._path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )

    def _remove_stale_owner(self) -> bool:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, Mapping):
            return False
        pid = payload.get("pid")
        if type(pid) is not int or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                self._path.unlink()
            except FileNotFoundError:
                return True
            except OSError:
                return False
            return True
        except PermissionError:
            return False
        return False


class CollectorController:
    """Apply explicit lifecycle commands and scheduled source syncs."""

    def __init__(
        self,
        store: CollectorStateStore,
        *,
        clock: Callable[[], int],
    ) -> None:
        if type(store) is not CollectorStateStore or not callable(clock):
            raise ConnectorContractError("invalid collector controller")
        self._store = store
        self._clock = clock

    def enable(
        self,
        *,
        source_id: str,
        selection: SourceResourceSelection,
        interval_seconds: int,
        credential_ref: str | None = None,
    ) -> CollectorCommandResult:
        _validate_source_id(source_id)
        if type(selection) is not SourceResourceSelection:
            raise ConnectorContractError("invalid collector source")
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= _MAX_INTERVAL:
            raise ConnectorContractError("invalid collector schedule")
        if credential_ref is not None:
            _validate_source_id(credential_ref)
        state = self._store.load()
        sources = _sources(state)
        previous = sources.get(source_id)
        if not isinstance(previous, dict):
            previous = {}
        sources[source_id] = {
            "committed_revisions": previous.get("committed_revisions", {}),
            "active_run": previous.get("active_run"),
            "connection_id": selection.connection_id,
            "connector_name": selection.connector_name,
            "credential_ref": credential_ref,
            "interval_seconds": interval_seconds,
            "last_run": previous.get("last_run"),
            "last_success_epoch": previous.get(
                "last_success_epoch",
                _last_success_epoch_from_last_run(previous.get("last_run")),
            ),
            "next_cursor": previous.get("next_cursor"),
            "next_run_epoch": self._clock(),
            "pause_ack_epoch": None,
            "resource_id": selection.resource_id,
            "resource_type": selection.resource_type,
            "status": "enabled",
        }
        self._store.save(state)
        return self.status(source_id)

    def disable(self, source_id: str) -> CollectorCommandResult:
        state = self._store.load()
        entry = _source(_sources(state), source_id)
        entry["status"] = "disabled"
        entry["next_run_epoch"] = None
        self._store.save(state)
        return self.status(source_id)

    def pause(self, source_id: str) -> CollectorCommandResult:
        state = self._store.load()
        entry = _source(_sources(state), source_id)
        now = self._clock()
        entry["status"] = "paused"
        entry["pause_ack_epoch"] = now
        self._store.save(state)
        return self.status(source_id)

    def resume(self, source_id: str) -> CollectorCommandResult:
        state = self._store.load()
        entry = _source(_sources(state), source_id)
        entry["status"] = "enabled"
        entry["pause_ack_epoch"] = None
        entry["next_run_epoch"] = self._clock()
        self._store.save(state)
        return self.status(source_id)

    def status(self, source_id: str) -> CollectorCommandResult:
        state = self._store.load()
        entry = _source(_sources(state), source_id)
        return CollectorCommandResult(
            source_id=source_id,
            status=cast(str, entry["status"]),
            outcome=CollectorRunOutcome.SKIPPED,
            next_run_epoch=cast(int | None, entry.get("next_run_epoch")),
            pause_ack_epoch=cast(int | None, entry.get("pause_ack_epoch")),
            next_cursor=cast(str | None, entry.get("next_cursor")),
        )

    def schedule(self, source_id: str, interval_seconds: int) -> CollectorCommandResult:
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= _MAX_INTERVAL:
            raise ConnectorContractError("invalid collector schedule")
        state = self._store.load()
        entry = _source(_sources(state), source_id)
        entry["interval_seconds"] = interval_seconds
        if entry["status"] == "enabled":
            entry["next_run_epoch"] = self._clock()
        elif entry["status"] == "disabled":
            entry["next_run_epoch"] = None
        self._store.save(state)
        return self.status(source_id)

    def sync_now(self, source_id: str) -> CollectorCommandResult:
        state = self._store.load()
        entry = _source(_sources(state), source_id)
        if entry["status"] != "enabled":
            raise ConnectorContractError("collector source not enabled")
        entry["next_run_epoch"] = self._clock()
        self._store.save(state)
        return self.status(source_id)

    def sync_due(
        self,
        *,
        source_id: str,
        runtime: CollectorSourceRuntime,
        capture_sink: _CaptureSink,
        credential_status: CredentialStatusProvider | None = None,
    ) -> CollectorCommandResult:
        if not isinstance(runtime, CollectorSourceRuntime) or not isinstance(
            capture_sink,
            _CaptureSink,
        ):
            raise ConnectorContractError("invalid collector runtime")
        if credential_status is not None and not isinstance(
            credential_status,
            CredentialStatusProvider,
        ):
            raise ConnectorContractError("invalid collector credential status")
        state = self._store.load()
        entry = _source(_sources(state), source_id)
        status = cast(str, entry["status"])
        if status == "disabled":
            self._record_terminal_run(state, source_id, entry, CollectorRunOutcome.SKIPPED, 0, 0)
            return _result(source_id, entry, CollectorRunOutcome.SKIPPED)
        if status == "paused":
            self._record_terminal_run(state, source_id, entry, CollectorRunOutcome.DEFERRED, 0, 0)
            return _result(source_id, entry, CollectorRunOutcome.DEFERRED)
        next_run = cast(int, entry["next_run_epoch"])
        now = self._clock()
        if now < next_run:
            self._record_terminal_run(state, source_id, entry, CollectorRunOutcome.DEFERRED, 0, 0)
            return _result(source_id, entry, CollectorRunOutcome.DEFERRED)
        if credential_status is not None:
            credential_state = credential_status.status_for(source_id)
            if credential_state not in _CREDENTIAL_STATUS:
                raise ConnectorContractError("invalid collector credential status")
            if credential_state != "available":
                failure_code = f"credential_{credential_state}"
                self._record_terminal_run(
                    state,
                    source_id,
                    entry,
                    CollectorRunOutcome.FAILED,
                    0,
                    0,
                    failure_code=failure_code,
                )
                return _result(
                    source_id,
                    entry,
                    CollectorRunOutcome.FAILED,
                    failure_code=failure_code,
                )

        selection = _selection_from_entry(entry)
        page = runtime.fetch_page(selection, cast(str | None, entry.get("next_cursor")))
        if page.selection != selection:
            raise ConnectorContractError("invalid collector page")
        run_id = _run_id(source_id, now, cast(str | None, entry.get("next_cursor")))
        entry["active_run"] = _run_payload(run_id, now, page)
        self._store.save(state)
        committed = cast(dict[str, str], entry["committed_revisions"])
        captured = 0
        duplicates = 0
        try:
            for intake in page.intakes:
                latest_state = self._store.load()
                latest_entry = _source(_sources(latest_state), source_id)
                latest_status = cast(str, latest_entry["status"])
                if latest_status in {"paused", "disabled"}:
                    last_run = _last_run_payload(
                        run_id=run_id,
                        finished_epoch=now,
                        outcome=CollectorRunOutcome.FAILED,
                        captured_count=captured,
                        duplicate_count=duplicates,
                        next_cursor=cast(str | None, entry.get("next_cursor")),
                        failure_code=(
                            "collector_paused"
                            if latest_status == "paused"
                            else "collector_disabled"
                        ),
                    )
                    latest_committed = cast(dict[str, str], latest_entry["committed_revisions"])
                    latest_committed.update(committed)
                    latest_entry["last_run"] = last_run
                    latest_entry["active_run"] = None
                    if latest_status == "disabled":
                        latest_entry["next_run_epoch"] = None
                    self._store.save(latest_state)
                    return CollectorCommandResult(
                        source_id=source_id,
                        status=latest_status,
                        outcome=CollectorRunOutcome.FAILED,
                        captured_count=captured,
                        duplicate_count=duplicates,
                        next_run_epoch=cast(int | None, latest_entry.get("next_run_epoch")),
                        pause_ack_epoch=cast(int | None, latest_entry.get("pause_ack_epoch")),
                        next_cursor=cast(str | None, latest_entry.get("next_cursor")),
                        failure_code=cast(str, last_run["failure_code"]),
                    )
                delivery_id = intake.key.delivery_id()
                revision_identity = intake.key.revision_identity()
                if committed.get(delivery_id) == revision_identity:
                    duplicates += 1
                    continue
                capture_sink.submit(intake)
                committed[delivery_id] = revision_identity
                captured += 1
        except Exception:
            last_run = _last_run_payload(
                run_id=run_id,
                finished_epoch=now,
                outcome=CollectorRunOutcome.FAILED,
                captured_count=captured,
                duplicate_count=duplicates,
                next_cursor=cast(str | None, entry.get("next_cursor")),
                failure_code="capture_failed",
            )
            latest_state = self._store.load()
            latest_entry = _source(_sources(latest_state), source_id)
            latest_committed = cast(dict[str, str], latest_entry["committed_revisions"])
            latest_committed.update(committed)
            latest_entry["last_run"] = last_run
            if latest_entry["status"] == "enabled":
                latest_entry["active_run"] = entry["active_run"]
            else:
                latest_entry["active_run"] = None
            if latest_entry["status"] == "disabled":
                latest_entry["next_run_epoch"] = None
            self._store.save(latest_state)
            raise
        last_run = _last_run_payload(
            run_id=run_id,
            finished_epoch=now,
            outcome=CollectorRunOutcome.COMPLETED if captured else CollectorRunOutcome.EMPTY,
            captured_count=captured,
            duplicate_count=duplicates,
            next_cursor=page.next_cursor,
            failure_code=None,
        )
        latest_state = self._store.load()
        latest_entry = _source(_sources(latest_state), source_id)
        latest_committed = cast(dict[str, str], latest_entry["committed_revisions"])
        latest_committed.update(committed)
        latest_entry["next_cursor"] = page.next_cursor
        latest_entry["active_run"] = None
        latest_entry["last_run"] = last_run
        latest_entry["last_success_epoch"] = now
        latest_status = cast(str, latest_entry["status"])
        if latest_status == "enabled":
            latest_entry["next_run_epoch"] = now + cast(int, latest_entry["interval_seconds"])
        elif latest_status == "disabled":
            latest_entry["next_run_epoch"] = None
        self._store.save(latest_state)
        return CollectorCommandResult(
            source_id=source_id,
            status=latest_status,
            outcome=CollectorRunOutcome.COMPLETED if captured else CollectorRunOutcome.EMPTY,
            captured_count=captured,
            duplicate_count=duplicates,
            next_run_epoch=cast(int | None, latest_entry.get("next_run_epoch")),
            pause_ack_epoch=cast(int | None, latest_entry.get("pause_ack_epoch")),
            next_cursor=page.next_cursor,
        )

    def _record_terminal_run(
        self,
        state: dict[str, object],
        source_id: str,
        entry: dict[str, object],
        outcome: CollectorRunOutcome,
        captured_count: int,
        duplicate_count: int,
        failure_code: str | None = None,
    ) -> None:
        now = self._clock()
        entry["last_run"] = _last_run_payload(
            run_id=_run_id(
                source_id,
                now,
                cast(str | None, entry.get("next_cursor")),
            ),
            finished_epoch=now,
            outcome=outcome,
            captured_count=captured_count,
            duplicate_count=duplicate_count,
            next_cursor=cast(str | None, entry.get("next_cursor")),
            failure_code=failure_code,
        )
        self._store.save(state)


class MemoryCaptureSink:
    """Small test sink that records accepted delivery IDs without payload bodies."""

    def __init__(self) -> None:
        self.delivery_ids: list[str] = []

    def submit(self, intake: SourceRecordIntake) -> None:
        if type(intake) is not SourceRecordIntake:
            raise ConnectorContractError("invalid collector capture")
        self.delivery_ids.append(intake.key.delivery_id())


class EngineCaptureSink:
    """Collector host sink that persists changed source records into one Brain."""

    def __init__(self, sink: PublicJobCaptureSink) -> None:
        if type(sink) is not PublicJobCaptureSink:
            raise ConnectorContractError("invalid collector capture")
        self._sink = sink

    def submit(self, intake: SourceRecordIntake) -> None:
        if type(intake) is not SourceRecordIntake:
            raise ConnectorContractError("invalid collector capture")
        self._sink.submit(
            intake.payload(),
            delivery_id=intake.key.delivery_id(),
            source_origin="third_party",
            source_reference=intake.source_reference,
            provenance=intake.provenance(),
            privacy=intake.privacy,
            intent="reference",
            title=intake.title,
        )


def _result(
    source_id: str,
    entry: Mapping[str, object],
    outcome: CollectorRunOutcome,
    failure_code: str | None = None,
) -> CollectorCommandResult:
    return CollectorCommandResult(
        source_id=source_id,
        status=cast(str, entry["status"]),
        outcome=outcome,
        next_run_epoch=cast(int | None, entry.get("next_run_epoch")),
        pause_ack_epoch=cast(int | None, entry.get("pause_ack_epoch")),
        next_cursor=cast(str | None, entry.get("next_cursor")),
        failure_code=failure_code,
    )


def _selection_from_entry(entry: Mapping[str, object]) -> SourceResourceSelection:
    return SourceResourceSelection(
        connector_name=cast(str, entry["connector_name"]),
        connection_id=cast(str, entry["connection_id"]),
        resource_id=cast(str, entry["resource_id"]),
        resource_type=cast(str, entry["resource_type"]),
    )


def _run_id(source_id: str, now: int, cursor: str | None) -> str:
    suffix = cursor if cursor is not None else "initial"
    return f"{source_id}:{now}:{suffix}"


def _run_payload(
    run_id: str,
    started_epoch: int,
    page: CollectorRunPage,
) -> dict[str, object]:
    return {
        "cursor": page.next_cursor,
        "intakes": [
            {
                "delivery_id": intake.key.delivery_id(),
                "revision_identity": intake.key.revision_identity(),
                "source_reference": intake.source_reference,
            }
            for intake in page.intakes
        ],
        "run_id": run_id,
        "started_epoch": started_epoch,
    }


def _last_run_payload(
    *,
    run_id: str,
    finished_epoch: int,
    outcome: CollectorRunOutcome,
    captured_count: int,
    duplicate_count: int,
    next_cursor: str | None,
    failure_code: str | None,
) -> dict[str, object]:
    return {
        "captured_count": captured_count,
        "duplicate_count": duplicate_count,
        "failure_code": failure_code,
        "finished_epoch": finished_epoch,
        "next_cursor": next_cursor,
        "outcome": outcome.value,
        "run_id": run_id,
    }


def _sources(state: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], state["sources"])


def _source(sources: dict[str, object], source_id: str) -> dict[str, object]:
    _validate_source_id(source_id)
    value = sources.get(source_id)
    if not isinstance(value, dict):
        raise ConnectorContractError("unknown collector source")
    return value


def _validate_state(state: Mapping[str, object]) -> None:
    if set(state) != {"schema_version", "sources"} or state.get("schema_version") != 1:
        raise ConnectorContractError("invalid collector state")
    sources = state.get("sources")
    if not isinstance(sources, dict):
        raise ConnectorContractError("invalid collector state")
    for source_id, entry in sources.items():
        _validate_source_id(cast(str, source_id))
        if isinstance(entry, dict) and "credential_ref" not in entry:
            entry["credential_ref"] = None
        if isinstance(entry, dict) and "last_success_epoch" not in entry:
            entry["last_success_epoch"] = _last_success_epoch_from_last_run(
                entry.get("last_run")
            )
        if not isinstance(entry, dict) or set(entry) != {
            "active_run",
            "committed_revisions",
            "connection_id",
            "connector_name",
            "credential_ref",
            "interval_seconds",
            "last_run",
            "last_success_epoch",
            "next_cursor",
            "next_run_epoch",
            "pause_ack_epoch",
            "resource_id",
            "resource_type",
            "status",
        }:
            raise ConnectorContractError("invalid collector state")
        status = entry["status"]
        committed = entry["committed_revisions"]
        interval_seconds = entry["interval_seconds"]
        next_run_epoch = entry["next_run_epoch"]
        pause_ack_epoch = entry["pause_ack_epoch"]
        next_cursor = entry["next_cursor"]
        credential_ref = entry["credential_ref"]
        active_run = entry["active_run"]
        last_run = entry["last_run"]
        last_success_epoch = entry["last_success_epoch"]
        if (
            status not in _STATUS
            or not isinstance(committed, dict)
            or any(
                type(key) is not str or type(value) is not str
                for key, value in committed.items()
            )
            or type(interval_seconds) is not int
            or not 1 <= interval_seconds <= _MAX_INTERVAL
            or (
                next_run_epoch is not None
                and (type(next_run_epoch) is not int or next_run_epoch < 0)
            )
            or (
                pause_ack_epoch is not None
                and (type(pause_ack_epoch) is not int or pause_ack_epoch < 0)
            )
            or (
                last_success_epoch is not None
                and (type(last_success_epoch) is not int or last_success_epoch < 0)
            )
            or (
                next_cursor is not None
                and (
                    type(next_cursor) is not str
                    or _SOURCE_ID.fullmatch(next_cursor) is None
                )
            )
            or (
                credential_ref is not None
                and (
                    type(credential_ref) is not str
                    or _SOURCE_ID.fullmatch(credential_ref) is None
                )
            )
        ):
            raise ConnectorContractError("invalid collector state")
        _validate_active_run(active_run)
        _validate_last_run(last_run)
        _selection_from_entry(entry)


def _validate_source_id(source_id: str) -> None:
    if type(source_id) is not str or _SOURCE_ID.fullmatch(source_id) is None:
        raise ConnectorContractError("invalid collector source")


def _validate_active_run(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "cursor",
        "intakes",
        "run_id",
        "started_epoch",
    }:
        raise ConnectorContractError("invalid collector state")
    if (
        type(value["run_id"]) is not str
        or not value["run_id"]
        or type(value["started_epoch"]) is not int
        or value["started_epoch"] < 0
        or (
            value["cursor"] is not None
            and (
                type(value["cursor"]) is not str
                or _SOURCE_ID.fullmatch(value["cursor"]) is None
            )
        )
        or not isinstance(value["intakes"], list)
        or len(value["intakes"]) > 25
    ):
        raise ConnectorContractError("invalid collector state")
    for intake in value["intakes"]:
        if not isinstance(intake, dict) or set(intake) != {
            "delivery_id",
            "revision_identity",
            "source_reference",
        }:
            raise ConnectorContractError("invalid collector state")
        if any(type(intake[field]) is not str or not intake[field] for field in intake):
            raise ConnectorContractError("invalid collector state")


def _validate_last_run(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "captured_count",
        "duplicate_count",
        "failure_code",
        "finished_epoch",
        "next_cursor",
        "outcome",
        "run_id",
    }:
        raise ConnectorContractError("invalid collector state")
    if (
        type(value["run_id"]) is not str
        or not value["run_id"]
        or type(value["finished_epoch"]) is not int
        or value["finished_epoch"] < 0
        or value["outcome"] not in _OUTCOME
        or type(value["captured_count"]) is not int
        or value["captured_count"] < 0
        or type(value["duplicate_count"]) is not int
        or value["duplicate_count"] < 0
        or (
            value["failure_code"] is not None
            and value["failure_code"] not in _FAILURE_CODE
        )
        or (
            value["next_cursor"] is not None
            and (
                type(value["next_cursor"]) is not str
                or _SOURCE_ID.fullmatch(value["next_cursor"]) is None
            )
        )
    ):
        raise ConnectorContractError("invalid collector state")


def _last_success_epoch_from_last_run(value: object) -> int | None:
    if not isinstance(value, Mapping) or value.get("outcome") not in {"completed", "empty"}:
        return None
    finished_epoch = value.get("finished_epoch")
    return finished_epoch if type(finished_epoch) is int and finished_epoch >= 0 else None
