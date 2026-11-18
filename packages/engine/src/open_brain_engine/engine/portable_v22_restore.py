"""Restore calendar-day revisit intents after their exact parent captures."""

import sqlite3
from collections.abc import Mapping
from typing import ClassVar

from open_brain_engine.portable.browser_revisits_v22 import (
    REVISIT_COLUMNS,
    validate_browser_revisits,
)
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v22_catalog import PORTABLE_V22_SCHEMA_CATALOG_DIGEST

from .portable_v21_restore import V21RestoreBundle


class V22RestoreBundle(V21RestoreBundle):
    portable_version: ClassVar[int] = 22
    catalog_digest: ClassVar[str] = PORTABLE_V22_SCHEMA_CATALOG_DIGEST


def install_browser_revisits(connection: sqlite3.Connection, files: Mapping[str, bytes]) -> None:
    rows = validate_browser_revisits(files)
    if connection.execute("SELECT 1 FROM browser_revisits LIMIT 1").fetchone() is not None:
        raise PortableValidationError("browser revisit evidence must land in an empty store")
    placeholders = ", ".join("?" for _ in REVISIT_COLUMNS)
    columns = ", ".join(REVISIT_COLUMNS)
    connection.executemany(
        f"INSERT INTO browser_revisits ({columns}) VALUES ({placeholders})",
        (tuple(row[name] for name in REVISIT_COLUMNS) for row in rows),
    )
