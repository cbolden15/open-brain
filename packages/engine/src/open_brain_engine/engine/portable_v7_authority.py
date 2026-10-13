"""Exact sharing authority rows for the required Portable 7 sidecar."""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.v7 import SHARING_APPROVALS_PATH, SHARING_COLUMNS

from .contracts import JournalEnvelope


def sharing_authority_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    if (
        not connection.in_transaction
        or connection.execute("PRAGMA user_version").fetchone()[0] != 12
    ):
        raise ValueError("Portable 7 requires one active schema-12 snapshot")
    return _sharing_authority_metadata(connection)


def _sharing_authority_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    """Shared row codec; public format entry points keep exact schema floors."""
    value: dict[str, Any] = {"schema_version": 1}
    for table, columns in SHARING_COLUMNS.items():
        names = [column.split(":")[0] for column in columns]
        rows = []
        for row in connection.execute(
            f"SELECT {','.join(names)} FROM {table} ORDER BY {names[0]} COLLATE BINARY"  # noqa: S608
        ):
            result = dict(row)
            for column in columns:
                name, kind = column.split(":")
                if kind.startswith("b") and result[name] is not None:
                    result[name] = base64.b64encode(result[name]).decode("ascii")
            rows.append(result)
        value[table] = rows
    return value


def sharing_authority_sidecar(connection: sqlite3.Connection) -> tuple[str, bytes]:
    return SHARING_APPROVALS_PATH, portable_canonical_json_bytes(
        sharing_authority_metadata(connection)
    )


def restore_sharing_authority(connection: sqlite3.Connection, value: dict[str, Any]) -> None:
    """Install only semantically validated rows in the hidden restore transaction."""
    for table, columns in SHARING_COLUMNS.items():
        names = [column.split(":")[0] for column in columns]
        for row in value[table]:
            insert_names = names
            values = [
                base64.b64decode(row[name], validate=True)
                if column.split(":")[1].startswith("b") and row[name] is not None
                else row[name]
                for name, column in zip(names, columns, strict=True)
            ]
            if table == "sharing_previews":
                insert_names = [*names, "imported"]
                values = [*values, 1]
            connection.execute(
                f"INSERT INTO {table} ({','.join(insert_names)}) "  # noqa: S608
                f"VALUES ({','.join('?' for _ in insert_names)})",
                values,
            )
    for decision in value["sharing_decisions"]:
        if decision["copy_capture_id"] is None:
            continue
        submission = JournalEnvelope.from_bytes(
            base64.b64decode(decision["copy_submission_bytes"], validate=True)
        ).submission
        retained = connection.execute(
            "SELECT source_bytes FROM source_revisions WHERE capture_id=?",
            (decision["copy_capture_id"],),
        ).fetchone()
        if retained is None:
            raise ValueError("Portable 7 sharing copy capture missing")
        # The frozen record includes transformation_receipts, while the live
        # public-job Provenance DTO has exactly these three fields. Derive them
        # from the independently retained record, never from the copy envelope.
        provenance = json.loads(retained[0])["provenance"]
        provenance = {
            key: provenance[key] for key in ("source_ref", "content_origin", "owner_context")
        }
        restored = connection.execute(
            "UPDATE captures SET delivery_id=?,request_sha256=?,provenance_json=?,"
            "submission_path=? "
            "WHERE capture_id=?",
            (
                decision["copy_delivery_id"],
                decision["copy_submission_sha256"],
                portable_canonical_json_bytes(provenance).decode("utf-8"),
                submission.submission_path.value,
                decision["copy_capture_id"],
            ),
        )
        if restored.rowcount != 1:
            raise ValueError("Portable 7 sharing copy capture missing")


__all__ = [
    "SHARING_COLUMNS",
    "restore_sharing_authority",
    "sharing_authority_metadata",
    "sharing_authority_sidecar",
]
