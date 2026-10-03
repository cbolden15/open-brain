"""Exact allocated identities must survive recovery, not be resubmitted anew."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    CaptureFault,
    CaptureSubmission,
    InjectedFault,
    TextPayload,
)
from open_brain_engine.engine.capture_recovery import (
    MAX_CAPTURE_RECOVERY_BYTES,
    CaptureRecoveryPlan,
    CaptureReservationIdentity,
)
from open_brain_engine.engine.contracts import JournalEnvelope
from open_brain_engine.engine.recovery_journal import RecoveryBaseline, RecoveryRecord

from open_brain.profile import compile_single_user_local

BASELINE = RecoveryBaseline("brn_" + "a" * 26, 1, "0" * 64)
WHEN = "2026-10-03T12:00:00Z"


def _plan(tmp_path: Path) -> CaptureRecoveryPlan:
    submission = CaptureSubmission.for_local_owner(
        profile=compile_single_user_local(tmp_path), payload=TextPayload("Synthetic exact body"),
        delivery_id="synthetic.recovery", title="Original supplied title", privacy_tier="personal",
    )
    return CaptureRecoveryPlan(
        BASELINE, JournalEnvelope(submission, submission.privacy),
        CaptureReservationIdentity.allocate(canonical=False, accepted_at=WHEN),
    )


def test_closed_plan_roundtrip_preserves_original_metadata(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    restored = CaptureRecoveryPlan.from_bytes(plan.to_bytes())
    assert restored == plan
    assert restored.envelope.submission.title == "Original supplied title"
    assert restored.envelope.submission.submission_path == plan.envelope.submission.submission_path
    assert restored.envelope.admitted_privacy == plan.envelope.admitted_privacy
    assert (
        restored.envelope.submission.request_sha256()
        == plan.envelope.submission.request_sha256()
    )
    assert restored.identities == plan.identities


def test_capture_plan_record_kind_and_destination_binding(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    record = RecoveryRecord(
        baseline=BASELINE, sequence=1, previous_sha256=BASELINE.artifact_sha256,
        kind="capture", payload=plan.to_bytes(),
    )
    assert CaptureRecoveryPlan.from_record(record) == plan
    with pytest.raises(ValueError):
        CaptureRecoveryPlan.from_record(replace(record, kind="control"))
    with pytest.raises(ValueError):
        CaptureRecoveryPlan.from_record(replace(
            record, baseline=replace(BASELINE, issuer_epoch=2),
        ))


def test_capture_plan_bound_before_json_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json, "loads", lambda *args, **kwargs: pytest.fail("decoded oversize"))
    with pytest.raises(ValueError, match="size"):
        CaptureRecoveryPlan.from_bytes(b"x" * (MAX_CAPTURE_RECOVERY_BYTES + 1))


@pytest.mark.parametrize("field,value", (
    ("capture_id", "capture_invalid"), ("canonical", True), ("canonical", 1),
    ("accepted_at", "2026-10-03"), ("page_id", "page_invalid"),
))
def test_invalid_identity_set_refuses(field: str, value: object) -> None:
    identities = CaptureReservationIdentity.allocate(canonical=False, accepted_at=WHEN)
    with pytest.raises(ValueError):
        replace(cast(Any, identities), **{field: value})


def test_canonical_set_and_action_binding(tmp_path: Path) -> None:
    identities = CaptureReservationIdentity.allocate(canonical=True, accepted_at=WHEN)
    assert all((identities.auto_proposal_id, identities.auto_proposal_receipt_id,
                identities.auto_decision_id, identities.auto_decision_receipt_id,
                identities.page_id, identities.publication_id))
    with pytest.raises(ValueError):
        replace(identities, auto_decision_receipt_id=identities.accepted_receipt_id)
    with pytest.raises(ValueError):
        replace(_plan(tmp_path), identities=identities)


@pytest.mark.parametrize("case", ("extra", "duplicate", "pretty", "wrong_action"))
def test_closed_decoder_refuses(tmp_path: Path, case: str) -> None:
    raw = _plan(tmp_path).to_bytes()
    value = json.loads(raw)
    if case == "extra":
        value["owner"] = True
        raw = portable_canonical_json_bytes(value)
    elif case == "duplicate":
        field = b'"contract_version":"capture-recovery-plan.v1"'
        raw = raw.replace(field, field + b"," + field)
    elif case == "pretty":
        raw = json.dumps(value, indent=2).encode()
    else:
        value["identities"]["canonical"] = True
        raw = portable_canonical_json_bytes(value)
    with pytest.raises(ValueError):
        CaptureRecoveryPlan.from_bytes(raw)


@pytest.mark.parametrize("canonical", (False, True))
@pytest.mark.parametrize("interrupted", (False, True))
def test_real_capture_reservation_uses_exact_allocated_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canonical: bool, interrupted: bool,
) -> None:
    identities = CaptureReservationIdentity.allocate(canonical=canonical, accepted_at=WHEN)
    monkeypatch.setattr(CaptureReservationIdentity, "allocate", lambda **kwargs: identities)
    profile = compile_single_user_local(tmp_path, starter_spaces=("Recovery",))
    engine = BrainEngine.open(
        profile, faults={CaptureFault.AFTER_CAPTURE_RESERVATION} if interrupted else set(),
    )
    submission = CaptureSubmission.for_local_owner(
        profile=profile, payload=TextPayload("Synthetic reservation"), delivery_id="identity",
        action=CaptureAction.CANONICAL_NOTE if canonical else CaptureAction.QUICK,
        space_id=engine.tasks.spaces.spaces()[0].space_id if canonical else None,
    )
    plan = CaptureRecoveryPlan(
        BASELINE, JournalEnvelope(submission, submission.privacy), identities,
    )
    assert CaptureRecoveryPlan.from_bytes(plan.to_bytes()) == plan
    if interrupted:
        with pytest.raises(InjectedFault):
            engine.tasks.capture.submit(submission)
        engine = BrainEngine.open(profile)
    receipt = engine.tasks.capture.submit(submission)
    assert receipt.capture_id == identities.capture_id
    with sqlite3.connect(tmp_path / ".open-brain/state/phase1.sqlite3") as connection:
        row = connection.execute(
            "SELECT capture_id,accepted_receipt_id,accepted_at,auto_proposal_id,"
            "auto_proposal_receipt_id,auto_decision_id,auto_decision_receipt_id,page_id,"
            "publication_id FROM captures WHERE delivery_id='identity'",
        ).fetchone()
    assert row == (
        identities.capture_id, identities.accepted_receipt_id, WHEN,
        identities.auto_proposal_id, identities.auto_proposal_receipt_id,
        identities.auto_decision_id, identities.auto_decision_receipt_id,
        identities.page_id, identities.publication_id,
    )
    assert engine.tasks.capture.submit(submission).capture_id == identities.capture_id
