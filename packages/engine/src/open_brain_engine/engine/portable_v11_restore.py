"""Portable11 development installer layered over unchanged facet/base restore."""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from open_brain_engine.portable.developments_v11 import (
    validate_append_operations,
    validate_development_evidence,
)
from open_brain_engine.portable.v11_catalog import PORTABLE_V11_SCHEMA_CATALOG_DIGEST

from .contracts import LocalEngineContext
from .portable_v10_restore import V10RestoreBundle


class V11RestoreBundle(V10RestoreBundle):
    portable_version: ClassVar[int] = 11
    catalog_digest: ClassVar[str] = PORTABLE_V11_SCHEMA_CATALOG_DIGEST

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        super().install(connection, profile=profile)
        for row in validate_development_evidence(self.snapshot.files):
            connection.execute(
                "INSERT INTO development_evidence VALUES(?,?,?,?,?,?)",
                (
                    row["derived_capture_id"],
                    row["operation_id"],
                    row["principal_id"],
                    row["request_sha256"],
                    row["request_json"],
                    row["receipt_json"],
                ),
            )
        for row in validate_append_operations(self.snapshot.files):
            connection.execute(
                "INSERT INTO development_append_operations VALUES(?,?,?,?,?)",
                (
                    row["delivery_id"],
                    row["request_sha256"],
                    row["request_json"],
                    row["draft_json"],
                    row["proposal_id"],
                ),
            )
        self.checkpoint("development_evidence_restored")
