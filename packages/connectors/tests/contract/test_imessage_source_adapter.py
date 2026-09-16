from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import PublicJobCaptureContext, open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_connectors.runtime.connectors import (
    ConnectorBudget,
    ConnectorBudgetLimits,
    ConnectorCaptureSink,
    ConnectorContractError,
    ConnectorFailureCode,
    ConnectorOutcome,
    ConnectorRunEvidence,
)
from open_brain_connectors.runtime.imessage import (
    ImessageCheckpoint,
    ImessageCheckpointStore,
    ImessagePageStatus,
    ImessageSourceAdapter,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

from .test_source_intake import _privacy


def test_imessage_adapter_reads_only_selected_conversation_from_sqlite(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    adapter = ImessageSourceAdapter()
    selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=database,
        conversation_id="chat-open-brain",
        owner_permission_status="granted",
    )

    page = adapter.page_from_sqlite(
        selection,
        database_path=database,
        privacy=_privacy(),
        limit=2,
    )

    assert page.status is ImessagePageStatus.READY
    assert page.preview is not None
    assert page.preview.selection == selection
    assert [record.content_type for record in page.preview.records] == ["message", "message"]
    assert page.preview.next_cursor == "row:2"
    assert "Other conversation" not in repr(page.preview.to_dict())
    assert "must not appear" not in repr(page.preview.to_dict())


def test_imessage_import_updates_revision_checkpoint_and_replays_safely(
    tmp_path: Path,
) -> None:
    database = _fixture_database(tmp_path)
    adapter = ImessageSourceAdapter()
    selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=database,
        conversation_id="chat-open-brain",
        owner_permission_status="granted",
    )
    original_page = adapter.page_from_sqlite(
        selection,
        database_path=database,
        privacy=_privacy(),
        limit=1,
    )
    assert original_page.preview is not None
    original_intake = _intakes_from_preview(adapter, selection, database, limit=1)[0]
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        ImessageCheckpoint.initial(selection),
        original_page,
        (original_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert checkpoint.committed_revision_identities == (
        original_intake.key.revision_identity(),
    )

    replayed_checkpoint, replayed_receipt = adapter.import_page(
        checkpoint,
        original_page,
        (original_intake,),
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE message SET text = ?, date_edited = ? WHERE ROWID = ?",
            ("Synthetic edited iMessage body.", 1790000008, 1),
        )
    changed_page = adapter.page_from_sqlite(
        selection,
        database_path=database,
        privacy=_privacy(),
        limit=1,
    )
    changed_intake = _intakes_from_preview(adapter, selection, database, limit=1)[0]
    changed_checkpoint, changed_receipt = adapter.import_page(
        checkpoint,
        changed_page,
        (changed_intake,),
        sink,
    )

    assert changed_page.preview is not None
    assert (
        original_page.preview.records[0].delivery_id
        == changed_page.preview.records[0].delivery_id
    )
    assert changed_receipt.outcome is ConnectorOutcome.COMPLETED
    assert changed_receipt.created_count == 1
    assert changed_checkpoint.committed_delivery_ids == checkpoint.committed_delivery_ids
    assert changed_checkpoint.committed_revision_identities == (
        changed_intake.key.revision_identity(),
    )


def test_imessage_deletions_are_revisioned_without_original_text(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    adapter = ImessageSourceAdapter()
    selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=database,
        conversation_id="chat-open-brain",
        owner_permission_status="granted",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE message SET text = NULL, date_deleted = ? WHERE ROWID = ?",
            (1790000010, 1),
        )

    page = adapter.page_from_sqlite(
        selection,
        database_path=database,
        privacy=_privacy(),
        limit=1,
    )
    intake = _intakes_from_preview(adapter, selection, database, limit=1)[0]

    assert page.preview is not None
    assert page.preview.records[0].content_type == "message_deleted"
    assert "Synthetic hello" not in intake.text
    assert "deleted" in intake.text


def test_imessage_checkpoint_store_is_metadata_only_and_atomic(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    adapter = ImessageSourceAdapter()
    selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=database,
        conversation_id="chat-open-brain",
        owner_permission_status="granted",
    )
    page = adapter.page_from_sqlite(
        selection,
        database_path=database,
        privacy=_privacy(),
        limit=1,
    )
    intakes = _intakes_from_preview(adapter, selection, database, limit=1)
    assert page.preview is not None
    checkpoint = ImessageCheckpoint.initial(selection).advance(
        page.preview,
        committed_delivery_ids=tuple(record.delivery_id for record in page.preview.records),
        committed_revision_identities=tuple(intake.key.revision_identity() for intake in intakes),
    )

    path = ImessageCheckpointStore(tmp_path / "checkpoints").save(checkpoint)

    assert ImessageCheckpointStore(tmp_path / "checkpoints").load(selection) == checkpoint
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic hello" not in path.read_text(encoding="utf-8")


def test_imessage_missing_database_unsupported_schema_and_permission_states(
    tmp_path: Path,
) -> None:
    adapter = ImessageSourceAdapter()
    missing = tmp_path / "missing-chat.db"
    selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=missing,
        conversation_id="chat-open-brain",
        owner_permission_status="granted",
    )

    assert (
        adapter.page_from_sqlite(selection, database_path=missing, privacy=_privacy()).status
        is ImessagePageStatus.MISSING_DATABASE
    )
    with pytest.raises(ConnectorContractError, match="permission"):
        adapter.conversation_selection(
            connection_id="account:imessage-fixture",
            database_path=missing,
            conversation_id="chat-open-brain",
            owner_permission_status="denied",
        )

    unsupported = tmp_path / "unsupported.db"
    with sqlite3.connect(unsupported) as connection:
        connection.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT)")
    unsupported_selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=unsupported,
        conversation_id="chat-open-brain",
        owner_permission_status="granted",
    )
    assert (
        adapter.page_from_sqlite(
            unsupported_selection,
            database_path=unsupported,
            privacy=_privacy(),
        ).status
        is ImessagePageStatus.UNSUPPORTED_SCHEMA
    )


def test_imessage_import_reports_distinct_non_ready_statuses(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    adapter = ImessageSourceAdapter()
    selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=database,
        conversation_id="chat-open-brain",
        owner_permission_status="granted",
    )
    checkpoint = ImessageCheckpoint.initial(selection)

    _unchanged, missing = adapter.import_page(
        checkpoint,
        adapter.missing_database_page(),
        (),
        _capture_sink(tmp_path),
    )
    _unchanged, unsupported = adapter.import_page(
        checkpoint,
        adapter.unsupported_schema_page(),
        (),
        _capture_sink(tmp_path),
    )
    _unchanged, denied = adapter.import_page(
        checkpoint,
        adapter.not_allowed_page(),
        (),
        _capture_sink(tmp_path),
    )

    assert missing.failure_code is ConnectorFailureCode.NOT_DISCOVERED
    assert unsupported.failure_code is ConnectorFailureCode.UNSUPPORTED_CAPABILITY
    assert denied.failure_code is ConnectorFailureCode.NOT_ALLOWED


def test_imessage_rejects_real_messages_database_and_cross_conversation_records(
    tmp_path: Path,
) -> None:
    database = _fixture_database(tmp_path)
    adapter = ImessageSourceAdapter()
    selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=database,
        conversation_id="chat-open-brain",
        owner_permission_status="granted",
    )
    other_selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=database,
        conversation_id="chat-other",
        owner_permission_status="granted",
    )
    other_intake = _intakes_from_preview(adapter, other_selection, database, limit=1)[0]

    with pytest.raises(ConnectorContractError, match="real imessage database"):
        adapter.conversation_selection(
            connection_id="account:imessage-fixture",
            database_path=Path.home() / "Library" / "Messages" / "chat.db",
            conversation_id="chat-open-brain",
            owner_permission_status="granted",
        )
    with pytest.raises(ConnectorContractError, match="invalid imessage import"):
        adapter.import_page(
            ImessageCheckpoint.initial(selection),
            adapter.page_from_sqlite(
                selection,
                database_path=database,
                privacy=_privacy(),
                limit=1,
            ),
            (other_intake,),
            _capture_sink(tmp_path),
        )


def test_imessage_records_attachment_presence_but_excludes_attachment_payloads(
    tmp_path: Path,
) -> None:
    database = _fixture_database(tmp_path)
    adapter = ImessageSourceAdapter()
    selection = adapter.conversation_selection(
        connection_id="account:imessage-fixture",
        database_path=database,
        conversation_id="chat-open-brain",
        owner_permission_status="granted",
    )

    intakes = _intakes_from_preview(adapter, selection, database, limit=3)

    assert all("attachment-guid" not in intake.text for intake in intakes)
    assert all(
        intake.title is not None and "Attachments" not in intake.title for intake in intakes
    )


def _intakes_from_preview(
    adapter: ImessageSourceAdapter,
    selection: SourceResourceSelection,
    database: Path,
    *,
    limit: int,
) -> tuple[SourceRecordIntake, ...]:
    records = adapter.records_from_sqlite(selection, database_path=database, limit=limit)
    assert records is not None
    return tuple(adapter.intake(selection, record, privacy=_privacy()) for record in records)


def _fixture_database(tmp_path: Path) -> Path:
    database = tmp_path / "synthetic-chat.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE chat (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT NOT NULL UNIQUE,
              display_name TEXT
            );
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT NOT NULL UNIQUE,
              text TEXT,
              date INTEGER,
              date_edited INTEGER,
              date_deleted INTEGER,
              cache_has_attachments INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE chat_message_join (
              chat_id INTEGER NOT NULL,
              message_id INTEGER NOT NULL
            );
            CREATE TABLE attachment (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT NOT NULL,
              filename TEXT NOT NULL
            );
            CREATE TABLE message_attachment_join (
              message_id INTEGER NOT NULL,
              attachment_id INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO chat (ROWID, guid, display_name) VALUES (?, ?, ?)",
            ((1, "chat-open-brain", "Open Brain Test"), (2, "chat-other", "Other Test")),
        )
        connection.executemany(
            """
            INSERT INTO message
              (ROWID, guid, text, date, date_edited, date_deleted, cache_has_attachments)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (1, "message-1", "Synthetic hello iMessage body.", 1790000001, 0, 0, 0),
                (2, "message-2", "Synthetic second iMessage body.", 1790000002, 0, 0, 1),
                (3, "message-3", "Other conversation must not appear.", 1790000003, 0, 0, 0),
            ),
        )
        connection.executemany(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            ((1, 1), (1, 2), (2, 3)),
        )
        connection.execute(
            "INSERT INTO attachment (ROWID, guid, filename) VALUES (?, ?, ?)",
            (1, "attachment-guid-secret", "/tmp/secret-attachment.png"),
        )
        connection.execute(
            "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)",
            (2, 1),
        )
    return database


def _capture_sink(tmp_path: Path) -> ConnectorCaptureSink:
    tasks = open_local_engine(compile_single_user_local(tmp_path / f"brain-{uuid4()}"))
    actor_id = f"actor_{uuid4()}"
    context = PublicJobCaptureContext.create(
        profile=tasks.profile,
        actor_id=actor_id,
        role_claim={
            "actor_id": actor_id,
            "capabilities": ["capture.accept"],
            "role_claim_id": f"role_claim_{uuid4()}",
            "role_id": f"role_{uuid4()}",
            "tenant_id": tasks.profile.tenant_id,
        },
    )
    sink = tasks.capture.public_job_sink(context)
    return ConnectorCaptureSink(
        sink,
        ConnectorBudget(ConnectorBudgetLimits(max_submissions=8)),
        ConnectorRunEvidence(),
    )
