"""Original ordinary journal transitions survive a closed typed recovery codec."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import (
    AdmissionLimits,
    BrainEngine,
    CaptureAction,
    CaptureCustodyReceipt,
    CaptureFault,
    CaptureSubmission,
    FilePayload,
    InjectedFault,
    TextPayload,
)
from open_brain_engine.engine.capture_recovery import emit_owner_capture_plan
from open_brain_engine.engine.custody_recovery import (
    CaptureCustodyRecoveryPlan,
    emit_owner_custody_plan,
)
from open_brain_engine.engine.ingestion import IngestionJournal
from open_brain_engine.engine.journal_recovery import (
    CaptureJournalRecoveryEvent,
    CaptureJournalRecoveryPlan,
    CaptureJournalRecoveryTombstone,
)
from open_brain_engine.engine.recovery_journal import RecoveryBaseline, RecoveryRecord
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local


def _queued(tmp_path: Path, *, canonical_note: bool = False, file: bool = False) -> tuple[
    BrainEngine, CaptureSubmission, CaptureCustodyRecoveryPlan, EffectiveAuthority,
]:
    ticks = count()
    start = datetime(2026, 10, 3, 12, tzinfo=UTC)
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Recovery",)),
        clock=lambda: start + timedelta(seconds=next(ticks)),
        admission_limits=AdmissionLimits(max_journal_attempts=2),
    )
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile,
        payload=FilePayload("synthetic.bin", "application/octet-stream", b"\0\xff")
        if file else TextPayload("Synthetic journal transition body"),
        delivery_id="synthetic.journal.transition", privacy_tier="personal",
        action=CaptureAction.CANONICAL_NOTE if canonical_note else CaptureAction.QUICK,
        space_id=engine.tasks.spaces.spaces()[0].space_id if canonical_note else None,
    )
    receipt = engine.ingestion.enqueue(submission)
    assert isinstance(receipt, CaptureCustodyReceipt)
    baseline = RecoveryBaseline(receipt.brain_id, receipt.issuer_epoch, "0" * 64)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "journal-recovery", frozenset(), None, owner=True,
    )
    custody = emit_owner_custody_plan(engine, submission, baseline=baseline, authority=owner)
    return engine, submission, custody, owner


def _events(engine: BrainEngine, delivery: str) -> tuple[CaptureJournalRecoveryEvent, ...]:
    with engine._store.connect() as connection:
        return tuple(
            CaptureJournalRecoveryEvent(**dict(row)) for row in connection.execute(
                "SELECT event_sequence,event_kind,attempt_number,receipt_json,recorded_at "
                "FROM capture_ingestion_events WHERE delivery_id=? ORDER BY event_sequence",
                (delivery,),
            )
        )


def _terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, canonical_note: bool = False,
    duplicate: bool = False, file: bool = False,
) -> tuple[BrainEngine, CaptureJournalRecoveryPlan]:
    engine, submission, custody, owner = _queued(tmp_path, canonical_note=canonical_note, file=file)
    if duplicate:
        engine._faults.add(CaptureFault.AFTER_CANONICAL_COMPLETION)
        with pytest.raises(InjectedFault):
            engine.capture.submit(submission)
        engine._faults.clear()
        capture = emit_owner_capture_plan(
            engine, submission, baseline=custody.baseline, authority=owner,
        )
    with monkeypatch.context() as retained:
        retained.setattr(IngestionJournal, "compact", lambda self, delivery_id: None)
        assert engine.recover() == 1
        if not duplicate:
            capture = emit_owner_capture_plan(
                engine, submission, baseline=custody.baseline, authority=owner,
            )
        plan = CaptureJournalRecoveryPlan(
            custody, _events(engine, submission.delivery_id), capture,
        )
    return engine, plan


def test_original_queue_roundtrip_and_record(tmp_path: Path) -> None:
    engine, submission, custody, _ = _queued(tmp_path)
    plan = CaptureJournalRecoveryPlan(custody, _events(engine, submission.delivery_id))
    assert CaptureJournalRecoveryPlan.from_bytes(plan.to_bytes()) == plan
    assert plan.baseline == custody.baseline
    assert plan.envelope == custody.envelope
    record = RecoveryRecord(
        baseline=plan.baseline, sequence=1, previous_sha256=plan.baseline.artifact_sha256,
        kind="capture_journal", payload=plan.to_bytes(),
    )
    assert CaptureJournalRecoveryPlan.from_record(
        RecoveryRecord.from_bytes(record.to_bytes())
    ) == plan
    with pytest.raises(ValueError, match="kind"):
        CaptureJournalRecoveryPlan.from_record(replace(record, kind="capture"))
    with pytest.raises(ValueError, match="baseline"):
        CaptureJournalRecoveryPlan.from_record(replace(
            record, baseline=replace(plan.baseline, issuer_epoch=plan.baseline.issuer_epoch + 1),
        ))


@pytest.mark.parametrize("canonical_note,duplicate,file", [
    (False, False, False), (False, True, False), (True, False, False), (True, True, False),
    (False, False, True),
])
def test_real_terminal_history_preserves_capture_and_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canonical_note: bool, duplicate: bool,
    file: bool,
) -> None:
    engine, plan = _terminal(
        tmp_path, monkeypatch, canonical_note=canonical_note, duplicate=duplicate, file=file,
    )
    assert plan.capture is not None
    assert plan.events[-1].event_kind == ("duplicate" if duplicate else "accepted")
    assert CaptureJournalRecoveryPlan.from_bytes(plan.to_bytes()) == plan
    engine.ingestion.compact(plan.envelope.submission.delivery_id)
    compacted = replace(plan, compacted=True)
    assert CaptureJournalRecoveryPlan.from_bytes(compacted.to_bytes()) == compacted
    assert compacted.events == plan.events
    assert compacted.capture is not None
    assert compacted.capture.identities == plan.capture.identities
    with engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM capture_ingestion_items").fetchone()[0] == 0


def test_reserved_nonterminal_plan_is_preserved(tmp_path: Path) -> None:
    engine, submission, custody, owner = _queued(tmp_path)
    engine._faults.add(CaptureFault.AFTER_CAPTURE_RESERVATION)
    with pytest.raises(InjectedFault):
        engine.capture.submit(submission)
    capture = emit_owner_capture_plan(
        engine, submission, baseline=custody.baseline, authority=owner,
    )
    plan = CaptureJournalRecoveryPlan(custody, _events(engine, submission.delivery_id), capture)
    assert len(plan.events) == 1
    assert CaptureJournalRecoveryPlan.from_bytes(plan.to_bytes()).capture == capture


def _failed_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[BrainEngine, CaptureCustodyRecoveryPlan, tuple[CaptureJournalRecoveryEvent, ...]]:
    engine, submission, custody, _ = _queued(tmp_path)

    def retryable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic retryable failure")

    def invalid(*args: object, **kwargs: object) -> None:
        raise ValueError("synthetic invalid submission")

    with monkeypatch.context() as failed:
        failed.setattr(engine, "_materialize_capture_locked", retryable)
        assert engine.recover() == 0
        assert engine.recover() == 0
        engine.ingestion.retry(submission.delivery_id)
        assert engine.recover() == 0
        engine.ingestion.retry(submission.delivery_id)
        failed.setattr(engine, "_materialize_capture_locked", invalid)
        assert engine.recover() == 0
    return engine, custody, _events(engine, submission.delivery_id)


def test_real_retry_quarantine_and_discard_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, custody, events = _failed_history(tmp_path, monkeypatch)
    assert tuple((event.event_kind, event.attempt_number) for event in events) == (
        ("queued", 0), ("attempt_failed", 1), ("quarantined", 2), ("queued", 0),
        ("quarantined", 3), ("queued", 0), ("quarantined", 3),
    )
    plan = CaptureJournalRecoveryPlan(custody, events)
    assert CaptureJournalRecoveryPlan.from_bytes(plan.to_bytes()) == plan
    original = IngestionJournal._append_event
    discarded: list[CaptureJournalRecoveryEvent] = []

    def observe(self: IngestionJournal, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        row = args[0].execute(
            "SELECT event_sequence,event_kind,attempt_number,receipt_json,recorded_at "
            "FROM capture_ingestion_events ORDER BY event_sequence DESC LIMIT 1"
        ).fetchone()
        discarded.append(CaptureJournalRecoveryEvent(**dict(row)))

    monkeypatch.setattr(IngestionJournal, "_append_event", observe)
    engine.ingestion.discard(custody.receipt.delivery_id, reason="owner_requested")
    with engine._store.connect() as connection:
        row = connection.execute(
            "SELECT request_sha256,result_json,decided_at FROM capture_ingestion_tombstones"
        ).fetchone()
    tombstone = CaptureJournalRecoveryTombstone(**dict(row))
    plan = replace(plan, events=(*events, *discarded), tombstone=tombstone, compacted=True)
    assert plan.events[-1].recorded_at != tombstone.decided_at
    assert CaptureJournalRecoveryPlan.from_bytes(plan.to_bytes()) == plan
    with pytest.raises(ValueError, match="tombstone"):
        replace(plan, tombstone=replace(tombstone, request_sha256="1" * 64))
    with pytest.raises(ValueError, match="tombstone"):
        replace(plan, tombstone=None)
    with pytest.raises(ValueError, match="tombstone"):
        replace(plan, tombstone=replace(
            tombstone, result_json=canonical({"status": "discarded", "reason": "changed"}).decode(),
        ))


def test_retry_history_can_reach_original_accepted_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, custody, _ = _failed_history(tmp_path, monkeypatch)
    submission = custody.envelope.submission
    engine.ingestion.retry(submission.delivery_id)
    with monkeypatch.context() as retained:
        retained.setattr(IngestionJournal, "compact", lambda self, delivery_id: None)
        assert engine.recover() == 1
    capture = emit_owner_capture_plan(
        engine, submission, baseline=custody.baseline,
        authority=EffectiveAuthority(
            engine.profile.owner_actor_id, "journal-recovery", frozenset(), None, owner=True,
        ),
    )
    plan = CaptureJournalRecoveryPlan(custody, _events(engine, submission.delivery_id), capture)
    assert plan.events[-1].event_kind == "accepted"
    assert plan.events[-1].attempt_number == 0
    assert CaptureJournalRecoveryPlan.from_bytes(plan.to_bytes()) == plan


def test_initial_invalid_quarantine_keeps_zero_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, submission, custody, _ = _queued(tmp_path)

    def invalid(*args: object, **kwargs: object) -> None:
        raise ValueError("synthetic invalid submission")

    monkeypatch.setattr(engine, "_materialize_capture_locked", invalid)
    assert engine.recover() == 0
    plan = CaptureJournalRecoveryPlan(custody, _events(engine, submission.delivery_id))
    assert plan.events[-1].event_kind == "quarantined"
    assert plan.events[-1].attempt_number == 0
    assert CaptureJournalRecoveryPlan.from_bytes(plan.to_bytes()) == plan


@pytest.mark.parametrize("case", [
    "event-extra", "event-list", "sequence-bool", "sequence-float", "sequence-large",
    "sequence-order", "attempt-bool", "attempt-negative", "attempt-order", "status-mismatch",
    "reason-type", "source-fenced", "queued-history", "terminal-history", "pretty-receipt",
    "duplicate-receipt-field", "time-naive", "time-offset", "compacted-type", "compacted-active",
])
def test_malformed_later_history_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    _, custody, events = _failed_history(tmp_path, monkeypatch)
    value = json.loads(CaptureJournalRecoveryPlan(custody, events).to_bytes())
    event = value["events"][1]
    if case == "event-extra":
        event["authority"] = "owner"
    elif case == "event-list":
        value["events"][1] = []
    elif case == "sequence-bool":
        event["event_sequence"] = True
    elif case == "sequence-float":
        event["event_sequence"] = 2.0
    elif case == "sequence-large":
        event["event_sequence"] = 2**63
    elif case == "sequence-order":
        event["event_sequence"] = value["events"][0]["event_sequence"]
    elif case == "attempt-bool":
        event["attempt_number"] = True
    elif case == "attempt-negative":
        event["attempt_number"] = -1
    elif case == "attempt-order":
        value["events"][4]["attempt_number"] = 1
    elif case in {"status-mismatch", "reason-type", "source-fenced"}:
        metadata = json.loads(event["receipt_json"])
        metadata["status" if case == "status-mismatch" else "reason"] = (
            "quarantined" if case == "status-mismatch" else [] if case == "reason-type"
            else "source_fenced"
        )
        event["receipt_json"] = canonical(metadata).decode()
    elif case == "queued-history":
        event.update(event_kind="queued", attempt_number=0, receipt_json='{"status":"queued"}')
    elif case == "terminal-history":
        event.update(event_kind="discarded", attempt_number=0,
                     receipt_json='{"reason":"owner_requested","status":"discarded"}')
    elif case == "pretty-receipt":
        event["receipt_json"] = json.dumps(json.loads(event["receipt_json"]), indent=2)
    elif case == "duplicate-receipt-field":
        event["receipt_json"] = '{"reason":"retryable","status":"attempt_failed",' \
                                '"status":"attempt_failed"}'
    elif case == "time-naive":
        event["recorded_at"] = "2026-10-03T12:00:00"
    elif case == "time-offset":
        event["recorded_at"] = "2026-10-03T12:00:00+00:00"
    elif case == "compacted-type":
        value["compacted"] = 1
    else:
        value["compacted"] = True
    with pytest.raises(ValueError):
        CaptureJournalRecoveryPlan.from_bytes(canonical(value))


@pytest.mark.parametrize("field,bad", [
    ("capture_id", "capture_invalid"), ("payload_family", "event"), ("duplicate", 0),
    ("state", "reserved"), ("enrichment_state", "invented"), ("space_id", "space_invalid"),
    ("canonical_path", "content/elsewhere.md"), ("requested_tier", "public"),
    ("final_admitted_tier", "public"), ("delivery_id", "synthetic.other"),
    ("request_sha256", "1" * 64), ("destination_brain_id", "brn_" + "a" * 26),
    ("issuer_epoch", True), ("extra", True),
])
def test_terminal_receipt_type_and_crossbindings_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, bad: object,
) -> None:
    _, plan = _terminal(tmp_path, monkeypatch)
    value = json.loads(plan.to_bytes())
    metadata = json.loads(value["events"][-1]["receipt_json"])
    metadata["receipt"][field] = bad
    value["events"][-1]["receipt_json"] = canonical(metadata).decode()
    with pytest.raises(ValueError):
        CaptureJournalRecoveryPlan.from_bytes(canonical(value))


def test_custody_capture_and_terminal_bindings_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _terminal(tmp_path, monkeypatch, canonical_note=True)
    assert plan.capture is not None
    with pytest.raises(ValueError):
        replace(plan, capture=None)
    with pytest.raises(ValueError):
        replace(plan, capture=replace(
            plan.capture, baseline=replace(plan.baseline, issuer_epoch=2),
        ))
    changed = replace(
        plan.envelope.submission, source_reference="synthetic:other",
        provenance=replace(plan.envelope.submission.provenance, source_ref="synthetic:other"),
    )
    assert changed.request_sha256() == plan.envelope.submission.request_sha256()
    other = replace(plan.capture, envelope=replace(plan.envelope, submission=changed))
    with pytest.raises(ValueError, match="binding"):
        replace(plan, capture=other)
    with pytest.raises(ValueError, match="initial"):
        replace(plan, events=(replace(plan.events[0], recorded_at="2026-10-03T13:00:00Z"),
                              *plan.events[1:]))
    with pytest.raises(ValueError, match="initial"):
        replace(plan, events=(replace(plan.events[0], receipt_json='{"status":"queued"}'),
                              *plan.events[1:]))
    with pytest.raises(ValueError, match="order"):
        replace(plan, events=(*plan.events, replace(
            plan.events[-1], event_sequence=plan.events[-1].event_sequence + 1,
        )))
    with pytest.raises(ValueError):
        replace(cast(Any, plan), events=list(plan.events))
    value = json.loads(plan.to_bytes())
    value["events"][-1]["event_kind"] = "duplicate"
    with pytest.raises(ValueError):
        CaptureJournalRecoveryPlan.from_bytes(canonical(value))


@pytest.mark.parametrize("case", ["extra", "pretty", "duplicate", "capture-type", "tombstone-type"])
def test_closed_outer_fields(tmp_path: Path, case: str) -> None:
    engine, submission, custody, _ = _queued(tmp_path)
    raw = CaptureJournalRecoveryPlan(custody, _events(engine, submission.delivery_id)).to_bytes()
    value = json.loads(raw)
    if case == "extra":
        value["replay_sql"] = "SELECT 1"
    elif case == "pretty":
        raw = json.dumps(value, indent=2).encode()
    elif case == "duplicate":
        raw = raw.replace(b'"compacted":false', b'"compacted":false,"compacted":false')
    elif case == "capture-type":
        value["capture"] = []
    else:
        value["tombstone"] = {"result_json": "{}"}
    with pytest.raises(ValueError):
        CaptureJournalRecoveryPlan.from_bytes(
            raw if case in {"pretty", "duplicate"} else canonical(value),
        )


@pytest.mark.parametrize("field,bad", [
    ("request_sha256", "A" * 64), ("decided_at", "2026-10-03"),
    ("result_json", '{"reason":"","status":"discarded"}'),
    ("result_json", '{"reason":true,"status":"discarded"}'),
    ("result_json", canonical({"status": "discarded", "reason": "x" * 129}).decode()),
])
def test_tombstone_closed_types(field: str, bad: object) -> None:
    tombstone = CaptureJournalRecoveryTombstone(
        "0" * 64, '{"reason":"owner_requested","status":"discarded"}',
        "2026-10-03T12:00:00Z",
    )
    with pytest.raises(ValueError):
        replace(cast(Any, tombstone), **{field: bad})


def test_size_and_type_bound_precedes_json_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json, "loads", lambda *args, **kwargs: pytest.fail("decoded invalid size"))
    for raw in (b"", b"x" * (16 * 1024 * 1024 + 1), bytearray(b"{}")):
        with pytest.raises(ValueError, match="size"):
            CaptureJournalRecoveryPlan.from_bytes(cast(bytes, raw))
