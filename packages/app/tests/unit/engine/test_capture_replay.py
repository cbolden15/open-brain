"""Owner recovery replays exact admitted identities, never creates replacements."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyDecision, PrivacyTier
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
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.contracts import JournalEnvelope
from open_brain_engine.engine.portability import _manifest_digest
from open_brain_engine.engine.recovery_journal import RecoveryBaseline, RecoveryHead, RecoveryRecord
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local


def _fixture(
    tmp_path: Path, *, canonical: bool = False, file_payload: bool = False
) -> tuple[
    BrainEngine,
    EffectiveAuthority,
    CaptureRecoveryPlan,
    RecoveryRecord,
    RecoveryHead,
]:
    primary = BrainEngine.open(
        compile_single_user_local(
            tmp_path / "primary",
            starter_spaces=("Recovery",),
        )
    )
    archive = tmp_path / "baseline"
    primary.portability.export(archive, export_id="export_" + str(uuid4()))
    with primary._store.connect() as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    baseline = RecoveryBaseline(
        identity[0],
        identity[1],
        _manifest_digest(json.loads((archive / "portable-manifest.json").read_bytes())),
    )
    submission = CaptureSubmission.for_local_owner(
        profile=primary.profile,
        payload=(
            FilePayload("synthetic.txt", "text/plain", b"Synthetic retained file body")
            if file_payload
            else TextPayload("Synthetic retained plan body")
        ),
        delivery_id="recovery.owner",
        title="Original title",
        privacy_tier="personal",
        action=CaptureAction.CANONICAL_NOTE if canonical else CaptureAction.QUICK,
        space_id=primary.inbox.spaces()[0].space_id if canonical else None,
    )
    primary.capture.submit(submission)
    with primary._store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM captures WHERE delivery_id='recovery.owner'"
        ).fetchone()
    assert row is not None
    ids = CaptureReservationIdentity(
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
        JournalEnvelope(
            submission,
            PrivacyDecision.from_dict(json.loads(row["privacy_json"])),
        ),
        ids,
    )
    record = RecoveryRecord(
        baseline=baseline,
        sequence=1,
        previous_sha256=baseline.artifact_sha256,
        kind="capture",
        payload=plan.to_bytes(),
    )
    primary.portability.import_clean(
        archive, tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    (tmp_path / "primary").rename(tmp_path / "retained-unavailable-primary")
    archive.rename(tmp_path / "retained-unavailable-baseline")
    restored = BrainEngine.open(compile_single_user_local(tmp_path / "restored"))
    owner = EffectiveAuthority(
        restored.profile.owner_actor_id, "recovery", frozenset(), None, owner=True
    )
    head = RecoveryHead(baseline, 1, record.record_sha256)
    return restored, owner, plan, record, head


@pytest.mark.parametrize("canonical", (False, True))
@pytest.mark.parametrize("interrupted", (False, True))
def test_actual_owner_capture_replay_preserves_ids_after_primary_loss(
    tmp_path: Path,
    canonical: bool,
    interrupted: bool,
) -> None:
    engine, owner, plan, record, head = _fixture(tmp_path, canonical=canonical)
    if interrupted:
        engine._faults.add(CaptureFault.AFTER_CAPTURE_RESERVATION)
        with pytest.raises(InjectedFault):
            replay_owner_capture_chain(engine, (record,), expected_head=head, authority=owner)
        engine = BrainEngine.open(engine.profile)
    outcomes = replay_owner_capture_chain(engine, (record,), expected_head=head, authority=owner)
    assert outcomes[0].capture_id == plan.identities.capture_id
    with engine._store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM captures WHERE delivery_id='recovery.owner'"
        ).fetchone()
    assert row is not None
    assert row["accepted_receipt_id"] == plan.identities.accepted_receipt_id
    assert row["accepted_at"] == plan.identities.accepted_at
    assert row["title"] == plan.envelope.submission.title
    assert json.loads(row["privacy_json"]) == plan.envelope.admitted_privacy.to_dict()
    assert row["page_id"] == plan.identities.page_id
    assert row["publication_id"] == plan.identities.publication_id
    replay = replay_owner_capture_chain(engine, (record,), expected_head=head, authority=owner)
    assert replay[0].duplicate and replay[0].capture_id == plan.identities.capture_id


@pytest.mark.parametrize(
    "case",
    ("nonowner", "wrong_principal", "head", "epoch", "identity", "provider", "baseline"),
)
def test_invalid_owner_replay_refuses_before_capture_write(tmp_path: Path, case: str) -> None:
    engine, owner, plan, record, head = _fixture(tmp_path)
    if case == "nonowner":
        owner = replace(owner, owner=False)
    elif case == "wrong_principal":
        owner = replace(owner, principal_id="another_owner")
    elif case == "head":
        head = replace(head, record_sha256="1" * 64)
    elif case == "epoch":
        baseline = replace(plan.baseline, issuer_epoch=plan.baseline.issuer_epoch + 1)
        plan = replace(plan, baseline=baseline)
        record = replace(record, baseline=baseline, payload=plan.to_bytes())
        head = RecoveryHead(baseline, 1, record.record_sha256)
    elif case == "provider":
        owner = replace(
            owner,
            egress_mode=EgressMode.EXTERNAL_PROVIDER,
            provider_id="openai",
            consent_id="consent_" + "1" * 32,
            brain_id=plan.baseline.brain_id,
            issuer_epoch=plan.baseline.issuer_epoch,
        )
    elif case == "baseline":
        ready = engine.profile.root / ".open-brain/state/portability-ready.json"
        ready.rename(ready.with_suffix(".retained"))
    else:
        engine.capture.accept(
            TextPayload("Synthetic retained plan body"),
            delivery_id="recovery.owner",
            title="Original title",
            privacy_tier=PrivacyTier.PERSONAL,
        )
    database = engine.profile.root / ".open-brain/state/phase1.sqlite3"
    before = database.read_bytes()
    with pytest.raises(ValueError):
        replay_owner_capture_chain(engine, (record,), expected_head=head, authority=owner)
    assert database.read_bytes() == before


@pytest.mark.parametrize("case", ("collision", "foreign_profile", "current_secret"))
def test_chain_preflight_refuses_later_record_before_first_write(
    tmp_path: Path,
    case: str,
) -> None:
    engine, owner, plan, record, _ = _fixture(tmp_path)
    submission = replace(plan.envelope.submission, delivery_id="recovery.second")
    identities = CaptureReservationIdentity.allocate(
        canonical=False,
        accepted_at=plan.identities.accepted_at,
    )
    if case == "collision":
        identities = replace(identities, accepted_receipt_id=plan.identities.accepted_receipt_id)
    elif case == "foreign_profile":
        foreign = compile_single_user_local(tmp_path / "foreign")
        submission = CaptureSubmission.for_local_owner(
            profile=foreign,
            payload=TextPayload("Synthetic foreign body"),
            delivery_id="recovery.second",
            privacy_tier=PrivacyTier.PERSONAL,
        )
    else:
        engine._boundary_classifier = lambda value: (
            PrivacyTier.SECRET if value.delivery_id == "recovery.second" else None
        )
    second_plan = replace(
        plan,
        identities=identities,
        envelope=JournalEnvelope(submission, plan.envelope.admitted_privacy),
    )
    second = RecoveryRecord(
        baseline=plan.baseline,
        sequence=2,
        previous_sha256=record.record_sha256,
        kind="capture",
        payload=second_plan.to_bytes(),
    )
    head = RecoveryHead(plan.baseline, 2, second.record_sha256)
    database = engine.profile.root / ".open-brain/state/phase1.sqlite3"
    before = database.read_bytes()
    with pytest.raises(ValueError):
        replay_owner_capture_chain(engine, (record, second), expected_head=head, authority=owner)
    assert database.read_bytes() == before


def test_same_request_does_not_authorize_changed_retained_privacy(tmp_path: Path) -> None:
    engine, owner, plan, record, head = _fixture(tmp_path)
    replay_owner_capture_chain(engine, (record,), expected_head=head, authority=owner)
    changed_submission = CaptureSubmission.for_local_owner(
        profile=engine.profile,
        payload=plan.envelope.submission.payload,
        delivery_id=plan.envelope.submission.delivery_id,
        title=plan.envelope.submission.title,
        privacy_tier=PrivacyTier.WORK,
    )
    assert changed_submission.request_sha256() == plan.envelope.submission.request_sha256()
    changed = replace(
        plan, envelope=JournalEnvelope(changed_submission, changed_submission.privacy)
    )
    record = replace(record, payload=changed.to_bytes())
    head = RecoveryHead(plan.baseline, 1, record.record_sha256)
    database = engine.profile.root / ".open-brain/state/phase1.sqlite3"
    before = database.read_bytes()
    with pytest.raises(ValueError):
        replay_owner_capture_chain(engine, (record,), expected_head=head, authority=owner)
    assert database.read_bytes() == before


@pytest.mark.parametrize("chain", ("alone", "completed_first", "new_first"))
@pytest.mark.parametrize(
    "target,damage",
    (("source", "changed"), ("source", "missing"), ("blob", "changed"), ("blob", "missing")),
)
def test_repeated_capture_replay_refuses_damaged_completed_files_before_any_write(
    tmp_path: Path,
    target: str,
    damage: str,
    chain: str,
) -> None:
    """The damaged completed record may sit anywhere in the chain.

    ``new_first`` places an unprocessed capture ahead of the damaged completed
    one: the whole-chain preflight must refuse before the new record writes.
    """
    engine, owner, plan, record, head = _fixture(tmp_path, file_payload=True)
    replay_owner_capture_chain(engine, (record,), expected_head=head, authority=owner)
    with engine._store.connect() as connection:
        row = connection.execute(
            "SELECT source_path,payload_json FROM captures WHERE delivery_id='recovery.owner'"
        ).fetchone()
    if target == "source":
        path = engine.profile.root / row["source_path"]
    else:
        digest = json.loads(row["payload_json"])["blob_sha256"]
        path = engine.profile.root / f"sources/blobs/sha256/{digest[:2]}/{digest}"
    raw = path.read_bytes()
    if damage == "changed":
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    else:
        path.unlink()
    records: tuple[RecoveryRecord, ...] = (record,)
    if chain != "alone":
        new_plan = replace(
            plan,
            identities=CaptureReservationIdentity.allocate(
                canonical=False, accepted_at=plan.identities.accepted_at
            ),
            envelope=JournalEnvelope(
                replace(plan.envelope.submission, delivery_id="recovery.second"),
                plan.envelope.admitted_privacy,
            ),
        )
        ordered = (plan, new_plan) if chain == "completed_first" else (new_plan, plan)
        records = ()
        previous = plan.baseline.artifact_sha256
        for sequence, chained_plan in enumerate(ordered, start=1):
            records += (
                RecoveryRecord(
                    baseline=plan.baseline,
                    sequence=sequence,
                    previous_sha256=previous,
                    kind="capture",
                    payload=chained_plan.to_bytes(),
                ),
            )
            previous = records[-1].record_sha256
        head = RecoveryHead(plan.baseline, 2, records[-1].record_sha256)
    database = engine.profile.root / ".open-brain/state/phase1.sqlite3"
    before = database.read_bytes()
    with pytest.raises(ValueError, match=f"completed {target} mismatch"):
        replay_owner_capture_chain(engine, records, expected_head=head, authority=owner)
    assert database.read_bytes() == before
    with engine._store.connect() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM captures WHERE delivery_id='recovery.second'"
            ).fetchone()
            is None
        )
    if damage == "changed":
        assert path.read_bytes() == raw[:-1] + bytes([raw[-1] ^ 1])
    else:
        assert not path.exists()
