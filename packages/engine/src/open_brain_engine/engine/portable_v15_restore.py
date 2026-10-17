"""Restore shared and consumer authority before inherited source/search projections."""

from __future__ import annotations

import base64
import sqlite3
from hashlib import sha256
from typing import ClassVar, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.consumer_v15 import (
    BLOB_COLUMNS,
    CONSUMER_OBJECT_PREFIX,
    PROJECTION_COLUMNS,
    validate_consumer_evidence,
)
from open_brain_engine.portable.lifecycle_v15 import lifecycle_evidence_value
from open_brain_engine.portable.retraction_v15 import validate_retraction_evidence
from open_brain_engine.portable.v15_catalog import PORTABLE_V15_SCHEMA_CATALOG_DIGEST

from .capture_lifecycle_contracts import canonical_bytes
from .consumer_journal import restore_engine_lifecycle_journal_v2
from .contracts import LocalEngineContext
from .portable_v12_restore import V12RestoreBundle


class V15RestoreBundle(V12RestoreBundle):
    portable_version: ClassVar[int] = 15
    catalog_digest: ClassVar[str] = PORTABLE_V15_SCHEMA_CATALOG_DIGEST

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        files = self.snapshot.files
        consumer = validate_consumer_evidence(files)
        approvals = validate_retraction_evidence(files)
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        journal = restore_engine_lifecycle_journal_v2(
            connection,
            lifecycle_evidence_value(files),
            brain_id=identity[0],
            issuer_epoch=identity[1],
        )
        for descriptor in consumer["objects"]:
            key = descriptor["sha256"]
            connection.execute(
                "INSERT INTO consumer_intent_objects VALUES(?,?,?)",
                (key, descriptor["kind"], files[CONSUMER_OBJECT_PREFIX + key]),
            )
        for approval in consumer["approvals"]:
            request = journal._state().requests[approval["operation_id"]]
            connection.execute(
                "INSERT INTO consumer_approvals VALUES(?,?,?,?)",
                (
                    request.operation_id,
                    approval["root_sha256"],
                    request.canonical_bytes,
                    request.request_sha256,
                ),
            )
        for table, columns in PROJECTION_COLUMNS.items():
            for row in consumer[table]:
                values = [
                    base64.b64decode(row[key], validate=True) if key in BLOB_COLUMNS else row[key]
                    for key in columns
                ]
                connection.execute(
                    f"INSERT INTO {table} VALUES({','.join('?' for _ in columns)})", values
                )
        for record in approvals:
            owner_request = cast(dict[str, object], record["request"])
            request_raw, receipt_raw = canonical(owner_request), canonical_bytes(record["receipt"])
            connection.execute(
                "INSERT INTO retraction_approvals VALUES(?,?,?,?,?)",
                (
                    owner_request["operation_id"],
                    request_raw,
                    sha256(request_raw).hexdigest(),
                    receipt_raw,
                    sha256(receipt_raw).hexdigest(),
                ),
            )
        super().install(connection, profile=profile)
        self.checkpoint("consumer_intake_restored")
