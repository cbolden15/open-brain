"""Progressed, allocated and baseline queue items stay protected and replayable."""

from open_brain_engine.engine import (
    BrainEngine,
    CaptureCustodyReceipt,
    CaptureReceipt,
    CaptureSubmission,
    TextPayload,
)
from open_brain_engine.engine.journal_recovery import CaptureJournalRecoveryPlan
from open_brain_engine.engine.recovery_protection import RecoveryProtectionGuard
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

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
