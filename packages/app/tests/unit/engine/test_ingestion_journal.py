"""Schema-10 journal contract and cutover tests."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    AdmissionLimits,
    BrainEngine,
    CaptureAction,
    CaptureCustodyReceipt,
    CaptureFault,
    CaptureSubmission,
    InjectedFault,
    JournalEnvelope,
    JournalOperationError,
    StateSchemaUnavailableError,
    TextPayload,
    coordinate_local_migration,
    local_schema,
    verify_capture_custody_receipt,
)
from open_brain_engine.engine.ingestion import JournalCapacityError
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE, inspect_phase1_state
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import SingleUserLocalProfile, compile_single_user_local


def _clock() -> datetime:
    return datetime.now(UTC)


def _owner(profile: SingleUserLocalProfile) -> EffectiveAuthority:
    return EffectiveAuthority(
        principal_id=profile.owner_actor_id,
        session_id="journal-test-owner",
        capabilities=frozenset(),
        space_ids=None,
        owner=True,
    )


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


def test_compacted_replay_preserves_requested_tier_after_privacy_narrowing(
    tmp_path: Path,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(
        profile,
        boundary_classifier=lambda _: PrivacyTier.SECRET,
    )
    submission = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("narrowed replay"),
        delivery_id="journal.narrowed-replay",
        privacy_tier=PrivacyTier.WORK,
    )

    first = engine.capture.submit(submission)
    replay = engine.capture.submit(submission)

    assert first.requested_tier is PrivacyTier.WORK
    assert first.final_admitted_tier is PrivacyTier.SECRET
    assert replay.duplicate is True
    assert replay.requested_tier is PrivacyTier.WORK
    assert replay.final_admitted_tier is PrivacyTier.SECRET


def test_quarantined_item_is_not_retried_without_owner_action(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    space = engine.inbox.create_space("Quarantine", delivery_id="journal.quarantine.space")
    submission = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("invalid canonical destination"),
        delivery_id="journal.quarantine",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    engine.ingestion.enqueue(submission)
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        connection.execute("DELETE FROM spaces WHERE space_id = ?", (space.space_id,))

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


def test_owner_journal_operations_are_metadata_only_and_discard_is_tombstoned(
    tmp_path: Path,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    space = engine.inbox.create_space("Quarantine", delivery_id="journal.ops.space")
    submission = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("owner operation content must not be displayed"),
        delivery_id="journal.owner-ops",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    engine.ingestion.enqueue(submission)
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        row = connection.execute(
            "SELECT * FROM spaces WHERE space_id = ?", (space.space_id,)
        ).fetchone()
        columns = tuple(item[1] for item in connection.execute("PRAGMA table_info(spaces)"))
        connection.execute("DELETE FROM spaces WHERE space_id = ?", (space.space_id,))
    assert engine.recover() == 0

    journal = engine.tasks.journal
    assert journal is not None
    with pytest.raises(JournalOperationError, match="owner_required"):
        journal.status(
            authority=EffectiveAuthority(
                principal_id="agent",
                session_id="untrusted",
                capabilities=frozenset(),
                space_ids=None,
            )
        )
    item = journal.status(authority=_owner(profile))[0]
    assert item.delivery_id == submission.delivery_id
    assert "content" not in repr(item)
    summary = journal.summary(authority=_owner(profile))
    assert summary.pending_count == 0
    assert summary.quarantined_count == 1
    assert summary.retained_bytes > 0
    assert summary.last_failure_code == "invalid"

    journal.retry(submission.delivery_id, authority=_owner(profile))
    assert journal.status(authority=_owner(profile))[0].state == "queued"
    journal.drain(authority=_owner(profile))
    assert journal.status(authority=_owner(profile))[0].state == "quarantined"
    journal.discard(submission.delivery_id, reason="owner_requested", authority=_owner(profile))
    assert journal.status(authority=_owner(profile)) == ()
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute(
            "SELECT count(*) FROM capture_ingestion_tombstones WHERE delivery_id = ?",
            (submission.delivery_id,),
        ).fetchone() == (1,)
        assert row is not None
        connection.execute(
            f"INSERT INTO spaces ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            row,
        )
    with pytest.raises(ValueError, match="delivery discarded"):
        engine.ingestion.enqueue(submission)


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


def test_journal_capacity_duplicate_conflict_and_fifo_drain(tmp_path: Path) -> None:
    from open_brain_engine.engine.capture import DeliveryConflict

    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile, admission_limits=AdmissionLimits(max_journal_items=2))
    first = CaptureSubmission.for_local_owner(
        profile=profile, payload=TextPayload("first FIFO"), delivery_id="journal.fifo.first"
    )
    second = CaptureSubmission.for_local_owner(
        profile=profile, payload=TextPayload("second FIFO"), delivery_id="journal.fifo.second"
    )
    third = CaptureSubmission.for_local_owner(
        profile=profile, payload=TextPayload("third capacity"), delivery_id="journal.fifo.third"
    )
    first_custody = engine.ingestion.enqueue(first)
    engine.ingestion.enqueue(second)
    assert engine.ingestion.enqueue(first) == first_custody
    with pytest.raises(DeliveryConflict):
        engine.ingestion.enqueue(
            CaptureSubmission.for_local_owner(
                profile=profile,
                payload=TextPayload("changed digest"),
                delivery_id=first.delivery_id,
            )
        )
    with pytest.raises(JournalCapacityError, match="journal capacity exceeded"):
        engine.ingestion.enqueue(third)

    with engine._writer_lease.acquire_shared_writer():
        receipts = engine.ingestion.drain_locked()
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        deliveries = {
            row[0]: row[1]
            for row in connection.execute("SELECT capture_id, delivery_id FROM captures")
        }
    assert tuple(deliveries[receipt.capture_id] for receipt in receipts) == (
        first.delivery_id,
        second.delivery_id,
    )


def test_simultaneous_enqueue_assigns_one_order_and_drains_in_that_order(
    tmp_path: Path,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    submissions = tuple(
        CaptureSubmission.for_local_owner(
            profile=profile,
            payload=TextPayload(f"simultaneous journal item {index}"),
            delivery_id=f"journal.simultaneous.{index}",
        )
        for index in range(8)
    )
    start = threading.Barrier(len(submissions))

    def enqueue(submission: CaptureSubmission) -> CaptureCustodyReceipt:
        start.wait()
        result = engine.ingestion.enqueue(submission)
        assert isinstance(result, CaptureCustodyReceipt)
        return result

    with ThreadPoolExecutor(max_workers=len(submissions)) as executor:
        custody = tuple(executor.map(enqueue, submissions))

    assert len({item.ingestion_id for item in custody}) == len(submissions)
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        ordered_deliveries = tuple(
            row[0]
            for row in connection.execute(
                "SELECT delivery_id FROM capture_ingestion_items ORDER BY journal_sequence"
            )
        )
        sequences = tuple(
            row[0]
            for row in connection.execute(
                "SELECT journal_sequence FROM capture_ingestion_items ORDER BY journal_sequence"
            )
        )
    assert len(ordered_deliveries) == len(submissions)
    assert sequences == tuple(range(sequences[0], sequences[0] + len(submissions)))

    with engine._writer_lease.acquire_shared_writer():
        receipts = engine.ingestion.drain_locked()
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        capture_deliveries = {
            row[0]: row[1]
            for row in connection.execute("SELECT capture_id, delivery_id FROM captures")
        }
    assert tuple(capture_deliveries[item.capture_id] for item in receipts) == ordered_deliveries


def test_corrupt_envelope_is_quarantined_and_owner_status_paginates(
    tmp_path: Path,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    submissions = tuple(
        CaptureSubmission.for_local_owner(
            profile=profile,
            payload=TextPayload(
                "corruptonlyneedle" if index == 0 else f"healthy pagination item {index}"
            ),
            delivery_id=f"journal.pagination.{index}",
        )
        for index in range(3)
    )
    for submission in submissions:
        engine.ingestion.enqueue(submission)
    journal = engine.tasks.journal
    assert journal is not None
    first_page = journal.status(authority=_owner(profile), limit=2)
    second_page = journal.status(
        authority=_owner(profile),
        limit=2,
        after_sequence=first_page[-1].journal_sequence,
    )
    assert tuple(item.delivery_id for item in (*first_page, *second_page)) == tuple(
        item.delivery_id for item in submissions
    )
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'capture_ingestion_payloads_update_immutable'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER capture_ingestion_payloads_update_immutable")
        connection.execute(
            "UPDATE capture_ingestion_payloads SET envelope_bytes = ? WHERE delivery_id = ?",
            (b"corrupt-envelope", submissions[0].delivery_id),
        )
        connection.execute(trigger_sql)

    with engine._writer_lease.acquire_shared_writer():
        receipts = engine.ingestion.drain_locked()
    assert len(receipts) == 2
    quarantined = journal.status(authority=_owner(profile), limit=1)
    assert len(quarantined) == 1
    assert quarantined[0].delivery_id == submissions[0].delivery_id
    assert quarantined[0].state == "quarantined"
    assert (
        journal.status(
            authority=_owner(profile),
            limit=1,
            after_sequence=quarantined[0].journal_sequence,
        )
        == ()
    )
    assert not engine.retrieval.search("corruptonlyneedle")
