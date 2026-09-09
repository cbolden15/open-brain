from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.engine import TextPayload, open_local_engine
from open_brain_engine.engine.local import (
    ReadViewUnavailableError,
    StateSchemaUnavailableError,
    open_local_read_view,
)
from open_brain_engine.engine.maintenance import (
    APPLIANCE_BACKUP_EVIDENCE,
    APPLIANCE_EXPORT_EVIDENCE,
    inspect_live_search,
    live_search_is_healthy,
    read_maintenance_snapshot,
)

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.appliance_init import initialize_appliance


def test_open_local_read_view_rejects_absent_and_newer_schema_without_mutation(
    tmp_path: Path,
) -> None:
    absent_root = tmp_path / "absent"
    compile_single_user_local(absent_root)

    with pytest.raises(ReadViewUnavailableError, match="schema is absent"):
        open_local_read_view(open_existing_single_user_local(absent_root))

    assert not (absent_root / ".open-brain" / "state" / "phase1.sqlite3").exists()
    assert not (absent_root / ".open-brain" / ".open-brain-locks").exists()

    newer_root = tmp_path / "newer"
    initialize_appliance(newer_root)
    database = newer_root / ".open-brain" / "state" / "phase1.sqlite3"
    lock_directory = newer_root / ".open-brain" / ".open-brain-locks"
    lock_before = _lock_bytes(lock_directory)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 2")
    after_schema_change = database.read_bytes()

    with pytest.raises(ReadViewUnavailableError, match="schema is newer"):
        open_local_read_view(open_existing_single_user_local(newer_root))

    assert sqlite3.connect(database).execute("PRAGMA user_version").fetchone() == (2,)
    lock_after = _lock_bytes(lock_directory)
    assert lock_after == lock_before
    assert sqlite3.connect(database).execute("PRAGMA user_version").fetchone() == (2,)
    assert database.read_bytes() == after_schema_change


def test_mutating_engine_rejects_newer_schema_before_writer_acquisition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root)
    database = root / ".open-brain" / "state" / "phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 2")
    lock_directory = root / ".open-brain" / ".open-brain-locks"
    locks_before = _lock_bytes(lock_directory)

    with pytest.raises(StateSchemaUnavailableError, match="schema is newer"):
        open_local_engine(open_existing_single_user_local(root))

    assert _lock_bytes(lock_directory) == locks_before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


def test_read_only_view_rejects_missing_live_search_schema_without_adopting_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    tasks.capture.accept(
        TextPayload("Synthetic read-only search state"),
        delivery_id="maintenance.read-only.search",
    )
    database = root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        for trigger in (
            "search_documents_result_id_immutable",
            "search_documents_fts_insert",
            "search_documents_fts_update",
            "search_documents_fts_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE search_documents_fts")
        connection.execute("DROP TABLE search_fts_identity")
    locks_before = _lock_bytes(root / ".open-brain/.open-brain-locks")

    with pytest.raises(ReadViewUnavailableError, match="live search schema is unavailable"):
        open_local_read_view(open_existing_single_user_local(root))

    assert _lock_bytes(root / ".open-brain/.open-brain-locks") == locks_before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'search_documents_fts'"
        ).fetchone() is None
        assert connection.execute("SELECT count(*) FROM search_documents").fetchone() == (1,)


def test_mutating_engine_migrates_legacy_schema_without_replacing_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root)
    tasks = open_local_engine(open_existing_single_user_local(root))
    captured = tasks.capture.accept(
        TextPayload("Synthetic legacy schema content"),
        delivery_id="maintenance.legacy.capture",
    )
    database = root / ".open-brain" / "state" / "phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 0")

    reopened = open_local_engine(open_existing_single_user_local(root))

    assert reopened.retrieval.fetch(captured.capture_id) is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


def test_maintenance_snapshot_projects_schema_index_writer_backup_export_and_queue_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Personal",))
    tasks = open_local_engine(compile_single_user_local(root))
    tasks.capture.accept(
        TextPayload("Synthetic maintenance document"),
        delivery_id="maintenance.capture",
    )
    tasks.portability.rebuild_index()
    (root / APPLIANCE_BACKUP_EVIDENCE).write_bytes(
        canonical_json_bytes(
                {
                    "backup_id": "backup_" + "a" * 64,
                    "created_at": datetime(2026, 9, 1, 12, 0, tzinfo=UTC).isoformat().replace(
                        "+00:00", "Z"
                    ),
                "manifest_digest_sha256": "a" * 64,
                "schema_version": 1,
            }
        )
    )
    (root / APPLIANCE_EXPORT_EVIDENCE).write_bytes(
        canonical_json_bytes(
                {
                    "created_at": datetime(2026, 9, 1, 12, 5, tzinfo=UTC).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "export_id": "export_" + "b" * 64,
                    "manifest_digest_sha256": "b" * 64,
                    "schema_version": 1,
                }
        )
    )

    script = """
import json
import sys
from pathlib import Path

from open_brain_engine.storage.locks import FileLease

root = Path(sys.argv[1])
with FileLease(root / ".open-brain", "synthetic-holder").acquire_shared_writer():
    sys.stdout.write("held\\n")
    sys.stdout.flush()
    sys.stdin.readline()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(root)],
        cwd=Path(__file__).parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdin is not None
    assert holder.stdout.readline().strip() == "held"
    snapshot = read_maintenance_snapshot(open_existing_single_user_local(root))
    holder.stdin.write("\n")
    holder.stdin.flush()
    completed = holder.wait(timeout=5)
    stderr = holder.stderr.read() if holder.stderr is not None else ""

    assert snapshot.schema.state == "current"
    assert snapshot.schema.version == 1
    assert snapshot.live_search.state == "current"
    assert snapshot.live_search.projection_count >= 1
    assert snapshot.live_search.projection_count == snapshot.live_search.identity_count
    assert snapshot.live_search.projection_count == snapshot.live_search.fts_count
    assert snapshot.live_search.result_ids_agree is True
    assert snapshot.live_search.contents_agree is True
    assert snapshot.index.state == "current"
    assert snapshot.index.generation is not None
    assert snapshot.index.document_count >= 1
    assert completed == 0, stderr
    assert snapshot.writer.held_count == 1
    assert snapshot.writer.held_leases == ("shared-writer",)
    assert snapshot.backup.state == "present"
    assert snapshot.backup.operation_id == "backup_" + "a" * 64
    assert snapshot.export.state == "present"
    assert snapshot.export.operation_id == "export_" + "b" * 64
    assert snapshot.queue.state == "unavailable"
    assert snapshot.queue.pending_count == 0


def test_live_search_diagnostics_detect_stale_content_without_using_portable_snapshot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    tasks.capture.accept(
        TextPayload("Synthetic diagnostic content"),
        delivery_id="maintenance.search.capture",
    )
    profile = open_existing_single_user_local(root)

    healthy = inspect_live_search(profile)

    assert healthy.state == "current"
    assert healthy.projection_count == healthy.identity_count == healthy.fts_count == 1
    assert live_search_is_healthy(profile) is True
    assert not (root / ".open-brain/indexes/search.sqlite3").exists()

    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.execute("UPDATE search_documents_fts SET body = 'stale synthetic bytes'")

    stale = inspect_live_search(profile)

    assert stale.state == "invalid"
    assert stale.result_ids_agree is True
    assert stale.contents_agree is False
    assert live_search_is_healthy(profile) is False


@pytest.mark.parametrize("column", ("result_id", "title", "body"))
def test_live_search_diagnostics_detect_null_fts_content(
    tmp_path: Path,
    column: str,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    tasks.capture.accept(
        TextPayload("Synthetic diagnostic content"),
        delivery_id=f"maintenance.search.null.{column}",
    )
    profile = open_existing_single_user_local(root)
    update_sql = {
        "result_id": "UPDATE search_documents_fts SET result_id = NULL",
        "title": "UPDATE search_documents_fts SET title = NULL",
        "body": "UPDATE search_documents_fts SET body = NULL",
    }[column]

    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.execute(update_sql)

    stale = inspect_live_search(profile)

    assert stale.state == "invalid"
    assert stale.contents_agree is False
    assert live_search_is_healthy(profile) is False


def _lock_bytes(lock_directory: Path) -> dict[str, bytes]:
    if not lock_directory.exists():
        return {}
    return {path.name: path.read_bytes() for path in lock_directory.iterdir()}
