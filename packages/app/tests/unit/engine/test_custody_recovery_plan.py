"""Original queued receipt and envelope are distinct from capture reservation."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureCustodyReceipt,
    CaptureFault,
    CaptureSubmission,
    FilePayload,
    InjectedFault,
    TextPayload,
)
from open_brain_engine.engine.custody_recovery import (
    CaptureCustodyRecoveryPlan,
    emit_owner_custody_plan,
)
from open_brain_engine.engine.recovery_journal import RecoveryBaseline, RecoveryRecord
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local


@pytest.fixture
def queued(tmp_path: Path) -> tuple[
    BrainEngine, CaptureSubmission, CaptureCustodyReceipt, RecoveryBaseline, EffectiveAuthority
]:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=TextPayload("Synthetic queued body"),
        delivery_id="synthetic.custody.recovery", privacy_tier="personal",
    )
    receipt = engine.ingestion.enqueue(submission)
    assert isinstance(receipt, CaptureCustodyReceipt)
    baseline = RecoveryBaseline(receipt.brain_id, receipt.issuer_epoch, "0" * 64)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "recovery", frozenset(), None, owner=True
    )
    return engine, submission, receipt, baseline, owner


def test_original_custody_emission_and_record_roundtrip(queued: tuple[
    BrainEngine, CaptureSubmission, CaptureCustodyReceipt, RecoveryBaseline, EffectiveAuthority
]) -> None:
    engine, submission, receipt, baseline, owner = queued
    plan = emit_owner_custody_plan(engine, submission, baseline=baseline, authority=owner)
    assert plan.receipt == receipt
    with engine._store.connect() as connection:
        item = connection.execute("SELECT * FROM capture_ingestion_items").fetchone()
        event = connection.execute("SELECT * FROM capture_ingestion_events").fetchone()
        body = connection.execute(
            "SELECT envelope_bytes FROM capture_ingestion_payloads"
        ).fetchone()
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
    assert plan.journal_sequence == item["journal_sequence"]
    assert plan.event_sequence == event["event_sequence"]
    assert plan.recorded_at == event["recorded_at"]
    assert plan.envelope.to_bytes() == body[0]
    assert emit_owner_custody_plan(
        engine, submission, baseline=baseline, authority=owner
    ).to_bytes() == plan.to_bytes()
    record = RecoveryRecord(
        baseline=baseline, sequence=1, previous_sha256=baseline.artifact_sha256,
        kind="capture_custody", payload=plan.to_bytes(),
    )
    assert CaptureCustodyRecoveryPlan.from_record(
        RecoveryRecord.from_bytes(record.to_bytes())
    ) == plan


@pytest.mark.parametrize("invalid", [
    "owner", "epoch", "authority-epoch", "same-hash-metadata", "missing",
])
def test_emission_refuses_without_draining(queued: tuple[
    BrainEngine, CaptureSubmission, CaptureCustodyReceipt, RecoveryBaseline, EffectiveAuthority
], invalid: str) -> None:
    engine, submission, receipt, baseline, owner = queued
    if invalid == "owner":
        owner = replace(owner, owner=False)
    elif invalid == "epoch":
        baseline = replace(baseline, issuer_epoch=baseline.issuer_epoch + 1)
    elif invalid == "authority-epoch":
        owner = replace(owner, brain_id=receipt.brain_id, issuer_epoch=receipt.issuer_epoch + 1)
    elif invalid == "same-hash-metadata":
        original = submission
        submission = replace(
            submission, source_reference="synthetic:changed",
            provenance=replace(submission.provenance, source_ref="synthetic:changed"),
        )
        assert submission.request_sha256() == original.request_sha256()
    else:
        submission = replace(submission, delivery_id="synthetic.missing")
    with pytest.raises(ValueError):
        emit_owner_custody_plan(engine, submission, baseline=baseline, authority=owner)
    assert engine.ingestion.enqueue(queued[1]) == receipt
    with engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM capture_ingestion_events"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("field", ["extra", "journal_sequence", "event_sequence", "recorded_at"])
def test_closed_plan_refuses_malformed_fields(queued: tuple[
    BrainEngine, CaptureSubmission, CaptureCustodyReceipt, RecoveryBaseline, EffectiveAuthority
], field: str) -> None:
    engine, submission, _, baseline, owner = queued
    plan = emit_owner_custody_plan(engine, submission, baseline=baseline, authority=owner)
    value = json.loads(plan.to_bytes())
    value[field] = True
    from open_brain_engine.core.ids import portable_canonical_json_bytes

    with pytest.raises(ValueError):
        CaptureCustodyRecoveryPlan.from_bytes(portable_canonical_json_bytes(value))


def test_file_custody_preserves_original_bytes(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile,
        payload=FilePayload("synthetic.bin", "application/octet-stream", b"\0\xff"),
        delivery_id="synthetic.file.custody", privacy_tier="personal",
    )
    receipt = engine.ingestion.enqueue(submission)
    assert isinstance(receipt, CaptureCustodyReceipt)
    plan = emit_owner_custody_plan(
        engine, submission,
        baseline=RecoveryBaseline(receipt.brain_id, receipt.issuer_epoch, "0" * 64),
        authority=EffectiveAuthority(
            engine.profile.owner_actor_id, "recovery", frozenset(), None, owner=True
        ),
    )
    restored = CaptureCustodyRecoveryPlan.from_bytes(plan.to_bytes())
    assert restored.envelope.submission.payload == submission.payload


@pytest.mark.parametrize("progress", ["reserved", "terminal", "attempted"])
def test_progressed_custody_is_not_represented_as_initial(queued: tuple[
    BrainEngine, CaptureSubmission, CaptureCustodyReceipt, RecoveryBaseline, EffectiveAuthority
], progress: str) -> None:
    engine, submission, _, baseline, owner = queued
    if progress == "reserved":
        engine._faults.add(CaptureFault.AFTER_CAPTURE_RESERVATION)
        with pytest.raises(InjectedFault):
            engine.capture.submit(submission)
    elif progress == "terminal":
        engine.capture.submit(submission)
    else:
        engine.ingestion._terminal_metadata(
            submission.delivery_id, "attempt_failed", 1, "retryable"
        )
    with pytest.raises(ValueError, match="initial unreserved custody"):
        emit_owner_custody_plan(engine, submission, baseline=baseline, authority=owner)


@pytest.mark.parametrize("mismatch", ["kind", "baseline", "receipt-request", "receipt-destination"])
def test_record_and_cue_bindings_refuse_substitution(queued: tuple[
    BrainEngine, CaptureSubmission, CaptureCustodyReceipt, RecoveryBaseline, EffectiveAuthority
], mismatch: str) -> None:
    engine, submission, _, baseline, owner = queued
    plan = emit_owner_custody_plan(engine, submission, baseline=baseline, authority=owner)
    record = RecoveryRecord(
        baseline=baseline, sequence=1, previous_sha256=baseline.artifact_sha256,
        kind="capture_custody", payload=plan.to_bytes(),
    )
    with pytest.raises(ValueError):
        if mismatch == "kind":
            CaptureCustodyRecoveryPlan.from_record(replace(record, kind="capture"))
        elif mismatch == "baseline":
            CaptureCustodyRecoveryPlan.from_record(replace(
                record, baseline=replace(baseline, issuer_epoch=baseline.issuer_epoch + 1)
            ))
        elif mismatch == "receipt-request":
            replace(plan, receipt=replace(plan.receipt, request_sha256="1" * 64))
        else:
            replace(plan, receipt=replace(plan.receipt, issuer_epoch=baseline.issuer_epoch + 1))
