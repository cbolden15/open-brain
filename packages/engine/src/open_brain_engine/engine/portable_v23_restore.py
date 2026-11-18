"""Restore proven opaque filing witnesses after their canonical routes."""

import sqlite3
from collections.abc import Mapping
from typing import ClassVar

from open_brain_engine.portable.browser_filing_v23 import (
    FILING_WITNESS_COLUMNS,
    validate_filing_witnesses,
)
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v23_catalog import PORTABLE_V23_SCHEMA_CATALOG_DIGEST

from .portable_v22_restore import V22RestoreBundle
from .saved_item_identity import SavedItemSource


class V23RestoreBundle(V22RestoreBundle):
    portable_version: ClassVar[int] = 23
    catalog_digest: ClassVar[str] = PORTABLE_V23_SCHEMA_CATALOG_DIGEST


def install_filing_witnesses(connection: sqlite3.Connection, files: Mapping[str, bytes]) -> None:
    rows = validate_filing_witnesses(files)
    if connection.execute("SELECT 1 FROM browser_filing_witnesses LIMIT 1").fetchone() is not None:
        raise PortableValidationError("browser filing evidence must land in an empty store")
    for row in rows:
        source = SavedItemSource(row["connector_name"], row["connection_id"], row["resource_id"])
        source.witness(connection, row["item_key"])
        connection.execute(
            "INSERT INTO browser_filing_witnesses VALUES (?,?,?,?,?,?,?)",
            tuple(row[name] for name in FILING_WITNESS_COLUMNS),
        )
