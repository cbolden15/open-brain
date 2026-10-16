"""Install validated lifecycle state before inherited search reconstruction."""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from open_brain_engine.portable.lifecycle_v13 import lifecycle_evidence_value
from open_brain_engine.portable.v13_catalog import PORTABLE_V13_SCHEMA_CATALOG_DIGEST

from .capture_lifecycle_journal import restore_engine_lifecycle_journal
from .contracts import LocalEngineContext
from .portable_v12_restore import V12RestoreBundle


class V13RestoreBundle(V12RestoreBundle):
    portable_version: ClassVar[int] = 13
    catalog_digest: ClassVar[str] = PORTABLE_V13_SCHEMA_CATALOG_DIGEST

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        restore_engine_lifecycle_journal(
            connection, lifecycle_evidence_value(self.snapshot.files),
            brain_id=identity[0], issuer_epoch=identity[1],
        )
        super().install(connection, profile=profile)
        self.checkpoint("capture_lifecycle_restored")
