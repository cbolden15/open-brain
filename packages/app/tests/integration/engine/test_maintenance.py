from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import TextPayload, open_local_engine
from open_brain_engine.engine.local import (
    ReadViewUnavailableError,
    StateSchemaUnavailableError,
    open_local_read_view,
)
from open_brain_engine.engine.maintenance import (
    inspect_live_search,
    live_search_is_healthy,
    read_maintenance_snapshot,
)

from open_brain.profile import compile_single_user_local, open_existing_single_user_local

from ._local_schema_fixtures import rematerialize_w2


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
    open_local_engine(compile_single_user_local(newer_root))
    database = newer_root / ".open-brain" / "state" / "phase1.sqlite3"
    lock_directory = newer_root / ".open-brain" / ".open-brain-locks"
    lock_before = _lock_bytes(lock_directory)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 3")
    after_schema_change = database.read_bytes()

    with pytest.raises(ReadViewUnavailableError, match="schema is newer"):
        open_local_read_view(open_existing_single_user_local(newer_root))

    assert sqlite3.connect(database).execute("PRAGMA user_version").fetchone() == (3,)
    lock_after = _lock_bytes(lock_directory)
    assert lock_after == lock_before
    assert sqlite3.connect(database).execute("PRAGMA user_version").fetchone() == (3,)
    assert database.read_bytes() == after_schema_change


def test_mutating_engine_rejects_newer_schema_before_writer_acquisition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    open_local_engine(compile_single_user_local(root))
    database = root / ".open-brain" / "state" / "phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 3")
    lock_directory = root / ".open-brain" / ".open-brain-locks"
    locks_before = _lock_bytes(lock_directory)

    with pytest.raises(StateSchemaUnavailableError, match="schema is newer"):
        open_local_engine(open_existing_single_user_local(root))

    assert _lock_bytes(lock_directory) == locks_before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)


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

    with pytest.raises(ReadViewUnavailableError, match="state schema is invalid"):
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
    open_local_engine(compile_single_user_local(root))
    tasks = open_local_engine(open_existing_single_user_local(root))
    captured = tasks.capture.accept(
        TextPayload("Synthetic legacy schema content"),
        delivery_id="maintenance.legacy.capture",
    )
    database = root / ".open-brain" / "state" / "phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        rematerialize_w2(connection, version=0)

    reopened = open_local_engine(open_existing_single_user_local(root))

    assert reopened.retrieval.fetch(captured.capture_id) is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


def test_maintenance_snapshot_projects_local_schema_and_search_indexes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root, starter_spaces=("Personal",)))
    tasks.capture.accept(
        TextPayload("Synthetic maintenance document"),
        delivery_id="maintenance.capture",
    )
    tasks.portability.rebuild_index()
    snapshot = read_maintenance_snapshot(open_existing_single_user_local(root))

    assert snapshot.schema.state == "current"
    assert snapshot.schema.version == 2
    assert snapshot.live_search.state == "current"
    assert snapshot.live_search.projection_count >= 1
    assert snapshot.live_search.projection_count == snapshot.live_search.identity_count
    assert snapshot.live_search.projection_count == snapshot.live_search.fts_count
    assert snapshot.live_search.result_ids_agree is True
    assert snapshot.live_search.contents_agree is True
    assert snapshot.index.state == "current"
    assert snapshot.index.generation is not None
    assert snapshot.index.document_count >= 1


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
