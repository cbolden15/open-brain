from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from open_brain_legacy.review.store import (
    REVIEW_SCHEMA_VERSION,
    SqliteReviewStore,
    inspect_review_schema,
)


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 16, 20, 0, tzinfo=UTC)


def test_review_schema_inspection_is_read_only_and_detects_checksum_drift(
    tmp_path: Path,
) -> None:
    with SqliteReviewStore(
        root=tmp_path,
        database_name="review.sqlite3",
        clock=FixedClock(),
    ):
        pass

    healthy = inspect_review_schema(root=tmp_path, database_name="review.sqlite3")
    writable = sqlite3.connect(tmp_path / "review.sqlite3")
    try:
        writable.execute("UPDATE review_schema_migrations SET checksum = ?", ("0" * 64,))
        writable.commit()
    finally:
        writable.close()
    drifted = inspect_review_schema(root=tmp_path, database_name="review.sqlite3")

    assert healthy.version == REVIEW_SCHEMA_VERSION
    assert healthy.valid is True
    assert drifted.version == REVIEW_SCHEMA_VERSION
    assert drifted.valid is False
