"""Progressed, allocated and baseline queue items stay protected and replayable."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.engine import (
    AdmissionLimits,
    BrainEngine,
    CaptureAction,
    CaptureCustodyReceipt,
    CaptureFault,
    CaptureReceipt,
    CaptureSubmission,
    InjectedFault,
    TextPayload,
)
from open_brain_engine.engine.capture_recovery import (
    CaptureRecoveryPlan,
    CaptureReservationIdentity,
)
from open_brain_engine.engine.custody_recovery import CaptureCustodyRecoveryPlan
from open_brain_engine.engine.journal_ops import JournalOperationError
from open_brain_engine.engine.journal_recovery import (
    CaptureJournalRecoveryEvent,
    CaptureJournalRecoveryPlan,
    CaptureJournalRecoveryTombstone,
)
from open_brain_engine.engine.owner_replay import replay_owner_recovery_chain
from open_brain_engine.engine.portability import restore_portable_clean
from open_brain_engine.engine.recovery_journal import RecoveryBaseline, RecoveryHead, RecoveryRecord
from open_brain_engine.engine.recovery_protection import (
    RecoveryProtectionGuard,
    RecoveryProtectionPendingError,
    operation_sha256,
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

    def flaky(
        submission: CaptureSubmission, *, admitted_privacy: PrivacyDecision,
        recovery_plan: CaptureRecoveryPlan | None = None,
    ) -> CaptureReceipt:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("synthetic transient failure")
        return original(
            submission, admitted_privacy=admitted_privacy, recovery_plan=recovery_plan,
        )

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


def _fail_after_reservation_once(engine: BrainEngine) -> None:
    """A retryable failure after stage-0 reservation leaves an allocated failed item."""
    original = engine._fault
    calls = {"count": 0}

    def fault(point: object) -> None:
        if point is CaptureFault.AFTER_CAPTURE_RESERVATION and calls["count"] == 0:
            calls["count"] += 1
            raise RuntimeError("synthetic failure after reservation")
        original(point)  # type: ignore[arg-type]

    engine._fault = fault  # type: ignore[method-assign]


def _tamper_journal_sequence(engine: BrainEngine, delivery_id: str) -> None:
    """Rows are immutable; an out-of-band tamper re-sequences the item, validly shaped."""
    with engine._store.transaction() as connection:
        guards = [tuple(row) for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND sql LIKE '%DELETE%' AND sql LIKE '%capture_ingestion_%'"
        )]
        for name, _ in guards:
            connection.execute(f"DROP TRIGGER {name}")
        item = dict(connection.execute(
            "SELECT * FROM capture_ingestion_items WHERE delivery_id=?", (delivery_id,),
        ).fetchone())
        payload = connection.execute(
            "SELECT envelope_bytes FROM capture_ingestion_payloads WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()[0]
        events = [dict(row) for row in connection.execute(
            "SELECT * FROM capture_ingestion_events WHERE delivery_id=? ORDER BY event_sequence",
            (delivery_id,),
        )]
        connection.execute(
            "DELETE FROM capture_ingestion_items WHERE delivery_id=?", (delivery_id,),
        )
        item["journal_sequence"] = item["journal_sequence"] + 100
        connection.execute(
            "INSERT INTO capture_ingestion_items VALUES(?,?,?,?,?,?,?,?)", tuple(item.values()),
        )
        connection.execute(
            "INSERT INTO capture_ingestion_payloads VALUES(?,?)", (delivery_id, payload),
        )
        for event in events:
            connection.execute(
                "INSERT INTO capture_ingestion_events VALUES(?,?,?,?,?,?)", tuple(event.values()),
            )
        for _, sql in guards:
            connection.execute(sql)


def test_baseline_existing_reservation_closure_replays_in_custody_first_order(
    tmp_path: Path,
) -> None:
    """Startup protection of an old reservation must still yield a replayable chain."""
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
    faulty = BrainEngine.open(profile, faults={CaptureFault.AFTER_CAPTURE_RESERVATION})
    with pytest.raises(InjectedFault):
        faulty.capture.accept(TextPayload("Synthetic old reservation"), delivery_id="old.alloc")
    port = SyntheticPort(baseline, files)
    guard = RecoveryProtectionGuard(baseline, port)
    guarded_engine = BrainEngine.open(profile, recovery_protection_guard=guard)
    port.engine = guarded_engine
    journal = guarded_engine.tasks.journal
    assert journal is not None
    journal.drain(authority=_owner(guarded_engine))
    assert [record.kind for record in port.records] == [
        "capture_custody", "capture", "capture_journal",
    ]
    records, head = _closure(port)
    restored, owner = _restore(guarded_engine, tmp_path)
    receipts = replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert len(receipts) == 1 and isinstance(receipts[0], CaptureReceipt)
    assert receipts[0].capture_id == CaptureJournalRecoveryPlan.from_record(
        port.records[-1]
    ).capture.identities.capture_id  # type: ignore[union-attr]


def test_tampered_retained_cue_is_refused_without_poisoning_the_protected_chain(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    engine, port, _ = guarded
    journal = engine.tasks.journal
    assert journal is not None
    engine.ingestion.enqueue(CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=TextPayload("Synthetic tampered"),
        delivery_id="owner.tampered",
    ))
    _fail_next_materialization(engine)
    assert journal.drain(authority=_owner(engine)).receipts == ()
    protected = list(port.records)
    _tamper_journal_sequence(engine, "owner.tampered")
    with pytest.raises(RecoveryProtectionPendingError):
        journal.drain(authority=_owner(engine))
    # Refusal must not append a second cue: the authenticated head is unchanged.
    assert port.records == protected
    assert _journal_rows(engine, "owner.tampered")[:2] == (1, 1)


@pytest.mark.parametrize("fault", [
    CaptureFault.AFTER_CAPTURE_RESERVATION, CaptureFault.AFTER_SOURCE_WRITE,
])
@pytest.mark.parametrize("shape", ["allocation_only", "allocated_failed_history"])
def test_interrupted_pending_allocation_replay_resumes(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
    fault: CaptureFault, shape: str,
) -> None:
    primary, port, _ = guarded
    journal = primary.tasks.journal
    assert journal is not None
    if shape == "allocation_only":
        receipt = primary.capture.accept(TextPayload("Synthetic resume"), delivery_id="resume.one")
        assert isinstance(receipt, CaptureReceipt)
        records, head = _prefix(_closure(port)[0], 2)
        capture_id = receipt.capture_id
    else:
        primary.ingestion.enqueue(CaptureSubmission.for_local_owner(
            profile=primary.profile, payload=TextPayload("Synthetic resume"),
            delivery_id="resume.one",
        ))
        _fail_after_reservation_once(primary)
        assert journal.drain(authority=_owner(primary)).receipts == ()
        with primary._store.connect() as connection:
            capture_id, stage = connection.execute(
                "SELECT capture_id,stage FROM captures WHERE delivery_id='resume.one'"
            ).fetchone()
        assert stage == 0
        assert _journal_rows(primary, "resume.one")[2] == ["queued", "attempt_failed"]
        records, head = _closure(port)
        assert [record.kind for record in records] == [
            "capture_custody", "capture", "capture_journal",
        ]
    owner = EffectiveAuthority(
        primary.profile.owner_actor_id, "recovery", frozenset(), None, owner=True,
    )
    primary.profile.root.rename(tmp_path / "unavailable-primary")
    target = tmp_path / "restored"
    restore_portable_clean(
        tmp_path / "baseline", target, import_id="import_" + str(uuid4()), authority=owner,
    )
    interrupted = BrainEngine.open(compile_single_user_local(target), faults={fault})
    with pytest.raises(InjectedFault):
        replay_owner_recovery_chain(interrupted, records, expected_head=head, authority=owner)
    assert _journal_rows(interrupted, "resume.one")[:2] == (0, 0)
    restored = BrainEngine.open(compile_single_user_local(target))
    replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    with restored._store.connect() as connection:
        row = connection.execute(
            "SELECT capture_id,stage FROM captures WHERE delivery_id='resume.one'"
        ).fetchone()
    assert tuple(row) == (capture_id, 3)
    expected_events = ["queued"] if shape == "allocation_only" else ["queued", "attempt_failed"]
    assert _journal_rows(restored, "resume.one") == (1, 1, expected_events, 0)
    before = _snapshot(restored)
    replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert _snapshot(restored) == before
    restored_journal = restored.tasks.journal
    assert restored_journal is not None
    drained = restored_journal.drain(authority=_owner(restored)).receipts
    assert [item.capture_id for item in drained] == [capture_id]


def test_quarantined_prefix_then_discard_closure_replays_forward(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
) -> None:
    primary, port, _ = guarded
    journal = primary.tasks.journal
    assert journal is not None
    _quarantine(primary, "forward.discard")
    prefix_records, prefix_head = _closure(port)
    journal.discard("forward.discard", reason="owner_requested", authority=_owner(primary))
    records, head = _closure(port)
    restored, owner = _restore(primary, tmp_path)
    replay_owner_recovery_chain(
        restored, prefix_records, expected_head=prefix_head, authority=owner,
    )
    assert _journal_rows(restored, "forward.discard") == (1, 1, ["queued", "quarantined"], 0)
    replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert _journal_rows(restored, "forward.discard") == (0, 0, [], 1)
    before = _snapshot(restored)
    replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert _snapshot(restored) == before


def test_resubmission_after_transient_failure_acknowledges_duplicate(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    engine, port, _ = guarded
    journal = engine.tasks.journal
    assert journal is not None
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=TextPayload("Synthetic resubmitted"),
        delivery_id="owner.resubmit",
    )
    engine.ingestion.enqueue(submission)
    _fail_next_materialization(engine)
    assert journal.drain(authority=_owner(engine)).receipts == ()
    receipts = journal.drain(authority=_owner(engine)).receipts
    assert len(receipts) == 1
    assert len(_journals(port)) == 2
    duplicate = engine.capture.submit(submission)
    assert isinstance(duplicate, CaptureReceipt)
    assert duplicate.duplicate and duplicate.capture_id == receipts[0].capture_id


def test_replay_refuses_a_discarded_allocation_before_any_write(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
) -> None:
    primary, port, _ = guarded
    journal = primary.tasks.journal
    assert journal is not None
    primary.ingestion.enqueue(CaptureSubmission.for_local_owner(
        profile=primary.profile, payload=TextPayload("Synthetic forged discard"),
        delivery_id="forged.discard",
    ))
    _fail_after_reservation_once(primary)
    assert journal.drain(authority=_owner(primary)).receipts == ()
    records, _ = _closure(port)
    cue = CaptureCustodyRecoveryPlan.from_record(records[0])
    capture = CaptureRecoveryPlan.from_record(records[1])
    failed = CaptureJournalRecoveryPlan.from_record(records[2])
    result = canonical({"status": "discarded", "reason": "forged"}).decode()
    last = failed.events[-1]
    forged = CaptureJournalRecoveryPlan(
        custody=cue,
        events=(
            *failed.events,
            CaptureJournalRecoveryEvent(
                last.event_sequence + 1, "quarantined", last.attempt_number,
                canonical({"status": "quarantined", "reason": "invalid"}).decode(),
                last.recorded_at,
            ),
            CaptureJournalRecoveryEvent(
                last.event_sequence + 2, "discarded", 0, result, last.recorded_at,
            ),
        ),
        capture=capture,
        tombstone=CaptureJournalRecoveryTombstone(
            cue.receipt.request_sha256, result, last.recorded_at,
        ),
        compacted=True,
    )
    forged_record = RecoveryRecord(
        baseline=cue.baseline, sequence=4, previous_sha256=records[2].record_sha256,
        kind="capture_journal", payload=forged.to_bytes(),
    )
    records = (*records, forged_record)
    head = RecoveryHead(cue.baseline, 4, forged_record.record_sha256)
    restored, owner = _restore(primary, tmp_path)
    before = _snapshot(restored)
    with pytest.raises(ValueError, match="discarded allocation"):
        replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert _snapshot(restored) == before
    with restored._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 0


def test_discard_refuses_an_allocated_item(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    """One permitted attempt quarantines an allocated item inside a single drain."""
    engine, port, guard = guarded
    strict = BrainEngine.open(
        engine.profile, recovery_protection_guard=guard,
        admission_limits=AdmissionLimits(max_journal_attempts=1),
    )
    port.engine = strict
    journal = strict.tasks.journal
    assert journal is not None
    strict.ingestion.enqueue(CaptureSubmission.for_local_owner(
        profile=strict.profile, payload=TextPayload("Synthetic allocated quarantine"),
        delivery_id="owner.allocated",
    ))
    _fail_after_reservation_once(strict)
    assert journal.drain(authority=_owner(strict)).receipts == ()
    assert journal.status(authority=_owner(strict))[0].state == "quarantined"
    with strict._store.connect() as connection:
        capture_id, stage = connection.execute(
            "SELECT capture_id,stage FROM captures WHERE delivery_id='owner.allocated'"
        ).fetchone()
    assert stage == 0
    with pytest.raises(JournalOperationError, match="allocated"):
        journal.discard("owner.allocated", reason="owner_requested", authority=_owner(strict))
    assert _journal_rows(strict, "owner.allocated") == (1, 1, ["queued", "quarantined"], 0)
    assert all(plan.events[-1].event_kind != "discarded" for plan in _journals(port))
    journal.retry("owner.allocated", authority=_owner(strict))
    receipts = journal.drain(authority=_owner(strict)).receipts
    assert [item.capture_id for item in receipts] == [capture_id]


def test_replay_refuses_an_allocation_recorded_after_a_discard(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
) -> None:
    primary, port, _ = guarded
    journal = primary.tasks.journal
    assert journal is not None
    _quarantine(primary, "late.alloc")
    journal.discard("late.alloc", reason="owner_requested", authority=_owner(primary))
    records, _ = _closure(port)
    cue = CaptureCustodyRecoveryPlan.from_record(records[0])
    forged_capture = CaptureRecoveryPlan(
        cue.baseline, cue.envelope,
        CaptureReservationIdentity.allocate(canonical=True, accepted_at=cue.receipt.queued_at),
    )
    forged = RecoveryRecord(
        baseline=cue.baseline, sequence=len(records) + 1,
        previous_sha256=records[-1].record_sha256, kind="capture",
        payload=forged_capture.to_bytes(),
    )
    records = (*records, forged)
    head = RecoveryHead(cue.baseline, len(records), forged.record_sha256)
    restored, owner = _restore(primary, tmp_path)
    before = _snapshot(restored)
    with pytest.raises(ValueError, match="allocation after"):
        replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert _snapshot(restored) == before


def test_pending_protection_of_an_old_reservation_protects_its_cue_first(
    tmp_path: Path,
) -> None:
    """Resubmission before startup recovery must not append the allocation first."""
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
    faulty = BrainEngine.open(profile, faults={CaptureFault.AFTER_CAPTURE_RESERVATION})
    submission = CaptureSubmission.for_local_owner(
        profile=profile, payload=TextPayload("Synthetic old reservation"),
        delivery_id="old.resubmit",
    )
    with pytest.raises(InjectedFault):
        faulty.capture.submit(submission)
    port = SyntheticPort(baseline, files)
    guard = RecoveryProtectionGuard(baseline, port)
    # Startup recovery did not run (writer busy); the guard sees the reservation cold.
    faulty._recovery_protection_guard = guard
    port.engine = faulty
    guard.protect_pending(faulty, submission)
    assert [record.kind for record in port.records] == ["capture_custody", "capture"]


def test_custody_only_chain_refuses_a_foreign_capture_row(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
) -> None:
    """A capture row alone never proves it belongs to the cue being restored."""
    primary, port, _ = guarded
    journal = primary.tasks.journal
    assert journal is not None
    primary.capture.accept(TextPayload("Synthetic first"), delivery_id="warm.one")
    primary.capture.accept(TextPayload("Synthetic second"), delivery_id="warm.two")
    # A discarded gap consumes a primary sequence that the restore never re-inserts.
    _quarantine(primary, "warm.gap")
    journal.discard("warm.gap", reason="owner_requested", authority=_owner(primary))
    cue = primary.ingestion.enqueue(CaptureSubmission.for_local_owner(
        profile=primary.profile, payload=TextPayload("Synthetic original"),
        delivery_id="foreign.cue",
    ))
    assert isinstance(cue, CaptureCustodyReceipt)
    records, head = _closure(port)
    assert records[-1].kind == "capture_custody"
    prefix_records, prefix_head = _prefix(records, len(records) - 1)
    restored, owner = _restore(primary, tmp_path)
    replay_owner_recovery_chain(
        restored, prefix_records, expected_head=prefix_head, authority=owner,
    )
    # The foreign row takes the gap's free sequence, so only ownership can refuse it.
    foreign = restored.capture.accept(TextPayload("Synthetic foreign"), delivery_id="foreign.cue")
    assert isinstance(foreign, CaptureReceipt)
    before = _snapshot(restored)
    with pytest.raises(ValueError, match="delivery collision"):
        replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert _snapshot(restored) == before


def test_duplicate_validation_rejects_a_forged_journal_with_a_different_cue(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    engine, port, _ = guarded
    journal = engine.tasks.journal
    assert journal is not None
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=TextPayload("Synthetic forged extension"),
        delivery_id="owner.forged",
    )
    engine.ingestion.enqueue(submission)
    _fail_next_materialization(engine)
    assert journal.drain(authority=_owner(engine)).receipts == ()
    assert len(journal.drain(authority=_owner(engine)).receipts) == 1
    pending = next(plan for plan in _journals(port) if not plan.compacted)
    # Same events, a re-sequenced cue: progression checks on events alone accept it.
    forged = replace(
        pending, custody=replace(
            pending.custody, journal_sequence=pending.custody.journal_sequence + 100,
        ),
    )
    port.records.append(RecoveryRecord(
        baseline=forged.baseline, sequence=len(port.records) + 1,
        previous_sha256=port.records[-1].record_sha256, kind="capture_journal",
        payload=forged.to_bytes(),
    ))
    port.plans[operation_sha256(forged)] = forged
    with pytest.raises(RecoveryProtectionPendingError):
        engine.capture.submit(submission)
