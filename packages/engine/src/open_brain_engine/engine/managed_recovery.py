"""Narrow owner recovery for legacy writes with unverifiable target relationships."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    _validated_parts,
    assert_root_identity,
    read_confined,
)
from open_brain_engine.storage.locks import FileLease
from open_brain_engine.storage.sqlite import SchemaError

from .contracts import LocalEngineContext, ManagedWorkspaceFailure
from .local_schema import (
    LocalSchemaUpgradeCommittedError,
    classify_local_schema,
    open_local_database_read_only,
)
from .local_store import _LocalStore
from .managed_workspace import ManagedWorkspaceTasks, _digest, _request_sha256, _timestamp
from .markdown_import_fs import (
    MAX_FILE_BYTES,
    ImportDirectoryUnavailable,
    roots_overlap,
    snapshot_directory,
)
from .normalization import _delivery_id, _portable_id, _utc_now

_PENDING = {"prepared", "writing", "promoted"}
_DECISION = "owner_abandon_unverifiable_legacy_write"
_IMMUTABLE_OPERATION = (
    "operation_id",
    "request_sha256",
    "workspace_id",
    "note_id",
    "kind",
    "caller_actor_id",
    "target_relative_path",
    "expected_revision_id",
    "expected_target_sha256",
    "body_sha256",
    "created_at",
)
_IMMUTABLE_WORKSPACE = (
    "workspace_id",
    "root_path",
    "device",
    "inode",
    "owner_actor_id",
    "origin_owner_actor_id",
    "created_at",
)


class ManagedRecoveryFailure(RuntimeError):
    """Bounded failure, including whether a separate schema upgrade already committed."""

    def __init__(self, code: str, *, schema_upgraded: bool = False) -> None:
        if code not in {
            "invalid_request",
            "operation_replay_mismatch",
            "stale_preview",
            "request_replay_mismatch",
            "not_eligible",
            "unknown_operation",
            "recovery_unavailable",
        }:
            raise ValueError("invalid managed recovery failure")
        self.code = code
        self.schema_upgraded = schema_upgraded
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ManagedRecoveryPreview:
    operation_id: str
    workspace_id: str
    note_id: str | None
    status: str
    eligible: bool
    reason: str
    preview_digest: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "workspace_id": self.workspace_id,
            "note_id": self.note_id,
            "status": self.status,
            "eligible": self.eligible,
            "reason": self.reason,
            "preview_digest": self.preview_digest,
        }


@dataclass(frozen=True, slots=True)
class ManagedRecoveryInspection:
    entries: tuple[ManagedRecoveryPreview, ...]
    next_after: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "inspected",
            "entries": [entry.to_dict() for entry in self.entries],
            "next_after": self.next_after,
        }


@dataclass(frozen=True, slots=True)
class ManagedRecoveryReceipt:
    request_id: str
    operation_id: str
    duplicate: bool
    schema_upgraded: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "abandoned",
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "duplicate": self.duplicate,
            "schema_upgraded": self.schema_upgraded,
        }


def _identity(profile: LocalEngineContext) -> dict[str, object]:
    assert_root_identity(profile.root, profile.root_identity)
    return {
        "tenant_id": str(profile.tenant_id),
        "owner_actor_id": str(profile.owner_actor_id),
        "root_device": profile.root_identity[0],
        "root_inode": profile.root_identity[1],
    }


def _schema_version(connection: sqlite3.Connection) -> int:
    state = classify_local_schema(connection)
    if state.state not in {"current", "supported_old"} or state.version not in {
        5,
        6,
        7,
        8,
        9,
    }:
        raise ManagedRecoveryFailure("recovery_unavailable")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    return state.version


def _safe_path(value: object) -> str:
    if not isinstance(value, str):
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    try:
        _validated_parts(value)
    except RootConfinementError, ValueError:
        raise ManagedRecoveryFailure("operation_replay_mismatch") from None
    return value


def _operation(connection: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM managed_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        raise ManagedRecoveryFailure("unknown_operation")
    return cast(sqlite3.Row, row)


def _workspace(
    connection: sqlite3.Connection, profile: LocalEngineContext, operation: sqlite3.Row
) -> sqlite3.Row:
    _identity(profile)
    row = connection.execute(
        "SELECT * FROM managed_workspaces WHERE workspace_id=?", (operation["workspace_id"],)
    ).fetchone()
    if row is None or row["owner_actor_id"] != profile.owner_actor_id:
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    root = row["root_path"]
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    try:
        if roots_overlap(snapshot_directory(profile.root), snapshot_directory(Path(root))):
            raise ValueError
        assert_root_identity(Path(root), (int(row["device"]), int(row["inode"])))
        metadata = Path(root).stat(follow_symlinks=False)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise ValueError
    except ImportDirectoryUnavailable, RootConfinementError, OSError, TypeError, ValueError:
        raise ManagedRecoveryFailure("operation_replay_mismatch") from None
    return cast(sqlite3.Row, row)


def _legacy_marker(connection: sqlite3.Connection, operation: sqlite3.Row, version: int) -> bool:
    if version == 5:
        return True  # A virtual marker; preview cannot mutate the predecessor.
    marker = connection.execute(
        "SELECT * FROM managed_write_authority WHERE operation_id=?", (operation["operation_id"],)
    ).fetchone()
    if marker is None or marker["authority_version"] not in {0, 1}:
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    if marker["authority_version"] == 0:
        if marker["descriptor_json"] is not None or marker["descriptor_sha256"] is not None:
            raise ManagedRecoveryFailure("operation_replay_mismatch")
        return True
    return False


def _revision(connection: sqlite3.Connection, note_id: str, revision_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM managed_note_revisions WHERE note_id=? AND revision_id=?",
        (note_id, revision_id),
    ).fetchone()
    if row is None or not isinstance(row["body_bytes"], bytes):
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    if _digest(row["body_bytes"]) != row["body_sha256"]:
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    return cast(sqlite3.Row, row)


def _revision_metadata(row: sqlite3.Row) -> dict[str, object]:
    return {
        key: row[key]
        for key in (
            "revision_id",
            "note_id",
            "kind",
            "parent_revision_id",
            "body_sha256",
            "accepted_by_actor_id",
            "recorded_at",
            "operation_id",
        )
    }


def _preview(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    operation: sqlite3.Row,
    version: int,
) -> tuple[ManagedRecoveryPreview, dict[str, object] | None]:
    operation_id = cast(str, operation["operation_id"])
    workspace_id = cast(str, operation["workspace_id"])
    note_id = cast(str | None, operation["note_id"])
    _delivery_id(operation_id)
    _portable_id(workspace_id, "workspace")
    if note_id is not None:
        _portable_id(note_id, "page")

    def refused(reason: str) -> tuple[ManagedRecoveryPreview, None]:
        return ManagedRecoveryPreview(
            operation_id, workspace_id, note_id, operation["status"], False, reason, None
        ), None

    if operation["kind"] != "materialize":
        return refused("standalone_materialize_required")
    workspace = _workspace(connection, profile, operation)
    if not _legacy_marker(connection, operation, version):
        return refused("legacy_authority_required")
    if operation["status"] not in _PENDING:
        return refused("pending_operation_required")
    if note_id is None or operation["caller_actor_id"] != profile.owner_actor_id:
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    if operation["request_sha256"] != _request_sha256(
        {
            "kind": "materialize",
            "note_id": note_id,
            "operation_id": operation_id,
            "workspace_id": workspace_id,
        }
    ):
        # Refresh leaf hashes use the parent request. Neither they nor damaged
        # caller hashes can authorize this owner-only legacy abandonment.
        return refused("standalone_request_required")
    for parent in connection.execute(
        "SELECT * FROM managed_operations WHERE workspace_id=? AND kind='refresh' "
        "AND body_bytes IS NOT NULL",
        (workspace_id,),
    ):
        if operation_id in ManagedWorkspaceTasks._refresh_children(parent):
            return refused("standalone_request_required")
    note = connection.execute(
        "SELECT * FROM managed_notes WHERE note_id=? AND workspace_id=?", (note_id, workspace_id)
    ).fetchone()
    if note is None:
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    target = _safe_path(operation["target_relative_path"])
    current_path = _safe_path(note["relative_path"])
    for path in {target, current_path}:
        read_confined(
            root=Path(workspace["root_path"]),
            relative=path,
            expected_root_identity=(int(workspace["device"]), int(workspace["inode"])),
            maximum_bytes=MAX_FILE_BYTES,
        )
    revision = _revision(connection, note_id, operation["expected_revision_id"])
    current_revision = _revision(connection, note_id, note["accepted_revision_id"])
    if note["materialized_revision_id"] is None:
        if note["materialized_sha256"] is not None or note["write_base_sha256"] is not None:
            raise ManagedRecoveryFailure("operation_replay_mismatch")
    else:
        materialized = _revision(connection, note_id, note["materialized_revision_id"])
        if materialized["body_sha256"] != note["materialized_sha256"]:
            raise ManagedRecoveryFailure("operation_replay_mismatch")
    body = operation["body_bytes"]
    if (
        not isinstance(body, bytes)
        or _digest(body) != operation["body_sha256"]
        or revision["body_bytes"] != body
        or revision["body_sha256"] != operation["body_sha256"]
        or not ManagedWorkspaceTasks._retained_write_preimage(connection, operation)
    ):
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    if note["accepted_revision_id"] != operation["expected_revision_id"]:
        valid_transition = ManagedWorkspaceTasks._accepted_write_successor(
            connection, note, operation, profile.owner_actor_id
        )
    else:
        valid_transition = note["write_base_sha256"] == operation[
            "expected_target_sha256"
        ] or ManagedWorkspaceTasks._completed_same_revision_write(
            connection, note, operation, profile.owner_actor_id
        )
    if not valid_transition:
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    if target == current_path:
        return refused("target_evidence_present")
    snapshot: dict[str, object] = {
        "version": 1,
        "profile": _identity(profile),
        "authority_version": 0,
        "operation": {
            key: operation[key]
            for key in (*_IMMUTABLE_OPERATION, "status", "stage", "completed_at")
        },
        "workspace": {key: workspace[key] for key in _IMMUTABLE_WORKSPACE},
        "note": dict(note),
        "expected_revision": _revision_metadata(revision),
        "current_revision": _revision_metadata(current_revision),
        "parent_ids": [],
    }
    payload = portable_canonical_json_bytes(snapshot)
    if len(payload) > 16_384:
        raise ManagedRecoveryFailure("recovery_unavailable")
    return ManagedRecoveryPreview(
        operation_id,
        workspace_id,
        note_id,
        operation["status"],
        True,
        "unverifiable_legacy_target",
        _digest(payload),
    ), snapshot


def inspect_managed_recovery(
    profile: LocalEngineContext,
    *,
    operation_id: str | None = None,
    after: str | None = None,
    limit: int = 100,
) -> ManagedRecoveryInspection:
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or operation_id is not None
        and after is not None
    ):
        raise ManagedRecoveryFailure("invalid_request")
    try:
        _identity(profile)
        for value in (operation_id, after):
            if value is not None:
                _delivery_id(value)
        connection = open_local_database_read_only(profile, allow_old=True)
        try:
            version = _schema_version(connection)
            if operation_id is not None:
                rows = [_operation(connection, operation_id)]
            else:
                rows = list(
                    connection.execute(
                        "SELECT * FROM managed_operations WHERE kind IN ('setup','materialize') "
                        "AND status IN ('prepared','writing','promoted') AND operation_id > ? "
                        "ORDER BY operation_id LIMIT ?",
                        (after or "", limit + 1),
                    )
                )
            entries = tuple(_preview(connection, profile, row, version)[0] for row in rows[:limit])
            next_after = entries[-1].operation_id if len(rows) > limit else None
            return ManagedRecoveryInspection(entries, next_after)
        finally:
            connection.close()
    except ManagedRecoveryFailure:
        raise
    except (
        ManagedWorkspaceFailure,
        SchemaError,
        sqlite3.Error,
        RootConfinementError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
    ):
        raise ManagedRecoveryFailure("recovery_unavailable") from None


def _request_digest(operation_id: str, preview_digest: str) -> str:
    return _request_sha256(
        {"decision": _DECISION, "operation_id": operation_id, "preview_digest": preview_digest}
    )


def _existing_decision(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    operation_id: str,
    request_id: str,
    expected_digest: str,
) -> ManagedRecoveryReceipt | None:
    by_id = connection.execute(
        "SELECT * FROM managed_recovery_decisions WHERE recovery_request_id=?", (request_id,)
    ).fetchone()
    if by_id is not None and (
        by_id["target_operation_id"] != operation_id
        or by_id["request_sha256"] != _request_digest(operation_id, expected_digest)
    ):
        raise ManagedRecoveryFailure("request_replay_mismatch")
    decision = (
        by_id
        if by_id is not None
        else connection.execute(
            "SELECT * FROM managed_recovery_decisions WHERE target_operation_id=?", (operation_id,)
        ).fetchone()
    )
    if decision is None:
        return None
    if decision["preview_sha256"] != expected_digest:
        raise ManagedRecoveryFailure("stale_preview")
    operation = _operation(connection, operation_id)
    workspace = _workspace(connection, profile, operation)
    snapshot = json.loads(decision["snapshot_json"])
    if (
        not isinstance(snapshot, dict)
        or set(snapshot)
        != {
            "version",
            "profile",
            "authority_version",
            "operation",
            "workspace",
            "note",
            "expected_revision",
            "current_revision",
            "parent_ids",
        }
        or snapshot["version"] != 1
        or snapshot["authority_version"] != 0
        or snapshot["parent_ids"] != []
        or portable_canonical_json_bytes(snapshot).decode() != decision["snapshot_json"]
        or _digest(portable_canonical_json_bytes(snapshot)) != decision["preview_sha256"]
        or decision["request_sha256"] != _request_digest(operation_id, decision["preview_sha256"])
        or decision["decision"] != _DECISION
        or decision["actor_id"] != profile.owner_actor_id
        or decision["workspace_id"] != operation["workspace_id"]
        or snapshot["profile"] != _identity(profile)
        or snapshot["workspace"] != {key: workspace[key] for key in _IMMUTABLE_WORKSPACE}
        or not _legacy_marker(connection, operation, 6)
        or operation["status"] != "cancelled"
        or operation["stage"] != 3
        or operation["completed_at"] != decision["completed_at"]
        or not isinstance(operation["body_bytes"], bytes)
        or _digest(operation["body_bytes"]) != operation["body_sha256"]
    ):
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    recorded = snapshot["operation"]
    if not isinstance(recorded, dict) or set(recorded) != {
        *_IMMUTABLE_OPERATION,
        "status",
        "stage",
        "completed_at",
    }:
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    if (
        any(recorded[key] != operation[key] for key in _IMMUTABLE_OPERATION)
        or recorded["status"] not in _PENDING
        or recorded["completed_at"] is not None
    ):
        raise ManagedRecoveryFailure("operation_replay_mismatch")
    return ManagedRecoveryReceipt(decision["recovery_request_id"], operation_id, True)


def _preflight(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    operation_id: str,
    request_id: str,
    expected_digest: str,
) -> tuple[ManagedRecoveryReceipt | None, dict[str, object] | None]:
    version = _schema_version(connection)
    if version >= 6:
        previous = _existing_decision(
            connection,
            profile,
            operation_id=operation_id,
            request_id=request_id,
            expected_digest=expected_digest,
        )
        if previous is not None:
            return previous, None
    entry, snapshot = _preview(connection, profile, _operation(connection, operation_id), version)
    if not entry.eligible:
        raise ManagedRecoveryFailure("not_eligible")
    if entry.preview_digest != expected_digest:
        raise ManagedRecoveryFailure("stale_preview")
    return None, snapshot


def abandon_managed_write(
    profile: LocalEngineContext,
    *,
    operation_id: str,
    expected_digest: str,
    request_id: str,
    validate_before_write: Callable[[], None],
    clock: Callable[[], datetime] | None = None,
) -> ManagedRecoveryReceipt:
    clock = clock or _utc_now
    try:
        _delivery_id(operation_id)
        _delivery_id(request_id)
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_digest)
            or not callable(validate_before_write)
        ):
            raise ValueError
    except TypeError, ValueError:
        raise ManagedRecoveryFailure("invalid_request") from None
    upgraded = False
    try:
        validate_before_write()
        _identity(profile)
        lease = FileLease(
            profile.root / ".open-brain",
            "engine-" + sha256(profile.owner_actor_id.encode()).hexdigest()[:32],
            clock=clock,
            validate_acquire=validate_before_write,
            parent_root_identity=profile.root_identity,
        )
        with lease.acquire_shared_writer():
            connection = open_local_database_read_only(profile, allow_old=True)
            try:
                initial_version = _schema_version(connection)
                previous, _ = _preflight(
                    connection,
                    profile,
                    operation_id=operation_id,
                    request_id=request_id,
                    expected_digest=expected_digest,
                )
                if previous is not None:
                    return previous
            finally:
                connection.close()
            validate_before_write()
            store = _LocalStore(
                profile, clock=clock, schema_version=6 if initial_version == 5 else initial_version
            )
            upgraded = initial_version == 5
            with store.transaction() as connection:
                validate_before_write()
                previous, snapshot = _preflight(
                    connection,
                    profile,
                    operation_id=operation_id,
                    request_id=request_id,
                    expected_digest=expected_digest,
                )
                if previous is not None:
                    return ManagedRecoveryReceipt(previous.request_id, operation_id, True, upgraded)
                if snapshot is None:
                    raise ManagedRecoveryFailure("operation_replay_mismatch")
                now = _timestamp(clock())
                connection.execute(
                    "INSERT INTO managed_recovery_decisions "
                    "(recovery_request_id, target_operation_id, actor_id, workspace_id, "
                    "preview_sha256, request_sha256, snapshot_json, decision, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        operation_id,
                        profile.owner_actor_id,
                        _operation(connection, operation_id)["workspace_id"],
                        expected_digest,
                        _request_digest(operation_id, expected_digest),
                        portable_canonical_json_bytes(snapshot).decode(),
                        _DECISION,
                        now,
                    ),
                )
                cursor = connection.execute(
                    "UPDATE managed_operations SET status='cancelled', stage=3, completed_at=? "
                    "WHERE operation_id=? AND status IN ('prepared','writing','promoted')",
                    (now, operation_id),
                )
                if cursor.rowcount != 1:
                    raise ManagedRecoveryFailure("operation_replay_mismatch")
                validate_before_write()
        return ManagedRecoveryReceipt(request_id, operation_id, False, upgraded)
    except LocalSchemaUpgradeCommittedError:
        raise ManagedRecoveryFailure("recovery_unavailable", schema_upgraded=True) from None
    except ManagedRecoveryFailure as error:
        raise ManagedRecoveryFailure(error.code, schema_upgraded=upgraded) from None
    except (
        ManagedWorkspaceFailure,
        SchemaError,
        sqlite3.Error,
        RootConfinementError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
    ):
        raise ManagedRecoveryFailure("recovery_unavailable", schema_upgraded=upgraded) from None


def legacy_recovery_blocker(
    connection: sqlite3.Connection, profile: LocalEngineContext, operation_id: str
) -> bool:
    """Classify only the missing-evidence case; corruption remains a hard startup error."""
    try:
        entry, _ = _preview(
            connection, profile, _operation(connection, operation_id), _schema_version(connection)
        )
        return entry.eligible
    except (
        ManagedRecoveryFailure,
        ManagedWorkspaceFailure,
        RootConfinementError,
        sqlite3.Error,
        OSError,
        TypeError,
        ValueError,
        KeyError,
    ):
        return False
