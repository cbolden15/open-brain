from __future__ import annotations

import json
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine import DecisionOutcome, ProposalDraft, TextPayload
from open_brain_engine.engine.contracts import (
    CaptureSubmission,
    PublicJobCaptureContext,
)
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.source_intake import SourceRevisionSubmission
from open_brain_engine.engine.source_lifecycle_contracts import (
    SourceInspectRequest,
    SourceWithdrawRequest,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.portable.developments_v11 import validate_development_evidence
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v5 import EFFECTIVE_PRIVACY_PATH
from open_brain_engine.portable.versioned import validate_portable_root

from open_brain.profile import compile_single_user_local, open_existing_single_user_local


def _authority() -> EffectiveAuthority:
    return EffectiveAuthority(
        principal_id="synthetic-collector",
        session_id="synthetic-collector",
        capabilities=frozenset({"development-bind", "review-propose", "review-read"}),
        space_ids=None,
    )


def _engine(root: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(root, starter_spaces=("Projects",)))


def _raw(engine: BrainEngine, sequence: int, head: str | None = None) -> str:
    actor = "actor_00000000-0000-4000-8000-000000000091"
    context = PublicJobCaptureContext.create(
        profile=engine.profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000092",
            "role_id": "role_00000000-0000-4000-8000-000000000093",
            "tenant_id": engine.profile.tenant_id,
        },
    )
    reference = "https://example.test/slack-message"
    capture = CaptureSubmission.for_public_job(
        context=context,
        payload=TextPayload(f"Synthetic revision {sequence}"),
        delivery_id="slack.raw",
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=reference,
        provenance=Provenance.create(
            source_ref=reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.WORK,
            reason=PrivacyReason.POLICY_WORK,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )
    receipt = engine.sources.submit_revision(
        SourceRevisionSubmission(
            capture=capture,
            namespace={
                "connector_name": "slack",
                "connection_id": "synthetic",
                "resource_id": "channel:synthetic",
                "external_id": "message:1",
            },
            revision_key=str(sequence),
            canonical_sha256=capture.request_sha256(),
            expected_head=head,
            ordering={
                "kind": "monotonic",
                "provider_namespace": "slack",
                "epoch": "one",
                "sequence": sequence,
            },
            expected_control_epoch=0,
        )
    )
    assert receipt.capture_id is not None
    return receipt.capture_id


def _derived(engine: BrainEngine, key: str) -> str:
    return engine.capture.accept(
        TextPayload(f"Synthetic development {key}"),
        delivery_id=key,
        space_id=engine.inbox.spaces()[0].space_id,
        privacy_tier=PrivacyTier.WORK,
    ).capture_id


def test_corrected_and_missing_evidence_blocks_transitive_approval(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    raw = _raw(engine, 1)
    first, second = _derived(engine, "first"), _derived(engine, "second")
    receipt = engine.developments.bind(
        first, (raw,), operation_id="bind-first", authority=_authority()
    )
    assert (
        engine.developments.bind(first, (raw,), operation_id="bind-first", authority=_authority())
        == receipt
    )
    engine.developments.bind(
        second,
        (raw,),
        operation_id="bind-second",
        authority=_authority(),
        supports_development_ids=(first,),
    )
    proposal = engine.review.propose(
        (second,), (ProposalDraft("Decision", "Synthetic decision"),), delivery_id="propose"
    )[0]
    _raw(engine, 2, raw)
    assert (
        engine.developments.bind(first, (raw,), operation_id="bind-first", authority=_authority())
        == receipt
    )
    with pytest.raises(ValueError, match="development evidence revision conflict"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="approve",
            expected_review_digest=proposal.review_digest,
        )
    # Rejection remains possible despite stale support.
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.REJECTED,
        delivery_id="reject",
        expected_review_digest=proposal.review_digest,
    )


def test_exact_replay_scope_and_portable_restore(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    raw = _raw(engine, 1)
    derived = _derived(engine, "derived")
    with pytest.raises(T03Error, match="unsupported_capability"):
        engine.developments.bind(
            derived,
            (raw,),
            operation_id="bind",
            authority=EffectiveAuthority(
                "reader",
                "reader",
                frozenset(),
                None,
            ),
        )
    receipt = engine.developments.bind(derived, (raw,), operation_id="bind", authority=_authority())
    with pytest.raises(ValueError, match="conflicting development"):
        engine.developments.bind(derived, (raw,), operation_id="changed", authority=_authority())
    exported = tmp_path / "exported"
    engine.portability.export(exported, export_id="export_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd4")
    assert validate_portable_root(exported)["schema_version"] == 11
    restored = tmp_path / "restored"
    engine.portability.import_clean(
        exported, restored, import_id="import_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd4"
    )
    reopened = BrainEngine.open(open_existing_single_user_local(restored))
    assert (
        reopened.developments.bind(derived, (raw,), operation_id="bind", authority=_authority())
        == receipt
    )
    proposal = reopened.review.propose(
        (derived,), (ProposalDraft("Restored", "Supported claim"),), delivery_id="restored.propose"
    )[0]
    reopened.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="restored.approve",
        expected_review_digest=proposal.review_digest,
    )


def test_withdrawn_support_refuses_approval(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    raw, derived = _raw(engine, 1), _derived(engine, "derived")
    bound = engine.developments.bind(derived, (raw,), operation_id="bind", authority=_authority())
    proposal = engine.review.propose(
        (derived,), (ProposalDraft("Decision", "Supported"),), delivery_id="propose"
    )[0]
    owner = EffectiveAuthority("owner", "owner", frozenset(), None, owner=True)
    source = engine.sources.inspect(
        SourceInspectRequest(source_id=bound["source_states"][0]["source_id"]), authority=owner
    )
    engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.synthetic",
            source_id=source.source_id,
            expected_head=raw,
            expected_lifecycle_version=source.lifecycle_version,
            brain_id=source.destination_brain_id,
            issuer_epoch=source.issuer_epoch,
            reason_code="synthetic_deleted",
        ),
        authority=owner,
    )
    with pytest.raises(ValueError, match="development evidence revision conflict"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="approve",
            expected_review_digest=proposal.review_digest,
        )


def test_portable_development_refuses_privacy_widening(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    raw, derived = _raw(engine, 1), _derived(engine, "derived")
    engine.developments.bind(derived, (raw,), operation_id="bind", authority=_authority())
    exported = tmp_path / "exported"
    engine.portability.export(exported, export_id="export_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd4")
    files = {
        path.relative_to(exported).as_posix(): path.read_bytes()
        for path in exported.rglob("*")
        if path.is_file()
    }
    assert len(validate_development_evidence(files)) == 1
    privacy = json.loads(files[EFFECTIVE_PRIVACY_PATH])
    for row in privacy["resolved_revisions"]:
        if row["target_id"] == derived:
            row["effective_privacy"]["tier"] = "public"
    files[EFFECTIVE_PRIVACY_PATH] = portable_canonical_json_bytes(privacy)
    with pytest.raises(PortableValidationError, match="development evidence invalid"):
        validate_development_evidence(files)


def test_append_retry_survives_page_drift_and_portable_restore(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    seed = _derived(engine, "seed")
    page = engine.review.propose(
        (seed,), (ProposalDraft("Page", "Original"),), delivery_id="seed.propose"
    )[0]
    engine.review.decide(
        page.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="seed.approve",
        expected_review_digest=page.review_digest,
    )
    assert page.page_id is not None
    raw, derived = _raw(engine, 1), _derived(engine, "derived")
    engine.developments.bind(derived, (raw,), operation_id="bind", authority=_authority())
    review = engine.review.with_authority(_authority())
    proposal = review.propose_append(
        (derived,),
        target_page_id=page.page_id,
        append_markdown="Supported update",
        delivery_id="append",
    )[0]
    drift_source = _derived(engine, "drift.source")
    drift = engine.review.propose_append(
        (drift_source,),
        target_page_id=page.page_id,
        append_markdown="Owner update",
        delivery_id="drift",
    )[0]
    engine.review.decide(
        drift.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="drift.approve",
        expected_review_digest=drift.review_digest,
    )
    replay = review.propose_append(
        (derived,),
        target_page_id=page.page_id,
        append_markdown="Supported update",
        delivery_id="append",
    )[0]
    assert replay.proposal_id == proposal.proposal_id
    with pytest.raises(ValueError, match="conflicting append delivery"):
        review.propose_append(
            (derived,),
            target_page_id=page.page_id,
            append_markdown="Different",
            delivery_id="append",
        )
    with pytest.raises(T03Error, match="unsupported_capability"):
        review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="collector.approve",
            expected_review_digest=proposal.review_digest,
        )
    exported, restored = tmp_path / "exported", tmp_path / "restored"
    engine.portability.export(exported, export_id="export_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd4")
    engine.portability.import_clean(
        exported, restored, import_id="import_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd4"
    )
    reopened = BrainEngine.open(open_existing_single_user_local(restored))
    retry = reopened.review.with_authority(_authority()).propose_append(
        (derived,),
        target_page_id=page.page_id,
        append_markdown="Supported update",
        delivery_id="append",
    )[0]
    assert retry.proposal_id == proposal.proposal_id


def test_schema15_explicit_migration_old_archive_and_forward_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_engine.engine import local_schema
    from open_brain_engine.engine.local import StateSchemaUnavailableError
    from open_brain_engine.engine.local_migration import coordinate_local_migration
    from open_brain_engine.portable.v1 import PortableValidationError
    from open_brain_engine.portable.v10 import validated_portable_snapshot_v10

    old_root, exported = tmp_path / "old", tmp_path / "exported"
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 15)
        legacy = _engine(old_root)
        _derived(legacy, "legacy")
        legacy.portability.export(exported, export_id="export_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd4")
        assert validate_portable_root(exported)["schema_version"] == 10
    profile = open_existing_single_user_local(old_root)
    with pytest.raises(StateSchemaUnavailableError, match="development migration"):
        BrainEngine.open(profile)
    coordinate_local_migration(profile)
    upgraded = BrainEngine.open(profile)
    with local_schema.open_local_database_read_only(profile) as connection:
        assert local_schema.classify_local_schema(connection).version == 16
        assert connection.execute("SELECT COUNT(*) FROM development_evidence").fetchone()[0] == 0
    restored = tmp_path / "restored"
    upgraded.portability.import_clean(
        exported, restored, import_id="import_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd4"
    )
    reopened = BrainEngine.open(open_existing_single_user_local(restored))
    latest = tmp_path / "latest"
    reopened.portability.export(latest, export_id="export_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd5")
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v10(latest)
    with monkeypatch.context() as older_runtime:
        older_runtime.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 15)
        with pytest.raises(StateSchemaUnavailableError, match="newer"):
            BrainEngine.open(open_existing_single_user_local(restored))


def test_binding_cannot_widen_privacy_or_attach_after_proposal(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    raw = _raw(engine, 1)
    public = engine.capture.accept(
        TextPayload("Public derived claim"),
        delivery_id="public",
        space_id=engine.inbox.spaces()[0].space_id,
        privacy_tier=PrivacyTier.PUBLIC,
    ).capture_id
    with pytest.raises(ValueError, match="development evidence privacy conflict"):
        engine.developments.bind(public, (raw,), operation_id="public.bind", authority=_authority())
    derived = _derived(engine, "proposed")
    engine.review.propose(
        (derived,), (ProposalDraft("Already proposed", "Frozen"),), delivery_id="propose"
    )
    with pytest.raises(ValueError, match="must precede proposal"):
        engine.developments.bind(derived, (raw,), operation_id="late.bind", authority=_authority())
    with pytest.raises(T03Error, match="unsupported_capability"):
        engine.review.with_authority(
            EffectiveAuthority(
                "reader",
                "reader",
                frozenset({"review-read"}),
                None,
            )
        ).propose((public,), (ProposalDraft("Denied", "Denied"),), delivery_id="denied")


def test_selected_raw_correction_conflicts_without_derived_evidence(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    raw = _raw(engine, 1)
    engine.inbox.route(raw, engine.inbox.spaces()[0].space_id, delivery_id="route.raw")
    proposal = engine.review.propose(
        (raw,), (ProposalDraft("Raw", "Raw supported update"),), delivery_id="raw.propose"
    )[0]
    _raw(engine, 2, raw)
    with pytest.raises(ValueError, match="development evidence revision conflict"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="raw.approve",
            expected_review_digest=proposal.review_digest,
        )
