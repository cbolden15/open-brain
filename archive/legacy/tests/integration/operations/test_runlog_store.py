from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.runlog import RunMetadata
from open_brain_legacy.operations.runlog_store import FilesystemRunLogStore, RunLogStoreError
from open_brain_legacy.services.entrypoints import run_legacy_cli as run


def _time(minute: int = 0) -> datetime:
    return datetime(2026, 8, 25, 12, minute, tzinfo=UTC)


def _metadata(*, finished: datetime | None = None) -> RunMetadata:
    end = finished or _time(1)
    return RunMetadata.create(
        job_id="JOB-004",
        started_at=end - timedelta(seconds=2),
        finished_at=end,
        exit_code=0,
        error_class=None,
        metrics={},
    )


def test_runlog_store_round_trips_and_deduplicates_exact_metadata(tmp_path: Path) -> None:
    store = FilesystemRunLogStore(root=tmp_path)
    metadata = _metadata()

    first = store.append(metadata)
    replay = store.append(metadata)
    reports = store.reports(now=_time(2), window_seconds=300)

    assert first == replay
    assert reports == (metadata,)
    assert len(tuple((tmp_path / "runlog" / "JOB-004").glob("*.json"))) == 1


def test_runlog_store_rejects_corrupt_metadata_without_residue(tmp_path: Path) -> None:
    store = FilesystemRunLogStore(root=tmp_path)
    store.append(_metadata())
    record = next((tmp_path / "runlog" / "JOB-004").glob("*.json"))
    record.write_text('{"schema_version":1,"broken":true}')

    with pytest.raises(RunLogStoreError, match="invalid run metadata"):
        store.reports(now=_time(2), window_seconds=300)


def test_scheduler_process_context_persists_one_metadata_only_report(tmp_path: Path) -> None:
    roots = {
        name: tmp_path / name
        for name in ("work", "personal", "capture", "saved", "state", "backup")
    }
    for root in roots.values():
        root.mkdir()
    database_root = roots["state"] / "events"
    database_root.mkdir()
    with sqlite3.connect(database_root / "events.sqlite3") as connection:
        connection.execute("CREATE TABLE events(value INTEGER NOT NULL)")
    job = get_job("JOB-004")
    environment = {
        "OPEN_BRAIN_STATE_ROOT": str(roots["state"]),
        "OPEN_BRAIN_WORK_ROOT": str(roots["work"]),
        "OPEN_BRAIN_PERSONAL_ROOT": str(roots["personal"]),
        "OPEN_BRAIN_CAPTURE_ROOT": str(roots["capture"]),
        "OPEN_BRAIN_SAVED_CONTENT_ROOT": str(roots["saved"]),
        "OPEN_BRAIN_BACKUP_ROOT": str(roots["backup"]),
        "OPEN_BRAIN_JOB_ID": job.id,
    }

    exit_code = run(job.command[1:], environment=environment)
    reports = FilesystemRunLogStore(root=roots["state"]).reports(
        now=datetime.now(UTC) + timedelta(seconds=1),
        window_seconds=300,
    )

    assert exit_code == 0
    assert len(reports) == 1
    assert reports[0].job_id == job.id
    assert reports[0].exit_code == 0
    assert reports[0].metrics == ()
