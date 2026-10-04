"""Interrupted owner captures recover from baseline and exact reserved plans."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    CaptureFault,
    CaptureSubmission,
    FilePayload,
    InjectedFault,
    TextPayload,
)
from open_brain_engine.engine.capture_recovery import (
    CaptureRecoveryPlan,
    CaptureReservationIdentity,
)
from open_brain_engine.engine.capture_replay import replay_owner_capture_chain
from open_brain_engine.engine.contracts import JournalEnvelope
from open_brain_engine.engine.portability import _manifest_digest, restore_portable_clean
from open_brain_engine.engine.recovery_journal import RecoveryBaseline, RecoveryHead, RecoveryRecord
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local


@pytest.mark.parametrize(
    "variant,fault",
    (
        ("quick", CaptureFault.AFTER_CAPTURE_RESERVATION),
        ("quick", CaptureFault.AFTER_SOURCE_WRITE),
        ("canonical", CaptureFault.AFTER_CAPTURE_RESERVATION),
        ("canonical", CaptureFault.AFTER_SOURCE_WRITE),
        ("canonical", CaptureFault.AFTER_AUTOMATIC_PROPOSAL_WRITE),
        ("canonical", CaptureFault.AFTER_AUTOMATIC_DECISION_WRITE),
        ("canonical", CaptureFault.AFTER_CANONICAL_PAGE_WRITE),
        ("canonical", CaptureFault.AFTER_PUBLICATION_WRITE),
        ("file", CaptureFault.AFTER_CAPTURE_RESERVATION),
        ("file", CaptureFault.AFTER_BLOB_WRITE),
        ("file", CaptureFault.AFTER_SOURCE_WRITE),
    ),
)
def test_partial_owner_capture_recovers_original_allocation_without_primary(
    tmp_path: Path,
    variant: str,
    fault: CaptureFault,
) -> None:
    canonical = variant == "canonical"
    primary_path = tmp_path / "primary"
    primary = BrainEngine.open(
        compile_single_user_local(primary_path, starter_spaces=("Recovery",))
    )
    archive = tmp_path / "baseline"
    primary.portability.export(archive, export_id="export_" + str(uuid4()))
    with primary._store.connect() as connection:
        brain_id, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    baseline = RecoveryBaseline(
        brain_id,
        epoch,
        _manifest_digest(json.loads((archive / "portable-manifest.json").read_bytes())),
    )
    submission = CaptureSubmission.for_local_owner(
        profile=primary.profile,
        payload=(
            FilePayload("synthetic.txt", "text/plain", b"Synthetic interrupted original file bytes")
            if variant == "file"
            else TextPayload("Synthetic interrupted original body")
        ),
        delivery_id="recovery.partial.owner",
        title="Original interrupted title",
        privacy_tier="personal",
        action=CaptureAction.CANONICAL_NOTE if canonical else CaptureAction.QUICK,
        space_id=primary.inbox.spaces()[0].space_id if canonical else None,
    )
    primary._faults.add(fault)
    with pytest.raises(InjectedFault):
        primary.capture.submit(submission)
    with primary._store.connect() as connection:
        row = dict(
            connection.execute(
                "SELECT * FROM captures WHERE delivery_id=?", (submission.delivery_id,)
            ).fetchone()
        )
    assert row["stage"] < 3
    identities = CaptureReservationIdentity(
        capture_id=row["capture_id"],
        accepted_receipt_id=row["accepted_receipt_id"],
        accepted_at=row["accepted_at"],
        canonical=canonical,
        auto_proposal_id=row["auto_proposal_id"],
        auto_proposal_receipt_id=row["auto_proposal_receipt_id"],
        auto_decision_id=row["auto_decision_id"],
        auto_decision_receipt_id=row["auto_decision_receipt_id"],
        page_id=row["page_id"],
        publication_id=row["publication_id"],
    )
    plan = CaptureRecoveryPlan(
        baseline,
        JournalEnvelope(submission, PrivacyDecision.from_dict(json.loads(row["privacy_json"]))),
        identities,
    )
    record = RecoveryRecord(
        baseline=baseline,
        sequence=1,
        previous_sha256=baseline.artifact_sha256,
        kind="capture",
        payload=plan.to_bytes(),
    )
    # Only this encoded recovery record and the earlier archive remain available
    # to recovery. This is a semantic drill, not independent-backend protection.
    encoded = record.to_bytes()
    expected_head = RecoveryHead(baseline, 1, record.record_sha256)
    owner = EffectiveAuthority(
        primary.profile.owner_actor_id, "recovery", frozenset(), None, owner=True
    )
    primary_path.rename(tmp_path / "unavailable-primary")
    del primary
    restored_path = tmp_path / "restored"
    restore_portable_clean(
        archive, restored_path, import_id="import_" + str(uuid4()), authority=owner
    )
    restored = BrainEngine.open(compile_single_user_local(restored_path))
    restored_record = RecoveryRecord.from_bytes(encoded)
    result = replay_owner_capture_chain(
        restored, (restored_record,), expected_head=expected_head, authority=owner
    )
    assert result[0].capture_id == row["capture_id"]
    with restored._store.connect() as connection:
        recovered = dict(
            connection.execute(
                "SELECT * FROM captures WHERE delivery_id=?", (submission.delivery_id,)
            ).fetchone()
        )
    for name in (
        "capture_id",
        "accepted_receipt_id",
        "accepted_at",
        "title",
        "privacy_json",
        "payload_json",
        "file_bytes",
        "auto_proposal_id",
        "auto_proposal_receipt_id",
        "auto_decision_id",
        "auto_decision_receipt_id",
        "page_id",
        "publication_id",
    ):
        assert recovered[name] == row[name]
    assert recovered["stage"] == 3
    replay = restored.capture.submit(submission)
    assert replay.duplicate and replay.capture_id == row["capture_id"]
    assert not primary_path.exists()
