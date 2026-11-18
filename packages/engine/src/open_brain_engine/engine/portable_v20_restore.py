"""Restore canonical saved-item admission and operation evidence without local grants."""

import sqlite3
from collections.abc import Mapping
from typing import ClassVar

from open_brain_engine.portable.saved_item_admission_v20 import (
    TABLE_COLUMNS,
    validate_saved_item_admission,
)
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v20_catalog import PORTABLE_V20_SCHEMA_CATALOG_DIGEST

from .portable_v19_restore import V19RestoreBundle


class V20RestoreBundle(V19RestoreBundle):
    portable_version: ClassVar[int] = 20
    catalog_digest: ClassVar[str] = PORTABLE_V20_SCHEMA_CATALOG_DIGEST

def install_saved_item_admission(
    connection: sqlite3.Connection, files: Mapping[str, bytes]
) -> None:
    # Install after journal custody so replayed events do not fire the outcome
    # trigger against the already-canonical archive bindings.
    records = validate_saved_item_admission(files)
    for table in TABLE_COLUMNS:
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise PortableValidationError("saved-item admission must land in an empty store")
    for table in ("saved_item_admissions", "browser_operations"):
        columns = TABLE_COLUMNS[table]
        placeholders = ", ".join("?" for _ in columns)
        connection.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            (tuple(row[name] for name in columns) for row in records[table]),
        )
