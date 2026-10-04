"""Replay original queue identities without the original primary path."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    CaptureCustodyReceipt,
    CaptureSubmission,
    FilePayload,
    TextPayload,
)
from open_brain_engine.engine.custody_recovery import (
    CaptureCustodyRecoveryPlan,
    emit_owner_custody_plan,
)
from open_brain_engine.engine.custody_replay import replay_owner_custody_chain
from open_brain_engine.engine.portability import _manifest_digest, restore_portable_clean
from open_brain_engine.engine.recovery_journal import RecoveryHead, RecoveryRecord
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local


def _records(plans: tuple[CaptureCustodyRecoveryPlan, ...]) -> tuple[RecoveryRecord, ...]:
    previous = plans[0].baseline.artifact_sha256
    records = []
    for index, plan in enumerate(plans, 1):
        record = RecoveryRecord(
            baseline=plan.baseline, sequence=index, previous_sha256=previous,
            kind="capture_custody", payload=plan.to_bytes(),
        )
        records.append(record)
        previous = record.record_sha256
    return tuple(records)


@pytest.fixture
def recovery(tmp_path: Path) -> tuple[
    BrainEngine, tuple[CaptureCustodyRecoveryPlan, ...], EffectiveAuthority
]:
    primary_path = tmp_path / "primary"
    primary = BrainEngine.open(compile_single_user_local(primary_path))
    primary.capture.accept(
        TextPayload("Synthetic earlier baseline"), delivery_id="baseline.settled"
    )
    archive = tmp_path / "baseline"
    primary.portability.export(archive, export_id="export_" + str(uuid4()))
    owner = EffectiveAuthority(
        primary.profile.owner_actor_id, "recovery", frozenset(), None, owner=True
    )
    from open_brain_engine.engine.recovery_journal import RecoveryBaseline

    plans = []
    payloads: tuple[TextPayload | FilePayload, ...] = (
        TextPayload("Synthetic queued body"),
        FilePayload("synthetic.bin", "application/octet-stream", b"\0\xfforiginal"),
    )
    for index, payload in enumerate(payloads):
        submission = CaptureSubmission.for_local_owner(
            profile=primary.profile, payload=payload,
            delivery_id=f"synthetic.queue.{index}", privacy_tier="personal",
        )
        cue = primary.ingestion.enqueue(submission)
        assert isinstance(cue, CaptureCustodyReceipt)
        baseline = RecoveryBaseline(cue.brain_id, cue.issuer_epoch, _manifest_digest(
            json.loads((archive / "portable-manifest.json").read_bytes())
        ))
        plans.append(emit_owner_custody_plan(
            primary, submission, baseline=baseline, authority=owner
        ))
    encoded = tuple(plan.to_bytes() for plan in plans)
    primary_path.rename(tmp_path / "unavailable-primary")
    del primary
    target = tmp_path / "restored"
    restore_portable_clean(archive, target, import_id="import_" + str(uuid4()), authority=owner)
    restored = BrainEngine.open(compile_single_user_local(target))
    return restored, tuple(CaptureCustodyRecoveryPlan.from_bytes(raw) for raw in encoded), owner


def _snapshot(engine: BrainEngine) -> tuple[tuple[tuple[object, ...], ...], ...]:
    with engine._store.connect() as connection:
        return tuple(tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
                     for table in (
                         "captures", "capture_ingestion_items", "capture_ingestion_payloads",
                         "capture_ingestion_events", "capture_ingestion_tombstones",
                         "sqlite_sequence",
                     ))


def test_custody_replays_exactly_then_normal_ingress_continues(recovery: tuple[
    BrainEngine, tuple[CaptureCustodyRecoveryPlan, ...], EffectiveAuthority
]) -> None:
    engine, plans, owner = recovery
    records = tuple(RecoveryRecord.from_bytes(record.to_bytes()) for record in _records(plans))
    head = RecoveryHead(plans[0].baseline, len(records), records[-1].record_sha256)
    result = replay_owner_custody_chain(engine, records, expected_head=head, authority=owner)
    assert result == tuple(plan.receipt for plan in plans)
    before = _snapshot(engine)
    assert replay_owner_custody_chain(
        engine, records, expected_head=head, authority=owner
    ) == result
    assert _snapshot(engine) == before
    for plan in plans:
        assert emit_owner_custody_plan(
            engine, plan.envelope.submission, baseline=plan.baseline, authority=owner
        ).to_bytes() == plan.to_bytes()
        assert engine.ingestion.enqueue(plan.envelope.submission) == plan.receipt
    next_submission = CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=TextPayload("Synthetic next queue item"),
        delivery_id="synthetic.queue.next", privacy_tier="personal",
    )
    next_cue = engine.ingestion.enqueue(next_submission)
    assert isinstance(next_cue, CaptureCustodyReceipt)
    next_plan = emit_owner_custody_plan(
        engine, next_submission, baseline=plans[0].baseline, authority=owner
    )
    assert next_plan.journal_sequence > plans[-1].journal_sequence
    assert next_plan.event_sequence > plans[-1].event_sequence
    receipt = engine.capture.submit(plans[0].envelope.submission)
    assert receipt.final_admitted_tier == plans[0].receipt.final_admitted_tier
    assert engine.capture.submit(plans[0].envelope.submission).duplicate


@pytest.mark.parametrize("damage", ["attempted", "terminal", "missing-baseline", "foreign-item"])
def test_retained_state_conflicts_refuse_atomically(recovery: tuple[
    BrainEngine, tuple[CaptureCustodyRecoveryPlan, ...], EffectiveAuthority
], damage: str) -> None:
    engine, plans, owner = recovery
    records = _records(plans)
    head = RecoveryHead(plans[0].baseline, len(records), records[-1].record_sha256)
    if damage in {"attempted", "terminal"}:
        replay_owner_custody_chain(engine, records, expected_head=head, authority=owner)
        if damage == "attempted":
            engine.ingestion._terminal_metadata(
                plans[-1].receipt.delivery_id, "attempt_failed", 1, "retryable"
            )
        else:
            engine.capture.submit(plans[-1].envelope.submission)
    elif damage == "missing-baseline":
        (engine.profile.root / "portable-manifest.json").rename(
            engine.profile.root / "unavailable-baseline.json"
        )
    else:
        # Ordinary enqueue would allocate different custody identities/order.
        engine.ingestion.enqueue(plans[-1].envelope.submission)
    before = _snapshot(engine)
    with pytest.raises(ValueError):
        replay_owner_custody_chain(engine, records, expected_head=head, authority=owner)
    assert _snapshot(engine) == before


@pytest.mark.parametrize("invalid", [
    "nonowner", "wrong-principal", "prefix", "ingestion-collision", "item-order",
    "event-order", "queue-full", "stale-authority", "baseline-collision",
    "event-baseline", "narrowed-policy", "item-size",
])
def test_whole_chain_refusal_has_no_partial_write(recovery: tuple[
    BrainEngine, tuple[CaptureCustodyRecoveryPlan, ...], EffectiveAuthority
], invalid: str) -> None:
    engine, plans, owner = recovery
    if invalid == "nonowner":
        owner = replace(owner, owner=False)
    elif invalid == "wrong-principal":
        owner = replace(owner, principal_id="actor_other")
    elif invalid == "ingestion-collision":
        plans = (plans[0], replace(plans[1], receipt=replace(
            plans[1].receipt, ingestion_id=plans[0].receipt.ingestion_id
        )))
    elif invalid == "item-order":
        plans = (plans[0], replace(plans[1], journal_sequence=plans[0].journal_sequence))
    elif invalid == "event-order":
        plans = (plans[0], replace(plans[1], event_sequence=plans[0].event_sequence))
    elif invalid == "queue-full":
        engine._admission_limits = replace(engine._admission_limits, max_journal_items=1)
    elif invalid == "stale-authority":
        owner = replace(owner, brain_id=plans[0].baseline.brain_id,
                        issuer_epoch=plans[0].baseline.issuer_epoch + 1)
    elif invalid == "baseline-collision":
        # A compacted baseline retains its high-water mark despite empty tables.
        plans = (replace(plans[0], journal_sequence=1), plans[1])
    elif invalid == "event-baseline":
        plans = (replace(plans[0], event_sequence=2), plans[1])
    elif invalid == "narrowed-policy":
        engine._boundary_classifier = lambda submission: PrivacyTier.SECRET
    elif invalid == "item-size":
        engine._admission_limits = replace(engine._admission_limits, max_journal_item_bytes=1)
    records = _records(plans)
    head = RecoveryHead(plans[0].baseline, len(records), records[-1].record_sha256)
    if invalid == "prefix":
        records = records[:1]
    before = _snapshot(engine)
    with pytest.raises(ValueError):
        replay_owner_custody_chain(engine, records, expected_head=head, authority=owner)
    assert _snapshot(engine) == before
