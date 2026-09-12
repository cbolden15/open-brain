from __future__ import annotations

import stat
from pathlib import Path

import pytest

from open_brain_legacy.operations.replay_journal import SqliteReplayJournal
from open_brain_legacy.operations.writer_jobs import (
    JobRunDisposition,
    JobRunResult,
    ScheduledEffect,
    WriterJobError,
)
from tests.unit.storage._factories import FixedClock

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _result(*, digest: str = _DIGEST_A, effect_count: int = 1) -> JobRunResult:
    return JobRunResult(
        job_id="JOB-011",
        replay_key="2026-08-16T12:00Z",
        request_digest_sha256=digest,
        disposition=JobRunDisposition.APPLIED,
        effect=ScheduledEffect.BACKUP_SNAPSHOT,
        effect_count=effect_count,
        review_items_queued=0,
        approved_inputs_applied=0,
    )


def test_begin_is_idempotent_and_rejects_digest_conflict(tmp_path: Path) -> None:
    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        journal.begin("JOB-011", "2026-08-16T12:00Z", _DIGEST_A)
        journal.begin("JOB-011", "2026-08-16T12:00Z", _DIGEST_A)

        with pytest.raises(WriterJobError, match="replay digest conflict"):
            journal.begin("JOB-011", "2026-08-16T12:00Z", _DIGEST_B)

        assert journal.completed("JOB-011", "2026-08-16T12:00Z") is None


def test_complete_upserts_without_begin_and_persists_result(tmp_path: Path) -> None:
    expected = _result()
    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        journal.complete(expected)
        journal.complete(expected)
        assert journal.completed(expected.job_id, expected.replay_key) == expected

    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as reopened:
        assert reopened.completed(expected.job_id, expected.replay_key) == expected

    database = tmp_path / "operations" / "replay-journal.sqlite3"
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_complete_promotes_matching_pending_run(tmp_path: Path) -> None:
    expected = _result()
    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        journal.begin(expected.job_id, expected.replay_key, expected.request_digest_sha256)
        journal.complete(expected)

        assert journal.completed(expected.job_id, expected.replay_key) == expected


def test_complete_rejects_pending_digest_conflict(tmp_path: Path) -> None:
    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        journal.begin("JOB-011", "2026-08-16T12:00Z", _DIGEST_A)

        with pytest.raises(WriterJobError, match="replay digest conflict"):
            journal.complete(_result(digest=_DIGEST_B))

        assert journal.completed("JOB-011", "2026-08-16T12:00Z") is None


def test_complete_rejects_conflicting_completed_result(tmp_path: Path) -> None:
    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        journal.complete(_result())

        with pytest.raises(WriterJobError, match="replay result conflict"):
            journal.complete(_result(effect_count=2))

        assert journal.completed("JOB-011", "2026-08-16T12:00Z") == _result()
