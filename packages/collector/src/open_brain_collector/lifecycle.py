"""Durable lifecycle controls for the optional unattended collector."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, cast, runtime_checkable

from open_brain_engine.engine import DeliveryConflict, PublicJobCaptureSink
from open_brain_engine.engine.t03_contracts import T03Error

from open_brain_collector.custody import CustodyStore, intake_digest
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_host import existing_source_profile
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
_SELECTION_GUARDS_LOCK = threading.Lock()
_SELECTION_GUARDS: dict[Path, threading.RLock] = {}


def _selection_guard(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _SELECTION_GUARDS_LOCK:
        return _SELECTION_GUARDS.setdefault(resolved, threading.RLock())


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
    def submit(self, intake: SourceRecordIntake) -> object:
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
    quarantined_count: int = 0
    next_run_epoch: int | None = None
    pause_ack_epoch: int | None = None
    next_cursor: str | None = None
    failure_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "captured_count": self.captured_count,
            "duplicate_count": self.duplicate_count,
            "quarantined_count": self.quarantined_count,
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
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, encoding="utf-8") as handle:
                info = os.fstat(handle.fileno())
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) & 0o077
                ):
                    os.fchmod(handle.fileno(), 0o600)
                    os.fsync(handle.fileno())
                    info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise CollectorStorageError("collector_storage_unavailable")
                loaded = json.load(handle)
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
                handle.flush()
                os.fsync(handle.fileno())
                temp_name = handle.name
            Path(temp_name).chmod(0o600)
            os.replace(temp_name, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
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
        payload = (
            json.dumps(
                {"owner": owner, "pid": os.getpid() if pid is None else pid, "schema_version": 1},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
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
        except OSError, json.JSONDecodeError:
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
        brain_root: Path | None = None,
    ) -> None:
        if type(store) is not CollectorStateStore or not callable(clock):
            raise ConnectorContractError("invalid collector controller")
        self._store = store
        self._clock = clock
        self._capture_guard = _selection_guard(store.path.parent / "custody" / "selection")
        self._in_capture = threading.local()
        self._private_store = PrivateJsonStore(store.path.parent.resolve() / "custody")
        self._custody = CustodyStore(self._private_store)
        self._requested_brain_root = brain_root
        if brain_root is None:
            binding_value: object = ["state", str(store.path.resolve())]
            self._brain_bound = False
        else:
            try:
                profile = existing_source_profile(brain_root)
            except ConnectorContractError, LiveSourceError:
                binding_value = ["state", str(store.path.resolve())]
                self._brain_bound = False
            else:
                binding_value = [str(profile.root), profile.root_identity, profile.tenant_id]
                self._brain_bound = True
        if self._brain_bound:
            values = cast(list[object], binding_value)
            self._binding = PublicJobCaptureSink.fingerprint_for(
                cast(str, values[0]),
                cast(tuple[int, int], values[1]),
                cast(str, values[2]),
            )
        else:
            self._binding = hashlib.sha256(bounded_json(binding_value)).hexdigest()
        if self._brain_bound:
            with self._private_store.lock("selection"):
                marker = self._private_store.read("brain.json")
                expected = {"schema_version": 1, "binding": self._binding}
                if marker is None:
                    try:
                        self._private_store.write("brain.json", expected)
                    except TypeError:
                        raise CollectorStorageError("collector_storage_unavailable") from None
                elif marker != expected:
                    raise LiveSourceError("source_brain_mismatch")

    def _bind_capture_sink(self, capture_sink: _CaptureSink) -> None:
        if not isinstance(capture_sink, EngineCaptureSink):
            return
        binding = capture_sink.brain_binding
        if not self._brain_bound and self._requested_brain_root is not None:
            try:
                profile = existing_source_profile(self._requested_brain_root)
            except ConnectorContractError, LiveSourceError:
                raise LiveSourceError("source_brain_unavailable") from None
            requested = PublicJobCaptureSink.fingerprint_for(
                str(profile.root), profile.root_identity, profile.tenant_id
            )
            if requested != binding:
                raise LiveSourceError("source_brain_mismatch")
        with self._private_store.lock("selection"):
            marker = self._private_store.read("brain.json")
            expected = {"schema_version": 1, "binding": binding}
            if marker is None:
                try:
                    self._private_store.write("brain.json", expected)
                except TypeError:
                    raise CollectorStorageError("collector_storage_unavailable") from None
            elif marker != expected:
                raise LiveSourceError("source_brain_mismatch")
        if self._brain_bound and self._binding != binding:
            raise LiveSourceError("source_brain_mismatch")
        self._binding, self._brain_bound = binding, True

    @contextmanager
    def _selection_barrier(self) -> Iterator[None]:
        if getattr(self._in_capture, "active", False):
            yield
            return
        with self._private_store.lock("selection"):
            yield

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
        with self._capture_guard, self._selection_barrier():
            state = self._store.load()
            sources = _sources(state)
            previous = sources.get(source_id)
            if not isinstance(previous, dict):
                previous = {}
            generation = hashlib.sha256(bounded_json(asdict(selection))).hexdigest()
            old_generation = previous.get("generation")
            same = old_generation == generation
            control_epoch = cast(int, previous.get("control_epoch", 0))
            if old_generation is not None and not same:
                control_epoch += 1
            cancel_receipts = cast(list[str], previous.get("cancel_receipts", []))
            active = previous.get("active_run")
            if (
                not same
                and isinstance(active, dict)
                and isinstance(active.get("custody_ids"), list)
            ):
                cancel_receipts = list(
                    dict.fromkeys([*cancel_receipts, *cast(list[str], active["custody_ids"])])
                )
            sources[source_id] = {
                "committed_capture_ids": previous.get("committed_capture_ids", {}) if same else {},
                "committed_digests": previous.get("committed_digests", {}) if same else {},
                "committed_revisions": previous.get("committed_revisions", {}) if same else {},
                "active_run": previous.get("active_run") if same else None,
                "cancel_receipts": cancel_receipts,
                "cleanup_receipts": previous.get("cleanup_receipts", []),
                "retry_receipts": previous.get("retry_receipts", []),
                "control_epoch": control_epoch,
                "generation": generation,
                "connection_id": selection.connection_id,
                "connector_name": selection.connector_name,
                "credential_ref": credential_ref,
                "interval_seconds": interval_seconds,
                "last_run": previous.get("last_run") if same else None,
                "last_success_epoch": previous.get(
                    "last_success_epoch",
                    _last_success_epoch_from_last_run(previous.get("last_run")),
                ),
                "next_cursor": previous.get("next_cursor") if same else None,
                "next_run_epoch": self._clock(),
                "pause_ack_epoch": None,
                "resource_id": selection.resource_id,
                "resource_type": selection.resource_type,
                "status": "enabled",
            }
            self._store.save(state)
        self._drain_cleanup(source_id)
        return self.status(source_id)

    def disable(self, source_id: str) -> CollectorCommandResult:
        with self._capture_guard, self._selection_barrier():
            state = self._store.load()
            entry = _source(_sources(state), source_id)
            active = entry["active_run"]
            if isinstance(active, dict) and isinstance(active.get("custody_ids"), list):
                entry["cancel_receipts"] = list(
                    dict.fromkeys(
                        [
                            *cast(list[str], entry["cancel_receipts"]),
                            *cast(list[str], active["custody_ids"]),
                        ]
                    )
                )
            entry["status"] = "disabled"
            entry["control_epoch"] = cast(int, entry["control_epoch"]) + 1
            entry["next_run_epoch"] = None
            entry["active_run"] = None
            self._store.save(state)
        self._drain_cleanup(source_id)
        return self.status(source_id)

    def pause(self, source_id: str) -> CollectorCommandResult:
        with self._capture_guard, self._selection_barrier():
            state = self._store.load()
            entry = _source(_sources(state), source_id)
            active = entry["active_run"]
            if isinstance(active, dict) and isinstance(active.get("custody_ids"), list):
                entry["cancel_receipts"] = list(
                    dict.fromkeys(
                        [
                            *cast(list[str], entry["cancel_receipts"]),
                            *cast(list[str], active["custody_ids"]),
                        ]
                    )
                )
            now = self._clock()
            entry["status"] = "paused"
            entry["control_epoch"] = cast(int, entry["control_epoch"]) + 1
            entry["pause_ack_epoch"] = now
            entry["active_run"] = None
            self._store.save(state)
        self._drain_cleanup(source_id)
        return self.status(source_id)

    def resume(self, source_id: str) -> CollectorCommandResult:
        with self._capture_guard, self._selection_barrier():
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

    def custody_status(self, source_id: str | None = None) -> dict[str, object]:
        if source_id is not None:
            _source(_sources(self._store.load()), source_id)
        return self._custody.status(source_id)

    def custody_inspect(self, receipt_id: str) -> dict[str, object]:
        return self._custody.inspect(receipt_id)

    @staticmethod
    def _is_item_conflict(error: Exception) -> bool:
        return isinstance(error, DeliveryConflict) or (
            isinstance(error, T03Error) and error.code == "source_revision_conflict"
        )

    def retry(self, receipt_id: str, capture_sink: _CaptureSink) -> dict[str, object]:
        self._bind_capture_sink(capture_sink)
        with self._capture_guard, self._selection_barrier():
            return self._retry_locked(receipt_id, capture_sink)

    def _retry_locked(self, receipt_id: str, capture_sink: _CaptureSink) -> dict[str, object]:
        receipt = self._custody.receipt(receipt_id)
        if receipt["outcome"] != "quarantined":
            raise LiveSourceError("collector_custody_not_replayable")
        state = self._store.load()
        entry = _source(_sources(state), cast(str, receipt["source_id"]))
        if (
            receipt["binding"] != self._binding
            or receipt["generation"] != entry["generation"]
            or receipt["control_epoch"] != entry["control_epoch"]
            or entry["status"] == "paused"
        ):
            raise LiveSourceError("collector_custody_stale")
        retry_ids = cast(list[str], entry["retry_receipts"])
        entry["retry_receipts"] = list(dict.fromkeys([*retry_ids, receipt_id]))
        self._store.save(state)
        try:
            result = capture_sink.submit(self._custody.intake(receipt_id))
        except Exception as error:
            if self._is_item_conflict(error):
                entry["retry_receipts"] = [
                    item for item in cast(list[str], entry["retry_receipts"]) if item != receipt_id
                ]
                self._store.save(state)
                return self._custody.inspect(receipt_id)
            raise
        duplicate = bool(getattr(result, "duplicate", False))
        capture_id = getattr(result, "capture_id", None)
        result_outcome = getattr(result, "outcome", None)
        self._custody.outcome(
            receipt_id,
            "history_only"
            if result_outcome == "history_only"
            else "duplicate"
            if duplicate
            else "captured",
            capture_id=capture_id,
        )
        active = any(
            receipt_id in cast(list[str], item.get("active_run", {}).get("custody_ids", []))
            for item in _sources(state).values()
            if isinstance(item, dict) and isinstance(item.get("active_run"), dict)
        )
        if not active:
            self._custody.release_completed(
                (receipt_id,),
                protected_ids=self._cleanup_ids(state),
            )
        entry["retry_receipts"] = [
            item for item in cast(list[str], entry["retry_receipts"]) if item != receipt_id
        ]
        self._store.save(state)
        return self._custody.inspect(receipt_id)

    def schedule(self, source_id: str, interval_seconds: int) -> CollectorCommandResult:
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= _MAX_INTERVAL:
            raise ConnectorContractError("invalid collector schedule")
        with self._capture_guard, self._selection_barrier():
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
        with self._capture_guard, self._selection_barrier():
            state = self._store.load()
            entry = _source(_sources(state), source_id)
            if entry["status"] != "enabled":
                raise ConnectorContractError("collector source not enabled")
            entry["next_run_epoch"] = self._clock()
            self._store.save(state)
        return self.status(source_id)

    @staticmethod
    def _cleanup_ids(state: dict[str, object]) -> frozenset[str]:
        return frozenset(
            receipt_id
            for item in _sources(state).values()
            if isinstance(item, dict)
            for key in ("cleanup_receipts", "retry_receipts")
            if isinstance(item.get(key), list)
            for receipt_id in cast(list[str], item[key])
        )

    def _drain_cleanup(self, source_id: str) -> None:
        with self._capture_guard, self._selection_barrier():
            state = self._store.load()
            entry = _source(_sources(state), source_id)
            receipt_ids = tuple(cast(list[str], entry["cleanup_receipts"]))
            cancel_ids = tuple(cast(list[str], entry["cancel_receipts"]))
            retry_ids = tuple(cast(list[str], entry["retry_receipts"]))
            if not receipt_ids and not cancel_ids and not retry_ids:
                return
            if receipt_ids:
                self._custody.release_completed(
                    receipt_ids,
                    protected_ids=self._cleanup_ids(state),
                )
            active_ids = {
                receipt_id
                for item in _sources(state).values()
                if isinstance(item, dict) and isinstance(item.get("active_run"), dict)
                for receipt_id in cast(
                    list[str], cast(dict[str, object], item["active_run"]).get("custody_ids", [])
                )
            }
            releasable_retry_ids = tuple(
                receipt_id
                for receipt_id in retry_ids
                if receipt_id not in cancel_ids
                if self._custody.receipt(receipt_id)["outcome"] != "quarantined"
                and receipt_id not in active_ids
            )
            if releasable_retry_ids:
                self._custody.release_completed(
                    releasable_retry_ids,
                    protected_ids=self._cleanup_ids(state),
                )
            if cancel_ids:
                self._custody.discard_unacknowledged(cancel_ids)
            entry["cleanup_receipts"] = []
            entry["cancel_receipts"] = []
            entry["retry_receipts"] = []
            self._store.save(state)

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
        self._bind_capture_sink(capture_sink)
        self._drain_cleanup(source_id)
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
        active = entry.get("active_run")
        if isinstance(active, dict) and isinstance(active.get("custody_ids"), list):
            receipt_ids = tuple(cast(list[str], active["custody_ids"]))
            for receipt_id in receipt_ids:
                receipt = self._custody.receipt(receipt_id)
                if (
                    receipt["binding"] != self._binding
                    or receipt["generation"] != entry["generation"]
                    or receipt["control_epoch"] != entry["control_epoch"]
                    or receipt["source_id"] != source_id
                ):
                    raise LiveSourceError("collector_custody_stale")
            page = CollectorRunPage(
                selection=selection,
                intakes=tuple(self._custody.intake(item) for item in receipt_ids),
                next_cursor=cast(str | None, active["cursor"]),
            )
            run_id = cast(str, active["run_id"])
        else:
            page = runtime.fetch_page(selection, cast(str | None, entry.get("next_cursor")))
            if page.selection != selection:
                raise ConnectorContractError("invalid collector page")
            run_id = _run_id(source_id, now, cast(str | None, entry.get("next_cursor")))
            intended_ids = self._custody.receipt_ids(
                source_id=source_id,
                binding=self._binding,
                generation=cast(str, entry["generation"]),
                control_epoch=cast(int, entry["control_epoch"]),
                intakes=page.intakes,
            )
            with self._capture_guard, self._selection_barrier():
                latest_state = self._store.load()
                latest_entry = _source(_sources(latest_state), source_id)
                if (
                    latest_entry["generation"] != entry["generation"]
                    or latest_entry["control_epoch"] != entry["control_epoch"]
                    or latest_entry["status"] != "enabled"
                    or latest_entry["next_cursor"] != entry["next_cursor"]
                ):
                    raise LiveSourceError("collector_import_cancelled")
                cancel_ids = cast(list[str], latest_entry["cancel_receipts"])
                latest_entry["cancel_receipts"] = list(dict.fromkeys([*cancel_ids, *intended_ids]))
                self._store.save(latest_state)
                receipt_ids = self._custody.stage(
                    source_id=source_id,
                    binding=self._binding,
                    generation=cast(str, entry["generation"]),
                    control_epoch=cast(int, entry["control_epoch"]),
                    intakes=page.intakes,
                )
                latest_entry["active_run"] = _run_payload(run_id, now, page, receipt_ids)
                latest_entry["cancel_receipts"] = [
                    item
                    for item in cast(list[str], latest_entry["cancel_receipts"])
                    if item not in intended_ids
                ]
                self._store.save(latest_state)
                entry = latest_entry
        committed = cast(dict[str, str], entry["committed_revisions"])
        committed_capture_ids = cast(dict[str, str], entry["committed_capture_ids"])
        committed_digests = cast(dict[str, str], entry["committed_digests"])
        captured = 0
        duplicates = 0
        quarantined = 0
        try:
            for intake, receipt_id in zip(page.intakes, receipt_ids, strict=True):
                with self._capture_guard, self._selection_barrier():
                    latest_state = self._store.load()
                    latest_entry = _source(_sources(latest_state), source_id)
                    latest_status = cast(str, latest_entry["status"])
                    cancelled = (
                        latest_status in {"paused", "disabled"}
                        or latest_entry["control_epoch"] != entry["control_epoch"]
                        or latest_entry["generation"] != entry["generation"]
                    )
                    if cancelled:
                        failure_code = (
                            "collector_paused"
                            if latest_status == "paused"
                            else "collector_disabled"
                            if latest_status == "disabled"
                            else "capture_failed"
                        )
                        last_run = _last_run_payload(
                            run_id=run_id,
                            finished_epoch=now,
                            outcome=CollectorRunOutcome.FAILED,
                            captured_count=captured,
                            duplicate_count=duplicates,
                            next_cursor=cast(str | None, entry.get("next_cursor")),
                            failure_code=failure_code,
                        )
                        if latest_entry["generation"] == entry["generation"]:
                            cast(dict[str, str], latest_entry["committed_revisions"]).update(
                                committed
                            )
                            cast(dict[str, str], latest_entry["committed_capture_ids"]).update(
                                committed_capture_ids
                            )
                            cast(dict[str, str], latest_entry["committed_digests"]).update(
                                committed_digests
                            )
                            latest_entry["last_run"] = last_run
                            active_now = latest_entry.get("active_run")
                            if isinstance(active_now, dict) and active_now.get("run_id") == run_id:
                                latest_entry["active_run"] = None
                            self._store.save(latest_state)
                if cancelled:
                    return CollectorCommandResult(
                        source_id=source_id,
                        status=latest_status,
                        outcome=CollectorRunOutcome.FAILED,
                        captured_count=captured,
                        duplicate_count=duplicates,
                        quarantined_count=quarantined,
                        next_run_epoch=cast(int | None, latest_entry.get("next_run_epoch")),
                        pause_ack_epoch=cast(int | None, latest_entry.get("pause_ack_epoch")),
                        next_cursor=cast(str | None, latest_entry.get("next_cursor")),
                        failure_code=failure_code,
                    )
                delivery_id = intake.key.delivery_id()
                revision_identity = intake.key.revision_identity()
                digest = intake_digest(intake)
                prior = self._custody.receipt(receipt_id)
                if prior["outcome"] == "quarantined":
                    quarantined += 1
                    continue
                if prior["outcome"] in {"captured", "duplicate", "history_only"}:
                    duplicates += int(prior["outcome"] in {"captured", "duplicate"})
                    continue
                if (
                    committed.get(delivery_id) == revision_identity
                    and committed_digests.get(delivery_id) == digest
                ):
                    duplicates += 1
                    self._custody.outcome(
                        receipt_id,
                        "duplicate",
                        capture_id=committed_capture_ids.get(delivery_id),
                        evidence=(
                            "capture_id"
                            if delivery_id in committed_capture_ids
                            else "acceleration_cache"
                        ),
                    )
                    continue
                try:
                    with self._capture_guard, self._selection_barrier():
                        guarded_state = self._store.load()
                        guarded_entry = _source(_sources(guarded_state), source_id)
                        if (
                            guarded_entry["control_epoch"] != entry["control_epoch"]
                            or guarded_entry["generation"] != entry["generation"]
                            or guarded_entry["status"] != "enabled"
                        ):
                            raise LiveSourceError("collector_import_cancelled")
                        self._in_capture.active = True
                        try:
                            result = capture_sink.submit(intake)
                        finally:
                            self._in_capture.active = False
                except Exception as error:
                    if not self._is_item_conflict(error):
                        raise
                    self._custody.outcome(
                        receipt_id,
                        "quarantined",
                        reason_code="source_revision_conflict",
                    )
                    quarantined += 1
                    continue
                result_outcome = getattr(result, "outcome", None)
                duplicate = bool(getattr(result, "duplicate", False))
                capture_id = getattr(result, "capture_id", None)
                outcome = (
                    "history_only"
                    if result_outcome == "history_only"
                    else "duplicate"
                    if duplicate
                    else "captured"
                )
                self._custody.outcome(receipt_id, outcome, capture_id=capture_id)
                committed[delivery_id] = revision_identity
                if capture_id is not None:
                    committed_capture_ids[delivery_id] = capture_id
                committed_digests[delivery_id] = digest
                captured += int(not duplicate)
                duplicates += int(duplicate)
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
            with self._capture_guard, self._selection_barrier():
                latest_state = self._store.load()
                latest_entry = _source(_sources(latest_state), source_id)
                if latest_entry["generation"] == entry["generation"]:
                    latest_entry["last_run"] = last_run
                if (
                    latest_entry["generation"] == entry["generation"]
                    and latest_entry["control_epoch"] == entry["control_epoch"]
                ):
                    cast(dict[str, str], latest_entry["committed_revisions"]).update(committed)
                    cast(dict[str, str], latest_entry["committed_capture_ids"]).update(
                        committed_capture_ids
                    )
                    cast(dict[str, str], latest_entry["committed_digests"]).update(
                        committed_digests
                    )
                    latest_entry["active_run"] = (
                        entry["active_run"] if latest_entry["status"] == "enabled" else None
                    )
                    if latest_entry["status"] == "disabled":
                        latest_entry["next_run_epoch"] = None
                if latest_entry["generation"] == entry["generation"]:
                    self._store.save(latest_state)
            raise
        last_run = _last_run_payload(
            run_id=run_id,
            finished_epoch=now,
            outcome=(
                CollectorRunOutcome.COMPLETED
                if captured or quarantined
                else CollectorRunOutcome.EMPTY
            ),
            captured_count=captured,
            duplicate_count=duplicates,
            next_cursor=page.next_cursor,
            failure_code=None,
        )
        with self._capture_guard, self._selection_barrier():
            latest_state = self._store.load()
            latest_entry = _source(_sources(latest_state), source_id)
            if (
                latest_entry["control_epoch"] != entry["control_epoch"]
                or latest_entry["generation"] != entry["generation"]
                or latest_entry["status"] != "enabled"
            ):
                failure_code = (
                    "collector_paused"
                    if latest_entry["status"] == "paused"
                    else "collector_disabled"
                    if latest_entry["status"] == "disabled"
                    else "capture_failed"
                )
                cancelled_run = _last_run_payload(
                    run_id=run_id,
                    finished_epoch=now,
                    outcome=CollectorRunOutcome.FAILED,
                    captured_count=captured,
                    duplicate_count=duplicates,
                    next_cursor=cast(str | None, entry.get("next_cursor")),
                    failure_code=failure_code,
                )
                if latest_entry["generation"] == entry["generation"]:
                    active_now = latest_entry.get("active_run")
                    if isinstance(active_now, dict) and active_now.get("run_id") == run_id:
                        latest_entry["active_run"] = None
                    latest_entry["last_run"] = cancelled_run
                    self._store.save(latest_state)
                return CollectorCommandResult(
                    source_id=source_id,
                    status=cast(str, latest_entry["status"]),
                    outcome=CollectorRunOutcome.FAILED,
                    captured_count=captured,
                    duplicate_count=duplicates,
                    quarantined_count=quarantined,
                    next_run_epoch=cast(int | None, latest_entry.get("next_run_epoch")),
                    pause_ack_epoch=cast(int | None, latest_entry.get("pause_ack_epoch")),
                    next_cursor=cast(str | None, latest_entry.get("next_cursor")),
                    failure_code=failure_code,
                )
            self._custody.validate_terminal(receipt_ids)
            latest_committed = cast(dict[str, str], latest_entry["committed_revisions"])
            latest_committed.update(committed)
            cast(dict[str, str], latest_entry["committed_capture_ids"]).update(
                committed_capture_ids
            )
            cast(dict[str, str], latest_entry["committed_digests"]).update(committed_digests)
            latest_entry["next_cursor"] = page.next_cursor
            latest_entry["active_run"] = None
            latest_entry["cleanup_receipts"] = list(receipt_ids)
            latest_entry["last_run"] = last_run
            latest_entry["last_success_epoch"] = now
            latest_status = latest_entry["status"]
            latest_entry["next_run_epoch"] = now + cast(int, latest_entry["interval_seconds"])
            self._store.save(latest_state)
        self._drain_cleanup(source_id)
        return CollectorCommandResult(
            source_id=source_id,
            status=latest_status,
            outcome=(
                CollectorRunOutcome.COMPLETED
                if captured or quarantined
                else CollectorRunOutcome.EMPTY
            ),
            captured_count=captured,
            duplicate_count=duplicates,
            quarantined_count=quarantined,
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
        expected_generation = entry.get("generation")
        expected_epoch = entry.get("control_epoch")
        with self._capture_guard, self._selection_barrier():
            latest_state = self._store.load()
            latest = _source(_sources(latest_state), source_id)
            if (
                latest.get("generation") != expected_generation
                or latest.get("control_epoch") != expected_epoch
            ):
                entry.clear()
                entry.update(latest)
                return
            now = self._clock()
            latest["last_run"] = _last_run_payload(
                run_id=_run_id(
                    source_id,
                    now,
                    cast(str | None, latest.get("next_cursor")),
                ),
                finished_epoch=now,
                outcome=outcome,
                captured_count=captured_count,
                duplicate_count=duplicate_count,
                next_cursor=cast(str | None, latest.get("next_cursor")),
                failure_code=failure_code,
            )
            self._store.save(latest_state)
            entry.clear()
            entry.update(latest)


class MemoryCaptureSink:
    """Small test sink that records accepted delivery IDs without payload bodies."""

    def __init__(self) -> None:
        self.delivery_ids: list[str] = []

    def submit(self, intake: SourceRecordIntake) -> object:
        if type(intake) is not SourceRecordIntake:
            raise ConnectorContractError("invalid collector capture")
        self.delivery_ids.append(intake.key.delivery_id())
        return None


class EngineCaptureSink:
    """Collector host sink that persists changed source records into one Brain."""

    def __init__(self, sink: PublicJobCaptureSink) -> None:
        if type(sink) is not PublicJobCaptureSink:
            raise ConnectorContractError("invalid collector capture")
        self._sink = sink

    @property
    def brain_binding(self) -> str:
        binding = self._sink.brain_fingerprint
        if binding is None:
            raise LiveSourceError("source_brain_unavailable")
        return binding

    def submit(self, intake: SourceRecordIntake) -> object:
        if type(intake) is not SourceRecordIntake:
            raise ConnectorContractError("invalid collector capture")
        return self._sink.submit(
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
    custody_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "cursor": page.next_cursor,
        "custody_ids": list(custody_ids),
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
            entry["last_success_epoch"] = _last_success_epoch_from_last_run(entry.get("last_run"))
        if isinstance(entry, dict) and "control_epoch" not in entry:
            entry["control_epoch"] = 0
        if isinstance(entry, dict) and "committed_digests" not in entry:
            entry["committed_digests"] = {}
        if isinstance(entry, dict) and "committed_capture_ids" not in entry:
            entry["committed_capture_ids"] = {}
        if isinstance(entry, dict) and "cleanup_receipts" not in entry:
            entry["cleanup_receipts"] = []
        if isinstance(entry, dict) and "cancel_receipts" not in entry:
            entry["cancel_receipts"] = []
        if isinstance(entry, dict) and "retry_receipts" not in entry:
            entry["retry_receipts"] = []
        if isinstance(entry, dict) and "generation" not in entry:
            selection = {
                key: entry.get(key)
                for key in ("connector_name", "connection_id", "resource_id", "resource_type")
            }
            entry["generation"] = hashlib.sha256(bounded_json(selection)).hexdigest()
        if not isinstance(entry, dict) or set(entry) != {
            "active_run",
            "committed_capture_ids",
            "committed_digests",
            "committed_revisions",
            "cancel_receipts",
            "cleanup_receipts",
            "connection_id",
            "connector_name",
            "control_epoch",
            "credential_ref",
            "interval_seconds",
            "generation",
            "last_run",
            "last_success_epoch",
            "next_cursor",
            "next_run_epoch",
            "pause_ack_epoch",
            "resource_id",
            "resource_type",
            "retry_receipts",
            "status",
        }:
            raise ConnectorContractError("invalid collector state")
        status = entry["status"]
        committed = entry["committed_revisions"]
        committed_capture_ids = entry["committed_capture_ids"]
        cleanup_receipts = entry["cleanup_receipts"]
        cancel_receipts = entry["cancel_receipts"]
        retry_receipts = entry["retry_receipts"]
        committed_digests = entry["committed_digests"]
        interval_seconds = entry["interval_seconds"]
        next_run_epoch = entry["next_run_epoch"]
        pause_ack_epoch = entry["pause_ack_epoch"]
        next_cursor = entry["next_cursor"]
        credential_ref = entry["credential_ref"]
        active_run = entry["active_run"]
        last_run = entry["last_run"]
        last_success_epoch = entry["last_success_epoch"]
        control_epoch = entry["control_epoch"]
        generation = entry["generation"]
        if (
            status not in _STATUS
            or type(control_epoch) is not int
            or control_epoch < 0
            or type(generation) is not str
            or re.fullmatch(r"[0-9a-f]{64}", generation) is None
            or not isinstance(committed, dict)
            or not isinstance(committed_digests, dict)
            or not isinstance(committed_capture_ids, dict)
            or not isinstance(cleanup_receipts, list)
            or not isinstance(cancel_receipts, list)
            or not isinstance(retry_receipts, list)
            or any(
                type(item) is not str or re.fullmatch(r"custody:[0-9a-f]{64}", item) is None
                for item in cleanup_receipts
            )
            or any(
                type(item) is not str or re.fullmatch(r"custody:[0-9a-f]{64}", item) is None
                for item in cancel_receipts
            )
            or any(
                type(item) is not str or re.fullmatch(r"custody:[0-9a-f]{64}", item) is None
                for item in retry_receipts
            )
            or any(
                type(key) is not str or type(value) is not str for key, value in committed.items()
            )
            or any(
                type(key) is not str
                or type(value) is not str
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for key, value in committed_digests.items()
            )
            or any(
                type(key) is not str or type(value) is not str
                for key, value in committed_capture_ids.items()
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
                and (type(next_cursor) is not str or _SOURCE_ID.fullmatch(next_cursor) is None)
            )
            or (
                credential_ref is not None
                and (
                    type(credential_ref) is not str or _SOURCE_ID.fullmatch(credential_ref) is None
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
    if not isinstance(value, dict) or set(value) not in (
        {
            "cursor",
            "intakes",
            "run_id",
            "started_epoch",
        },
        {
            "cursor",
            "custody_ids",
            "intakes",
            "run_id",
            "started_epoch",
        },
    ):
        raise ConnectorContractError("invalid collector state")
    if (
        type(value["run_id"]) is not str
        or not value["run_id"]
        or type(value["started_epoch"]) is not int
        or value["started_epoch"] < 0
        or (
            value["cursor"] is not None
            and (type(value["cursor"]) is not str or _SOURCE_ID.fullmatch(value["cursor"]) is None)
        )
        or not isinstance(value["intakes"], list)
        or len(value["intakes"]) > 25
        or (
            "custody_ids" in value
            and (
                not isinstance(value["custody_ids"], list)
                or len(value["custody_ids"]) != len(value["intakes"])
                or any(
                    type(item) is not str or re.fullmatch(r"custody:[0-9a-f]{64}", item) is None
                    for item in value["custody_ids"]
                )
            )
        )
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
        or (value["failure_code"] is not None and value["failure_code"] not in _FAILURE_CODE)
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
