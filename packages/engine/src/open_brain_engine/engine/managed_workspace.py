"""Durable owner-controlled projection of accepted pages into one Markdown workspace."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    RootConfinementError,
    RootIdentity,
    assert_root_identity,
    atomic_replace,
    atomic_write_new,
    read_confined,
)
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown

from .contracts import (
    ManagedNoteObservation,
    ManagedWorkspaceFailure,
    ManagedWorkspaceFault,
    ManagedWorkspaceObservation,
    ManagedWorkspaceReceipt,
)
from .markdown_import_fs import (
    MAX_FILE_BYTES,
    ImportDirectoryUnavailable,
    ImportScanIncomplete,
    MarkdownCandidate,
    ScanLimits,
    enumerate_markdown,
    pin_import_root,
    read_markdown_candidate,
    roots_overlap,
    snapshot_directory,
)
from .normalization import _delivery_id, _new_id, _portable_id

if TYPE_CHECKING:
    from .local import BrainEngine


@dataclass(frozen=True, slots=True)
class _Workspace:
    workspace_id: str
    root: Path
    root_identity: RootIdentity
    owner_actor_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class _CanonicalPage:
    note_id: str
    relative_path: str
    payload: bytes
    privacy_json: str
    provenance_json: str


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid managed-workspace clock")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _request_sha256(value: dict[str, object]) -> str:
    return sha256(portable_canonical_json_bytes(value)).hexdigest()


def _fingerprint(candidate: MarkdownCandidate) -> str:
    observed = candidate.observed
    return portable_canonical_json_bytes(
        {
            "changed_ns": observed.changed_ns,
            "device": observed.device,
            "inode": observed.inode,
            "link_count": observed.link_count,
            "mode": observed.mode,
            "modified_ns": observed.modified_ns,
            "size": observed.size,
        }
    ).decode("utf-8")


class ManagedWorkspaceTasks:
    """Owner-only mutations and bounded observations for one sibling Markdown projection."""

    def __init__(self, engine: BrainEngine, *, scan_limits: ScanLimits | None = None) -> None:
        self._engine = engine
        self._scan_limits = scan_limits or ScanLimits()

    def setup(self, directory: str, *, operation_id: str) -> ManagedWorkspaceReceipt:
        _delivery_id(operation_id)
        try:
            with pin_import_root(directory) as pinned:
                self._require_safe_workspace(
                    pinned.snapshot.canonical_path, pinned.snapshot.identity
                )
                existing = self._workspace_by_identity(pinned.snapshot.identity)
                if existing is not None:
                    if existing.root != pinned.snapshot.canonical_path:
                        raise ManagedWorkspaceFailure("unsafe_workspace")
                    recovered = self.recover()
                    return ManagedWorkspaceReceipt(
                        "setup",
                        existing.workspace_id,
                        generation=existing.generation,
                        duplicate=recovered == 0,
                    )
                inventory = enumerate_markdown(
                    pinned.descriptor,
                    limits=self._scan_limits,
                    allow_large_vault=False,
                    interrupted=lambda: False,
                )
                if inventory.candidates or any(outcome.failed for outcome in inventory.outcomes):
                    raise ManagedWorkspaceFailure("unsafe_workspace")
                pages = self._canonical_pages()
                workspace_id = _new_id("workspace")
                now = _timestamp(self._engine._clock())
                operation_ids: list[str] = []
                with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
                    with self._engine._store.transaction() as connection:
                        connection.execute(
                            """INSERT INTO managed_workspaces
                            (workspace_id, root_path, device, inode, owner_actor_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                workspace_id,
                                str(pinned.snapshot.canonical_path),
                                str(pinned.snapshot.identity[0]),
                                str(pinned.snapshot.identity[1]),
                                self._engine.profile.owner_actor_id,
                                now,
                            ),
                        )
                        for page in pages:
                            revision_id = _new_id("revision")
                            internal_operation_id = _new_id("operation")
                            operation_ids.append(internal_operation_id)
                            body_sha256 = _digest(page.payload)
                            connection.execute(
                                """INSERT INTO managed_notes
                                (note_id, workspace_id, relative_path, accepted_revision_id,
                                 created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?)""",
                                (
                                    page.note_id,
                                    workspace_id,
                                    page.relative_path,
                                    revision_id,
                                    now,
                                    now,
                                ),
                            )
                            connection.execute(
                                """INSERT INTO managed_note_revisions
                                (revision_id, note_id, kind, body_bytes, body_sha256,
                                 accepted_by_actor_id, provenance_json, privacy_json, recorded_at,
                                 operation_id)
                                VALUES (?, ?, 'setup', ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    revision_id,
                                    page.note_id,
                                    page.payload,
                                    body_sha256,
                                    self._engine.profile.owner_actor_id,
                                    page.provenance_json,
                                    page.privacy_json,
                                    now,
                                    internal_operation_id,
                                ),
                            )
                            request_sha256 = _request_sha256(
                                {
                                    "caller_operation_id": operation_id,
                                    "kind": "setup",
                                    "note_id": page.note_id,
                                    "revision_id": revision_id,
                                    "target": page.relative_path,
                                    "workspace_id": workspace_id,
                                }
                            )
                            self._insert_operation(
                                connection,
                                operation_id=internal_operation_id,
                                request_sha256=request_sha256,
                                workspace_id=workspace_id,
                                note_id=page.note_id,
                                kind="setup",
                                target_relative_path=page.relative_path,
                                expected_revision_id=revision_id,
                                expected_target_sha256=None,
                                body=page.payload,
                                now=now,
                            )
                    self._engine._fault(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
                    for internal_operation_id in operation_ids:
                        self._process_materialization(internal_operation_id)
                return ManagedWorkspaceReceipt("setup", workspace_id, generation=0)
        except ManagedWorkspaceFailure:
            raise
        except (ImportDirectoryUnavailable, ImportScanIncomplete, OSError, ValueError):
            raise ManagedWorkspaceFailure("unsafe_workspace") from None

    def observe(self, workspace_id: str) -> ManagedWorkspaceObservation:
        _portable_id(workspace_id, "workspace")
        workspace = self._workspace(workspace_id)
        try:
            with pin_import_root(str(workspace.root)) as pinned:
                if pinned.snapshot.identity != workspace.root_identity:
                    raise ManagedWorkspaceFailure("unsafe_workspace")
                inventory = enumerate_markdown(
                    pinned.descriptor,
                    limits=self._scan_limits,
                    allow_large_vault=False,
                    interrupted=lambda: False,
                )
                if any(outcome.failed for outcome in inventory.outcomes):
                    raise ManagedWorkspaceFailure("invalid_observation")
                known = self._known_notes(workspace_id)
                observed: dict[str, tuple[str, bytes, str]] = {}
                for candidate in inventory.candidates:
                    payload = read_markdown_candidate(
                        pinned.descriptor,
                        candidate,
                        maximum_bytes=self._scan_limits.file_bytes,
                    )
                    try:
                        parsed = parse_markdown(payload)
                    except MarkdownFormatError:
                        continue
                    note_id = parsed.fields.get("page_id")
                    if not isinstance(note_id, str) or note_id not in known:
                        continue
                    if note_id in observed:
                        raise ManagedWorkspaceFailure("invalid_observation")
                    observed[note_id] = (
                        candidate.relative_path,
                        payload,
                        _fingerprint(candidate),
                    )
                with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
                    with self._engine._store.transaction() as connection:
                        current = self._workspace_row(connection, workspace_id)
                        self._assert_workspace_row(current, workspace)
                        generation = int(current["observation_generation"]) + 1
                        connection.execute(
                            "DELETE FROM managed_note_observations WHERE workspace_id = ?",
                            (workspace_id,),
                        )
                        moved = [
                            note_id
                            for note_id, (relative_path, _, _) in observed.items()
                            if known[note_id]["relative_path"] != relative_path
                        ]
                        for note_id in moved:
                            connection.execute(
                                "UPDATE managed_notes SET relative_path = ? WHERE note_id = ?",
                                (f".open-brain-pending/{note_id}", note_id),
                            )
                        result: list[ManagedNoteObservation] = []
                        now = _timestamp(self._engine._clock())
                        for note_id, (relative_path, payload, fingerprint_json) in sorted(
                            observed.items()
                        ):
                            note = known[note_id]
                            connection.execute(
                                "UPDATE managed_notes SET relative_path = ?, updated_at = ? "
                                "WHERE note_id = ?",
                                (relative_path, now, note_id),
                            )
                            observed_sha256 = _digest(payload)
                            connection.execute(
                                """INSERT INTO managed_note_observations
                                (workspace_id, generation, note_id, relative_path,
                                 accepted_revision_id, observed_sha256, materialized_sha256,
                                 fingerprint_json, observed_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    workspace_id,
                                    generation,
                                    note_id,
                                    relative_path,
                                    note["accepted_revision_id"],
                                    observed_sha256,
                                    note["materialized_sha256"],
                                    fingerprint_json,
                                    now,
                                ),
                            )
                            if int(note["active"]) == 1:
                                result.append(
                                    ManagedNoteObservation(
                                        note_id=note_id,
                                        relative_path=relative_path,
                                        accepted_revision_id=cast(
                                            str, note["accepted_revision_id"]
                                        ),
                                        materialized_sha256=cast(
                                            str | None, note["materialized_sha256"]
                                        ),
                                        observed_sha256=observed_sha256,
                                        changed=observed_sha256
                                        != cast(str | None, note["materialized_sha256"]),
                                    )
                                )
                        connection.execute(
                            "UPDATE managed_workspaces SET observation_generation = ? "
                            "WHERE workspace_id = ?",
                            (generation, workspace_id),
                        )
                return ManagedWorkspaceObservation(workspace_id, generation, tuple(result))
        except ManagedWorkspaceFailure:
            raise
        except (ImportDirectoryUnavailable, ImportScanIncomplete, OSError, ValueError):
            raise ManagedWorkspaceFailure("invalid_observation") from None

    def accept_observed(
        self,
        workspace_id: str,
        note_id: str,
        *,
        generation: int,
        operation_id: str,
    ) -> ManagedWorkspaceReceipt:
        _portable_id(workspace_id, "workspace")
        _portable_id(note_id, "page")
        _delivery_id(operation_id)
        if type(generation) is not int or generation < 1:
            raise ManagedWorkspaceFailure("stale_observation")
        workspace = self._workspace(workspace_id)
        request_sha256 = _request_sha256(
            {
                "generation": generation,
                "kind": "accept_revision",
                "note_id": note_id,
                "operation_id": operation_id,
                "workspace_id": workspace_id,
            }
        )
        existing = self._operation(operation_id)
        if existing is not None:
            self._require_matching_operation(existing, request_sha256, "accept_revision")
            return ManagedWorkspaceReceipt(
                "accepted", workspace_id, note_id, generation, duplicate=True
            )
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                note = self._note_row(connection, workspace_id, note_id)
                self._require_active_note(connection, note)
                observation = connection.execute(
                    """SELECT * FROM managed_note_observations
                    WHERE workspace_id = ? AND generation = ? AND note_id = ?""",
                    (workspace_id, generation, note_id),
                ).fetchone()
                if observation is None or int(
                    self._workspace_row(connection, workspace_id)["observation_generation"]
                ) != generation:
                    raise ManagedWorkspaceFailure("stale_observation")
                if observation["accepted_revision_id"] != note["accepted_revision_id"]:
                    raise ManagedWorkspaceFailure("stale_observation")
                payload = read_confined(
                    root=workspace.root,
                    relative=cast(str, observation["relative_path"]),
                    expected_root_identity=workspace.root_identity,
                    maximum_bytes=MAX_FILE_BYTES,
                )
                if payload is None or _digest(payload) != observation["observed_sha256"]:
                    raise ManagedWorkspaceFailure("stale_observation")
                previous = connection.execute(
                    "SELECT * FROM managed_note_revisions WHERE revision_id = ?",
                    (note["accepted_revision_id"],),
                ).fetchone()
                if previous is None:
                    raise ManagedWorkspaceFailure("operation_replay_mismatch")
                now = _timestamp(self._engine._clock())
                duplicate = previous["body_sha256"] == observation["observed_sha256"]
                if duplicate:
                    revision_id = cast(str, previous["revision_id"])
                else:
                    revision_id = _new_id("revision")
                    connection.execute(
                        """INSERT INTO managed_note_revisions
                        (revision_id, note_id, parent_revision_id, kind, body_bytes, body_sha256,
                         accepted_by_actor_id, provenance_json, privacy_json, recorded_at,
                         operation_id)
                        VALUES (?, ?, ?, 'edit', ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            revision_id,
                            note_id,
                            previous["revision_id"],
                            payload,
                            observation["observed_sha256"],
                            self._engine.profile.owner_actor_id,
                            previous["provenance_json"],
                            previous["privacy_json"],
                            now,
                            operation_id,
                        ),
                    )
                connection.execute(
                    """UPDATE managed_notes
                    SET accepted_revision_id = ?, materialized_revision_id = ?,
                        materialized_sha256 = ?, relative_path = ?, updated_at = ?
                    WHERE note_id = ?""",
                    (
                        revision_id,
                        revision_id,
                        observation["observed_sha256"],
                        observation["relative_path"],
                        now,
                        note_id,
                    ),
                )
                self._insert_operation(
                    connection,
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    workspace_id=workspace_id,
                    note_id=note_id,
                    kind="accept_revision",
                    target_relative_path=None,
                    expected_revision_id=cast(str, note["accepted_revision_id"]),
                    expected_target_sha256=cast(str, observation["observed_sha256"]),
                    body=payload,
                    now=now,
                    status="completed",
                )
                connection.execute(
                    "DELETE FROM managed_note_observations WHERE workspace_id = ?",
                    (workspace_id,),
                )
        return ManagedWorkspaceReceipt(
            "accepted", workspace_id, note_id, generation, duplicate=duplicate
        )

    def materialize(
        self, workspace_id: str, note_id: str, *, operation_id: str
    ) -> ManagedWorkspaceReceipt:
        _portable_id(workspace_id, "workspace")
        _portable_id(note_id, "page")
        _delivery_id(operation_id)
        request_sha256 = _request_sha256(
            {
                "kind": "materialize",
                "note_id": note_id,
                "operation_id": operation_id,
                "workspace_id": workspace_id,
            }
        )
        existing = self._operation(operation_id)
        if existing is not None:
            self._require_matching_operation(existing, request_sha256, "materialize")
            if existing["status"] == "conflict":
                raise ManagedWorkspaceFailure("target_changed")
            if existing["status"] != "completed":
                with self._engine._writer_lease.acquire_shared_writer():
                    self._process_materialization(operation_id)
            return ManagedWorkspaceReceipt("materialized", workspace_id, note_id, duplicate=True)
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                note = self._note_row(connection, workspace_id, note_id)
                self._require_active_note(connection, note)
                revision = connection.execute(
                    "SELECT * FROM managed_note_revisions WHERE revision_id = ?",
                    (note["accepted_revision_id"],),
                ).fetchone()
                if revision is None:
                    raise ManagedWorkspaceFailure("operation_replay_mismatch")
                now = _timestamp(self._engine._clock())
                self._insert_operation(
                    connection,
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    workspace_id=workspace_id,
                    note_id=note_id,
                    kind="materialize",
                    target_relative_path=cast(str, note["relative_path"]),
                    expected_revision_id=cast(str, note["accepted_revision_id"]),
                    expected_target_sha256=cast(str | None, note["materialized_sha256"]),
                    body=cast(bytes, revision["body_bytes"]),
                    now=now,
                )
            self._engine._fault(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
            self._process_materialization(operation_id)
        return ManagedWorkspaceReceipt("materialized", workspace_id, note_id)

    def deactivate(
        self, workspace_id: str, note_id: str, *, operation_id: str
    ) -> ManagedWorkspaceReceipt:
        return self._set_active(workspace_id, note_id, operation_id, active=False)

    def restore(
        self, workspace_id: str, note_id: str, *, operation_id: str
    ) -> ManagedWorkspaceReceipt:
        return self._set_active(workspace_id, note_id, operation_id, active=True)

    def recover(self) -> int:
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            return self._recover_locked()

    def _recover_locked(self) -> int:
        recovered = 0
        connection = self._engine._store.connect()
        try:
            operation_ids = [
                cast(str, row[0])
                for row in connection.execute(
                    """SELECT operation_id FROM managed_operations
                    WHERE status IN ('prepared', 'writing', 'promoted')
                    ORDER BY created_at, operation_id"""
                )
            ]
        finally:
            connection.close()
        for operation_id in operation_ids:
            self._process_materialization(operation_id)
            recovered += 1
        return recovered

    def _set_active(
        self,
        workspace_id: str,
        note_id: str,
        operation_id: str,
        *,
        active: bool,
    ) -> ManagedWorkspaceReceipt:
        _portable_id(workspace_id, "workspace")
        _portable_id(note_id, "page")
        _delivery_id(operation_id)
        kind = "restore" if active else "deactivate"
        status = "restored" if active else "deactivated"
        request_sha256 = _request_sha256(
            {
                "kind": kind,
                "note_id": note_id,
                "operation_id": operation_id,
                "workspace_id": workspace_id,
            }
        )
        existing = self._operation(operation_id)
        if existing is not None:
            self._require_matching_operation(existing, request_sha256, kind)
            return ManagedWorkspaceReceipt(status, workspace_id, note_id, duplicate=True)
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                note = self._note_row(connection, workspace_id, note_id)
                duplicate = int(note["active"]) == int(active)
                now = _timestamp(self._engine._clock())
                connection.execute(
                    "UPDATE managed_notes SET active = ?, updated_at = ? WHERE note_id = ?",
                    (int(active), now, note_id),
                )
                connection.execute(
                    """UPDATE managed_workspaces
                    SET policy_generation = policy_generation + 1 WHERE workspace_id = ?""",
                    (workspace_id,),
                )
                if not active:
                    connection.execute(
                        """UPDATE managed_consents SET active = 0, revoked_at = ?
                        WHERE workspace_id = ? AND active = 1""",
                        (now, workspace_id),
                    )
                self._insert_operation(
                    connection,
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    workspace_id=workspace_id,
                    note_id=note_id,
                    kind=kind,
                    target_relative_path=None,
                    expected_revision_id=cast(str, note["accepted_revision_id"]),
                    expected_target_sha256=None,
                    body=None,
                    now=now,
                    status="completed",
                )
                connection.execute(
                    "DELETE FROM managed_note_observations WHERE workspace_id = ?",
                    (workspace_id,),
                )
        return ManagedWorkspaceReceipt(status, workspace_id, note_id, duplicate=duplicate)

    def _process_materialization(self, operation_id: str) -> None:
        operation = self._operation(operation_id)
        if operation is None:
            raise ManagedWorkspaceFailure("operation_replay_mismatch")
        if operation["status"] == "completed":
            return
        if operation["status"] == "conflict":
            raise ManagedWorkspaceFailure("target_changed")
        if operation["kind"] not in {"setup", "materialize"}:
            raise ManagedWorkspaceFailure("operation_replay_mismatch")
        workspace = self._workspace(cast(str, operation["workspace_id"]))
        target = cast(str, operation["target_relative_path"])
        body = cast(bytes, operation["body_bytes"])
        expected = cast(str | None, operation["expected_target_sha256"])
        body_sha256 = cast(str, operation["body_sha256"])
        current = read_confined(
            root=workspace.root,
            relative=target,
            expected_root_identity=workspace.root_identity,
            maximum_bytes=MAX_FILE_BYTES,
        )
        current_sha256 = None if current is None else _digest(current)
        if current_sha256 != body_sha256:
            if current_sha256 != expected:
                self._record_conflict(operation, current or b"")
                raise ManagedWorkspaceFailure("target_changed")
            with self._engine._store.transaction() as connection:
                connection.execute(
                    "UPDATE managed_operations SET status = 'writing', stage = 1 "
                    "WHERE operation_id = ? AND status IN ('prepared', 'writing')",
                    (operation_id,),
                )
            try:
                if expected is None:
                    atomic_write_new(
                        root=workspace.root,
                        relative=target,
                        data=body,
                        expected_root_identity=workspace.root_identity,
                    )
                else:
                    atomic_replace(
                        root=workspace.root,
                        relative=target,
                        data=body,
                        require_existing=True,
                        expected_existing_sha256=expected,
                        expected_root_identity=workspace.root_identity,
                    )
            except DuplicateConflictError:
                changed = read_confined(
                    root=workspace.root,
                    relative=target,
                    expected_root_identity=workspace.root_identity,
                    maximum_bytes=MAX_FILE_BYTES,
                )
                self._record_conflict(operation, changed or b"")
                raise ManagedWorkspaceFailure("target_changed") from None
            self._engine._fault(ManagedWorkspaceFault.AFTER_TARGET_WRITE)
        with self._engine._store.transaction() as connection:
            connection.execute(
                "UPDATE managed_operations SET status = 'promoted', stage = 2 "
                "WHERE operation_id = ? AND status != 'completed'",
                (operation_id,),
            )
        self._engine._fault(ManagedWorkspaceFault.AFTER_OPERATION_PROMOTED)
        final = read_confined(
            root=workspace.root,
            relative=target,
            expected_root_identity=workspace.root_identity,
            maximum_bytes=MAX_FILE_BYTES,
        )
        if final is None or _digest(final) != body_sha256:
            self._record_conflict(operation, final or b"")
            raise ManagedWorkspaceFailure("target_changed")
        now = _timestamp(self._engine._clock())
        with self._engine._store.transaction() as connection:
            current_operation = connection.execute(
                "SELECT * FROM managed_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if current_operation is None or current_operation["request_sha256"] != operation[
                "request_sha256"
            ]:
                raise ManagedWorkspaceFailure("operation_replay_mismatch")
            note = self._note_row(
                connection,
                cast(str, operation["workspace_id"]),
                cast(str, operation["note_id"]),
            )
            if note["accepted_revision_id"] != operation["expected_revision_id"]:
                raise ManagedWorkspaceFailure("operation_replay_mismatch")
            connection.execute(
                """UPDATE managed_notes
                SET materialized_revision_id = ?, materialized_sha256 = ?, updated_at = ?
                WHERE note_id = ?""",
                (
                    operation["expected_revision_id"],
                    body_sha256,
                    now,
                    operation["note_id"],
                ),
            )
            connection.execute(
                """UPDATE managed_operations
                SET status = 'completed', stage = 3, completed_at = ?
                WHERE operation_id = ?""",
                (now, operation_id),
            )

    def _record_conflict(self, operation: sqlite3.Row, candidate: bytes) -> None:
        now = _timestamp(self._engine._clock())
        with self._engine._store.transaction() as connection:
            existing = connection.execute(
                "SELECT conflict_id FROM managed_conflicts WHERE note_id = ? AND status = 'open'",
                (operation["note_id"],),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO managed_conflicts
                    (conflict_id, note_id, base_revision_id, accepted_revision_id,
                     candidate_body_bytes, candidate_sha256, status, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'open', ?)""",
                    (
                        _new_id("conflict"),
                        operation["note_id"],
                        operation["expected_revision_id"],
                        operation["expected_revision_id"],
                        candidate,
                        _digest(candidate),
                        now,
                    ),
                )
            connection.execute(
                """UPDATE managed_operations
                SET status = 'conflict', completed_at = ?, stage = 3 WHERE operation_id = ?""",
                (now, operation["operation_id"]),
            )

    def _canonical_pages(self) -> tuple[_CanonicalPage, ...]:
        connection = self._engine._store.connect()
        try:
            rows = tuple(
                connection.execute(
                    """SELECT d.result_id AS note_id, d.canonical_path,
                              c.privacy_json, c.provenance_json
                    FROM search_documents AS d JOIN captures AS c USING (capture_id)
                    WHERE d.record_type = 'canonical' AND d.canonical_path IS NOT NULL
                    ORDER BY d.result_id"""
                )
            )
        finally:
            connection.close()
        pages: list[_CanonicalPage] = []
        targets: set[str] = set()
        for row in rows:
            note_id = cast(str, row["note_id"])
            _portable_id(note_id, "page")
            canonical_path = cast(str, row["canonical_path"])
            if not canonical_path.startswith("content/spaces/"):
                raise ManagedWorkspaceFailure("unsafe_workspace")
            target = canonical_path.removeprefix("content/")
            if target in targets:
                raise ManagedWorkspaceFailure("unsafe_workspace")
            payload = read_confined(
                root=self._engine.profile.root,
                relative=canonical_path,
                expected_root_identity=self._engine.profile.root_identity,
                maximum_bytes=MAX_FILE_BYTES,
            )
            try:
                parsed = None if payload is None else parse_markdown(payload)
            except MarkdownFormatError:
                parsed = None
            if parsed is None or parsed.fields.get("page_id") != note_id:
                raise ManagedWorkspaceFailure("unsafe_workspace")
            privacy_json = row["privacy_json"]
            provenance_json = row["provenance_json"]
            if not isinstance(privacy_json, str) or not isinstance(provenance_json, str):
                raise ManagedWorkspaceFailure("unsafe_workspace")
            json.loads(privacy_json)
            json.loads(provenance_json)
            pages.append(
                _CanonicalPage(
                    note_id,
                    target,
                    cast(bytes, payload),
                    privacy_json,
                    provenance_json,
                )
            )
            targets.add(target)
        return tuple(pages)

    def _require_safe_workspace(self, root: Path, identity: RootIdentity) -> None:
        engine_root = snapshot_directory(self._engine.profile.root)
        workspace_root = snapshot_directory(root)
        if workspace_root.identity != identity or roots_overlap(engine_root, workspace_root):
            raise ManagedWorkspaceFailure("unsafe_workspace")
        metadata = root.stat(follow_symlinks=False)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise ManagedWorkspaceFailure("unsafe_workspace")

    def _workspace_by_identity(self, identity: RootIdentity) -> _Workspace | None:
        connection = self._engine._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM managed_workspaces WHERE device = ? AND inode = ?",
                (str(identity[0]), str(identity[1])),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._workspace_value(row)

    def _workspace(self, workspace_id: str) -> _Workspace:
        connection = self._engine._store.connect()
        try:
            row = self._workspace_row(connection, workspace_id)
        finally:
            connection.close()
        value = self._workspace_value(row)
        try:
            assert_root_identity(value.root, value.root_identity)
        except (RootConfinementError, ValueError, OSError):
            raise ManagedWorkspaceFailure("unsafe_workspace") from None
        return value

    def _workspace_value(self, row: sqlite3.Row) -> _Workspace:
        root = Path(cast(str, row["root_path"]))
        if not root.is_absolute() or row["owner_actor_id"] != self._engine.profile.owner_actor_id:
            raise ManagedWorkspaceFailure("unsafe_workspace")
        return _Workspace(
            workspace_id=cast(str, row["workspace_id"]),
            root=root,
            root_identity=(int(row["device"]), int(row["inode"])),
            owner_actor_id=cast(str, row["owner_actor_id"]),
            generation=int(row["observation_generation"]),
        )

    def _workspace_row(self, connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM managed_workspaces WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        if row is None:
            raise ManagedWorkspaceFailure("unknown_workspace")
        return cast(sqlite3.Row, row)

    def _assert_workspace_row(self, row: sqlite3.Row, expected: _Workspace) -> None:
        if self._workspace_value(row) != expected:
            raise ManagedWorkspaceFailure("unsafe_workspace")

    def _known_notes(self, workspace_id: str) -> dict[str, sqlite3.Row]:
        connection = self._engine._store.connect()
        try:
            return {
                cast(str, row["note_id"]): row
                for row in connection.execute(
                    "SELECT * FROM managed_notes WHERE workspace_id = ? ORDER BY note_id",
                    (workspace_id,),
                )
            }
        finally:
            connection.close()

    def _note_row(
        self, connection: sqlite3.Connection, workspace_id: str, note_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM managed_notes WHERE workspace_id = ? AND note_id = ?",
            (workspace_id, note_id),
        ).fetchone()
        if row is None:
            raise ManagedWorkspaceFailure("unknown_note")
        return cast(sqlite3.Row, row)

    def _require_active_note(self, connection: sqlite3.Connection, note: sqlite3.Row) -> None:
        if int(note["active"]) != 1:
            raise ManagedWorkspaceFailure("inactive_note")
        if (
            connection.execute(
                "SELECT 1 FROM managed_conflicts WHERE note_id = ? AND status = 'open'",
                (note["note_id"],),
            ).fetchone()
            is not None
        ):
            raise ManagedWorkspaceFailure("conflict_open")

    def _operation(self, operation_id: str) -> sqlite3.Row | None:
        connection = self._engine._store.connect()
        try:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM managed_operations WHERE operation_id = ?", (operation_id,)
                ).fetchone(),
            )
        finally:
            connection.close()

    def _require_matching_operation(
        self, operation: sqlite3.Row, request_sha256: str, kind: str
    ) -> None:
        if operation["request_sha256"] != request_sha256 or operation["kind"] != kind:
            raise ManagedWorkspaceFailure("operation_replay_mismatch")

    def _insert_operation(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        request_sha256: str,
        workspace_id: str,
        note_id: str | None,
        kind: str,
        target_relative_path: str | None,
        expected_revision_id: str | None,
        expected_target_sha256: str | None,
        body: bytes | None,
        now: str,
        status: str = "prepared",
    ) -> None:
        connection.execute(
            """INSERT INTO managed_operations
            (operation_id, request_sha256, workspace_id, note_id, kind, caller_actor_id,
             target_relative_path, expected_revision_id, expected_target_sha256,
             body_bytes, body_sha256, status, stage, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                operation_id,
                request_sha256,
                workspace_id,
                note_id,
                kind,
                self._engine.profile.owner_actor_id,
                target_relative_path,
                expected_revision_id,
                expected_target_sha256,
                body,
                None if body is None else _digest(body),
                status,
                3 if status == "completed" else 0,
                now,
                now if status == "completed" else None,
            ),
        )
