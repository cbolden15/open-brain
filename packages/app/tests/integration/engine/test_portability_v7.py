from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.engine import ReferencePayload, open_local_engine
from open_brain_engine.engine.contracts import (
    CaptureFault,
    InjectedFault,
    PortabilityFault,
    PublicJobCaptureContext,
)
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.engine.sharing import EligibilityMode, sharing_eligible
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingError,
    SharingInspectRequest,
    SharingRevokeRequest,
)
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.portable.v7 import SHARING_APPROVALS_PATH
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission
from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


@pytest.mark.parametrize("state", ["approved", "rejected", "revoked"])
def test_current_portable_sharing_roundtrip(tmp_path: Path, state: str) -> None:
    tasks, request, owner, _text = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    approved = tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.portable.approve",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="reject" if state == "rejected" else "approve",
        ),
        authority=owner,
    )
    assert (approved.copy_capture_id is not None) == (state != "rejected")
    if state == "revoked":
        tasks.sharing.revoke(
            SharingRevokeRequest(
                operation_id="sharing.portable.revoke",
                approval_id=approved.approval_id,
                expected_approval_version=1,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                reason="owner_choice",
            ),
            authority=owner,
        )
    export = tmp_path / "export"
    receipt = tasks.portability.export(export, export_id="export_" + str(uuid4()))
    assert receipt.schema_version == 9
    snapshot = validated_portable_snapshot(export)
    assert SHARING_APPROVALS_PATH in snapshot.files
    imported = tmp_path / "imported"
    import_id = "import_" + str(uuid4())
    restored = tasks.portability.import_clean(export, imported, import_id=import_id)
    assert restored.schema_version == 9
    duplicate = tasks.portability.import_clean(export, imported, import_id=import_id)
    assert duplicate.duplicate and duplicate.schema_version == restored.schema_version
    with sqlite3.connect(imported / PHASE1_STATE_DATABASE) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == int(
            state != "rejected"
        )
        assert connection.execute("SELECT count(*) FROM sharing_revocations").fetchone()[0] == int(
            state == "revoked"
        )
        assert connection.execute("SELECT imported FROM sharing_previews").fetchone()[0] == 1
        if approved.copy_capture_id is not None:
            assert sharing_eligible(
                connection,
                approved.copy_capture_id,
                mode=EligibilityMode.EXTERNAL_READ,
                provider_id="openai",
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                profile=compile_single_user_local(imported),
            ) == (state == "approved")
    imported_tasks = open_local_engine(compile_single_user_local(imported))
    assert imported_tasks.sharing is not None
    with pytest.raises(SharingError, match="preview_expired"):
        imported_tasks.sharing.decide(
            SharingDecisionRequest(
                operation_id="sharing.portable.imported-decision",
                preview_id=preview.preview_id,
                preview_sha256=preview.preview_sha256,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                expected_decision_version=0,
                decision="approve",
            ),
            authority=owner,
        )
    if state == "approved":
        with sqlite3.connect(imported / PHASE1_STATE_DATABASE) as connection:
            connection.execute(
                "UPDATE source_revisions SET source_bytes=? WHERE capture_id=?",
                (b"{}", approved.copy_capture_id),
            )
        with pytest.raises(ValueError):
            tasks.portability.import_clean(export, imported, import_id=import_id)


def test_portable_v7_refuses_pending_copy_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)

    def pending(_submission: object) -> None:
        raise RuntimeError("synthetic pending copy")

    with monkeypatch.context() as patch:
        patch.setattr(tasks.sharing._engine.capture, "submit", pending)
        receipt = tasks.sharing.decide(
            SharingDecisionRequest(
                operation_id="sharing.portable.pending",
                preview_id=preview.preview_id,
                preview_sha256=preview.preview_sha256,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                expected_decision_version=0,
                decision="approve",
            ),
            authority=owner,
        )
    assert receipt.state == "pending"
    with pytest.raises(ValueError, match="ingestion_pending"):
        tasks.portability.export(tmp_path / "pending-export", export_id="export_" + str(uuid4()))
    reconciled = open_local_engine(tasks.profile)
    assert reconciled.sharing is not None
    terminal = reconciled.sharing.inspect(
        SharingInspectRequest(subject_id=preview.preview_id), authority=owner
    )
    assert terminal.copy_capture_id is not None
    assert terminal.decision_receipt is not None
    assert terminal.decision_receipt["state"] == "captured"
    exported = tmp_path / "reconciled-export"
    assert (
        reconciled.portability.export(exported, export_id="export_" + str(uuid4())).schema_version
        == 9
    )
    assert validated_portable_snapshot(exported).manifest["schema_version"] == 9


def test_frozen_portable_v6_import_creates_no_sharing_approvals(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[5] / "tests/fixtures/portable-v6-c663828"
    assert validated_portable_snapshot(fixture).manifest["schema_version"] == 6
    tasks = open_local_engine(compile_single_user_local(tmp_path / "caller"))
    imported = tmp_path / "imported-v6"
    receipt = tasks.portability.import_clean(fixture, imported, import_id="import_" + str(uuid4()))
    assert receipt.schema_version == 6
    with sqlite3.connect(imported / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0


@pytest.mark.parametrize("preview_count", [2, 3])
def test_portable_v7_restores_multiple_previews_and_exact_authority(
    tmp_path: Path, preview_count: int
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    for index in range(preview_count):
        preview = tasks.sharing.preview(
            replace(request, operation_id=f"sharing.multiple.preview.{index}"), authority=owner
        )
        if index < 2:
            tasks.sharing.decide(
                SharingDecisionRequest(
                    operation_id=f"sharing.multiple.decision.{index}",
                    preview_id=preview.preview_id,
                    preview_sha256=preview.preview_sha256,
                    brain_id=request.brain_id,
                    issuer_epoch=request.issuer_epoch,
                    destination_brain_id=request.brain_id,
                    expected_decision_version=0,
                    decision="approve" if index == 0 else "reject",
                ),
                authority=owner,
            )
    export, restored, again = (tmp_path / name for name in ("export", "restored", "again"))
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    before = validated_portable_snapshot(export)
    import_id = "import_" + str(uuid4())
    tasks.portability.import_clean(export, restored, import_id=import_id)
    assert tasks.portability.import_clean(export, restored, import_id=import_id).duplicate
    with sqlite3.connect(restored / PHASE1_STATE_DATABASE) as connection:
        assert (
            connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0]
            == preview_count
        )
        assert (
            connection.execute("SELECT count(*) FROM sharing_previews WHERE imported=1").fetchone()[
                0
            ]
            == preview_count
        )
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 1
    reopened = open_local_engine(compile_single_user_local(restored))
    reopened.portability.export(again, export_id="export_" + str(uuid4()))
    after = validated_portable_snapshot(again)
    assert all(
        after.files[path] == data
        for path, data in before.files.items()
        if path != "portable-manifest.json"
    )


def _advance_original(tasks, request) -> None:  # type: ignore[no-untyped-def]
    assert tasks.sharing is not None and tasks.sources is not None
    with tasks.sharing._engine._store.transaction() as connection:
        retained = json.loads(
            connection.execute(
                "SELECT envelope_bytes FROM managed_source_deliveries WHERE source_id=?",
                (request.source_id,),
            ).fetchone()[0]
        )
    privacy = PrivacyDecision.from_dict(retained["submission"]["capture"]["privacy"])
    original = retained["submission"]["capture"]
    context = PublicJobCaptureContext.create(
        profile=tasks.profile, actor_id=original["actor_id"], role_claim=original["role_claim"]
    )
    capture = replace(
        _public_submission(tasks, context=context),
        payload=ReferencePayload("https://example.test/synthetic", "Synthetic next revision"),
        privacy=privacy,
        delivery_id="synthetic.portable.next",
    )
    binding = SourceRevisionBinding(**retained["binding"])
    submission = SourceRevisionSubmission(
        capture=capture,
        namespace=binding.namespace,
        revision_key="synthetic-portable-next",
        canonical_sha256=capture.request_sha256(),
        expected_head=request.expected_head,
        ordering={"kind": "predecessor", "revision_key": "synthetic-revision"},
        expected_control_epoch=0,
    )
    observation = SourceRevisionObservation(
        original_sha256="c" * 64,
        transformed_sha256="d" * 64,
        normalization_version="saved-markdown-continuous.v1",
        privacy_policy_version=privacy.policy_version,
        privacy_policy_sha256=sha256(canonical(privacy.to_dict())).hexdigest(),
        admitted_payload_sha256=sha256(canonical(capture.payload.to_dict())).hexdigest(),
    )
    receipt = tasks.sources.public_revision_sink(binding).submit(
        SourceRevisionObservedDelivery(
            binding=binding,
            submission=submission,
            expected_lifecycle_version=0,
            delivery_id="synthetic.portable.next.delivery",
            observation=observation,
        )
    )
    assert receipt.outcome == "captured"


@pytest.mark.parametrize(
    "state",
    [
        "active",
        "superseded",
        "withdrawn",
        "rejected",
        "revoked",
        "undecided",
    ],
)
def test_portable_v7_multiple_preview_lifecycle_roundtrip_and_receipt_replay(
    tmp_path: Path, state: str
) -> None:
    tasks, request, owner, text = _managed_source(tmp_path)
    assert tasks.sharing is not None
    previews = [
        tasks.sharing.preview(
            replace(request, operation_id=f"sharing.states.preview.{index}"), authority=owner
        )
        for index in range(3)
    ]
    decisions = []
    receipts = []
    # Every archive has a retained rejection and an executable-local-only
    # undecided preview, alongside the parametrized primary lifecycle state.
    for index in [1] if state == "undecided" else [0, 1]:
        preview = previews[index]
        decision = SharingDecisionRequest(
            operation_id=f"sharing.states.decision.{index}",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="reject" if index == 1 or state == "rejected" else "approve",
        )
        decisions.append(decision)
        receipts.append(tasks.sharing.decide(decision, authority=owner))
    revocation = None
    revoke_receipt = None
    if state == "revoked":
        revocation = SharingRevokeRequest(
            operation_id="sharing.states.revoke",
            approval_id=receipts[0].approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        )
        revoke_receipt = tasks.sharing.revoke(revocation, authority=owner)
    if state == "withdrawn":
        assert tasks.sources is not None
        tasks.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.sharing.states",
                source_id=request.source_id,
                expected_head=request.expected_head,
                expected_lifecycle_version=0,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                reason_code="owner_choice",
            ),
            authority=owner,
        )
    if state == "superseded":
        _advance_original(tasks, request)
    export, imported, again = (tmp_path / name for name in ("export", "imported", "again"))
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    before = validated_portable_snapshot(export)
    import_id = "import_" + str(uuid4())
    tasks.portability.import_clean(export, imported, import_id=import_id)
    assert tasks.portability.import_clean(export, imported, import_id=import_id).duplicate
    reopened = open_local_engine(compile_single_user_local(imported))
    assert reopened.sharing is not None
    for decision, receipt in zip(decisions, receipts, strict=True):
        assert reopened.sharing.decide(decision, authority=owner) == receipt
    if revocation is not None:
        assert reopened.sharing.revoke(revocation, authority=owner) == revoke_receipt
    inspection = reopened.sharing.inspect(
        SharingInspectRequest(subject_id=previews[2].preview_id), authority=owner
    )
    assert inspection.preview.text == text and inspection.decision is None
    refused = SharingDecisionRequest(
        operation_id="sharing.states.imported.undecided",
        preview_id=previews[2].preview_id,
        preview_sha256=previews[2].preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    with pytest.raises(SharingError, match="preview_expired"):
        reopened.sharing.decide(refused, authority=owner)
    with sqlite3.connect(imported / PHASE1_STATE_DATABASE) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == len(
            decisions
        )
        copy_id = receipts[0].copy_capture_id
        if copy_id is not None:
            assert sharing_eligible(
                connection,
                copy_id,
                mode=EligibilityMode.EXTERNAL_READ,
                provider_id="openai",
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                profile=reopened.profile,
            ) == (state == "active")
        assert connection.execute("SELECT count(*) FROM managed_consents").fetchone()[0] == 0
    reopened.portability.export(again, export_id="export_" + str(uuid4()))
    after = validated_portable_snapshot(again)
    assert {
        path: data for path, data in before.files.items() if path != "portable-manifest.json"
    } == {path: data for path, data in after.files.items() if path != "portable-manifest.json"}


@pytest.mark.parametrize("fault", list(PortabilityFault))
def test_portable_v7_sharing_import_promotion_faults(
    tmp_path: Path, fault: PortabilityFault
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.fault.approve",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="approve",
        ),
        authority=owner,
    )
    export, target = tmp_path / "export", tmp_path / "target"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    before = validated_portable_snapshot(export)
    control = BrainEngine.open(compile_single_user_local(tmp_path / "control"), faults={fault})
    import_id = "import_" + str(uuid4())
    with pytest.raises(InjectedFault):
        control.portability.import_clean(export, target, import_id=import_id)
    assert target.exists() == (fault is PortabilityFault.AFTER_PROMOTION)
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".target")]
    recovered = control.portability.import_clean(export, target, import_id=import_id)
    assert recovered.duplicate == (fault is PortabilityFault.AFTER_PROMOTION)
    reopened = open_local_engine(compile_single_user_local(target))
    again = tmp_path / "again"
    reopened.portability.export(again, export_id="export_" + str(uuid4()))
    after = validated_portable_snapshot(again)
    assert {
        path: data for path, data in before.files.items() if path != "portable-manifest.json"
    } == {path: data for path, data in after.files.items() if path != "portable-manifest.json"}


@pytest.mark.parametrize(
    "table,column,replacement",
    [
        ("sharing_previews", "preview_bytes", b"{}"),
        ("sharing_decisions", "receipt_bytes", b"{}"),
        ("sharing_links", "marker", "urn:open-brain:sharing-copy:v1:forged"),
        ("sharing_revocations", "reason", "forged_reason"),
        ("sharing_job_identity", "issuer_epoch", 2),
    ],
)
def test_portable_v7_duplicate_import_audits_every_sharing_family(
    tmp_path: Path, table: str, column: str, replacement: object
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    approval = tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.audit.approve",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="approve",
        ),
        authority=owner,
    )
    tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="sharing.audit.revoke",
            approval_id=approval.approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        ),
        authority=owner,
    )
    export, imported = tmp_path / "export", tmp_path / "imported"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    import_id = "import_" + str(uuid4())
    tasks.portability.import_clean(export, imported, import_id=import_id)
    assert tasks.portability.import_clean(export, imported, import_id=import_id).duplicate
    with sqlite3.connect(imported / PHASE1_STATE_DATABASE) as connection:
        # Simulate damaged durable bytes, retaining the exact schema. Ordinary
        # writes must first prove that the immutable authority trigger refuses.
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"UPDATE {table} SET {column}=?", (replacement,))  # noqa: S608
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=? "
            "AND sql LIKE '%BEFORE UPDATE%'",
            (table,),
        ).fetchall()
        assert len(triggers) == 1
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')  # noqa: S608
        connection.execute(f"UPDATE {table} SET {column}=?", (replacement,))  # noqa: S608
        for _, statement in triggers:
            connection.execute(statement)
    with pytest.raises(ValueError):
        tasks.portability.import_clean(export, imported, import_id=import_id)
    with sqlite3.connect(imported / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute(f"SELECT {column} FROM {table}").fetchone()[0] == replacement  # noqa: S608


@pytest.mark.parametrize(
    "fault",
    [
        CaptureFault.AFTER_JOURNAL_COMMIT,
        CaptureFault.AFTER_CAPTURE_RESERVATION,
        CaptureFault.AFTER_SOURCE_WRITE,
        CaptureFault.AFTER_INDEX_UPDATE,
        CaptureFault.AFTER_CANONICAL_COMPLETION,
    ],
)
def test_portable_v7_journal_copy_pending_refusal_then_exact_reconciliation(
    tmp_path: Path, fault: CaptureFault
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    decision = SharingDecisionRequest(
        operation_id="sharing.journal.approve",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    tasks.sharing._engine._faults.add(fault)
    pending = tasks.sharing.decide(decision, authority=owner)
    assert pending.state == "pending" and pending.copy_capture_id is None
    with sqlite3.connect(tasks.profile.root / PHASE1_STATE_DATABASE) as connection:
        frozen = connection.execute(
            "SELECT copy_delivery_id,copy_submission_sha256,copy_submission_bytes "
            "FROM sharing_decisions"
        ).fetchone()
        assert (
            connection.execute("SELECT count(*) FROM capture_ingestion_pending").fetchone()[0] == 1
        )
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0
    refused = tmp_path / "pending-export"
    with pytest.raises(ValueError, match="ingestion_pending"):
        tasks.portability.export(refused, export_id="export_" + str(uuid4()))
    assert not refused.exists()
    reconciled = open_local_engine(tasks.profile)
    assert reconciled.sharing is not None
    terminal = reconciled.sharing.decide(decision, authority=owner)
    assert terminal.state == "captured" and terminal.copy_capture_id is not None
    assert reconciled.sharing.decide(decision, authority=owner) == terminal
    with sqlite3.connect(tasks.profile.root / PHASE1_STATE_DATABASE) as connection:
        assert (
            connection.execute(
                "SELECT copy_delivery_id,copy_submission_sha256,copy_submission_bytes "
                "FROM sharing_decisions"
            ).fetchone()
            == frozen
        )
        assert (
            connection.execute("SELECT count(*) FROM capture_ingestion_pending").fetchone()[0] == 0
        )
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM captures WHERE delivery_id=?", (terminal.copy_delivery_id,)
            ).fetchone()[0]
            == 1
        )
    export = tmp_path / "reconciled-export"
    reconciled.portability.export(export, export_id="export_" + str(uuid4()))
    assert validated_portable_snapshot(export).manifest["schema_version"] == 9


def test_portable_v7_managed_source_pending_refusal_then_exact_reconciliation(
    tmp_path: Path,
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    tasks.sharing.preview(request, authority=owner)
    tasks.sharing._engine._faults.add(CaptureFault.AFTER_CAPTURE_RESERVATION)
    with pytest.raises(InjectedFault):
        _advance_original(tasks, request)
    with sqlite3.connect(tasks.profile.root / PHASE1_STATE_DATABASE) as connection:
        frozen = connection.execute(
            "SELECT envelope_bytes FROM managed_source_deliveries WHERE receipt_json IS NULL"
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT count(*) FROM source_intakes WHERE receipt_json IS NULL"
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(ValueError, match="ingestion_pending"):
        tasks.portability.export(tmp_path / "pending-export", export_id="export_" + str(uuid4()))
    reconciled = open_local_engine(tasks.profile)
    _advance_original(reconciled, request)
    with sqlite3.connect(tasks.profile.root / PHASE1_STATE_DATABASE) as connection:
        assert (
            connection.execute(
                "SELECT envelope_bytes FROM managed_source_deliveries WHERE delivery_id=?",
                ("synthetic.portable.next.delivery",),
            ).fetchone()[0]
            == frozen
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM managed_source_deliveries WHERE receipt_json IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM source_intakes WHERE receipt_json IS NULL"
            ).fetchone()[0]
            == 0
        )
    export = tmp_path / "reconciled-export"
    reconciled.portability.export(export, export_id="export_" + str(uuid4()))
    assert validated_portable_snapshot(export).manifest["schema_version"] == 9
