"""Schema-10 journal contract and cutover tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.engine import (
    AdmissionLimits,
    BrainEngine,
    CaptureAction,
    CaptureCustodyReceipt,
    CaptureFault,
    CaptureSubmission,
    InjectedFault,
    JournalEnvelope,
    StateSchemaUnavailableError,
    TextPayload,
    coordinate_local_migration,
    local_schema,
    verify_capture_custody_receipt,
)
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE, inspect_phase1_state
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS

from open_brain.profile import compile_single_user_local


def _clock() -> datetime:
    return datetime.now(UTC)


def test_journal_v1_round_trips_exact_normalized_submission(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    submission = CaptureSubmission.for_local_owner(
        profile=profile, payload=TextPayload("journal contract"), delivery_id="journal.contract"
    )
    envelope = JournalEnvelope(submission, submission.privacy)

    assert JournalEnvelope.from_bytes(envelope.to_bytes()) == envelope
    with pytest.raises(ValueError, match="journal envelope"):
        JournalEnvelope.from_bytes(envelope.to_bytes() + b" ")


def test_custody_receipt_is_closed_and_privacy_narrowing_only(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    submission = CaptureSubmission.for_local_owner(
        profile=profile, payload=TextPayload("custody receipt"), delivery_id="journal.receipt"
    )
    receipt = CaptureCustodyReceipt(
        ingestion_id="ingestion_" + str(uuid4()),
        brain_id=derive_brain_id(profile.tenant_id),
        issuer_epoch=1,
        delivery_id=submission.delivery_id,
        request_sha256=submission.request_sha256(),
        requested_tier=submission.requested_tier,
        final_admitted_tier=submission.requested_tier,
        queued_at="2026-09-22T12:00:00Z",
    )

    assert verify_capture_custody_receipt(receipt.to_dict()) == receipt
    invalid = receipt.to_dict() | {"unexpected": None}
    with pytest.raises(ValueError, match="capture custody receipt"):
        verify_capture_custody_receipt(invalid)


def test_schema_nine_migrates_only_through_coordinator_and_preserves_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 9)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:9])
        engine = BrainEngine.open(profile)
        capture = engine.capture.accept(
            TextPayload("schema nine"), delivery_id="journal.schema-nine"
        )

    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", 9)
    with pytest.raises(StateSchemaUnavailableError, match="ingestion migration"):
        BrainEngine.open(profile)

    coordinate_local_migration(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute("SELECT capture_id FROM captures").fetchone() == (
            capture.capture_id,
        )
        assert connection.execute("SELECT count(*) FROM capture_ingestion_pending").fetchone() == (
            0,
        )
        connection.execute(
            "INSERT INTO capture_ingestion_items (ingestion_id, delivery_id, request_sha256, "
            "envelope_sha256, submission_path, byte_count, queued_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "ingestion_" + str(uuid4()),
                "journal.immutable",
                "0" * 64,
                "1" * 64,
                "owner",
                1,
                "2026-09-22T12:00:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="ingestion item is immutable"):
            connection.execute("UPDATE capture_ingestion_items SET byte_count=2")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO capture_ingestion_items (ingestion_id, delivery_id, request_sha256, "
                "envelope_sha256, submission_path, byte_count, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "ingestion_" + str(uuid4()),
                    "journal.bad-byte-count",
                    "0" * 64,
                    "1" * 64,
                    "owner",
                    0,
                    "2026-09-22T12:00:00Z",
                ),
            )


def test_portable_export_refuses_active_journal_payload(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    submission = CaptureSubmission.for_local_owner(
        profile=profile, payload=TextPayload("export gate"), delivery_id="journal.export"
    )
    envelope = JournalEnvelope(submission, submission.privacy)
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        connection.execute(
            "INSERT INTO capture_ingestion_items (ingestion_id, delivery_id, request_sha256, "
            "envelope_sha256, submission_path, byte_count, queued_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "ingestion_" + str(uuid4()),
                submission.delivery_id,
                submission.request_sha256(),
                envelope.sha256,
                submission.submission_path.value,
                len(envelope.to_bytes()),
                "2026-09-22T12:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO capture_ingestion_payloads VALUES (?, ?)",
            (submission.delivery_id, envelope.to_bytes()),
        )
        connection.execute(
            "INSERT INTO capture_ingestion_events (delivery_id, event_kind, attempt_number, "
            "receipt_json, recorded_at) "
            "VALUES (?, 'queued', 0, ?, ?)",
            (submission.delivery_id, '{"status":"queued"}', "2026-09-22T12:00:00Z"),
        )

    with pytest.raises(ValueError, match="^ingestion_pending$"):
        engine.portability.export(
            tmp_path / "portable", export_id="export_00000000-0000-4000-8000-000000000010"
        )


@pytest.mark.parametrize(
    "fault",
    (
        CaptureFault.AFTER_JOURNAL_COMMIT,
        CaptureFault.AFTER_CAPTURE_RESERVATION,
        CaptureFault.AFTER_CANONICAL_COMPLETION,
        CaptureFault.AFTER_JOURNAL_TERMINAL_EVENT,
    ),
)
def test_journal_crash_boundaries_recover_once_without_pending_content(
    tmp_path: Path, fault: CaptureFault
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    submission = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("journal crash boundary needle"),
        delivery_id="journal.crash." + fault.value,
    )
    engine = BrainEngine.open(profile, faults={fault})

    if fault is CaptureFault.AFTER_JOURNAL_COMMIT:
        with pytest.raises(InjectedFault):
            engine.ingestion.enqueue(submission)
    else:
        engine.ingestion.enqueue(submission)
        with pytest.raises(InjectedFault):
            engine.recover()

    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        expected_searches = (
            0
            if fault in {CaptureFault.AFTER_JOURNAL_COMMIT, CaptureFault.AFTER_CAPTURE_RESERVATION}
            else 1
        )
        assert connection.execute("SELECT count(*) FROM search_documents").fetchone() == (
            expected_searches,
        )
        assert connection.execute("SELECT count(*) FROM capture_ingestion_items").fetchone() == (1,)

    recovered = BrainEngine.open(profile)
    assert len(recovered.retrieval.search("journal crash boundary needle")) == 1
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM capture_ingestion_pending").fetchone() == (
            0,
        )
        assert connection.execute("SELECT count(*) FROM capture_ingestion_items").fetchone() == (0,)


def test_replay_during_canonical_terminalization_returns_original_custody(
    tmp_path: Path,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    submission = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("canonical overlap replay"),
        delivery_id="journal.overlap",
    )
    engine = BrainEngine.open(profile, faults={CaptureFault.AFTER_CANONICAL_COMPLETION})
    custody = engine.ingestion.enqueue(submission)

    with pytest.raises(InjectedFault):
        engine.recover()

    assert engine.ingestion.enqueue(submission) == custody
    recovered = BrainEngine.open(profile)
    assert len(recovered.retrieval.search("canonical overlap replay")) == 1


def test_quarantined_item_is_not_retried_without_owner_action(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    submission = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("invalid canonical destination"),
        delivery_id="journal.quarantine",
        action=CaptureAction.CANONICAL_NOTE,
        space_id="space_00000000-0000-4000-8000-000000000099",
    )
    engine.ingestion.enqueue(submission)

    assert engine.recover() == 0
    status = engine.ingestion.status()
    assert len(status) == 1
    assert status[0].state == "quarantined"
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        event_count = connection.execute(
            "SELECT count(*) FROM capture_ingestion_events WHERE delivery_id = ?",
            (submission.delivery_id,),
        ).fetchone()[0]

    assert engine.recover() == 0
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute(
            "SELECT count(*) FROM capture_ingestion_events WHERE delivery_id = ?",
            (submission.delivery_id,),
        ).fetchone() == (event_count,)


def test_writer_drain_respects_aggregate_batch_bytes(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    submissions = tuple(
        CaptureSubmission.for_local_owner(
            profile=profile,
            payload=TextPayload(f"bounded journal item {index}"),
            delivery_id=f"journal.batch.{index}",
        )
        for index in range(2)
    )
    item_bytes = max(
        len(JournalEnvelope(submission, submission.privacy).to_bytes())
        for submission in submissions
    )
    engine = BrainEngine.open(
        profile,
        admission_limits=AdmissionLimits(
            max_journal_item_bytes=item_bytes,
            max_journal_batch_bytes=item_bytes,
        ),
    )
    for submission in submissions:
        engine.ingestion.enqueue(submission)

    assert engine.recover() == 1
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM capture_ingestion_pending").fetchone() == (
            1,
        )
    assert engine.recover() == 1
