"""Restore Portable19 saved-item identities into an empty registry."""

import sqlite3
from typing import ClassVar

from open_brain_engine.portable.saved_item_sources_v19 import validate_saved_item_sources
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v19_catalog import PORTABLE_V19_SCHEMA_CATALOG_DIGEST

from .contracts import LocalEngineContext
from .portable_v18_restore import V18RestoreBundle


class V19RestoreBundle(V18RestoreBundle):
    portable_version: ClassVar[int] = 19
    catalog_digest: ClassVar[str] = PORTABLE_V19_SCHEMA_CATALOG_DIGEST

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        sources = validate_saved_item_sources(self.snapshot.files)
        if connection.execute("SELECT 1 FROM saved_item_sources LIMIT 1").fetchone() is not None:
            raise PortableValidationError("saved-item sources must land in an empty registry")
        super().install(connection, profile=profile)
        for source in sources:
            source.register(connection)
        self.checkpoint("saved_item_sources_restored")
