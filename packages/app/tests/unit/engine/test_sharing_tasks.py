from __future__ import annotations

import json
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import ReferencePayload, open_local_engine
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing import EligibilityMode, sharing_eligible
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingError,
    SharingInspectRequest,
    SharingPreviewRequest,
    SharingRevokeRequest,
    parse_sharing_request,
)
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


def _preview() -> SharingPreviewRequest:
    return SharingPreviewRequest(
        operation_id="sharing.synthetic-preview",
        source_id="source_synthetic",
        expected_head="capture_synthetic",
        expected_head_version=1,
        expected_lifecycle_version=0,
        expected_route_version=0,
        brain_id="brn_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        issuer_epoch=1,
        provider_ids=("anthropic", "openai"),
    )


@pytest.mark.parametrize("version", [True, 1.0])
def test_sharing_preview_rejects_non_integer_versions(version: object) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(_preview(), dto_version=version)  # type: ignore[arg-type]


def test_sharing_request_parser_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        parse_sharing_request(b'{"dto_version":1,"dto_version":1}', SharingPreviewRequest)
    with pytest.raises(SharingError, match="invalid_arguments"):
        parse_sharing_request(b'{"dto_version":NaN}', SharingPreviewRequest)


def test_sharing_decision_is_bound_to_one_preview_and_destination() -> None:
    decision = SharingDecisionRequest(
        operation_id="sharing.synthetic-decision",
        preview_id="00000000-0000-4000-8000-000000000001",
        preview_sha256="a" * 64,
        brain_id="brn_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        issuer_epoch=1,
        destination_brain_id="brn_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        expected_decision_version=0,
        decision="approve",
    )
    assert decision.request_sha256 != replace(decision, decision="reject").request_sha256
    assert (
        decision.request_sha256
        != replace(decision, destination_brain_id="brn_bbbbbbbbbbbbbbbbbbbbbbbbbb").request_sha256
    )


def _managed_source(  # type: ignore[no-untyped-def]
    tmp_path: Path, text: str = "Synthetic retained body 漢字\n", *, adopt_alias: bool = False
):
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    privacy = PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="policy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )
    original = _public_submission(tasks)
    capture = replace(
        original,
        payload=ReferencePayload("https://example.test/synthetic", text),
        privacy=privacy,
        delivery_id="synthetic.saved.original",
    )
    namespace = {
        "connector_name": "saved_markdown",
        "connection_id": "synthetic-connection",
        "resource_id": "synthetic-resource",
        "external_id": "item:synthetic",
    }
    baseline = tasks.capture.submit(capture) if adopt_alias else None
    submission = SourceRevisionSubmission(
        capture=capture,
        namespace=namespace,
        revision_key="synthetic-revision",
        canonical_sha256=capture.request_sha256(),
        expected_head=None if baseline is None else baseline.capture_id,
        ordering={"kind": "unordered"},
        expected_control_epoch=0,
    )
    with open_local_database_read_only(profile) as connection:
        brain_id, issuer_epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    binding = SourceRevisionBinding(
        destination_brain_id=brain_id,
        issuer_epoch=issuer_epoch,
        root_fingerprint="synthetic-root",
        accepted_source_id="synthetic-selection",
        namespace=namespace,
    )
    observation = SourceRevisionObservation(
        original_sha256="a" * 64,
        transformed_sha256="b" * 64,
        normalization_version="saved-markdown-continuous.v1",
        privacy_policy_version=privacy.policy_version,
        privacy_policy_sha256=sha256(portable_canonical_json_bytes(privacy.to_dict())).hexdigest(),
        admitted_payload_sha256=sha256(
            portable_canonical_json_bytes(capture.payload.to_dict())
        ).hexdigest(),
    )
    delivery = SourceRevisionObservedDelivery(
        binding=binding,
        submission=submission,
        expected_lifecycle_version=0,
        delivery_id="synthetic.saved.delivery",
        observation=observation,
    )
    assert tasks.sources is not None
    if adopt_alias:
        adopted = tasks.sources.submit_revision(submission)
        assert baseline is not None and adopted.capture_id == baseline.capture_id
    receipt = tasks.sources.public_revision_sink(binding).submit(delivery)
    assert receipt.source_receipt is not None
    source = receipt.source_receipt
    assert source.source_id is not None and source.capture_id is not None
    request = SharingPreviewRequest(
        operation_id="sharing.preview.synthetic",
        source_id=source.source_id,
        expected_head=source.capture_id,
        expected_head_version=1,
        expected_lifecycle_version=0,
        expected_route_version=0,
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
        provider_ids=("anthropic", "openai"),
    )
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    return tasks, request, owner, text


def test_sharing_preview_approve_replay_revoke_and_original_privacy(tmp_path: Path) -> None:
    tasks, request, owner, text = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    assert asdict(preview)["dto_version"] == 1
    assert preview.text == text
    assert tasks.sharing.preview(request, authority=owner) == preview
    decision = SharingDecisionRequest(
        operation_id="sharing.decision.synthetic",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    approved = tasks.sharing.decide(decision, authority=owner)
    assert approved.copy_capture_id is not None
    assert tasks.sharing.decide(decision, authority=owner) == approved
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 1
        assert sharing_eligible(
            connection,
            approved.copy_capture_id,
            mode=EligibilityMode.EXTERNAL_READ,
            provider_id="openai",
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
        )
        assert not sharing_eligible(
            connection,
            approved.copy_capture_id,
            mode=EligibilityMode.EXTERNAL_READ,
            provider_id="gemini",
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
        )
        assert not sharing_eligible(
            connection,
            request.expected_head,
            mode=EligibilityMode.EXTERNAL_READ,
            provider_id="openai",
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
        )
        original_privacy = connection.execute(
            "SELECT privacy_json FROM captures WHERE capture_id=?", (request.expected_head,)
        ).fetchone()[0]
        assert not PrivacyDecision.from_dict(json.loads(original_privacy)).authority.external_egress
    revoked = tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="sharing.revoke.synthetic",
            approval_id=approved.approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        ),
        authority=owner,
    )
    assert revoked.state == "revoked"
    with open_local_database_read_only(tasks.profile) as connection:
        assert not sharing_eligible(
            connection,
            approved.copy_capture_id,
            mode=EligibilityMode.EXTERNAL_READ,
            provider_id="openai",
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
        )


def test_sharing_preview_exact_bytes_and_decision_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    tasks, request, owner, text = _managed_source(tmp_path)
    assert tasks.sharing is not None
    scoped = EffectiveAuthority("synthetic-agent", "session", frozenset({"review-decide"}), None)
    with pytest.raises(SharingError, match="unsupported_capability"):
        tasks.sharing.preview(request, authority=scoped)
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 0
    preview = tasks.sharing.preview(request, authority=owner)
    assert preview.text == text
    assert preview.text_sha256 == sha256(text.encode()).hexdigest()
    for changed in (
        replace(request, expected_head_version=2, operation_id="sharing.changed.head"),
        replace(request, expected_route_version=1, operation_id="sharing.changed.route"),
        replace(request, expected_lifecycle_version=1, operation_id="sharing.changed.lifecycle"),
        replace(
            request,
            brain_id="brn_bbbbbbbbbbbbbbbbbbbbbbbbbb",
            operation_id="sharing.changed.brain",
        ),
    ):
        with pytest.raises(SharingError, match="revision_changed"):
            tasks.sharing.preview(changed, authority=owner)
    with pytest.raises(SharingError, match="invalid_arguments"):
        tasks.sharing.preview(replace(request, provider_ids=("gemini",)), authority=owner)
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 0
    decision = SharingDecisionRequest(
        operation_id="sharing.reject.synthetic",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="reject",
    )
    with pytest.raises(SharingError, match="binding_mismatch"):
        tasks.sharing.decide(replace(decision, preview_sha256="0" * 64), authority=owner)
    before = tasks.sharing._engine._clock()
    with monkeypatch.context() as patch:
        patch.setattr(tasks.sharing._engine, "_clock", lambda: before + timedelta(minutes=16))
        with pytest.raises(SharingError, match="preview_expired"):
            tasks.sharing.decide(decision, authority=owner)
    assert tasks.sharing.decide(decision, authority=owner).state == "rejected"
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM captures WHERE delivery_id LIKE 'sharing-copy.v1:%'"
            ).fetchone()[0]
            == 0
        )


def test_sharing_secret_bearing_public_capture_refuses_preview(tmp_path: Path) -> None:
    text = "Synthetic " + "password" + "=" + "placeholder"
    tasks, request, owner, _ = _managed_source(tmp_path, text=text)
    assert tasks.sharing is not None
    with pytest.raises(SharingError, match="unsupported_capability"):
        tasks.sharing.preview(request, authority=owner)
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text",
    ["Synthetic words\n" * 4096, "漢" * 21_845 + "x"],
    ids=["ascii-byte-limit", "unicode-byte-limit"],
)
def test_sharing_preview_exact_utf8_byte_ceiling_without_truncation(
    tmp_path: Path, text: str
) -> None:
    assert len(text.encode("utf-8")) == 65_536
    tasks, request, owner, _ = _managed_source(tmp_path, text=text)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    assert preview.text.encode("utf-8") == text.encode("utf-8")
    inspection = tasks.sharing.inspect(
        SharingInspectRequest(subject_id=preview.preview_id), authority=owner
    )
    assert asdict(inspection)["dto_version"] == 1
    assert inspection.preview == preview


def test_sharing_multibyte_text_over_byte_ceiling_refuses_before_preview_reservation(
    tmp_path: Path,
) -> None:
    text = "漢" * 21_846
    assert len(text) < 65_536 < len(text.encode("utf-8"))
    tasks, request, owner, _ = _managed_source(tmp_path, text=text)
    assert tasks.sharing is not None
    with pytest.raises(SharingError, match="response_too_large"):
        tasks.sharing.preview(request, authority=owner)
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 0


def test_sharing_inspection_budget_covers_receipts_not_only_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import sharing as sharing_module

    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    decision = SharingDecisionRequest(
        operation_id="sharing.budget.approve",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    receipt = tasks.sharing.decide(decision, authority=owner)
    tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="sharing.budget.revoke",
            approval_id=receipt.approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        ),
        authority=owner,
    )
    inspect_request = SharingInspectRequest(subject_id=receipt.approval_id)
    exact = tasks.sharing.inspect(inspect_request, authority=owner)
    exact_bytes = portable_canonical_json_bytes(asdict(exact))
    assert len(exact_bytes) > len(portable_canonical_json_bytes(asdict(preview)))
    with monkeypatch.context() as patch:
        patch.setattr(sharing_module, "_MAX_RESPONSE_BYTES", len(exact_bytes))
        assert tasks.sharing.inspect(inspect_request, authority=owner) == exact
        patch.setattr(sharing_module, "_MAX_RESPONSE_BYTES", len(exact_bytes) - 1)
        with pytest.raises(SharingError, match="response_too_large"):
            tasks.sharing.inspect(inspect_request, authority=owner)
    assert tasks.sharing.inspect(inspect_request, authority=owner) == exact


@pytest.mark.parametrize("state", ["captured", "pending", "rejected"])
def test_sharing_mutation_receipts_explicitly_bind_version_brain_and_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    decision = SharingDecisionRequest(
        operation_id="sharing.receipt.binding",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="reject" if state == "rejected" else "approve",
    )

    def pending(_submission: object) -> None:
        raise RuntimeError("synthetic pending receipt")

    with monkeypatch.context() as patch:
        if state == "pending":
            patch.setattr(tasks.sharing._engine.capture, "submit", pending)
        receipt = tasks.sharing.decide(decision, authority=owner)
    assert receipt.state == state
    values = [asdict(receipt)]
    if state != "rejected":
        revoked = tasks.sharing.revoke(
            SharingRevokeRequest(
                operation_id="sharing.receipt.revoke",
                approval_id=receipt.approval_id,
                expected_approval_version=1,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                reason="owner_choice",
            ),
            authority=owner,
        )
        values.append(asdict(revoked))
    for value in values:
        assert value["dto_version"] == 1
        assert value["brain_id"] == request.brain_id
        assert value["issuer_epoch"] == request.issuer_epoch
        digest = value.pop("receipt_sha256")
        assert (
            digest
            == sha256(
                b"open-brain-sharing-receipt.v1\0" + portable_canonical_json_bytes(value)
            ).hexdigest()
        )


@pytest.mark.parametrize("action", ["approve", "revoke"])
def test_sharing_mutation_refuses_insufficient_receipt_capacity_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from open_brain_engine.engine import sharing as sharing_module

    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    decision = SharingDecisionRequest(
        operation_id="sharing.capacity.approve",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    approved = tasks.sharing.decide(decision, authority=owner) if action == "revoke" else None
    with monkeypatch.context() as patch:
        patch.setattr(sharing_module, "_MAX_RESPONSE_BYTES", 100)
        with pytest.raises(SharingError, match="response_too_large"):
            if approved is None:
                tasks.sharing.decide(decision, authority=owner)
            else:
                tasks.sharing.revoke(
                    SharingRevokeRequest(
                        operation_id="sharing.capacity.revoke",
                        approval_id=approved.approval_id,
                        expected_approval_version=1,
                        brain_id=request.brain_id,
                        issuer_epoch=request.issuer_epoch,
                        destination_brain_id=request.brain_id,
                        reason="owner_choice",
                    ),
                    authority=owner,
                )
    with open_local_database_read_only(tasks.profile) as connection:
        expected = 1 if approved is not None else 0
        assert (
            connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == expected
        )
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == expected
        assert connection.execute("SELECT count(*) FROM sharing_revocations").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM captures WHERE delivery_id LIKE 'sharing-copy.v1:%'"
            ).fetchone()[0]
            == expected
        )
        if approved is not None:
            assert (
                connection.execute("SELECT approval_version FROM sharing_decisions").fetchone()[0]
                == 1
            )


def test_all_sharing_operations_require_actual_owner_local_authority(tmp_path: Path) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    decision = SharingDecisionRequest(
        operation_id="sharing.authority.approve",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    scoped = EffectiveAuthority(
        "synthetic-local-agent",
        "synthetic-session",
        frozenset({"review-read", "review-decide", "organize", "history-read"}),
        None,
    )
    for operation, value in (
        (tasks.sharing.preview, request),
        (tasks.sharing.inspect, SharingInspectRequest(subject_id=preview.preview_id)),
        (tasks.sharing.decide, decision),
        (
            tasks.sharing.revoke,
            SharingRevokeRequest(
                operation_id="sharing.authority.revoke",
                approval_id=preview.preview_id,
                expected_approval_version=1,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                reason="owner_choice",
            ),
        ),
    ):
        with pytest.raises(SharingError, match="unsupported_capability"):
            operation(value, authority=scoped)
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_previews").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sharing_revocations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"reason": "changed_reason"}, "invalid_arguments"),
        ({"expected_approval_version": 2}, "invalid_arguments"),
        ({"destination_brain_id": "brn_" + "b" * 26}, "invalid_arguments"),
        ({"issuer_epoch": 2}, "revision_changed"),
        ({"brain_id": "brn_" + "b" * 26}, "revision_changed"),
        ({"operation_id": "sharing.revoke.fresh"}, "revision_changed"),
        (
            {"operation_id": "sharing.revoke.foreign", "destination_brain_id": "brn_" + "b" * 26},
            "binding_mismatch",
        ),
    ],
)
def test_revocation_exact_replay_changed_replay_and_fresh_cas_refusals(
    tmp_path: Path, changes: dict[str, object], error: str
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    approved = tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.revoke.approve",
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
    revocation = SharingRevokeRequest(
        operation_id="sharing.revoke.original",
        approval_id=approved.approval_id,
        expected_approval_version=1,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        reason="owner_choice",
    )
    exact = tasks.sharing.revoke(revocation, authority=owner)
    assert tasks.sharing.revoke(revocation, authority=owner) == exact
    with pytest.raises(SharingError, match=error):
        tasks.sharing.revoke(replace(revocation, **changes), authority=owner)  # type: ignore[arg-type]
    assert tasks.sharing.revoke(revocation, authority=owner) == exact
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_revocations").fetchone()[0] == 1
        assert (
            connection.execute("SELECT approval_version FROM sharing_decisions").fetchone()[0] == 2
        )
