"""Progressed, allocated and baseline queue items stay protected and replayable."""

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    CaptureCustodyReceipt,
    CaptureFault,
    CaptureReceipt,
    CaptureSubmission,
    InjectedFault,
    TextPayload,
)
from open_brain_engine.engine.custody_recovery import CaptureCustodyRecoveryPlan
from open_brain_engine.engine.journal_recovery import CaptureJournalRecoveryPlan
from open_brain_engine.engine.owner_replay import replay_owner_recovery_chain
from open_brain_engine.engine.recovery_journal import RecoveryBaseline, RecoveryHead, RecoveryRecord
from open_brain_engine.engine.recovery_protection import (
    RecoveryProtectionGuard,
    RecoveryProtectionPendingError,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_custody_replay import _snapshot
from packages.app.tests.unit.engine.test_owner_replay import _restore
from packages.app.tests.unit.engine.test_recovery_protection import (
    SyntheticPort,
)
from packages.app.tests.unit.engine.test_recovery_protection import (
    guarded as guarded,
)


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return EffectiveAuthority(
        engine.profile.owner_actor_id, "pending-recovery", frozenset(), None, owner=True,
    )


def _fail_next_materialization(engine: BrainEngine) -> None:
    original = engine._materialize_capture_locked
    calls = {"count": 0}

    def flaky(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("synthetic transient failure")
        return original(*args, **kwargs)

    engine._materialize_capture_locked = flaky  # type: ignore[method-assign]


def _journals(port: SyntheticPort) -> list[CaptureJournalRecoveryPlan]:
    return [plan for plan in port.plans.values() if isinstance(plan, CaptureJournalRecoveryPlan)]


def test_progressed_unallocated_item_drains_under_guard_with_exact_failure_history(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    engine, port, _ = guarded
    journal = engine.tasks.journal
    assert journal is not None
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=TextPayload("Synthetic transient"),
        delivery_id="owner.transient",
    )
    cue = engine.ingestion.enqueue(submission)
    assert isinstance(cue, CaptureCustodyReceipt)
    _fail_next_materialization(engine)
    assert journal.drain(authority=_owner(engine)).receipts == ()
    with engine._store.connect() as connection:
        events = [row[0] for row in connection.execute(
            "SELECT event_kind FROM capture_ingestion_events ORDER BY event_sequence"
        )]
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
    assert events == ["queued", "attempt_failed"]
    # The failure is protected before any later drain depends on it.
    pending = _journals(port)
    assert [[event.event_kind for event in plan.events] for plan in pending] == [
        ["queued", "attempt_failed"],
    ]
    assert not pending[0].compacted and pending[0].capture is None
    # A progressed unallocated item must not stall the queue head.
    receipts = journal.drain(authority=_owner(engine)).receipts
    assert len(receipts) == 1 and isinstance(receipts[0], CaptureReceipt)
    assert [record.kind for record in port.records] == [
        "capture_custody", "capture_journal", "capture", "capture_journal",
    ]
    terminal = CaptureJournalRecoveryPlan.from_record(port.records[-1])
    assert [event.event_kind for event in terminal.events] == [
        "queued", "attempt_failed", "accepted",
    ]
    assert terminal.compacted and terminal.capture is not None
    with engine._store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM capture_ingestion_items"
        ).fetchone()[0] == 0


def _quarantine(engine: BrainEngine, delivery_id: str) -> CaptureSubmission:
    """Queue a canonical note whose destination disappears before the drain."""
    journal = engine.tasks.journal
    assert journal is not None
    space = engine.inbox.create_space("Quarantine", delivery_id=delivery_id + ".space")
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=TextPayload("Synthetic invalid destination"),
        delivery_id=delivery_id, action=CaptureAction.CANONICAL_NOTE, space_id=space.space_id,
    )
    engine.ingestion.enqueue(submission)
    with engine._store.transaction() as connection:
        connection.execute("DELETE FROM spaces WHERE space_id=?", (space.space_id,))
    assert journal.drain(authority=_owner(engine)).receipts == ()
    assert journal.status(authority=_owner(engine))[0].state == "quarantined"
    return submission


def _journal_rows(engine: BrainEngine, delivery_id: str) -> tuple[int, int, list[str], int]:
    with engine._store.connect() as connection:
        items = connection.execute(
            "SELECT count(*) FROM capture_ingestion_items WHERE delivery_id=?", (delivery_id,),
        ).fetchone()[0]
        payloads = connection.execute(
            "SELECT count(*) FROM capture_ingestion_payloads WHERE delivery_id=?", (delivery_id,),
        ).fetchone()[0]
        events = [row[0] for row in connection.execute(
            "SELECT event_kind FROM capture_ingestion_events WHERE delivery_id=? "
            "ORDER BY event_sequence", (delivery_id,),
        )]
        tombstones = connection.execute(
            "SELECT count(*) FROM capture_ingestion_tombstones WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()[0]
    return items, payloads, events, tombstones


def test_discard_protects_terminal_history_and_tombstone_before_deleting_custody(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    engine, port, _ = guarded
    journal = engine.tasks.journal
    assert journal is not None
    _quarantine(engine, "owner.discard")
    assert [[event.event_kind for event in plan.events] for plan in _journals(port)] == [
        ["queued", "quarantined"],
    ]
    port.fail_kind = "capture_journal"
    with pytest.raises(RecoveryProtectionPendingError):
        journal.discard("owner.discard", reason="owner_requested", authority=_owner(engine))
    # Phase one committed; local custody is retained until the history is protected.
    assert _journal_rows(engine, "owner.discard") == (
        1, 1, ["queued", "quarantined", "discarded"], 1,
    )
    port.fail_kind = None
    journal.discard("owner.discard", reason="owner_requested", authority=_owner(engine))
    # Deleting the item cascades its local events; the protected journal keeps them.
    assert _journal_rows(engine, "owner.discard") == (0, 0, [], 1)
    terminal = CaptureJournalRecoveryPlan.from_record(port.records[-1])
    assert [event.event_kind for event in terminal.events] == [
        "queued", "quarantined", "discarded",
    ]
    assert terminal.compacted and terminal.capture is None and terminal.tombstone is not None
    assert terminal.tombstone.request_sha256 == terminal.custody.receipt.request_sha256


def test_baseline_existing_queue_items_are_protected_late_instead_of_stalling(
    tmp_path: Path,
) -> None:
    """Items queued before the guard existed must still drain and compact."""
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    archive = tmp_path / "baseline"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    files = tuple(sorted(validated_portable_snapshot(archive).files.items()))
    with engine._store.connect() as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    baseline = RecoveryBaseline(
        identity[0], identity[1], sha256(dict(files)["portable-manifest.json"]).hexdigest()
    )
    _quarantine(engine, "baseline.progressed")
    faulty = BrainEngine.open(profile, faults={CaptureFault.AFTER_CAPTURE_RESERVATION})
    allocated = CaptureSubmission.for_local_owner(
        profile=profile, payload=TextPayload("Synthetic allocated before guard"),
        delivery_id="baseline.allocated",
    )
    with pytest.raises(InjectedFault):
        faulty.capture.submit(allocated)
    with faulty._store.connect() as connection:
        reservation = connection.execute(
            "SELECT capture_id,stage FROM captures WHERE delivery_id='baseline.allocated'"
        ).fetchone()
    assert reservation is not None and reservation[1] == 0
    assert _journal_rows(faulty, "baseline.allocated")[:3] == (1, 1, ["queued"])
    port = SyntheticPort(baseline, files)
    guard = RecoveryProtectionGuard(baseline, port)
    # Opening resumes the stage-0 reservation and drains it under the guard.
    guarded_engine = BrainEngine.open(profile, recovery_protection_guard=guard)
    port.engine = guarded_engine
    guarded_journal = guarded_engine.tasks.journal
    assert guarded_journal is not None
    guarded_journal.drain(authority=_owner(guarded_engine))
    with guarded_engine._store.connect() as connection:
        completed = connection.execute(
            "SELECT capture_id,stage FROM captures WHERE delivery_id='baseline.allocated'"
        ).fetchone()
    assert tuple(completed) == (reservation[0], 3)
    assert _journal_rows(guarded_engine, "baseline.allocated") == (0, 0, [], 0)
    # The quarantined item predates the guard too: owner actions protect it late.
    guarded_journal.retry("baseline.progressed", authority=_owner(guarded_engine))
    guarded_journal.drain(authority=_owner(guarded_engine))
    guarded_journal.discard(
        "baseline.progressed", reason="owner_requested", authority=_owner(guarded_engine),
    )
    assert _journal_rows(guarded_engine, "baseline.progressed") == (0, 0, [], 1)
    cues = {
        plan.receipt.delivery_id for plan in port.plans.values()
        if isinstance(plan, CaptureCustodyRecoveryPlan)
    }
    assert cues == {"baseline.allocated", "baseline.progressed"}
    terminals = {
        plan.custody.receipt.delivery_id: plan for plan in _journals(port) if plan.compacted
    }
    assert set(terminals) == cues
    assert terminals["baseline.allocated"].capture is not None
    assert terminals["baseline.allocated"].capture.identities.capture_id == reservation[0]
    assert terminals["baseline.progressed"].tombstone is not None
    assert [event.event_kind for event in terminals["baseline.progressed"].events] == [
        "queued", "quarantined", "queued", "quarantined", "discarded",
    ]
    # Resumed on open, the reservation terminalizes as a duplicate of its own row.
    assert [event.event_kind for event in terminals["baseline.allocated"].events] == [
        "queued", "duplicate",
    ]


def _closure(port: SyntheticPort) -> tuple[tuple[RecoveryRecord, ...], RecoveryHead]:
    """Whole protected chain; lookup needs an allocation, pending items may have none."""
    evidence = port._evidence(next(iter(port.plans.values())))
    return evidence.closure.records, evidence.closure.expected_head


def _prefix(records: tuple[RecoveryRecord, ...], count: int) -> tuple[
    tuple[RecoveryRecord, ...], RecoveryHead,
]:
    kept = records[:count]
    return kept, RecoveryHead(kept[0].baseline, count, kept[-1].record_sha256)


def test_pending_allocation_closure_restores_exact_reservation_then_normal_drain_completes(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
) -> None:
    primary, port, _ = guarded
    receipt = primary.capture.accept(TextPayload("Synthetic pending"), delivery_id="pending.alloc")
    assert isinstance(receipt, CaptureReceipt)
    records, _ = _closure(port)
    assert [record.kind for record in records] == ["capture_custody", "capture", "capture_journal"]
    # Protection stopped after the allocation: the terminal journal never reached the port.
    records, head = _prefix(records, 2)
    restored, owner = _restore(primary, tmp_path)
    result = replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert len(result) == 1 and isinstance(result[0], CaptureCustodyReceipt)
    with restored._store.connect() as connection:
        row = connection.execute(
            "SELECT capture_id,accepted_receipt_id,stage FROM captures "
            "WHERE delivery_id='pending.alloc'"
        ).fetchone()
    assert row is not None and row[0] == receipt.capture_id and row[2] == 3
    assert _journal_rows(restored, "pending.alloc") == (1, 1, ["queued"], 0)
    before = _snapshot(restored)
    assert replay_owner_recovery_chain(
        restored, records, expected_head=head, authority=owner,
    ) == result
    assert _snapshot(restored) == before
    journal = restored.tasks.journal
    assert journal is not None
    drained = journal.drain(authority=_owner(restored)).receipts
    assert [item.capture_id for item in drained] == [receipt.capture_id]
    assert _journal_rows(restored, "pending.alloc")[:2] == (0, 0)


def test_quarantined_history_closure_restores_exact_events(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
) -> None:
    primary, port, _ = guarded
    _quarantine(primary, "pending.quarantined")
    with primary._store.connect() as connection:
        original = [tuple(row) for row in connection.execute(
            "SELECT event_sequence,event_kind,attempt_number,receipt_json,recorded_at "
            "FROM capture_ingestion_events WHERE delivery_id='pending.quarantined' "
            "ORDER BY event_sequence"
        )]
        item = tuple(connection.execute(
            "SELECT * FROM capture_ingestion_items WHERE delivery_id='pending.quarantined'"
        ).fetchone())
    assert [row[1] for row in original] == ["queued", "quarantined"]
    records, head = _closure(port)
    assert [record.kind for record in records] == ["capture_custody", "capture_journal"]
    restored, owner = _restore(primary, tmp_path)
    replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    with restored._store.connect() as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT event_sequence,event_kind,attempt_number,receipt_json,recorded_at "
            "FROM capture_ingestion_events WHERE delivery_id='pending.quarantined' "
            "ORDER BY event_sequence"
        )] == original
        assert tuple(connection.execute(
            "SELECT * FROM capture_ingestion_items WHERE delivery_id='pending.quarantined'"
        ).fetchone()) == item
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
    journal = restored.tasks.journal
    assert journal is not None
    assert journal.status(authority=_owner(restored))[0].state == "quarantined"
    before = _snapshot(restored)
    replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert _snapshot(restored) == before
    journal.discard("pending.quarantined", reason="owner_requested", authority=_owner(restored))
    assert _journal_rows(restored, "pending.quarantined") == (0, 0, [], 1)


def test_discarded_closure_restores_tombstone_without_custody(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
) -> None:
    primary, port, _ = guarded
    journal = primary.tasks.journal
    assert journal is not None
    _quarantine(primary, "pending.discarded")
    journal.discard("pending.discarded", reason="owner_requested", authority=_owner(primary))
    with primary._store.connect() as connection:
        grave = tuple(connection.execute(
            "SELECT * FROM capture_ingestion_tombstones WHERE delivery_id='pending.discarded'"
        ).fetchone())
    records, head = _closure(port)
    assert [record.kind for record in records] == [
        "capture_custody", "capture_journal", "capture_journal",
    ]
    restored, owner = _restore(primary, tmp_path)
    result = replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert len(result) == 1 and isinstance(result[0], CaptureCustodyReceipt)
    with restored._store.connect() as connection:
        assert tuple(connection.execute(
            "SELECT * FROM capture_ingestion_tombstones WHERE delivery_id='pending.discarded'"
        ).fetchone()) == grave
    assert _journal_rows(restored, "pending.discarded") == (0, 0, [], 1)
    before = _snapshot(restored)
    replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert _snapshot(restored) == before
