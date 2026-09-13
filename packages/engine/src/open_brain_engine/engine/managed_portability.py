"""Path-free Portable Brain records for managed-workspace durable state."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.managed_v2 import (
    managed_workspace_path,
    validate_managed_workspace_record,
)
from open_brain_engine.storage.filesystem import read_confined

from .managed_workspace import _digest, _timestamp
from .markdown_import_fs import MAX_FILE_BYTES

if TYPE_CHECKING:
    from .local import BrainEngine


def export_managed_workspace_state(engine: BrainEngine) -> tuple[str, bytes] | None:
    """Serialize one stable workspace without any device-local filesystem identity."""
    connection = engine._store.connect()
    try:
        workspaces = tuple(
            connection.execute("SELECT * FROM managed_workspaces ORDER BY workspace_id")
        )
        if not workspaces:
            return None
        if len(workspaces) != 1:
            raise ValueError("Portable export requires exactly one managed workspace")
        workspace = cast(sqlite3.Row, workspaces[0])
        workspace_id = cast(str, workspace["workspace_id"])
        if connection.execute(
            "SELECT 1 FROM managed_conflicts WHERE status = 'open' LIMIT 1"
        ).fetchone() is not None:
            raise ValueError("Portable export requires all managed conflicts to be resolved")
        if connection.execute(
            """SELECT 1 FROM managed_inference_requests
            WHERE status IN ('reserved', 'dispatching') LIMIT 1"""
        ).fetchone() is not None:
            raise ValueError("Portable export requires all inference requests to be settled")
        notes = tuple(
            connection.execute(
                "SELECT * FROM managed_notes WHERE workspace_id = ? ORDER BY note_id",
                (workspace_id,),
            )
        )
        if workspace["root_path"] is not None:
            managed = engine.managed_workspace._workspace(workspace_id)
            for note in notes:
                if not bool(note["active"]):
                    continue
                relative = note["relative_path"]
                write_base = note["write_base_sha256"]
                if not isinstance(relative, str) or not isinstance(write_base, str):
                    raise ValueError("Portable export requires materialized active notes")
                payload = read_confined(
                    root=managed.root,
                    relative=relative,
                    expected_root_identity=managed.root_identity,
                    maximum_bytes=MAX_FILE_BYTES,
                )
                if payload is None or _digest(payload) != write_base:
                    raise ValueError("Portable export found an unaccepted managed edit")
        page_ids = {cast(str, note["note_id"]) for note in notes}
        record: dict[str, object] = {
            "budgets": _budgets(connection, workspace_id),
            "conflicts": _conflicts(connection, workspace_id),
            "consents": _consents(connection, workspace_id),
            "created_at": workspace["created_at"],
            "exclusions": _exclusions(connection, workspace_id, notes),
            "links": _links(connection, workspace_id),
            "notes": [_note(connection, note) for note in notes],
            "observation_generation": workspace["observation_generation"],
            "origin_owner_actor_id": workspace["origin_owner_actor_id"],
            "policy_generation": workspace["policy_generation"],
            "schema_version": 1,
            "tenant_id": engine.profile.tenant_id,
            "workspace_id": workspace_id,
        }
        payload = portable_canonical_json_bytes(record)
        validate_managed_workspace_record(
            payload,
            tenant_id=engine.profile.tenant_id,
            page_ids=page_ids,
        )
        return managed_workspace_path(workspace_id), payload
    finally:
        connection.close()


def import_managed_workspace_state(engine: BrainEngine, payload: bytes) -> str:
    """Restore validated managed state detached from any host workspace."""
    page_ids = _canonical_page_ids(engine)
    record = validate_managed_workspace_record(
        payload,
        tenant_id=engine.profile.tenant_id,
        page_ids=page_ids,
    )
    workspace_id = cast(str, record["workspace_id"])
    now = _timestamp(engine._clock())
    with engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
        with engine._store.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM managed_workspaces LIMIT 1"
            ).fetchone()
            if existing is not None:
                raise ValueError("Portable import destination already has managed workspace state")
            connection.execute(
                """INSERT INTO managed_workspaces
                (workspace_id, owner_actor_id, origin_owner_actor_id, created_at,
                 observation_generation, policy_generation)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    workspace_id,
                    engine.profile.owner_actor_id,
                    record["origin_owner_actor_id"],
                    record["created_at"],
                    record["observation_generation"],
                    record["policy_generation"],
                ),
            )
            for raw_note in cast(list[dict[str, object]], record["notes"]):
                _import_note(connection, workspace_id, raw_note, now)
            for raw_link in cast(list[dict[str, object]], record["links"]):
                connection.execute(
                    """INSERT INTO managed_links
                    (link_id, source_note_id, source_revision_id, target_note_id,
                     target_revision_id, source_quote, target_quote, provenance_json,
                     accepted_at, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        raw_link["link_id"],
                        raw_link["source_note_id"],
                        raw_link["source_revision_id"],
                        raw_link["target_note_id"],
                        raw_link["target_revision_id"],
                        raw_link["source_quote"],
                        raw_link["target_quote"],
                        portable_canonical_json_bytes(raw_link["provenance"]).decode(),
                        raw_link["accepted_at"],
                        int(cast(bool, raw_link["active"])),
                    ),
                )
            for raw_conflict in cast(list[dict[str, object]], record["conflicts"]):
                connection.execute(
                    """INSERT INTO managed_conflicts
                    (conflict_id, note_id, base_revision_id, accepted_revision_id,
                     candidate_body_bytes, candidate_sha256, status,
                     resolution_revision_id, detected_at, resolved_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'resolved', ?, ?, ?)""",
                    (
                        raw_conflict["conflict_id"],
                        raw_conflict["note_id"],
                        raw_conflict["base_revision_id"],
                        raw_conflict["accepted_revision_id"],
                        base64.b64decode(cast(str, raw_conflict["candidate_body_base64"])),
                        raw_conflict["candidate_sha256"],
                        raw_conflict["resolution_revision_id"],
                        raw_conflict["detected_at"],
                        raw_conflict["resolved_at"],
                    ),
                )
            for raw_consent in cast(list[dict[str, object]], record["consents"]):
                revoked_at = raw_consent["revoked_at"] or now
                connection.execute(
                    """INSERT INTO managed_consents
                    (consent_id, workspace_id, provider, access_mode, operation,
                     note_scope, owner_actor_id, granted_generation, active,
                     granted_at, revoked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (
                        raw_consent["consent_id"],
                        workspace_id,
                        raw_consent["provider"],
                        raw_consent["access_mode"],
                        raw_consent["operation"],
                        raw_consent["note_scope"],
                        engine.profile.owner_actor_id,
                        raw_consent["granted_generation"],
                        raw_consent["granted_at"],
                        revoked_at,
                    ),
                )
            for raw_exclusion in cast(list[dict[str, object]], record["exclusions"]):
                connection.execute(
                    """INSERT INTO managed_exclusions
                    (exclusion_id, workspace_id, kind, subject, active,
                     policy_generation, recorded_at)
                    VALUES (?, ?, 'portable_set', ?, ?, ?, ?)""",
                    (
                        raw_exclusion["exclusion_id"],
                        workspace_id,
                        portable_canonical_json_bytes(raw_exclusion["note_ids"]).decode(),
                        int(cast(bool, raw_exclusion["active"])),
                        raw_exclusion["policy_generation"],
                        raw_exclusion["recorded_at"],
                    ),
                )
            for raw_budget in cast(list[dict[str, object]], record["budgets"]):
                connection.execute(
                    """INSERT INTO managed_inference_budgets
                    (workspace_id, provider, window_key, request_limit, byte_limit,
                     used_requests, used_bytes, reserved_requests, reserved_bytes,
                     uncertain_requests, uncertain_bytes, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)""",
                    (
                        workspace_id,
                        raw_budget["provider"],
                        raw_budget["window_key"],
                        raw_budget["request_limit"],
                        raw_budget["byte_limit"],
                        raw_budget["used_requests"],
                        raw_budget["used_bytes"],
                        raw_budget["uncertain_requests"],
                        raw_budget["uncertain_bytes"],
                        raw_budget["updated_at"],
                    ),
                )
    return workspace_id


def _note(connection: sqlite3.Connection, note: sqlite3.Row) -> dict[str, object]:
    revisions = []
    for revision in connection.execute(
        "SELECT * FROM managed_note_revisions WHERE note_id = ? ORDER BY recorded_at, rowid",
        (note["note_id"],),
    ):
        revisions.append(
            {
                "accepted_by_actor_id": revision["accepted_by_actor_id"],
                "body_base64": base64.b64encode(revision["body_bytes"]).decode("ascii"),
                "body_sha256": revision["body_sha256"],
                "kind": revision["kind"],
                "operation_id": revision["operation_id"],
                "parent_revision_id": revision["parent_revision_id"],
                "privacy": json.loads(revision["privacy_json"]),
                "provenance": json.loads(revision["provenance_json"]),
                "recorded_at": revision["recorded_at"],
                "revision_id": revision["revision_id"],
            }
        )
    return {
        "accepted_revision_id": note["accepted_revision_id"],
        "active": bool(note["active"]),
        "note_id": note["note_id"],
        "revisions": revisions,
    }


def _links(connection: sqlite3.Connection, workspace_id: str) -> list[dict[str, object]]:
    rows = connection.execute(
        """SELECT l.* FROM managed_links AS l
        JOIN managed_notes AS n ON n.note_id = l.source_note_id
        WHERE n.workspace_id = ? ORDER BY l.link_id""",
        (workspace_id,),
    )
    return [
        {
            "accepted_at": row["accepted_at"],
            "active": bool(row["active"]),
            "link_id": row["link_id"],
            "provenance": json.loads(row["provenance_json"]),
            "source_note_id": row["source_note_id"],
            "source_quote": row["source_quote"],
            "source_revision_id": row["source_revision_id"],
            "target_note_id": row["target_note_id"],
            "target_quote": row["target_quote"],
            "target_revision_id": row["target_revision_id"],
        }
        for row in rows
    ]


def _conflicts(connection: sqlite3.Connection, workspace_id: str) -> list[dict[str, object]]:
    rows = connection.execute(
        """SELECT c.* FROM managed_conflicts AS c
        JOIN managed_notes AS n ON n.note_id = c.note_id
        WHERE n.workspace_id = ? AND c.status = 'resolved' ORDER BY c.conflict_id""",
        (workspace_id,),
    )
    return [
        {
            "accepted_revision_id": row["accepted_revision_id"],
            "base_revision_id": row["base_revision_id"],
            "candidate_body_base64": base64.b64encode(row["candidate_body_bytes"]).decode("ascii"),
            "candidate_sha256": row["candidate_sha256"],
            "conflict_id": row["conflict_id"],
            "detected_at": row["detected_at"],
            "note_id": row["note_id"],
            "resolution_revision_id": row["resolution_revision_id"],
            "resolved_at": row["resolved_at"],
        }
        for row in rows
    ]


def _consents(connection: sqlite3.Connection, workspace_id: str) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT * FROM managed_consents WHERE workspace_id = ? ORDER BY consent_id",
        (workspace_id,),
    )
    return [
        {
            "access_mode": row["access_mode"],
            "active_at_export": bool(row["active"]),
            "consent_id": row["consent_id"],
            "granted_at": row["granted_at"],
            "granted_generation": row["granted_generation"],
            "note_scope": row["note_scope"],
            "operation": row["operation"],
            "owner_actor_id": row["owner_actor_id"],
            "provider": row["provider"],
            "revoked_at": row["revoked_at"],
        }
        for row in rows
    ]


def _exclusions(
    connection: sqlite3.Connection, workspace_id: str, notes: tuple[sqlite3.Row, ...]
) -> list[dict[str, object]]:
    paths = {
        cast(str, note["note_id"]): cast(str | None, note["relative_path"])
        for note in notes
    }
    result: list[dict[str, object]] = []
    for row in connection.execute(
        "SELECT * FROM managed_exclusions WHERE workspace_id = ? ORDER BY exclusion_id",
        (workspace_id,),
    ):
        kind = cast(str, row["kind"])
        subject = cast(str, row["subject"])
        if kind == "note":
            note_ids = [subject]
        elif kind == "folder":
            folder = PurePosixPath(subject)
            note_ids = sorted(
                note_id
                for note_id, path in paths.items()
                if path is not None
                and (PurePosixPath(path) == folder or folder in PurePosixPath(path).parents)
            )
        elif kind == "portable_set":
            value = json.loads(subject)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError("Portable export found invalid managed exclusions")
            note_ids = sorted(cast(list[str], value))
        else:
            raise ValueError("Portable export found invalid managed exclusions")
        if not note_ids:
            continue
        result.append(
            {
                "active": bool(row["active"]),
                "exclusion_id": row["exclusion_id"],
                "note_ids": note_ids,
                "policy_generation": row["policy_generation"],
                "recorded_at": row["recorded_at"],
            }
        )
    return result


def _budgets(connection: sqlite3.Connection, workspace_id: str) -> list[dict[str, object]]:
    rows = connection.execute(
        """SELECT * FROM managed_inference_budgets
        WHERE workspace_id = ? ORDER BY provider, window_key""",
        (workspace_id,),
    )
    result = []
    for row in rows:
        if row["reserved_requests"] or row["reserved_bytes"]:
            raise ValueError("Portable export requires settled budget reservations")
        result.append(
            {
                "byte_limit": row["byte_limit"],
                "provider": row["provider"],
                "request_limit": row["request_limit"],
                "uncertain_bytes": row["uncertain_bytes"],
                "uncertain_requests": row["uncertain_requests"],
                "updated_at": row["updated_at"],
                "used_bytes": row["used_bytes"],
                "used_requests": row["used_requests"],
                "window_key": row["window_key"],
            }
        )
    return result


def _import_note(
    connection: sqlite3.Connection,
    workspace_id: str,
    note: dict[str, object],
    now: str,
) -> None:
    note_id = cast(str, note["note_id"])
    connection.execute(
        """INSERT INTO managed_notes
        (note_id, workspace_id, accepted_revision_id, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (
            note_id,
            workspace_id,
            note["accepted_revision_id"],
            int(cast(bool, note["active"])),
            now,
            now,
        ),
    )
    for revision in cast(list[dict[str, object]], note["revisions"]):
        connection.execute(
            """INSERT INTO managed_note_revisions
            (revision_id, note_id, parent_revision_id, kind, body_bytes, body_sha256,
             accepted_by_actor_id, provenance_json, privacy_json, recorded_at, operation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                revision["revision_id"],
                note_id,
                revision["parent_revision_id"],
                revision["kind"],
                base64.b64decode(cast(str, revision["body_base64"])),
                revision["body_sha256"],
                revision["accepted_by_actor_id"],
                portable_canonical_json_bytes(revision["provenance"]).decode(),
                portable_canonical_json_bytes(revision["privacy"]).decode(),
                revision["recorded_at"],
                revision["operation_id"],
            ),
        )


def _canonical_page_ids(engine: BrainEngine) -> set[str]:
    connection = engine._store.connect()
    try:
        return {
            cast(str, row[0])
            for row in connection.execute(
                "SELECT result_id FROM search_documents WHERE record_type = 'canonical'"
            )
        }
    finally:
        connection.close()


__all__ = ["export_managed_workspace_state", "import_managed_workspace_state"]
