"""Portable12 installer retains immutable engine-owned selection operations."""

from __future__ import annotations

import base64
import sqlite3
from typing import ClassVar

from open_brain_engine.portable.slack_v12 import SELECTION_COLUMNS, validate_selections
from open_brain_engine.portable.v12_catalog import PORTABLE_V12_SCHEMA_CATALOG_DIGEST

from .contracts import LocalEngineContext
from .portable_v11_restore import V11RestoreBundle


class V12RestoreBundle(V11RestoreBundle):
    portable_version: ClassVar[int] = 12
    catalog_digest: ClassVar[str] = PORTABLE_V12_SCHEMA_CATALOG_DIGEST

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        super().install(connection, profile=profile)
        for row in validate_selections(self.snapshot.files)["selections"]:
            values = [
                base64.b64decode(row[column.split(":")[0]], validate=True)
                if column.endswith(":b")
                else row[column.split(":")[0]]
                for column in SELECTION_COLUMNS
            ]
            connection.execute("INSERT INTO slack_recall_selections VALUES(?,?,?,?,?,?,?)", values)
        self.checkpoint("slack_recall_selections_restored")
