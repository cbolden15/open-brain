"""Restore annotations after their exact captures and journal custody are available."""

import sqlite3
from collections.abc import Mapping
from typing import ClassVar

from open_brain_engine.portable.browser_annotations_v21 import (
    ANNOTATION_COLUMNS,
    validate_browser_annotations,
)
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v21_catalog import PORTABLE_V21_SCHEMA_CATALOG_DIGEST

from .portable_v20_restore import V20RestoreBundle


class V21RestoreBundle(V20RestoreBundle):
    portable_version: ClassVar[int] = 21
    catalog_digest: ClassVar[str] = PORTABLE_V21_SCHEMA_CATALOG_DIGEST


def install_browser_annotations(connection: sqlite3.Connection, files: Mapping[str, bytes]) -> None:
    rows = validate_browser_annotations(files)
    if connection.execute("SELECT 1 FROM browser_annotations LIMIT 1").fetchone() is not None:
        raise PortableValidationError("browser annotation evidence must land in an empty store")
    placeholders = ", ".join("?" for _ in ANNOTATION_COLUMNS)
    columns = ", ".join(ANNOTATION_COLUMNS)
    connection.executemany(
        f"INSERT INTO browser_annotations ({columns}) VALUES ({placeholders})",
        (tuple(row[name] for name in ANNOTATION_COLUMNS) for row in rows),
    )
