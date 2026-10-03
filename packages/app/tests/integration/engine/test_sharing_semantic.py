from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    DecisionOutcome,
    EngineTaskSet,
    ManagedAccessMode,
    ManagedInferenceRequest,
    ManagedProvider,
    ManagedWorkspaceFailure,
    ProposalDraft,
    TextPayload,
)
from open_brain_engine.engine.managed_inference import ManagedInferenceTasks
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionReceipt,
    SharingDecisionRequest,
    SharingPreviewRequest,
    SharingRevokeRequest,
)
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    RecordReadRequest,
    SourceRouteRequest,
)
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.services.local_operations import refresh_graph
from open_brain.services.managed_providers import ManagedGraphProviderResult
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission
from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source

_BACKENDS = (
    (ManagedProvider.OPENAI_API, "openai"),
    (ManagedProvider.ANTHROPIC_API, "anthropic"),
    (ManagedProvider.GEMINI_API, "gemini"),
)


@dataclass
class _Case:
    engine: BrainEngine
    tasks: EngineTaskSet
    request: SharingPreviewRequest
    owner: EffectiveAuthority
    approval: SharingDecisionReceipt | None
    workspace_id: str
    workspace: Path
    copy_note: str
    ordinary_note: str


def _case(
    tmp_path: Path,
    provider_id: str,
    *,
    decision: str = "approve",
    original: bool = False,
) -> _Case:
    tasks, request, owner, _ = _managed_source(tmp_path)
    tasks = cast(EngineTaskSet, tasks)
    request = cast(SharingPreviewRequest, request)
    owner = cast(EffectiveAuthority, owner)
    engine = cast(ManagedInferenceTasks, tasks.managed_inference)._engine
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(replace(request, provider_ids=(provider_id,)), authority=owner)
    approval = (
        None
        if decision == "undecided"
        else tasks.sharing.decide(
            SharingDecisionRequest(
                operation_id="sharing.semantic.approve",
                preview_id=preview.preview_id,
                preview_sha256=preview.preview_sha256,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                destination_brain_id=request.brain_id,
                expected_decision_version=0,
                decision=decision,
            ),
            authority=owner,
        )
    )
    selected_capture = request.expected_head
    if approval is not None and decision == "approve" and not original:
        assert approval.copy_capture_id is not None
        selected_capture = approval.copy_capture_id
    space = tasks.inbox.create_space("Synthetic Notes", delivery_id="sharing.semantic.space")
    tasks.spaces.route(selected_capture, space.space_id, delivery_id="sharing.semantic.route")
    proposal = tasks.review.propose(
        selected_capture,
        (ProposalDraft("Synthetic copy", "Synthetic retained body 漢字"),),
        delivery_id="sharing.semantic.proposal",
    )[0]
    canonical_decision = tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="sharing.semantic.review",
        expected_review_digest=proposal.review_digest,
    )
    assert canonical_decision.page_id is not None
    engine.capture.accept(
        TextPayload("Synthetic ordinary evidence"),
        delivery_id="sharing.semantic.ordinary",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    ordinary_note = engine.retrieval.search("ordinary", record_type="canonical")[0].result_id
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_id = tasks.managed_workspace.setup(
        str(workspace), operation_id="sharing.semantic.setup"
    ).workspace_id
    return _Case(
        engine,
        tasks,
        request,
        owner,
        approval,
        workspace_id,
        workspace,
        canonical_decision.page_id,
        ordinary_note,
    )


def _grant(case: _Case, provider: ManagedProvider) -> None:
    case.tasks.managed_policy.grant_consent(
        case.workspace_id,
        provider,
        ManagedAccessMode.API_KEY,
        operation_id="sharing.semantic.consent." + str(uuid4()),
    )


def _prepare(
    case: _Case, provider: ManagedProvider, notes: tuple[str, ...]
) -> ManagedInferenceRequest:
    return case.tasks.managed_inference.prepare(
        case.workspace_id,
        provider,
        ManagedAccessMode.API_KEY,
        provider.value + ":synthetic-v1",
        notes,
        request_id="request_" + str(uuid4()),
        max_output_bytes=4096,
        timeout_seconds=30,
    )


def _revoke(case: _Case) -> None:
    assert case.tasks.sharing is not None
    assert case.approval is not None
    case.tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="sharing.semantic.revoke",
            approval_id=case.approval.approval_id,
            expected_approval_version=1,
            brain_id=case.request.brain_id,
            issuer_epoch=case.request.issuer_epoch,
            destination_brain_id=case.request.brain_id,
            reason="owner_choice",
        ),
        authority=case.owner,
    )


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, prompt: str, maximum: int, timeout: int) -> ManagedGraphProviderResult:
        self.calls.append(prompt)
        assert maximum > 0 and timeout > 0
        return ManagedGraphProviderResult(1, "Synthetic", 2, "Synthetic", "synthetic-v1", {})


def _refresh(
    case: _Case, provider: ManagedProvider, recording: _RecordingProvider
) -> dict[str, object]:
    result, _, _ = refresh_graph(
        case.tasks,
        provider=provider,
        access_mode=ManagedAccessMode.API_KEY,
        adapter_identity=provider.value + ":synthetic-v1",
        request_id="request_" + str(uuid4()),
        invoke=recording,
        remaining_attempts=1,
        remaining_input_bytes=16384,
    )
    return result


def _transition(case: _Case, transition: str, provider: ManagedProvider) -> None:
    if transition == "revoke":
        _revoke(case)
    elif transition == "consent":
        case.tasks.managed_policy.revoke_consent(
            case.workspace_id,
            provider,
            ManagedAccessMode.API_KEY,
            operation_id="sharing.semantic.revoke-consent",
        )
    elif transition == "replace-consent":
        case.tasks.managed_policy.revoke_consent(
            case.workspace_id,
            provider,
            ManagedAccessMode.API_KEY,
            operation_id="sharing.semantic.replace-consent",
        )
        _grant(case, provider)
    elif transition == "policy":
        case.tasks.managed_policy.set_exclusion(
            case.workspace_id,
            "note",
            case.copy_note,
            excluded=True,
            operation_id="sharing.semantic.exclude",
        )
    else:
        assert case.tasks.sources is not None
        request = case.request
        if transition == "withdraw":
            case.tasks.sources.withdraw(
                SourceWithdrawRequest(
                    operation_id="withdraw.sharing.semantic",
                    source_id=request.source_id,
                    expected_head=request.expected_head,
                    expected_lifecycle_version=request.expected_lifecycle_version,
                    brain_id=request.brain_id,
                    issuer_epoch=request.issuer_epoch,
                    reason_code="owner_choice",
                ),
                authority=case.owner,
            )
        elif transition == "route":
            space = case.tasks.spaces.create_space(
                "Synthetic moved source",
                delivery_id="sharing.semantic.move-space",
            )
            case.tasks.sources.route(
                SourceRouteRequest(
                    source_id=request.source_id,
                    expected_head=request.expected_head,
                    expected_route_version=request.expected_route_version,
                    space_id=space.space_id,
                    operation_id="operation_" + str(uuid4()),
                ),
                authority=case.owner,
            )
        else:
            assert transition == "head"
            privacy = PrivacyDecision.create(
                tier=PrivacyTier.PUBLIC,
                reason=PrivacyReason.POLICY_PUBLIC,
                policy_version="policy-v1",
                authority=Authority(cloud=False, external_egress=False),
            )
            capture = replace(
                _public_submission(case.tasks),
                payload=TextPayload("Synthetic next head"),
                privacy=privacy,
                delivery_id="sharing.semantic.next-head",
            )
            namespace = {
                "connector_name": "saved_markdown",
                "connection_id": "synthetic-connection",
                "resource_id": "synthetic-resource",
                "external_id": "item:synthetic",
            }
            submission = SourceRevisionSubmission(
                capture=capture,
                namespace=namespace,
                revision_key="synthetic-next",
                canonical_sha256=capture.request_sha256(),
                expected_head=request.expected_head,
                ordering={"kind": "predecessor", "revision_key": "synthetic-revision"},
                expected_control_epoch=0,
            )
            binding = SourceRevisionBinding(
                destination_brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                root_fingerprint="synthetic-root",
                accepted_source_id="synthetic-selection",
                namespace=namespace,
            )
            observation = SourceRevisionObservation(
                original_sha256="a" * 64,
                transformed_sha256="b" * 64,
                normalization_version="saved-markdown-continuous.v1",
                privacy_policy_version=privacy.policy_version,
                privacy_policy_sha256=sha256(
                    portable_canonical_json_bytes(privacy.to_dict())
                ).hexdigest(),
                admitted_payload_sha256=sha256(
                    portable_canonical_json_bytes(capture.payload.to_dict())
                ).hexdigest(),
            )
            receipt = case.tasks.sources.public_revision_sink(binding).submit(
                SourceRevisionObservedDelivery(
                    binding=binding,
                    submission=submission,
                    expected_lifecycle_version=0,
                    delivery_id="sharing.semantic.next-delivery",
                    observation=observation,
                )
            )
            assert receipt.source_receipt is not None
            assert receipt.source_receipt.capture_id != request.expected_head


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
def test_current_approved_copy_reaches_exact_mapped_provider(
    tmp_path: Path,
    provider: ManagedProvider,
    provider_id: str,
) -> None:
    case = _case(tmp_path, provider_id)
    _grant(case, provider)
    recording = _RecordingProvider()
    result = _refresh(case, provider, recording)
    assert result["status"] == "refreshed"
    assert len(recording.calls) == 1
    assert "Synthetic retained body 漢字" in recording.calls[0]
    assert case.request.expected_head not in recording.calls[0]
    assert case.approval is not None and case.approval.copy_capture_id is not None
    original = case.tasks.retrieval.read_record(
        RecordReadRequest(
            record_id=case.request.expected_head, expected_revision_id=case.request.expected_head
        ),
        authority=case.owner,
    ).to_wire()
    assert "Synthetic retained body" in cast(
        str, cast(dict[str, object], original["content"])["text"]
    )
    with case.engine._store.transaction() as connection:
        row = connection.execute(
            "SELECT provider,adapter_identity,status FROM managed_inference_requests"
        ).fetchone()
        assert tuple(row) == (provider.value, provider.value + ":synthetic-v1", "succeeded")
        privacy = PrivacyDecision.from_dict(
            json.loads(
                connection.execute(
                    "SELECT privacy_json FROM captures WHERE capture_id=?",
                    (case.request.expected_head,),
                ).fetchone()[0]
            )
        )
        assert not privacy.authority.cloud and not privacy.authority.external_egress


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
@pytest.mark.parametrize("decision", ["undecided", "reject", "approve"])
def test_canonical_original_never_inherits_copy_approval_or_managed_consent(
    tmp_path: Path,
    provider: ManagedProvider,
    provider_id: str,
    decision: str,
) -> None:
    case = _case(tmp_path, provider_id, decision=decision, original=True)
    _grant(case, provider)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        _refresh(case, provider, recording)
    assert recording.calls == []
    retained = case.tasks.retrieval.read_record(
        RecordReadRequest(
            record_id=case.request.expected_head, expected_revision_id=case.request.expected_head
        ),
        authority=case.owner,
    ).to_wire()
    assert cast(dict[str, object], retained["content"])["text"]


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
@pytest.mark.parametrize("identity", ["alias", "other-provider"])
def test_provider_alias_or_different_approval_does_not_release_copy(
    tmp_path: Path,
    provider: ManagedProvider,
    provider_id: str,
    identity: str,
) -> None:
    approved_id = (
        provider.value
        if identity == "alias"
        else next(value for _, value in _BACKENDS if value != provider_id)
    )
    case = _case(tmp_path, approved_id)
    _grant(case, provider)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        _refresh(case, provider, recording)
    assert recording.calls == []


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
@pytest.mark.parametrize("transition", ["revoke", "withdraw", "head", "route"])
def test_ineligible_copy_is_denied_at_prepare_before_any_provider_call(
    tmp_path: Path,
    provider: ManagedProvider,
    provider_id: str,
    transition: str,
) -> None:
    case = _case(tmp_path, provider_id)
    _grant(case, provider)
    _transition(case, transition, provider)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        _refresh(case, provider, recording)
    assert recording.calls == []


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
@pytest.mark.parametrize(
    "transition",
    [
        "revoke",
        "withdraw",
        "head",
        "route",
        "policy",
        "consent",
        "replace-consent",
    ],
)
def test_release_rechecks_approval_lifecycle_and_policy_before_provider_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ManagedProvider,
    provider_id: str,
    transition: str,
) -> None:
    case = _case(tmp_path, provider_id)
    _grant(case, provider)
    release = case.engine.managed_inference.release
    prepared_ids: list[str] = []

    def changed_release(request_id: str) -> ManagedInferenceRequest:
        prepared_ids.append(request_id)
        _transition(case, transition, provider)
        return release(request_id)

    monkeypatch.setattr(case.engine.managed_inference, "release", changed_release)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^(ineligible_source|stale_request)$"):
        _refresh(case, provider, recording)
    assert len(prepared_ids) == 1
    assert recording.calls == []
    with case.engine._store.transaction() as connection:
        assert (
            connection.execute(
                "SELECT status FROM managed_inference_requests WHERE request_id=?",
                (prepared_ids[0],),
            ).fetchone()[0]
            == "cancelled"
        )
        assert tuple(
            connection.execute(
                "SELECT reserved_requests,used_requests,uncertain_requests "
                "FROM managed_inference_budgets WHERE workspace_id=? AND provider=?",
                (case.workspace_id, provider.value),
            ).fetchone()
        ) == (0, 0, 0)


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
@pytest.mark.parametrize("stage", ["prepare", "release"])
def test_accepted_suggestion_retains_target_eligibility_before_provider_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ManagedProvider,
    provider_id: str,
    stage: str,
) -> None:
    case = _case(tmp_path, provider_id)
    _grant(case, provider)
    initial = _prepare(case, provider, (case.ordinary_note, case.copy_note))
    case.tasks.managed_inference.release(initial.request_id)
    suggestion = case.tasks.managed_inference.record_suggestion(
        initial.request_id,
        source_note_id=case.ordinary_note,
        target_note_id=case.copy_note,
        source_quote="Synthetic",
        target_quote="Synthetic",
        model="synthetic-v1",
    )
    case.tasks.managed_inference.accept_suggestion(
        case.workspace_id,
        suggestion.suggestion_id,
        operation_id="sharing.semantic.accept-link",
    )
    case.tasks.managed_workspace.materialize(
        case.workspace_id,
        case.ordinary_note,
        operation_id="sharing.semantic.materialize-link",
    )
    source_path = next(case.workspace.rglob(f"{case.ordinary_note}.md"))
    parsed = parse_markdown(source_path.read_bytes())
    source_path.write_text(
        render_markdown(fields=parsed.fields, body="Synthetic ordinary evidence"),
        encoding="utf-8",
    )
    observed = case.tasks.managed_workspace.observe(case.workspace_id)
    case.tasks.managed_workspace.accept_observed(
        case.workspace_id,
        case.ordinary_note,
        generation=observed.generation,
        operation_id="sharing.semantic.remove-visible-link",
    )
    # A missing selected file must not erase the durable link's retained ancestry.
    next(case.workspace.rglob(f"{case.copy_note}.md")).unlink()
    prepared = _prepare(case, provider, (case.ordinary_note,))
    assert "Synthetic ordinary evidence" in prepared.prompt
    case.tasks.managed_inference.fail(prepared.request_id)
    prepared_ids: list[str] = []
    if stage == "prepare":
        _revoke(case)
    else:
        release = case.engine.managed_inference.release

        def revoked_release(request_id: str) -> ManagedInferenceRequest:
            prepared_ids.append(request_id)
            _revoke(case)
            return release(request_id)

        monkeypatch.setattr(case.engine.managed_inference, "release", revoked_release)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        _refresh(case, provider, recording)
    assert recording.calls == []
    assert len(prepared_ids) == (1 if stage == "release" else 0)


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
@pytest.mark.parametrize("stage", ["prepare", "release"])
def test_owner_edit_cannot_remove_retained_canonical_copy_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ManagedProvider,
    provider_id: str,
    stage: str,
) -> None:
    case = _case(tmp_path, provider_id)
    _grant(case, provider)
    path = next(case.workspace.rglob(f"{case.copy_note}.md"))
    parsed = parse_markdown(path.read_bytes())
    ordinary_path = next(case.workspace.rglob(f"{case.ordinary_note}.md"))
    ordinary = parse_markdown(ordinary_path.read_bytes())
    fields = dict(parsed.fields)
    fields["provenance"] = ordinary.fields["provenance"]
    path.write_text(render_markdown(fields=fields, body="Synthetic edited body"), encoding="utf-8")
    observation = case.tasks.managed_workspace.observe(case.workspace_id)
    case.tasks.managed_workspace.accept_observed(
        case.workspace_id,
        case.copy_note,
        generation=observation.generation,
        operation_id="sharing.semantic.edit-provenance",
    )
    prepared = _prepare(case, provider, (case.copy_note, case.ordinary_note))
    assert "Synthetic edited body" in prepared.prompt
    case.tasks.managed_inference.fail(prepared.request_id)
    prepared_ids: list[str] = []
    if stage == "prepare":
        _revoke(case)
    else:
        release = case.engine.managed_inference.release

        def revoked_release(request_id: str) -> ManagedInferenceRequest:
            prepared_ids.append(request_id)
            _revoke(case)
            return release(request_id)

        monkeypatch.setattr(case.engine.managed_inference, "release", revoked_release)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        _refresh(case, provider, recording)
    assert recording.calls == []
    assert len(prepared_ids) == (1 if stage == "release" else 0)


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
@pytest.mark.parametrize("damage", ["missing-parent", "cycle"])
def test_unknown_or_cyclic_retained_ancestry_is_denied_at_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ManagedProvider,
    provider_id: str,
    damage: str,
) -> None:
    case = _case(tmp_path, provider_id)
    _grant(case, provider)
    release = case.engine.managed_inference.release
    prepared_ids: list[str] = []

    def damaged_release(request_id: str) -> ManagedInferenceRequest:
        prepared_ids.append(request_id)
        # Only this disposable corruption-injection connection bypasses foreign keys.
        with sqlite3.connect(case.tasks.profile.root / ".open-brain/state/phase1.sqlite3") as db:
            revision = db.execute(
                "SELECT accepted_revision_id FROM managed_notes WHERE note_id=?",
                (case.copy_note,),
            ).fetchone()[0]
            parent = revision if damage == "cycle" else "revision_" + str(uuid4())
            db.execute(
                "UPDATE managed_note_revisions SET parent_revision_id=? WHERE revision_id=?",
                (parent, revision),
            )
        return release(request_id)

    monkeypatch.setattr(case.engine.managed_inference, "release", damaged_release)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        _refresh(case, provider, recording)
    assert len(prepared_ids) == 1
    assert recording.calls == []


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
def test_copy_approval_without_managed_consent_never_reaches_provider(
    tmp_path: Path,
    provider: ManagedProvider,
    provider_id: str,
) -> None:
    case = _case(tmp_path, provider_id)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^active_consent_required$"):
        _refresh(case, provider, recording)
    assert recording.calls == []


@pytest.mark.parametrize(("provider", "provider_id"), _BACKENDS)
def test_semantic_adapter_identity_does_not_accept_session_provider_alias(
    tmp_path: Path,
    provider: ManagedProvider,
    provider_id: str,
) -> None:
    case = _case(tmp_path, provider_id)
    _grant(case, provider)
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^invalid_request$"):
        refresh_graph(
            case.tasks,
            provider=provider,
            access_mode=ManagedAccessMode.API_KEY,
            adapter_identity=provider_id + ":synthetic-v1",
            request_id="request_" + str(uuid4()),
            invoke=recording,
            remaining_attempts=1,
            remaining_input_bytes=16384,
        )
    assert recording.calls == []


def test_unmapped_subscription_does_not_release_approved_copy(tmp_path: Path) -> None:
    case = _case(tmp_path, "claude_subscription")
    case.tasks.managed_policy.grant_consent(
        case.workspace_id,
        ManagedProvider.CLAUDE_SUBSCRIPTION,
        ManagedAccessMode.SUBSCRIPTION,
        operation_id="sharing.semantic.subscription-consent",
    )
    recording = _RecordingProvider()
    with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
        refresh_graph(
            case.tasks,
            provider=ManagedProvider.CLAUDE_SUBSCRIPTION,
            access_mode=ManagedAccessMode.SUBSCRIPTION,
            adapter_identity="claude_subscription:synthetic-v1",
            request_id="request_" + str(uuid4()),
            invoke=recording,
            remaining_attempts=1,
            remaining_input_bytes=16384,
        )
    assert recording.calls == []
