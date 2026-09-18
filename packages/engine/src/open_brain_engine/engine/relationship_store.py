"""Deterministic public relationship evidence derived from committed engine decisions."""

from __future__ import annotations

import sqlite3
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.relationships_v1 import (
    RELATIONSHIP_METADATA_PATH,
    validate_relationship_metadata,
)
from open_brain_engine.storage.filesystem import atomic_replace, read_confined

from .contracts import LocalEngineContext


def relationship_metadata(connection: sqlite3.Connection) -> dict[str, Any] | None:
    from .relationships import entry
    from .source_store import source_metadata

    rows = connection.execute(
        "SELECT * FROM revision_relationships ORDER BY relationship_id COLLATE BINARY"
    ).fetchall()
    if not rows:
        return None
    value = {
        "schema_version": 1,
        "relationships": [entry(row) for row in rows],
        "decisions": [
            dict(row)
            for row in connection.execute(
                "SELECT "
                "decision_id,relationship_id,sequence,decision,"
                "relationship_version AS version,recorded_at,actor_id "
                "FROM relationship_decisions ORDER BY sequence"
            )
        ],
    }
    validate_relationship_metadata(
        portable_canonical_json_bytes(value), source_metadata(connection)
    )
    return value


def publish_relationship_metadata(
    connection: sqlite3.Connection, profile: LocalEngineContext
) -> None:
    value = relationship_metadata(connection)
    if value is None:
        return
    payload = portable_canonical_json_bytes(value)
    old = read_confined(
        root=profile.root,
        relative=RELATIONSHIP_METADATA_PATH,
        expected_root_identity=profile.root_identity,
    )
    if old != payload:
        atomic_replace(
            root=profile.root,
            relative=RELATIONSHIP_METADATA_PATH,
            data=payload,
            expected_root_identity=profile.root_identity,
        )
