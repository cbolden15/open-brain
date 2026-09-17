"""Durable owner-controlled projection of accepted pages into one Markdown workspace."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyDecision, ValidationError
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    RootConfinementError,
    RootIdentity,
    _validated_parts,
    assert_root_identity,
    atomic_replace,
    atomic_write_new,
    read_confined,
)
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown

from .contracts import (
    ManagedGraphSnapshot,
    ManagedGraphSource,
    ManagedNoteObservation,
    ManagedWorkspaceConflictReview,
    ManagedWorkspaceConflictSummary,
    ManagedWorkspaceFailure,
    ManagedWorkspaceFault,
    ManagedWorkspaceObservation,
    ManagedWorkspaceReceipt,
    ManagedWorkspaceStatus,
)
from .managed_selection import managed_note_is_excluded
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


class _CancelledRefresh(ManagedWorkspaceFailure):
    """A durably settled refresh authorization, safe for recovery to pass over."""

    def __init__(self) -> None:
        super().__init__("stale_request")


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

    def graph_snapshot(self, workspace_id: str) -> ManagedGraphSnapshot:
        """Return one bounded accepted-revision snapshot for local structural extraction."""
        _portable_id(workspace_id, "workspace")
        workspace = self._workspace(workspace_id)
        connection = self._engine._store.connect()
        try:
            current = self._workspace_row(connection, workspace_id)
            self._assert_workspace_row(current, workspace)
            rows = tuple(
                connection.execute(
                    """SELECT n.*, r.body_bytes, r.body_sha256, r.privacy_json
                    FROM managed_notes AS n
                    JOIN managed_note_revisions AS r
                      ON r.note_id = n.note_id AND r.revision_id = n.accepted_revision_id
                    WHERE n.workspace_id = ? AND n.active = 1
                    ORDER BY n.note_id""",
                    (workspace_id,),
                )
            )
            sources: list[ManagedGraphSource] = []
            paths: set[str] = set()
            for row in rows:
                note_id = cast(str, row["note_id"])
                relative_path = cast(str, row["relative_path"])
                if managed_note_is_excluded(
                    connection,
                    workspace_id,
                    note_id=note_id,
                    relative_path=relative_path,
                ) or any(
                    part.casefold()
                    in {".graphify", ".obsidian", ".open-brain", "graphify-out"}
                    for part in PurePosixPath(relative_path).parts
                ):
                    continue
                normalized_path = relative_path.casefold()
                if normalized_path in paths:
                    raise ManagedWorkspaceFailure("ineligible_source")
                paths.add(normalized_path)
                privacy_json = cast(str, row["privacy_json"])
                try:
                    privacy_value = json.loads(privacy_json)
                    if not isinstance(privacy_value, dict):
                        raise ValueError
                    PrivacyDecision.from_dict(privacy_value)
                    body = cast(bytes, row["body_bytes"]).decode("utf-8")
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                    ValidationError,
                ):
                    raise ManagedWorkspaceFailure("ineligible_source") from None
                sources.append(
                    ManagedGraphSource(
                        note_id=note_id,
                        revision_id=cast(str, row["accepted_revision_id"]),
                        relative_path=relative_path,
                        body=body,
                        body_sha256=cast(str, row["body_sha256"]),
                        privacy_sha256=_digest(privacy_json.encode("utf-8")),
                    )
                )
            if len(sources) > 64 or sum(
                len(source.body.encode("utf-8")) for source in sources
            ) > 16 * 1024:
                raise ManagedWorkspaceFailure("ineligible_source")
            identity = {
                "observation_generation": int(current["observation_generation"]),
                "policy_generation": int(current["policy_generation"]),
                "sources": [
                    {
                        "body_sha256": source.body_sha256,
                        "note_id": source.note_id,
                        "privacy_sha256": source.privacy_sha256,
                        "relative_path": source.relative_path,
                        "revision_id": source.revision_id,
                    }
                    for source in sources
                ],
                "workspace_id": workspace_id,
            }
            return ManagedGraphSnapshot(
                workspace_id=workspace_id,
                observation_generation=int(current["observation_generation"]),
                policy_generation=int(current["policy_generation"]),
                snapshot_sha256=_digest(portable_canonical_json_bytes(identity)),
                sources=tuple(sources),
            )
        except ManagedWorkspaceFailure:
            raise
        except (KeyError, sqlite3.DatabaseError, ValueError):
            raise ManagedWorkspaceFailure("ineligible_source") from None
        finally:
            connection.close()

    def status(self) -> ManagedWorkspaceStatus | None:
        connection = self._engine._store.connect()
        try:
            rows = tuple(
                connection.execute("SELECT * FROM managed_workspaces ORDER BY workspace_id")
            )
            if not rows:
                return None
            if len(rows) != 1:
                raise ManagedWorkspaceFailure("unsafe_workspace")
            row = cast(sqlite3.Row, rows[0])
            workspace_id = cast(str, row["workspace_id"])
            counts = connection.execute(
                """SELECT
                    sum(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_notes,
                    sum(CASE WHEN active = 0 THEN 1 ELSE 0 END) AS inactive_notes
                FROM managed_notes WHERE workspace_id = ?""",
                (workspace_id,),
            ).fetchone()
            conflicts = connection.execute(
                """SELECT count(*) FROM managed_conflicts AS c
                JOIN managed_notes AS n ON n.note_id = c.note_id
                WHERE n.workspace_id = ? AND c.status = 'open'""",
                (workspace_id,),
            ).fetchone()
            suggestions = connection.execute(
                """SELECT count(*) FROM managed_suggestions
                WHERE workspace_id = ? AND status = 'pending'""",
                (workspace_id,),
            ).fetchone()
        finally:
            connection.close()
        connected = False
        if row["root_path"] is not None:
            try:
                self._workspace(workspace_id)
            except ManagedWorkspaceFailure:
                pass
            else:
                connected = True
        return ManagedWorkspaceStatus(
            workspace_id=workspace_id,
            connected=connected,
            observation_generation=int(row["observation_generation"]),
            policy_generation=int(row["policy_generation"]),
            active_notes=int(counts["active_notes"] or 0),
            inactive_notes=int(counts["inactive_notes"] or 0),
            open_conflicts=int(conflicts[0]),
            pending_suggestions=int(suggestions[0]),
        )

    def open_conflicts(
        self, workspace_id: str, *, limit: int = 64
    ) -> tuple[ManagedWorkspaceConflictSummary, ...]:
        _portable_id(workspace_id, "workspace")
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ManagedWorkspaceFailure("invalid_request")
        connection = self._engine._store.connect()
        try:
            self._workspace_row(connection, workspace_id)
            rows = tuple(
                connection.execute(
                    """SELECT c.conflict_id, c.note_id, n.relative_path
                    FROM managed_conflicts AS c
                    JOIN managed_notes AS n ON n.note_id = c.note_id
                    WHERE n.workspace_id = ? AND c.status = 'open'
                    ORDER BY c.detected_at, c.conflict_id LIMIT ?""",
                    (workspace_id, limit),
                )
            )
            return tuple(
                ManagedWorkspaceConflictSummary(
                    conflict_id=cast(str, row["conflict_id"]),
                    note_id=cast(str, row["note_id"]),
                    relative_path=cast(str, row["relative_path"]),
                )
                for row in rows
            )
        except ManagedWorkspaceFailure:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError):
            raise ManagedWorkspaceFailure("operation_conflict") from None
        finally:
            connection.close()

    def review_conflict(
        self, workspace_id: str, note_id: str
    ) -> ManagedWorkspaceConflictReview:
        _portable_id(workspace_id, "workspace")
        _portable_id(note_id, "page")
        connection = self._engine._store.connect()
        try:
            row = connection.execute(
                """SELECT c.conflict_id, c.note_id, c.accepted_revision_id,
                          c.candidate_body_bytes, n.relative_path, r.body_bytes
                FROM managed_conflicts AS c
                JOIN managed_notes AS n ON n.note_id = c.note_id
                JOIN managed_note_revisions AS r
                  ON r.note_id = c.note_id AND r.revision_id = c.accepted_revision_id
                WHERE n.workspace_id = ? AND c.note_id = ? AND c.status = 'open'""",
                (workspace_id, note_id),
            ).fetchone()
            if row is None:
                raise ManagedWorkspaceFailure("operation_conflict")
            return ManagedWorkspaceConflictReview(
                conflict_id=cast(str, row["conflict_id"]),
                note_id=cast(str, row["note_id"]),
                relative_path=cast(str, row["relative_path"]),
                accepted_revision_id=cast(str, row["accepted_revision_id"]),
                accepted_body=cast(bytes, row["body_bytes"]).decode("utf-8"),
                workspace_body=cast(bytes, row["candidate_body_bytes"]).decode("utf-8"),
            )
        except ManagedWorkspaceFailure:
            raise
        except (sqlite3.DatabaseError, UnicodeDecodeError, TypeError, ValueError):
            raise ManagedWorkspaceFailure("operation_conflict") from None
        finally:
            connection.close()

    def note_id_for_path(self, workspace_id: str, relative_path: str) -> str:
        _portable_id(workspace_id, "workspace")
        if not isinstance(relative_path, str) or "\\" in relative_path:
            raise ManagedWorkspaceFailure("unknown_note")
        path = PurePosixPath(relative_path)
        if (
            not relative_path
            or path.is_absolute()
            or path.as_posix() != relative_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix.casefold() != ".md"
        ):
            raise ManagedWorkspaceFailure("unknown_note")
        connection = self._engine._store.connect()
        try:
            row = connection.execute(
                """SELECT note_id FROM managed_notes
                WHERE workspace_id = ? AND relative_path = ?""",
                (workspace_id, relative_path),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ManagedWorkspaceFailure("unknown_note")
        return cast(str, row["note_id"])

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
                detached = self._detached_workspace()
                if detached is not None:
                    return self._attach_detached(
                        detached,
                        root=pinned.snapshot.canonical_path,
                        identity=pinned.snapshot.identity,
                        caller_operation_id=operation_id,
                    )
                pages = self._canonical_pages()
                workspace_id = _new_id("workspace")
                now = _timestamp(self._engine._clock())
                operation_ids: list[str] = []
                with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
                    with self._engine._store.transaction() as connection:
                        connection.execute(
                            """INSERT INTO managed_workspaces
                            (workspace_id, root_path, device, inode, owner_actor_id,
                             origin_owner_actor_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (
                                workspace_id,
                                str(pinned.snapshot.canonical_path),
                                str(pinned.snapshot.identity[0]),
                                str(pinned.snapshot.identity[1]),
                                self._engine.profile.owner_actor_id,
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
                        locked_known = {
                            cast(str, row["note_id"]): row
                            for row in connection.execute(
                                "SELECT * FROM managed_notes WHERE workspace_id = ? "
                                "ORDER BY note_id",
                                (workspace_id,),
                            )
                        }
                        if {key: tuple(row) for key, row in known.items()} != {
                            key: tuple(row) for key, row in locked_known.items()
                        }:
                            raise ManagedWorkspaceFailure("invalid_observation")
                        known = locked_known
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
                                        present=True,
                                        changed=observed_sha256
                                        != cast(str | None, note["materialized_sha256"]),
                                    )
                                )
                        for note_id in sorted(set(known) - set(observed)):
                            note = known[note_id]
                            if int(note["active"]) == 1:
                                result.append(
                                    ManagedNoteObservation(
                                        note_id=note_id,
                                        relative_path=cast(str, note["relative_path"]),
                                        accepted_revision_id=cast(
                                            str, note["accepted_revision_id"]
                                        ),
                                        materialized_sha256=cast(
                                            str | None, note["materialized_sha256"]
                                        ),
                                        observed_sha256=None,
                                        present=False,
                                        changed=True,
                                    )
                                )
                        connection.execute(
                            "UPDATE managed_workspaces SET observation_generation = ? "
                            "WHERE workspace_id = ?",
                            (generation, workspace_id),
                        )
                        result.sort(key=lambda note: note.note_id)
                        # Keep the old path and its settlement in the same transaction.
                        # This helper cannot write files or recover unaffected siblings.
                        for note_id in moved:
                            for operation in tuple(connection.execute(
                                "SELECT * FROM managed_operations WHERE note_id = ? "
                                "AND kind IN ('setup', 'materialize') "
                                "AND status IN ('prepared', 'writing', 'promoted')",
                                (note_id,),
                            )):
                                self._settle_write_authority(
                                    connection,
                                    self._workspace_value(
                                        self._workspace_row(connection, workspace_id)
                                    ),
                                    operation,
                                    observed_from=cast(str, known[note_id]["relative_path"]),
                                )
                return ManagedWorkspaceObservation(workspace_id, generation, tuple(result))
        except ManagedWorkspaceFailure:
            raise
        except (ImportDirectoryUnavailable, ImportScanIncomplete, OSError, ValueError):
            raise ManagedWorkspaceFailure("invalid_observation") from None

    def refresh(self, workspace_id: str, *, operation_id: str) -> ManagedWorkspaceReceipt:
        _portable_id(workspace_id, "workspace")
        _delivery_id(operation_id)
        workspace = self._workspace(workspace_id)
        request_sha256 = _request_sha256(
            {"kind": "refresh", "operation_id": operation_id, "workspace_id": workspace_id}
        )
        duplicate = False
        with self._engine._writer_lease.acquire_shared_writer():
            with self._engine._store.transaction() as connection:
                current = self._workspace_row(connection, workspace_id)
                self._assert_workspace_row(current, workspace)
                existing = connection.execute(
                    "SELECT * FROM managed_operations WHERE operation_id = ?", (operation_id,)
                ).fetchone()
                if existing is not None:
                    self._require_matching_operation(existing, request_sha256, "refresh")
                    duplicate = True
                else:
                    pages = self._canonical_pages(connection, require_publication=True)
                    now = _timestamp(self._engine._clock())
                    children = []
                    for page in pages:
                        child = self._prepare_refresh_page(
                            connection, workspace, page, operation_id, now
                        )
                        if child is not None:
                            children.append(child)
                    if self._canonical_pages(connection, require_publication=True) != pages:
                        raise ManagedWorkspaceFailure("operation_conflict")
                    # The manifest and every child/revision commit together. A retry never
                    # reselects canonical inputs, even after another publication appears.
                    self._insert_operation(
                        connection,
                        operation_id=operation_id,
                        request_sha256=request_sha256,
                        workspace_id=workspace_id,
                        note_id=None,
                        kind="refresh",
                        target_relative_path=None,
                        expected_revision_id=None,
                        expected_target_sha256=None,
                        body=portable_canonical_json_bytes({"children": children}),
                        now=now,
                    )
            if not duplicate:
                self._engine._fault(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
            self._process_refresh(operation_id)
        return ManagedWorkspaceReceipt(
            "refreshed", workspace_id, generation=workspace.generation, duplicate=duplicate
        )

    def _canonical_ancestor(self, connection: sqlite3.Connection, note: sqlite3.Row) -> sqlite3.Row:
        revision_id = note["accepted_revision_id"]
        visited: set[str] = set()
        while revision_id is not None and revision_id not in visited:
            visited.add(revision_id)
            revision = connection.execute(
                "SELECT * FROM managed_note_revisions WHERE note_id = ? AND revision_id = ?",
                (note["note_id"], revision_id),
            ).fetchone()
            if revision is None:
                break
            operation = connection.execute(
                "SELECT * FROM managed_operations WHERE operation_id = ?",
                (revision["operation_id"],),
            ).fetchone()
            if operation is None:
                break  # Portable imports intentionally do not import write authority.
            if (
                operation["workspace_id"] == note["workspace_id"]
                and operation["note_id"] == note["note_id"]
                and operation["expected_revision_id"] == revision_id
                and operation["body_bytes"] == revision["body_bytes"]
                and operation["body_sha256"] == revision["body_sha256"]
            ):
                if revision["kind"] == "setup" and operation["kind"] == "setup":
                    return cast(sqlite3.Row, revision)
                if (
                    revision["kind"] == "edit"
                    and operation["kind"] == "materialize"
                    and self._is_refresh_child(connection, operation)
                ):
                    return cast(sqlite3.Row, revision)
            revision_id = revision["parent_revision_id"]
        raise ManagedWorkspaceFailure("ineligible_source")

    def _prepare_refresh_page(
        self,
        connection: sqlite3.Connection,
        workspace: _Workspace,
        page: _CanonicalPage,
        parent_id: str,
        now: str,
    ) -> str | None:
        note = connection.execute(
            "SELECT * FROM managed_notes WHERE workspace_id = ? AND note_id = ?",
            (workspace.workspace_id, page.note_id),
        ).fetchone()
        previous_id = None
        target = page.relative_path
        expected = None
        divergent = False
        disk: bytes | None = None
        if note is not None:
            target = cast(str, note["relative_path"])
            if not note["active"] or managed_note_is_excluded(
                connection, workspace.workspace_id, note_id=page.note_id, relative_path=target
            ):
                return None
            if (
                connection.execute(
                    "SELECT 1 FROM managed_conflicts WHERE note_id = ? AND status = 'open'",
                    (page.note_id,),
                ).fetchone()
                is not None
            ):
                return None
            if (
                connection.execute(
                    "SELECT 1 FROM managed_operations WHERE note_id = ? "
                    "AND status IN ('prepared', 'writing', 'promoted')",
                    (page.note_id,),
                ).fetchone()
                is not None
            ):
                raise ManagedWorkspaceFailure("operation_conflict")
            ancestor = self._canonical_ancestor(connection, note)
            if (
                ancestor["body_bytes"] == page.payload
                and ancestor["privacy_json"] == page.privacy_json
                and ancestor["provenance_json"] == page.provenance_json
            ):
                return None
            disk = read_confined(
                root=workspace.root,
                relative=target,
                expected_root_identity=workspace.root_identity,
                maximum_bytes=MAX_FILE_BYTES,
            )
            if disk is None:
                return None  # Absence never grants recreation authority.
            previous_id = note["accepted_revision_id"]
            expected = note["write_base_sha256"]
            divergent = (
                previous_id != ancestor["revision_id"]
                or note["materialized_revision_id"] != previous_id
                or _digest(disk) != expected
                or disk != ancestor["body_bytes"]
            )
        revision_id = _new_id("revision")
        child_id = _new_id("operation")
        if note is None:
            connection.execute(
                """INSERT INTO managed_notes
                (note_id, workspace_id, relative_path, accepted_revision_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (page.note_id, workspace.workspace_id, target, revision_id, now, now),
            )
        elif not divergent:
            connection.execute(
                "UPDATE managed_notes SET accepted_revision_id = ?, updated_at = ? "
                "WHERE note_id = ?",
                (revision_id, now, page.note_id),
            )
        connection.execute(
            """INSERT INTO managed_note_revisions
            (revision_id, note_id, parent_revision_id, kind, body_bytes, body_sha256,
             accepted_by_actor_id, provenance_json, privacy_json, recorded_at, operation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                revision_id,
                page.note_id,
                previous_id,
                "setup" if note is None else "edit",
                page.payload,
                _digest(page.payload),
                self._engine.profile.owner_actor_id,
                page.provenance_json,
                page.privacy_json,
                now,
                child_id,
            ),
        )
        kind = "setup" if note is None else "materialize"
        self._insert_operation(
            connection,
            operation_id=child_id,
            request_sha256=_request_sha256(
                {
                    "caller_operation_id": parent_id,
                    "kind": kind,
                    "note_id": page.note_id,
                    "revision_id": revision_id,
                    "target": target,
                    "workspace_id": workspace.workspace_id,
                }
            ),
            workspace_id=workspace.workspace_id,
            note_id=page.note_id,
            kind=kind,
            target_relative_path=target,
            expected_revision_id=revision_id,
            expected_target_sha256=expected,
            body=page.payload,
            now=now,
        )
        if divergent:
            # Keep the accepted owner head. The approved upstream revision is a
            # retained branch until explicit conflict resolution selects either side.
            candidate = disk
            if _digest(cast(bytes, disk)) == expected:
                accepted = connection.execute(
                    "SELECT body_bytes FROM managed_note_revisions WHERE revision_id = ?",
                    (previous_id,),
                ).fetchone()
                candidate = accepted["body_bytes"]
            connection.execute(
                """INSERT INTO managed_conflicts
                (conflict_id, note_id, base_revision_id, accepted_revision_id,
                 candidate_body_bytes, candidate_sha256, status, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?)""",
                (
                    _new_id("conflict"),
                    page.note_id,
                    ancestor["revision_id"],
                    revision_id,
                    candidate,
                    _digest(cast(bytes, candidate)),
                    now,
                ),
            )
            connection.execute(
                "UPDATE managed_operations SET status = 'conflict', stage = 3, completed_at = ? "
                "WHERE operation_id = ?",
                (now, child_id),
            )
        connection.execute(
            "DELETE FROM managed_note_observations WHERE workspace_id = ?",
            (workspace.workspace_id,),
        )
        return child_id

    @staticmethod
    def _refresh_children(operation: sqlite3.Row) -> tuple[str, ...]:
        body = operation["body_bytes"]
        if body is None and operation["status"] == "completed":
            return ()  # Legacy refresh had no bound children; never select new work.
        try:
            if not isinstance(body, bytes) or _digest(body) != operation["body_sha256"]:
                raise ValueError
            value = json.loads(body)
            children = value["children"]
            if (
                not isinstance(children, list)
                or any(not isinstance(child, str) for child in children)
                or len(set(children)) != len(children)
            ):
                raise ValueError
            return tuple(children)
        except ValueError, TypeError, KeyError:
            raise ManagedWorkspaceFailure("operation_replay_mismatch") from None

    def _require_refresh_child(self, parent: sqlite3.Row, child: sqlite3.Row) -> None:
        expected = _request_sha256(
            {
                "caller_operation_id": parent["operation_id"],
                "kind": child["kind"],
                "note_id": child["note_id"],
                "revision_id": child["expected_revision_id"],
                "target": child["target_relative_path"],
                "workspace_id": parent["workspace_id"],
            }
        )
        if (
            child["workspace_id"] != parent["workspace_id"]
            or parent["caller_actor_id"] != self._engine.profile.owner_actor_id
            or child["caller_actor_id"] != self._engine.profile.owner_actor_id
            or parent["request_sha256"] != _request_sha256({
                "kind": "refresh", "operation_id": parent["operation_id"],
                "workspace_id": parent["workspace_id"],
            })
            or child["kind"] not in {"setup", "materialize"}
            or child["request_sha256"] != expected
        ):
            raise ManagedWorkspaceFailure("operation_replay_mismatch")

    def _is_refresh_child(self, connection: sqlite3.Connection, child: sqlite3.Row) -> bool:
        for parent in connection.execute(
            "SELECT * FROM managed_operations WHERE workspace_id = ? "
            "AND kind = 'refresh' AND body_bytes IS NOT NULL",
            (child["workspace_id"],),
        ):
            if child["operation_id"] in self._refresh_children(parent):
                self._require_refresh_child(parent, child)
                return True
        return False

    def _process_refresh(self, operation_id: str) -> None:
        parent = self._operation(operation_id)
        if parent is None or parent["kind"] != "refresh":
            raise ManagedWorkspaceFailure("operation_replay_mismatch")
        conflict = False
        cancelled = False
        for child_id in self._refresh_children(parent):
            child = self._operation(child_id)
            if child is None:
                raise ManagedWorkspaceFailure("operation_replay_mismatch")
            self._require_refresh_child(parent, child)
            if parent["status"] in {"completed", "conflict", "cancelled"} and child[
                "status"
            ] not in {"completed", "conflict", "cancelled"}:
                raise ManagedWorkspaceFailure("operation_replay_mismatch")
            if child["status"] == "conflict":
                conflict = True
                continue
            try:
                self._process_materialization(child_id)
            except _CancelledRefresh:
                cancelled = True
            except ManagedWorkspaceFailure as error:
                if error.code != "target_changed":
                    raise
                conflict = True
        # Cancellation takes precedence over conflict: at least one authorized
        # write was abandoned. Individual conflict rows remain available to review.
        status = "cancelled" if cancelled else "conflict" if conflict else "completed"
        if parent["status"] not in {"prepared", "writing", "promoted", status}:
            raise ManagedWorkspaceFailure("operation_replay_mismatch")
        with self._engine._store.transaction() as connection:
            connection.execute(
                "UPDATE managed_operations SET status = ?, stage = 3, completed_at = ? "
                "WHERE operation_id = ? AND status IN ('prepared', 'writing', 'promoted')",
                (
                    status,
                    _timestamp(self._engine._clock()),
                    operation_id,
                ),
            )
        if cancelled:
            raise _CancelledRefresh
        if conflict:
            raise ManagedWorkspaceFailure("target_changed")

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
                        materialized_sha256 = ?, write_base_sha256 = ?,
                        relative_path = ?, updated_at = ?
                    WHERE note_id = ?""",
                    (
                        revision_id,
                        revision_id,
                        observation["observed_sha256"],
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
                    expected_target_sha256=cast(str | None, note["write_base_sha256"]),
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

    def resolve_conflict(
        self,
        workspace_id: str,
        note_id: str,
        choice: str,
        *,
        conflict_id: str | None = None,
        operation_id: str,
    ) -> ManagedWorkspaceReceipt:
        from .managed_policy import _advance_policy

        _portable_id(workspace_id, "workspace")
        _portable_id(note_id, "page")
        if conflict_id is not None:
            _portable_id(conflict_id, "conflict")
        _delivery_id(operation_id)
        if choice not in {"accepted", "workspace"}:
            raise ManagedWorkspaceFailure("operation_conflict")
        request_sha256 = _request_sha256(
            {
                "choice": choice,
                "conflict_id": conflict_id,
                "kind": "resolve",
                "note_id": note_id,
                "operation_id": operation_id,
                "workspace_id": workspace_id,
            }
        )
        existing = self._operation(operation_id)
        if existing is not None:
            self._require_matching_operation(existing, request_sha256, "resolve")
            return ManagedWorkspaceReceipt(
                "conflict_resolved", workspace_id, note_id, duplicate=True
            )
        workspace = self._workspace(workspace_id)
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                note = self._note_row(connection, workspace_id, note_id)
                conflict = connection.execute(
                    """SELECT * FROM managed_conflicts
                    WHERE note_id = ? AND status = 'open'""",
                    (note_id,),
                ).fetchone()
                if conflict is None:
                    raise ManagedWorkspaceFailure("operation_conflict")
                if conflict_id is not None and conflict["conflict_id"] != conflict_id:
                    raise ManagedWorkspaceFailure("operation_conflict")
                upstream = connection.execute(
                    "SELECT * FROM managed_note_revisions WHERE revision_id = ? AND note_id = ?",
                    (conflict["accepted_revision_id"], note_id),
                ).fetchone()
                if upstream is None:
                    raise ManagedWorkspaceFailure("operation_conflict")
                branch_operation = connection.execute(
                    "SELECT * FROM managed_operations WHERE operation_id = ?",
                    (upstream["operation_id"],),
                ).fetchone()
                refresh_branch = (
                    branch_operation is not None
                    and branch_operation["status"] == "conflict"
                    and branch_operation["expected_revision_id"] == upstream["revision_id"]
                    and upstream["parent_revision_id"] == note["accepted_revision_id"]
                    and self._is_refresh_child(connection, branch_operation)
                )
                if (
                    note["accepted_revision_id"] != conflict["accepted_revision_id"]
                    and not refresh_branch
                ):
                    raise ManagedWorkspaceFailure("operation_conflict")
                candidate = cast(bytes, conflict["candidate_body_bytes"])
                current = read_confined(
                    root=workspace.root,
                    relative=cast(str, note["relative_path"]),
                    expected_root_identity=workspace.root_identity,
                    maximum_bytes=MAX_FILE_BYTES,
                )
                # A refresh may preserve an accepted, not-yet-materialized link as
                # the workspace choice. Its original disk preimage remains guarded.
                candidate_on_disk = current == candidate
                if (
                    _digest(candidate) != conflict["candidate_sha256"]
                    or current is None
                    or (
                        not candidate_on_disk
                        and not (
                            refresh_branch
                            and _digest(current) == branch_operation["expected_target_sha256"]
                        )
                    )
                ):
                    raise ManagedWorkspaceFailure("target_changed")
                now = _timestamp(self._engine._clock())
                resolution_revision_id = cast(str, conflict["accepted_revision_id"])
                if choice == "workspace":
                    try:
                        parsed = parse_markdown(candidate)
                    except MarkdownFormatError:
                        raise ManagedWorkspaceFailure("operation_conflict") from None
                    if parsed.fields.get("page_id") != note_id:
                        raise ManagedWorkspaceFailure("operation_conflict")
                    previous = connection.execute(
                        "SELECT * FROM managed_note_revisions WHERE revision_id = ?",
                        (conflict["accepted_revision_id"],),
                    ).fetchone()
                    if previous is None:
                        raise ManagedWorkspaceFailure("operation_replay_mismatch")
                    resolution_revision_id = _new_id("revision")
                    connection.execute(
                        """INSERT INTO managed_note_revisions
                        (revision_id, note_id, parent_revision_id, kind, body_bytes,
                         body_sha256, accepted_by_actor_id, provenance_json, privacy_json,
                         recorded_at, operation_id)
                        VALUES (?, ?, ?, 'merge', ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            resolution_revision_id,
                            note_id,
                            previous["revision_id"],
                            candidate,
                            conflict["candidate_sha256"],
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
                            materialized_sha256 = ?, write_base_sha256 = ?, updated_at = ?
                        WHERE note_id = ?""",
                        (
                            resolution_revision_id,
                            resolution_revision_id
                            if candidate_on_disk
                            else note["materialized_revision_id"],
                            conflict["candidate_sha256"]
                            if candidate_on_disk
                            else note["materialized_sha256"],
                            _digest(current),
                            now,
                            note_id,
                        ),
                    )
                else:
                    connection.execute(
                        """UPDATE managed_notes SET accepted_revision_id = ?,
                            write_base_sha256 = ?, updated_at = ? WHERE note_id = ?""",
                        (resolution_revision_id, _digest(current), now, note_id),
                    )
                connection.execute(
                    """UPDATE managed_conflicts
                    SET status = 'resolved', resolution_revision_id = ?, resolved_at = ?
                    WHERE conflict_id = ?""",
                    (resolution_revision_id, now, conflict["conflict_id"]),
                )
                _advance_policy(connection, workspace_id, now)
                self._insert_operation(
                    connection,
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    workspace_id=workspace_id,
                    note_id=note_id,
                    kind="resolve",
                    target_relative_path=None,
                    expected_revision_id=cast(str, note["accepted_revision_id"]),
                    expected_target_sha256=cast(str, conflict["candidate_sha256"]),
                    body=candidate,
                    now=now,
                    status="completed",
                )
                connection.execute(
                    "DELETE FROM managed_note_observations WHERE workspace_id = ?",
                    (workspace_id,),
                )
        return ManagedWorkspaceReceipt("conflict_resolved", workspace_id, note_id)

    def recover(self) -> int:
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            return self._recover_locked()

    def _recover_locked(self) -> int:
        from .managed_recovery import legacy_recovery_blocker

        recovered = 0
        blocked = False
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
            operation = self._operation(operation_id)
            try:
                if operation is not None and operation["kind"] == "refresh":
                    self._process_refresh(operation_id)
                else:
                    self._process_materialization(operation_id)
            except _CancelledRefresh:
                pass  # Both leaf-first and parent-first recovery see durable cancellation.
            except ManagedWorkspaceFailure as error:
                if error.code != "target_changed":
                    connection = self._engine._store.connect()
                    try:
                        eligible = (
                            error.code == "operation_replay_mismatch"
                            and legacy_recovery_blocker(
                                connection, self._engine.profile, operation_id
                            )
                        )
                    finally:
                        connection.close()
                    if not eligible:
                        raise
                    blocked = True
            recovered += 1
        if blocked:
            raise ManagedWorkspaceFailure("workspace_recovery_required")
        return recovered

    def _set_active(
        self,
        workspace_id: str,
        note_id: str,
        operation_id: str,
        *,
        active: bool,
    ) -> ManagedWorkspaceReceipt:
        from .managed_policy import _advance_policy

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
                if not duplicate:
                    connection.execute(
                        "UPDATE managed_notes SET active = ?, updated_at = ? WHERE note_id = ?",
                        (int(active), now, note_id),
                    )
                    _advance_policy(
                        connection,
                        workspace_id,
                        now,
                        revoke_all_consents=not active,
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

    @staticmethod
    def _accepted_write_successor(
        connection: sqlite3.Connection,
        note: sqlite3.Row,
        operation: sqlite3.Row,
        owner_actor_id: str,
    ) -> bool:
        """Prove owner edit/link lineage and its current materialized base."""
        revision_id = note["accepted_revision_id"]
        visited: set[str] = set()
        materialized = False
        link_bases: set[str | None] = set()
        while revision_id != operation["expected_revision_id"]:
            if revision_id is None or revision_id in visited:
                return False
            visited.add(revision_id)
            revision = connection.execute(
                "SELECT * FROM managed_note_revisions WHERE note_id = ? AND revision_id = ?",
                (note["note_id"], revision_id),
            ).fetchone()
            if revision is None:
                return False
            acceptance = connection.execute(
                "SELECT * FROM managed_operations WHERE operation_id = ?",
                (revision["operation_id"],),
            ).fetchone()
            if (
                acceptance is None
                or (revision["kind"], acceptance["kind"])
                not in {
                    ("edit", "accept_revision"),
                    ("link", "accept_link"),
                }
                or acceptance["status"] != "completed"
                or acceptance["workspace_id"] != operation["workspace_id"]
                or acceptance["note_id"] != note["note_id"]
                or acceptance["caller_actor_id"] != owner_actor_id
                or acceptance["expected_revision_id"] != revision["parent_revision_id"]
                or revision["accepted_by_actor_id"] != owner_actor_id
                or acceptance["body_bytes"] != revision["body_bytes"]
                or acceptance["body_sha256"] != revision["body_sha256"]
                or (
                    revision["kind"] == "edit"
                    and acceptance["expected_target_sha256"] != revision["body_sha256"]
                )
                or _digest(revision["body_bytes"]) != revision["body_sha256"]
            ):
                return False
            link_bases.discard(revision["body_sha256"])
            if revision["kind"] == "link":
                link_bases.add(acceptance["expected_target_sha256"])
            if note["materialized_revision_id"] == revision_id:
                materialized = (
                    note["materialized_sha256"] == revision["body_sha256"]
                    and note["write_base_sha256"] == revision["body_sha256"]
                )
            revision_id = revision["parent_revision_id"]
        if note["materialized_revision_id"] == operation["expected_revision_id"]:
            materialized = (
                note["materialized_sha256"] == operation["body_sha256"]
                and note["write_base_sha256"] == operation["body_sha256"]
            )
        link_bases.difference_update(
            {operation["body_sha256"], operation["expected_target_sha256"]}
        )
        return bool(visited) and materialized and not link_bases

    @staticmethod
    def _completed_same_revision_write(
        connection: sqlite3.Connection,
        note: sqlite3.Row,
        operation: sqlite3.Row,
        owner_actor_id: str,
    ) -> bool:
        """A completed owner action may already have established the requested base."""
        if (
            note["accepted_revision_id"] != operation["expected_revision_id"]
            or note["materialized_revision_id"] != operation["expected_revision_id"]
            or note["materialized_sha256"] != operation["body_sha256"]
            or note["write_base_sha256"] != operation["body_sha256"]
        ):
            return False
        # The old preimage must still have durable evidence. A new completion
        # cannot legitimize a corrupt preimage in the interrupted request.
        expected = operation["expected_target_sha256"]
        if not ManagedWorkspaceTasks._retained_write_preimage(connection, operation):
            return False
        for completed in connection.execute(
            "SELECT * FROM managed_operations WHERE workspace_id = ? AND note_id = ? "
            "AND status = 'completed' AND kind IN ('materialize', 'accept_revision') "
            "AND expected_revision_id = ? AND operation_id != ?",
            (
                operation["workspace_id"],
                note["note_id"],
                operation["expected_revision_id"],
                operation["operation_id"],
            ),
        ):
            if (
                completed["caller_actor_id"] == owner_actor_id
                and completed["body_bytes"] == operation["body_bytes"]
                and completed["body_sha256"] == operation["body_sha256"]
                and (
                    completed["kind"] == "accept_revision"
                    and completed["expected_target_sha256"] == operation["body_sha256"]
                    or completed["kind"] == "materialize"
                    and completed["target_relative_path"] == operation["target_relative_path"]
                    and completed["expected_target_sha256"] in {expected, operation["body_sha256"]}
                )
            ):
                return True
        return False

    @staticmethod
    def _retained_write_preimage(connection: sqlite3.Connection, operation: sqlite3.Row) -> bool:
        expected = operation["expected_target_sha256"]
        if expected is None:
            # Initial setup has no preimage. A standalone write can inherit that
            # absence if setup had not completed when the write was prepared.
            # Recovery may have completed setup earlier in this same pass.
            return (
                operation["kind"] == "setup"
                or connection.execute(
                    "SELECT 1 FROM managed_note_revisions AS r JOIN managed_operations AS o "
                    "ON o.operation_id = r.operation_id WHERE r.revision_id = ? "
                    "AND r.note_id = ? AND r.kind = 'setup' AND o.kind = 'setup' "
                    "AND o.expected_target_sha256 IS NULL "
                    "AND (o.status IN ('prepared', 'writing', 'promoted') OR o.completed_at > ?)",
                    (
                        operation["expected_revision_id"],
                        operation["note_id"],
                        operation["created_at"],
                    ),
                ).fetchone()
                is not None
            )
        rows = connection.execute(
            "SELECT body_bytes, body_sha256 FROM managed_note_revisions "
            "WHERE note_id = ? AND body_sha256 = ? "
            "UNION ALL SELECT body_bytes, body_sha256 FROM managed_operations "
            "WHERE workspace_id = ? AND note_id = ? AND caller_actor_id = ? "
            "AND status = 'completed' AND kind IN ('accept_revision', 'resolve') "
            "AND body_sha256 = ?",
            (
                operation["note_id"],
                expected,
                operation["workspace_id"],
                operation["note_id"],
                operation["caller_actor_id"],
                expected,
            ),
        )
        return any(isinstance(row[0], bytes) and _digest(row[0]) == expected for row in rows)

    @staticmethod
    def _observed_write_move(
        connection: sqlite3.Connection,
        note: sqlite3.Row,
        operation: sqlite3.Row,
    ) -> bool:
        if note["relative_path"] == operation["target_relative_path"]:
            return False
        return (
            connection.execute(
                "SELECT 1 FROM managed_note_observations AS o "
                "JOIN managed_workspaces AS w USING (workspace_id) "
                "WHERE o.workspace_id = ? AND o.note_id = ? "
                "AND o.generation = w.observation_generation "
                "AND o.relative_path = ? AND o.accepted_revision_id = ?",
                (
                    note["workspace_id"],
                    note["note_id"],
                    note["relative_path"],
                    note["accepted_revision_id"],
                ),
            ).fetchone()
            is not None
        )

    def _settle_write_authority(
        self,
        connection: sqlite3.Connection,
        workspace: _Workspace,
        operation: sqlite3.Row,
        *,
        observed_from: str | None = None,
    ) -> tuple[bool, bool]:
        """Validate and settle on the caller's connection, without filesystem writes."""
        self._assert_workspace_row(
            self._workspace_row(connection, workspace.workspace_id), workspace
        )
        operation_id = cast(str, operation["operation_id"])
        target = cast(str, operation["target_relative_path"])
        body = cast(bytes, operation["body_bytes"])
        expected = cast(str | None, operation["expected_target_sha256"])
        body_sha256 = cast(str, operation["body_sha256"])
        note = self._note_row(connection, workspace.workspace_id, operation["note_id"])
        bound = self._validate_write_authority(connection, operation)
        refresh_child = self._is_refresh_child(connection, operation)
        revision = connection.execute(
            "SELECT * FROM managed_note_revisions WHERE note_id = ? AND revision_id = ?",
            (note["note_id"], operation["expected_revision_id"]),
        ).fetchone()
        if (
            operation["status"] not in {"prepared", "writing", "promoted", "cancelled"}
            or operation["caller_actor_id"] != self._engine.profile.owner_actor_id
            or not isinstance(body, bytes)
            or revision is None
            or revision["body_bytes"] != body
            or revision["body_sha256"] != body_sha256
            or _digest(body) != body_sha256
        ):
            raise ManagedWorkspaceFailure("operation_replay_mismatch")
        if refresh_child:
            # Validate the frozen preimage against retained history, including
            # after a later accepted edit replaced the note's current base.
            previous = connection.execute(
                "SELECT body_sha256 FROM managed_note_revisions "
                "WHERE note_id = ? AND revision_id = ?",
                (note["note_id"], revision["parent_revision_id"]),
            ).fetchone()
            if (
                revision["operation_id"] != operation_id
                or (revision["parent_revision_id"] is not None and previous is None)
                or expected != (previous["body_sha256"] if previous is not None else None)
            ):
                raise ManagedWorkspaceFailure("operation_replay_mismatch")
        elif operation["kind"] == "materialize":
            self._require_matching_operation(
                operation,
                _request_sha256(
                    {
                        "kind": "materialize",
                        "note_id": note["note_id"],
                        "operation_id": operation_id,
                        "workspace_id": workspace.workspace_id,
                    }
                ),
                "materialize",
            )
        if operation["status"] == "cancelled":
            return True, False
        superseded = self._accepted_write_successor(
            connection, note, operation, self._engine.profile.owner_actor_id
        )
        # Legacy standalone requests need the old path from this observation
        # transaction. New descriptors and refresh manifests retain that evidence.
        moved = (bound or refresh_child or observed_from == target) and self._observed_write_move(
            connection,
            note,
            operation,
        )
        if (
            not refresh_child
            and superseded
            and (
                (note["relative_path"] != target and not moved)
                or not self._retained_write_preimage(connection, operation)
            )
        ):
            raise ManagedWorkspaceFailure("operation_replay_mismatch")
        completed = note[
            "write_base_sha256"
        ] != expected and self._completed_same_revision_write(
            connection, note, operation, self._engine.profile.owner_actor_id
        )
        # A proven accepted successor abandons the original authorization,
        # including its old path. It never grants a write to the successor path.
        if not superseded and (
            (note["relative_path"] != target and not moved)
            or note["accepted_revision_id"] != operation["expected_revision_id"]
            or (note["write_base_sha256"] != expected and not completed)
        ):
            raise ManagedWorkspaceFailure("operation_replay_mismatch")
        cancelled = (
            superseded
            or moved
            or not note["active"]
            or (
                refresh_child
                and managed_note_is_excluded(
                    connection,
                    workspace.workspace_id,
                    note_id=note["note_id"],
                    relative_path=note["relative_path"],
                )
            )
        )
        if cancelled:
            connection.execute(
                "UPDATE managed_operations SET status = 'cancelled', "
                "stage = 3, completed_at = ? "
                "WHERE operation_id = ?",
                (_timestamp(self._engine._clock()), operation_id),
            )
        else:
            self._require_active_note(connection, note)
        return cancelled, completed

    def _process_materialization(
        self,
        operation_id: str,
    ) -> None:
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
        with self._engine._store.transaction() as connection:
            cancelled, completed = self._settle_write_authority(
                connection, workspace, operation
            )
        if cancelled:
            # Raise only after committing. Catchers may continue other bound work,
            # but must never interpret an abandoned write as a successful receipt.
            raise _CancelledRefresh
        current = read_confined(
            root=workspace.root,
            relative=target,
            expected_root_identity=workspace.root_identity,
            maximum_bytes=MAX_FILE_BYTES,
        )
        current_sha256 = None if current is None else _digest(current)
        if completed and current_sha256 != body_sha256:
            # Another owner action already consumed this authorization. A later
            # edit (even a reversion to the old preimage) cannot revive its write.
            with self._engine._store.transaction() as connection:
                connection.execute(
                    "UPDATE managed_operations SET status = 'cancelled', stage = 3, "
                    "completed_at = ? WHERE operation_id = ?",
                    (_timestamp(self._engine._clock()), operation_id),
                )
            raise _CancelledRefresh
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
            if (
                current_operation is None
                or current_operation["request_sha256"] != operation["request_sha256"]
            ):
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
                SET materialized_revision_id = ?, materialized_sha256 = ?,
                    write_base_sha256 = ?, updated_at = ?
                WHERE note_id = ?""",
                (
                    operation["expected_revision_id"],
                    body_sha256,
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
            note = self._note_row(
                connection,
                cast(str, operation["workspace_id"]),
                cast(str, operation["note_id"]),
            )
            base_revision_id = note["materialized_revision_id"] or operation[
                "expected_revision_id"
            ]
            if base_revision_id is None:
                raise ManagedWorkspaceFailure("operation_replay_mismatch")
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
                        base_revision_id,
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

    def _canonical_pages(
        self, connection: sqlite3.Connection | None = None, *, require_publication: bool = False
    ) -> tuple[_CanonicalPage, ...]:
        if connection is None:
            with self._engine._store.transaction() as snapshot:
                return self._canonical_pages(snapshot, require_publication=require_publication)
        rows = tuple(
            connection.execute(
                """SELECT d.result_id AS note_id, d.canonical_path,
                          c.privacy_json, c.provenance_json
                FROM search_documents AS d JOIN captures AS c USING (capture_id)
                WHERE d.record_type = 'canonical' AND d.canonical_path IS NOT NULL
                ORDER BY d.result_id"""
            )
        )
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
            head = connection.execute(
                "SELECT published_sha256 FROM review_page_heads WHERE page_id = ?",
                (note_id,),
            ).fetchone()
            if (
                require_publication
                and head is not None
                and head["published_sha256"] != _digest(cast(bytes, payload))
            ):
                raise ManagedWorkspaceFailure("ineligible_source")
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

    def _detached_workspace(self) -> sqlite3.Row | None:
        connection = self._engine._store.connect()
        try:
            rows = tuple(
                connection.execute(
                    "SELECT * FROM managed_workspaces WHERE root_path IS NULL ORDER BY workspace_id"
                )
            )
        finally:
            connection.close()
        if len(rows) > 1:
            raise ManagedWorkspaceFailure("unsafe_workspace")
        return None if not rows else cast(sqlite3.Row, rows[0])

    def _attach_detached(
        self,
        workspace_row: sqlite3.Row,
        *,
        root: Path,
        identity: RootIdentity,
        caller_operation_id: str,
    ) -> ManagedWorkspaceReceipt:
        workspace_id = cast(str, workspace_row["workspace_id"])
        pages = {page.note_id: page.relative_path for page in self._canonical_pages()}
        operation_ids: list[str] = []
        now = _timestamp(self._engine._clock())
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                current = self._workspace_row(connection, workspace_id)
                if (
                    current["root_path"] is not None
                    or current["owner_actor_id"] != self._engine.profile.owner_actor_id
                ):
                    raise ManagedWorkspaceFailure("unsafe_workspace")
                notes = tuple(
                    connection.execute(
                        "SELECT * FROM managed_notes WHERE workspace_id = ? ORDER BY note_id",
                        (workspace_id,),
                    )
                )
                if {cast(str, note["note_id"]) for note in notes} != set(pages):
                    raise ManagedWorkspaceFailure("unsafe_workspace")
                connection.execute(
                    """UPDATE managed_workspaces SET root_path = ?, device = ?, inode = ?
                    WHERE workspace_id = ?""",
                    (str(root), str(identity[0]), str(identity[1]), workspace_id),
                )
                for note in notes:
                    note_id = cast(str, note["note_id"])
                    target = pages[note_id]
                    connection.execute(
                        """UPDATE managed_notes SET relative_path = ?, updated_at = ?
                        WHERE note_id = ?""",
                        (target, now, note_id),
                    )
                    if not bool(note["active"]):
                        continue
                    revision = connection.execute(
                        """SELECT body_bytes FROM managed_note_revisions
                        WHERE note_id = ? AND revision_id = ?""",
                        (note_id, note["accepted_revision_id"]),
                    ).fetchone()
                    if revision is None:
                        raise ManagedWorkspaceFailure("unsafe_workspace")
                    internal_operation_id = _new_id("operation")
                    operation_ids.append(internal_operation_id)
                    self._insert_operation(
                        connection,
                        operation_id=internal_operation_id,
                        request_sha256=_request_sha256(
                            {
                                "caller_operation_id": caller_operation_id,
                                "kind": "setup",
                                "note_id": note_id,
                                "revision_id": note["accepted_revision_id"],
                                "target": target,
                                "workspace_id": workspace_id,
                            }
                        ),
                        workspace_id=workspace_id,
                        note_id=note_id,
                        kind="setup",
                        target_relative_path=target,
                        expected_revision_id=cast(str, note["accepted_revision_id"]),
                        expected_target_sha256=None,
                        body=cast(bytes, revision["body_bytes"]),
                        now=now,
                    )
            self._engine._fault(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
            for internal_operation_id in operation_ids:
                self._process_materialization(internal_operation_id)
        return ManagedWorkspaceReceipt(
            "setup",
            workspace_id,
            generation=int(workspace_row["observation_generation"]),
        )

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
        if row["root_path"] is None or row["device"] is None or row["inode"] is None:
            raise ManagedWorkspaceFailure("unsafe_workspace")
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
            operation = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM managed_operations WHERE operation_id = ?", (operation_id,)
                ).fetchone(),
            )
            if operation is not None and operation["kind"] in {"setup", "materialize"}:
                self._validate_write_authority(connection, operation)
            return operation
        finally:
            connection.close()

    def _write_descriptor(
        self, connection: sqlite3.Connection, operation: sqlite3.Row
    ) -> bytes:
        workspace = self._workspace_row(connection, operation["workspace_id"])
        target = operation["target_relative_path"]
        try:
            if not isinstance(target, str):
                raise ValueError
            _validated_parts(target)
        except (RootConfinementError, ValueError):
            raise ManagedWorkspaceFailure("operation_replay_mismatch") from None
        return portable_canonical_json_bytes({
            "authority_version": 1,
            "operation_id": operation["operation_id"],
            "kind": operation["kind"],
            "caller_actor_id": operation["caller_actor_id"],
            "request_sha256": operation["request_sha256"],
            "workspace_id": operation["workspace_id"],
            "root_path": workspace["root_path"],
            "root_device": workspace["device"],
            "root_inode": workspace["inode"],
            "workspace_owner_actor_id": workspace["owner_actor_id"],
            "workspace_created_at": workspace["created_at"],
            "note_id": operation["note_id"],
            "target_relative_path": target,
            "expected_revision_id": operation["expected_revision_id"],
            "expected_target_sha256": operation["expected_target_sha256"],
            "body_sha256": operation["body_sha256"],
            "created_at": operation["created_at"],
        })

    def _validate_write_authority(
        self, connection: sqlite3.Connection, operation: sqlite3.Row
    ) -> bool:
        """Version zero is explicit missing evidence, never an implicit fallback."""
        authority = connection.execute(
            "SELECT * FROM managed_write_authority WHERE operation_id = ?",
            (operation["operation_id"],),
        ).fetchone()
        if authority is None:
            raise ManagedWorkspaceFailure("operation_replay_mismatch")
        if authority["authority_version"] == 0:
            target = operation["target_relative_path"]
            try:
                if not isinstance(target, str):
                    raise ValueError
                _validated_parts(target)
            except (RootConfinementError, ValueError):
                raise ManagedWorkspaceFailure("operation_replay_mismatch") from None
            if (
                authority["descriptor_json"] is not None
                or authority["descriptor_sha256"] is not None
            ):
                raise ManagedWorkspaceFailure("operation_replay_mismatch")
            return False
        body = operation["body_bytes"]
        descriptor = self._write_descriptor(connection, operation)
        if (
            authority["authority_version"] != 1
            or authority["descriptor_json"] != descriptor.decode("utf-8")
            or authority["descriptor_sha256"] != _digest(descriptor)
            or not isinstance(body, bytes)
            or _digest(body) != operation["body_sha256"]
        ):
            raise ManagedWorkspaceFailure("operation_replay_mismatch")
        return True

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
        if kind in {"setup", "materialize"}:
            operation = connection.execute(
                "SELECT * FROM managed_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            descriptor = self._write_descriptor(connection, operation)
            connection.execute(
                "INSERT INTO managed_write_authority "
                "(operation_id, authority_version, descriptor_json, descriptor_sha256) "
                "VALUES (?, 1, ?, ?)",
                (operation_id, descriptor.decode("utf-8"), _digest(descriptor)),
            )
