"""Shared managed-workspace exclusion checks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import PurePosixPath
from typing import cast

from .contracts import ManagedWorkspaceFailure


def managed_note_is_excluded(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    note_id: str,
    relative_path: str,
) -> bool:
    """Apply the exact durable note, folder, and portable-set exclusions."""
    rows = connection.execute(
        """SELECT kind, subject FROM managed_exclusions
        WHERE workspace_id = ? AND active = 1""",
        (workspace_id,),
    )
    relative = PurePosixPath(relative_path)
    for kind, subject in rows:
        if kind == "note" and subject == note_id:
            return True
        if kind == "folder":
            folder = PurePosixPath(cast(str, subject))
            if relative == folder or folder in relative.parents:
                return True
        if kind == "portable_set":
            try:
                values = json.loads(cast(str, subject))
            except (TypeError, json.JSONDecodeError):
                raise ManagedWorkspaceFailure("invalid_policy") from None
            if isinstance(values, list) and note_id in values:
                return True
    return False


__all__ = ["managed_note_is_excluded"]
