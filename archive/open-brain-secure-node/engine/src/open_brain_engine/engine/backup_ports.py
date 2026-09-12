"""Local ports for engine-owned immutable backup snapshots."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from open_brain_engine.core.ids import canonical_json_bytes, validate_identifier
from open_brain_engine.portable import validate_portable_file_set
from open_brain_engine.storage.filesystem import (
    RootIdentity,
    StorageError,
    assert_root_identity,
    capture_root_identity,
    read_confined,
    read_confined_tree,
)
from open_brain_engine.storage.sqlite import connect_database_read_only

from .local_schema import classify_local_schema
from .maintenance import PHASE1_STATE_DATABASE
from .portability_ports import LocalTenantStorage

_MAXIMUM_APP_STATE_FILE_BYTES = 64 * 1024
_MAXIMUM_APP_STATE_ENTRIES = 256
_MAXIMUM_APP_STATE_TOTAL_BYTES = 2 * 1024 * 1024
_MAXIMUM_SQLITE_BYTES = 512 * 1024 * 1024
_APP_STATE_FILES = (
    ".open-brain/state/appliance-init.json",
    ".open-brain/state/appliance-export-evidence.json",
)
_SCHEDULER_DIRECTORY = ".open-brain/state/appliance-scheduler"
_SCHEDULER_ENTRY = re.compile(
    r"^runs/(?P<job>[A-Za-z0-9][A-Za-z0-9._:-]{0,99})/"
    r"(?P<run>run_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json$"
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RUN_STATUS = frozenset({"completed", "deferred", "empty", "failed"})
_INIT_KEYS = frozenset(
    {
        "owner_actor_id",
        "owner_role_claim_id",
        "profile",
        "provider_mode",
        "schema_version",
        "starter_spaces",
        "state_schema_version",
        "tenant_id",
    }
)
_EXPORT_KEYS = frozenset(
    {"created_at", "export_id", "manifest_digest_sha256", "schema_version"}
)
_RUN_KEYS = frozenset(
    {
        "attempt",
        "finished_at",
        "job_name",
        "next_due_at",
        "reason",
        "run_id",
        "started_at",
        "status",
    }
)


@dataclass(frozen=True, slots=True)
class BackupSourceEntry:
    path: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class LocalBackupSource:
    root: Path
    tenant_id: str
    root_identity: RootIdentity | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.root, Path)
            or not self.root.is_absolute()
            or not isinstance(self.tenant_id, str)
        ):
            raise ValueError("invalid local backup source")
        identity = (
            capture_root_identity(self.root)
            if self.root_identity is None
            else self.root_identity
        )
        assert_root_identity(self.root, identity)
        object.__setattr__(self, "root_identity", identity)

    @property
    def bound_root_identity(self) -> RootIdentity:
        identity = self.root_identity
        assert identity is not None
        return identity

    def entries(self) -> tuple[BackupSourceEntry, ...]:
        portable = tuple(self._portable_entries())
        validate_portable_file_set(
            {
                entry.path.removeprefix("portable/"): entry.payload
                for entry in portable
            },
            tenant_id=self.tenant_id,
        )
        entries = [*portable, *self._sqlite_entries(), *self._app_state_entries()]
        return tuple(sorted(entries, key=lambda entry: entry.path))

    def _portable_entries(self) -> Iterator[BackupSourceEntry]:
        storage = LocalTenantStorage(
            root=self.root,
            tenant_id=self.tenant_id,
            root_identity=self.bound_root_identity,
        )
        for relative, payload in storage.portable_files():
            if relative == "brain.toml" or relative.startswith(
                ("content/", "history/", "sources/")
            ):
                yield BackupSourceEntry(path=f"portable/{relative}", payload=payload)

    def _sqlite_entries(self) -> Iterator[BackupSourceEntry]:
        payload = _sqlite_backup_bytes(
            self.root,
            PHASE1_STATE_DATABASE,
            expected_root_identity=self.bound_root_identity,
        )
        yield BackupSourceEntry(path="sqlite/phase1.sqlite3", payload=payload)

    def _app_state_entries(self) -> Iterator[BackupSourceEntry]:
        for relative in _APP_STATE_FILES:
            payload = read_confined(
                root=self.root,
                relative=relative,
                expected_root_identity=self.bound_root_identity,
                maximum_bytes=_MAXIMUM_APP_STATE_FILE_BYTES,
            )
            if payload is not None:
                backup_path = "app-state/" + relative.removeprefix(
                    ".open-brain/state/"
                )
                validate_backup_app_state_entry(
                    backup_path,
                    payload,
                    tenant_id=self.tenant_id,
                )
                yield BackupSourceEntry(
                    path=backup_path,
                    payload=payload,
                )
        try:
            scheduler_entries = read_confined_tree(
                root=self.root,
                relative=_SCHEDULER_DIRECTORY,
                expected_root_identity=self.bound_root_identity,
                maximum_entries=_MAXIMUM_APP_STATE_ENTRIES,
                maximum_file_bytes=_MAXIMUM_APP_STATE_FILE_BYTES,
                maximum_total_bytes=_MAXIMUM_APP_STATE_TOTAL_BYTES,
            )
        except StorageError as error:
            raise ValueError("backup app state exceeds the bounded inventory") from error
        for scheduler_relative, payload in scheduler_entries:
            if scheduler_relative.as_posix() == "state.json":
                continue
            backup_path = (
                f"app-state/appliance-scheduler/{scheduler_relative.as_posix()}"
            )
            validate_backup_app_state_entry(
                backup_path,
                payload,
                tenant_id=self.tenant_id,
            )
            yield BackupSourceEntry(
                path=backup_path,
                payload=payload,
            )


def validate_backup_app_state_entry(
    path: str,
    payload: bytes,
    *,
    tenant_id: str,
) -> None:
    """Reject non-metadata or malformed appliance state before backup or restore."""

    if not isinstance(path, str) or not isinstance(payload, bytes):
        raise ValueError("backup app state is invalid")
    prefix = "app-state/appliance-scheduler/"
    scheduler_match = (
        _SCHEDULER_ENTRY.fullmatch(path.removeprefix(prefix))
        if path.startswith(prefix)
        else None
    )
    if path not in {
        "app-state/appliance-init.json",
        "app-state/appliance-export-evidence.json",
    } and scheduler_match is None:
        raise ValueError("backup app state inventory is invalid")
    if not payload or len(payload) > _MAXIMUM_APP_STATE_FILE_BYTES:
        raise ValueError("backup app state exceeds the bounded size")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("backup app state is invalid") from error
    if type(value) is not dict or canonical_json_bytes(value) != payload:
        raise ValueError("backup app state is invalid")
    record = cast(dict[str, object], value)
    if path == "app-state/appliance-init.json":
        _validate_init_state(record, tenant_id=tenant_id)
        return
    if path == "app-state/appliance-export-evidence.json":
        _validate_export_state(record)
        return
    assert scheduler_match is not None
    _validate_run_state(
        record,
        job_name=scheduler_match.group("job"),
        run_id=scheduler_match.group("run"),
    )


def _validate_init_state(record: dict[str, object], *, tenant_id: str) -> None:
    starters = record.get("starter_spaces")
    if (
        set(record) != _INIT_KEYS
        or record.get("profile") != "single-user-local"
        or record.get("provider_mode") not in {"none", "local", "cloud"}
        or record.get("schema_version") != 1
        or type(record.get("state_schema_version")) is not int
        or record.get("state_schema_version") not in (1, 2)
        or record.get("tenant_id") != tenant_id
        or not isinstance(starters, list)
        or len(starters) > 64
        or any(not isinstance(item, str) or not item or len(item) > 120 for item in starters)
        or len(set(cast(list[str], starters))) != len(starters)
    ):
        raise ValueError("backup app state is invalid")
    try:
        _validate_uuid_identifier(cast(str, record.get("tenant_id")), prefix="tenant_")
        _validate_uuid_identifier(cast(str, record.get("owner_actor_id")), prefix="actor_")
        _validate_uuid_identifier(
            cast(str, record.get("owner_role_claim_id")),
            prefix="role_claim_",
        )
    except ValueError as error:
        raise ValueError("backup app state is invalid") from error


def _validate_export_state(record: dict[str, object]) -> None:
    if (
        set(record) != _EXPORT_KEYS
        or record.get("schema_version") != 1
        or not isinstance(record.get("manifest_digest_sha256"), str)
        or _HEX64.fullmatch(cast(str, record["manifest_digest_sha256"])) is None
    ):
        raise ValueError("backup app state is invalid")
    try:
        validate_identifier(cast(str, record.get("export_id")), prefix="export_")
        _canonical_app_timestamp(record.get("created_at"))
    except (TypeError, ValueError) as error:
        raise ValueError("backup app state is invalid") from error


def _validate_run_state(
    record: dict[str, object],
    *,
    job_name: str,
    run_id: str,
) -> None:
    attempt = record.get("attempt")
    reason = record.get("reason")
    if (
        set(record) != _RUN_KEYS
        or record.get("job_name") != job_name
        or record.get("run_id") != run_id
        or type(attempt) is not int
        or attempt <= 0
        or record.get("status") not in _RUN_STATUS
        or reason is not None
        and (not isinstance(reason, str) or _REASON_CODE.fullmatch(reason) is None)
    ):
        raise ValueError("backup app state is invalid")
    started_at = _canonical_app_timestamp(record.get("started_at"))
    finished_at = _canonical_app_timestamp(record.get("finished_at"))
    if finished_at < started_at:
        raise ValueError("backup app state is invalid")
    next_due_at = record.get("next_due_at")
    if next_due_at is not None:
        _canonical_app_timestamp(next_due_at)


def _canonical_app_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("backup app state is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError("backup app state is invalid") from error
    canonical = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ValueError("backup app state is invalid")
    return parsed


def _validate_uuid_identifier(value: str, *, prefix: str) -> None:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("backup app state is invalid")
    try:
        identifier = uuid.UUID(value.removeprefix(prefix))
    except ValueError as error:
        raise ValueError("backup app state is invalid") from error
    if identifier.version != 4 or value != f"{prefix}{identifier}":
        raise ValueError("backup app state is invalid")


def _sqlite_backup_bytes(
    root: Path,
    database_name: str,
    *,
    expected_root_identity: RootIdentity,
) -> bytes:
    source = None
    try:
        source = connect_database_read_only(
            root=root,
            database_name=database_name,
            expected_root_identity=expected_root_identity,
        )
        source.execute("BEGIN")
        if classify_local_schema(source).state in {"invalid", "newer"}:
            source.close()
            raise ValueError("required backup SQLite source is unavailable")
    except StorageError as error:
        if source is not None:
            source.close()
        raise ValueError("required backup SQLite source is unavailable") from error
    descriptor, temp_name = tempfile.mkstemp(prefix="open-brain-backup-", suffix=".sqlite3")
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        destination = sqlite3.connect(temp_path)
        try:
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("backup SQLite snapshot failed integrity verification")
        finally:
            destination.close()
        payload = temp_path.read_bytes()
    finally:
        source.close()
        temp_path.unlink(missing_ok=True)
    validate_sqlite_backup_bytes(payload)
    return payload


def validate_sqlite_backup_bytes(payload: bytes) -> None:
    """Validate one bounded SQLite snapshot without exposing its rows."""

    if not isinstance(payload, bytes) or not payload or len(payload) > _MAXIMUM_SQLITE_BYTES:
        raise ValueError("backup SQLite snapshot is invalid")
    descriptor, temp_name = tempfile.mkstemp(prefix="open-brain-verify-", suffix=".sqlite3")
    temp_path = Path(temp_name)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short SQLite verification write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        connection = sqlite3.connect(temp_path)
        try:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("backup SQLite snapshot failed integrity verification")
            if classify_local_schema(connection).state in {"invalid", "newer"}:
                raise ValueError("backup SQLite snapshot schema is invalid")
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ValueError("backup SQLite snapshot is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for candidate in (temp_path, Path(temp_name + "-wal"), Path(temp_name + "-shm")):
            candidate.unlink(missing_ok=True)
