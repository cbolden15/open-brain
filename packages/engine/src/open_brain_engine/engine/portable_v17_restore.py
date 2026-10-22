"""Restore Portable17 owner egress policy history and make its latest row active."""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from open_brain_engine.portable.egress_policy_v17 import validate_owner_egress_policy_evidence
from open_brain_engine.portable.v17_catalog import PORTABLE_V17_SCHEMA_CATALOG_DIGEST

from .contracts import LocalEngineContext
from .owner_egress_policy import install_restored_policy
from .portable_v16_restore import V16RestoreBundle


class V17RestoreBundle(V16RestoreBundle):
    portable_version: ClassVar[int] = 17
    catalog_digest: ClassVar[str] = PORTABLE_V17_SCHEMA_CATALOG_DIGEST

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        # Validate before any inherited destination write.
        rows = validate_owner_egress_policy_evidence(self.snapshot.files)
        super().install(connection, profile=profile)
        install_restored_policy(connection, rows)
        self.checkpoint("owner_egress_policy_restored")
