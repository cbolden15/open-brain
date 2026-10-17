"""Restore immutable approvals in the inherited lifecycle installation transaction."""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from typing import ClassVar, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.retraction_v14 import validate_retraction_evidence
from open_brain_engine.portable.v14_catalog import PORTABLE_V14_SCHEMA_CATALOG_DIGEST

from .contracts import LocalEngineContext
from .portable_v13_restore import V13RestoreBundle


class V14RestoreBundle(V13RestoreBundle):
    portable_version: ClassVar[int] = 14
    catalog_digest: ClassVar[str] = PORTABLE_V14_SCHEMA_CATALOG_DIGEST

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        records = validate_retraction_evidence(self.snapshot.files)
        super().install(connection, profile=profile)
        for record in records:
            request = cast(dict[str, object], record["request"])
            request_raw, receipt_raw = canonical(request), canonical(record["receipt"])
            connection.execute(
                "INSERT INTO retraction_approvals "
                "(operation_id,request_bytes,request_sha256,receipt_bytes,receipt_sha256) "
                "VALUES(?,?,?,?,?)",
                (request["operation_id"], request_raw, sha256(request_raw).hexdigest(),
                 receipt_raw, sha256(receipt_raw).hexdigest()),
            )
        self.checkpoint("owner_retraction_approvals_restored")
