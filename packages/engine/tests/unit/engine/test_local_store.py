from __future__ import annotations

import sqlite3

import pytest
from open_brain_engine.engine.local_schema import _prepare_local_schema
from open_brain_engine.storage.sqlite import SchemaError


def test_partial_legacy_route_layout_is_refused_without_resetting_operations() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE route_operations (
            delivery_id TEXT PRIMARY KEY,
            request_sha256 TEXT NOT NULL,
            capture_id TEXT NOT NULL,
            space_id TEXT NOT NULL,
            receipt_id TEXT NOT NULL UNIQUE,
            recorded_at TEXT NOT NULL,
            stage INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    capture_id = "capture_123e4567-e89b-42d3-a456-426614174100"
    rows = (
        (
            "delivery.first",
            "1" * 64,
            capture_id,
            "space_123e4567-e89b-42d3-a456-426614174004",
            "receipt_123e4567-e89b-42d3-a456-426614174120",
            "2026-08-30T12:00:00Z",
            1,
        ),
        (
            "delivery.second",
            "2" * 64,
            capture_id,
            "space_123e4567-e89b-42d3-a456-426614174014",
            "receipt_123e4567-e89b-42d3-a456-426614174121",
            "2026-08-30T12:01:00Z",
            1,
        ),
    )
    connection.executemany(
        """
        INSERT INTO route_operations (
            delivery_id, request_sha256, capture_id, space_id,
            receipt_id, recorded_at, stage
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    before = list(connection.iterdump())
    with pytest.raises(SchemaError, match="invalid"):
        _prepare_local_schema(connection, created=False, setup_required=True)
    assert list(connection.iterdump()) == before
    assert [row[0] for row in connection.execute("SELECT stage FROM route_operations")] == [1, 1]
    connection.close()
