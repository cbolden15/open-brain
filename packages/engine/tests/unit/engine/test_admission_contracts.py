from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
    narrowest_tier,
)
from open_brain_engine.engine.contracts import (
    AdmissionLimits,
    CaptureAdmissionError,
    CaptureAdmissionResult,
    CapturePrivacyManifest,
    CaptureReceipt,
    CaptureSubmission,
    CaptureSubmissionPath,
    LocalEngineContext,
    MarkdownImportFailure,
    PublicJobCaptureContext,
    TextPayload,
    destination_bound_request_sha256,
    project_public_capture_receipt,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.providers.base import ProviderMode

SYNTHETIC_SOURCE_REFERENCE = "https://example.test/synthetic-admission"
OWNER_REQUEST_VALUE_KEYS = {
    "action",
    "capture_why",
    "intent",
    "payload",
    "source_origin",
    "space_id",
    "title",
}
ADMISSION_RESULT_VALUES = {
    "envelope_too_large",
    "body_too_large",
    "rate_limited",
    "admission_busy",
    "writer_queue_full",
    "storage_high",
    "storage_critical",
    "tier_not_permitted",
}
RETRYABLE_RESULTS = {
    CaptureAdmissionResult.RATE_LIMITED,
    CaptureAdmissionResult.ADMISSION_BUSY,
    CaptureAdmissionResult.WRITER_QUEUE_FULL,
    CaptureAdmissionResult.STORAGE_HIGH,
}
RESTRICTIVENESS = {
    PrivacyTier.PUBLIC: 0,
    PrivacyTier.WORK: 1,
    PrivacyTier.PERSONAL: 2,
    PrivacyTier.SECRET: 3,
    PrivacyTier.UNKNOWN: 4,
}


def _portable_id(prefix: str) -> str:
    return f"{prefix}_{uuid4()}"


def _profile() -> LocalEngineContext:
    tenant_id = _portable_id("tenant")
    owner_actor_id = _portable_id("actor")
    return LocalEngineContext(
        root=Path("/synthetic/open-brain/admission-contracts"),
        root_identity=(1000, 1),
        tenant_id=tenant_id,
        owner_actor_id=owner_actor_id,
        owner_role_claim={
            "actor_id": owner_actor_id,
            "capabilities": ["owner.capture"],
            "role_claim_id": _portable_id("role_claim"),
            "role_id": _portable_id("role"),
            "tenant_id": tenant_id,
        },
        provider_mode=ProviderMode.NONE,
        starter_spaces=(),
    )


def _public_job_context(profile: LocalEngineContext) -> PublicJobCaptureContext:
    actor_id = _portable_id("actor")
    return PublicJobCaptureContext.create(
        profile=profile,
        actor_id=actor_id,
        role_claim={
            "actor_id": actor_id,
            "capabilities": ["capture.accept"],
            "role_claim_id": _portable_id("role_claim"),
            "role_id": _portable_id("role"),
            "tenant_id": profile.tenant_id,
        },
    )


def _privacy(tier: PrivacyTier) -> PrivacyDecision:
    reasons = {
        PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
        PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
        PrivacyTier.PERSONAL: PrivacyReason.PERSONAL_LOCAL_ONLY,
        PrivacyTier.SECRET: PrivacyReason.SECRET_DETECTED,
        PrivacyTier.UNKNOWN: PrivacyReason.CLASSIFICATION_MISSING,
    }
    return PrivacyDecision.create(
        tier=tier,
        reason=reasons[tier],
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _public_submission(
    context: PublicJobCaptureContext,
    *,
    privacy: PrivacyDecision,
    delivery_id: str = "delivery.admission.public-job",
) -> CaptureSubmission:
    return CaptureSubmission.for_public_job(
        context=context,
        payload=TextPayload("Synthetic admission contract capture"),
        delivery_id=delivery_id,
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=SYNTHETIC_SOURCE_REFERENCE,
        provenance=Provenance.create(
            source_ref=SYNTHETIC_SOURCE_REFERENCE,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=privacy,
    )


def test_admission_limits_defaults_are_safe_non_zero_and_ordered() -> None:
    limits = AdmissionLimits()
    counts = (
        limits.max_envelope_bytes,
        limits.max_body_bytes,
        limits.requests_per_minute_per_principal,
        limits.max_concurrent_admissions,
        limits.max_writer_waiters,
    )
    assert all(type(count) is int and count > 0 for count in counts)
    assert type(limits.storage_high_free_bytes) is int
    assert type(limits.storage_critical_free_bytes) is int
    assert limits.storage_high_free_bytes == 2 * 1024 * 1024 * 1024
    assert limits.storage_critical_free_bytes == 512 * 1024 * 1024
    assert limits.storage_critical_free_bytes < limits.storage_high_free_bytes


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_envelope_bytes": 0},
        {"max_body_bytes": -1},
        {"requests_per_minute_per_principal": 0},
        {"max_concurrent_admissions": None},
        {"max_writer_waiters": -5},
        {"storage_high_free_bytes": 0},
        {"storage_critical_free_bytes": -1},
        {"storage_high_free_bytes": True},
        {"storage_critical_free_bytes": "1024"},
        {"storage_high_free_bytes": 512, "storage_critical_free_bytes": 512},
        {"storage_high_free_bytes": 256, "storage_critical_free_bytes": 512},
    ],
)
def test_admission_limits_reject_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AdmissionLimits(**overrides)  # type: ignore[arg-type]


def test_admission_limits_round_trip_and_reject_unknown_or_missing_keys() -> None:
    limits = AdmissionLimits(
        max_envelope_bytes=1024,
        max_body_bytes=512,
        requests_per_minute_per_principal=7,
        max_concurrent_admissions=2,
        max_writer_waiters=4,
        storage_high_free_bytes=4096,
        storage_critical_free_bytes=1024,
    )
    encoded = limits.to_dict()
    assert AdmissionLimits.from_dict(encoded) == limits
    with pytest.raises(ValueError):
        AdmissionLimits.from_dict({**encoded, "unexpected": True})
    dropped = {key: value for key, value in encoded.items() if key != "max_envelope_bytes"}
    with pytest.raises(ValueError):
        AdmissionLimits.from_dict(dropped)


def test_capture_admission_result_values_are_stable_and_classified() -> None:
    assert {result.value for result in CaptureAdmissionResult} == ADMISSION_RESULT_VALUES
    for result in CaptureAdmissionResult:
        error = CaptureAdmissionError(result)
        assert isinstance(error, ValueError)
        assert error.result is result
        assert error.retryable is (result in RETRYABLE_RESULTS)
        assert result.value in str(error)


def test_capture_admission_error_retryability_split() -> None:
    terminal = {
        CaptureAdmissionResult.ENVELOPE_TOO_LARGE,
        CaptureAdmissionResult.BODY_TOO_LARGE,
        CaptureAdmissionResult.STORAGE_CRITICAL,
        CaptureAdmissionResult.TIER_NOT_PERMITTED,
    }
    assert set(CaptureAdmissionResult) == RETRYABLE_RESULTS | terminal
    assert not RETRYABLE_RESULTS & terminal
    for result in RETRYABLE_RESULTS:
        assert CaptureAdmissionError(result).retryable is True
    for result in terminal:
        assert CaptureAdmissionError(result).retryable is False


def test_requested_tier_exposes_the_submission_privacy_tier() -> None:
    profile = _profile()
    context = _public_job_context(profile)
    for tier in PrivacyTier:
        submission = _public_submission(context, privacy=_privacy(tier))
        assert submission.requested_tier is tier
    owner = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("Synthetic owner requested tier capture"),
        delivery_id="delivery.admission.owner",
    )
    assert owner.requested_tier is PrivacyTier.PERSONAL


def test_public_job_request_digest_binds_the_requested_tier() -> None:
    profile = _profile()
    context = _public_job_context(profile)
    public_first = _public_submission(context, privacy=_privacy(PrivacyTier.PUBLIC))
    public_repeat = _public_submission(context, privacy=_privacy(PrivacyTier.PUBLIC))
    secret = _public_submission(context, privacy=_privacy(PrivacyTier.SECRET))
    assert public_first.requested_tier is PrivacyTier.PUBLIC
    assert secret.requested_tier is PrivacyTier.SECRET
    assert "privacy" in public_first.request_value()
    assert public_first.request_sha256() == public_repeat.request_sha256()
    assert public_first.request_sha256() != secret.request_sha256()


def test_owner_request_value_and_digest_are_byte_for_byte_unchanged() -> None:
    profile = _profile()
    payload = TextPayload("Synthetic owner digest stability capture")
    owner = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=payload,
        delivery_id="delivery.admission.owner",
    )
    expected_value = {
        "action": "quick",
        "capture_why": None,
        "intent": None,
        "payload": payload.to_dict(),
        "source_origin": "owner",
        "space_id": None,
        "title": None,
    }
    request_value = owner.request_value()
    assert set(request_value) == OWNER_REQUEST_VALUE_KEYS
    assert "privacy" not in request_value
    assert request_value == expected_value
    assert (
        owner.request_sha256() == sha256(portable_canonical_json_bytes(expected_value)).hexdigest()
    )


@pytest.mark.parametrize(
    "current,candidate,expected",
    [
        (PrivacyTier.PUBLIC, PrivacyTier.PUBLIC, PrivacyTier.PUBLIC),
        (PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.WORK),
        (PrivacyTier.WORK, PrivacyTier.PUBLIC, PrivacyTier.WORK),
        (PrivacyTier.PUBLIC, PrivacyTier.PERSONAL, PrivacyTier.PERSONAL),
        (PrivacyTier.PERSONAL, PrivacyTier.SECRET, PrivacyTier.SECRET),
        (PrivacyTier.SECRET, PrivacyTier.PERSONAL, PrivacyTier.SECRET),
        (PrivacyTier.WORK, PrivacyTier.SECRET, PrivacyTier.SECRET),
        (PrivacyTier.SECRET, PrivacyTier.UNKNOWN, PrivacyTier.UNKNOWN),
        (PrivacyTier.UNKNOWN, PrivacyTier.SECRET, PrivacyTier.UNKNOWN),
        (PrivacyTier.UNKNOWN, PrivacyTier.PUBLIC, PrivacyTier.UNKNOWN),
        (PrivacyTier.PUBLIC, PrivacyTier.UNKNOWN, PrivacyTier.UNKNOWN),
    ],
)
def test_narrowest_tier_narrows_by_documented_precedence(
    current: PrivacyTier, candidate: PrivacyTier, expected: PrivacyTier
) -> None:
    assert narrowest_tier(current, candidate) is expected


def test_narrowest_tier_never_widens_across_all_pairs() -> None:
    for current in PrivacyTier:
        for candidate in PrivacyTier:
            result = narrowest_tier(current, candidate)
            assert result in {current, candidate}
            assert RESTRICTIVENESS[result] >= RESTRICTIVENESS[current]
            assert RESTRICTIVENESS[result] >= RESTRICTIVENESS[candidate]


def test_engine_package_reexports_the_admission_contracts() -> None:
    import open_brain_engine.engine as engine_package

    assert engine_package.AdmissionLimits is AdmissionLimits
    assert engine_package.CaptureAdmissionError is CaptureAdmissionError
    assert engine_package.CaptureAdmissionResult is CaptureAdmissionResult


def test_capture_receipt_defaults_fail_closed_to_equal_unknown_tiers() -> None:
    receipt = CaptureReceipt("capture_synthetic", "text", "inbox", "pending_enrichment", None, None)
    assert receipt.duplicate is False
    assert receipt.requested_tier is PrivacyTier.UNKNOWN
    assert receipt.final_admitted_tier is PrivacyTier.UNKNOWN


def test_public_capture_receipt_projection_keeps_both_bound_tiers() -> None:
    receipt = CaptureReceipt(
        capture_id="capture_synthetic",
        payload_family="text",
        state="inbox",
        enrichment_state="pending_enrichment",
        space_id=None,
        canonical_path="sources/captures/2026/09/20/capture_synthetic.md",
        duplicate=False,
        requested_tier=PrivacyTier.WORK,
        final_admitted_tier=PrivacyTier.SECRET,
    )

    projected = project_public_capture_receipt(receipt)

    assert projected.canonical_path == receipt.capture_id
    assert projected.requested_tier is PrivacyTier.WORK
    assert projected.final_admitted_tier is PrivacyTier.SECRET


def test_redaction_boundary_classifier_reuses_the_approved_detector() -> None:
    from open_brain_engine.engine.capture import redaction_boundary_classifier

    profile = _profile()
    plain = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("Synthetic plain owner capture"),
        delivery_id="delivery.redaction.plain",
    )
    credential = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("Synthetic password: hunter2-specimen capture"),
        delivery_id="delivery.redaction.credential",
    )

    assert redaction_boundary_classifier(plain) is None
    assert redaction_boundary_classifier(credential) is PrivacyTier.SECRET


def test_for_local_owner_accepts_every_explicit_tier_with_the_canonical_reason() -> None:
    profile = _profile()
    for tier in PrivacyTier:
        submission = CaptureSubmission.for_local_owner(
            profile=profile,
            payload=TextPayload("Synthetic explicit tier owner capture"),
            delivery_id=f"delivery.admission.owner.explicit.{tier.value}",
            privacy_tier=tier,
        )
        assert submission.privacy == _privacy(tier)
        assert submission.requested_tier is tier
        submission.validate_profile(profile)


def test_for_local_owner_without_a_tier_keeps_the_fixed_local_decision() -> None:
    profile = _profile()
    fixed = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("Synthetic fixed local privacy owner capture"),
        delivery_id="delivery.admission.owner.fixed",
    )
    explicit_personal = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("Synthetic fixed local privacy owner capture"),
        delivery_id="delivery.admission.owner.fixed",
        privacy_tier=PrivacyTier.PERSONAL,
    )
    assert fixed.privacy == _privacy(PrivacyTier.PERSONAL)
    assert fixed.privacy == explicit_personal.privacy
    assert fixed == explicit_personal
    fixed.validate_profile(profile)
    explicit_personal.validate_profile(profile)


def test_owner_explicit_tier_does_not_change_the_owner_request_digest() -> None:
    profile = _profile()
    payload = TextPayload("Synthetic owner digest tier stability capture")
    fixed = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=payload,
        delivery_id="delivery.admission.owner.digest",
    )
    tiered = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=payload,
        delivery_id="delivery.admission.owner.digest",
        privacy_tier=PrivacyTier.WORK,
    )
    assert tiered.request_sha256() == fixed.request_sha256()


def test_validate_profile_rejects_an_owner_submission_with_a_mismatched_tier() -> None:
    profile = _profile()
    submission = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("Synthetic mismatched owner tier capture"),
        delivery_id="delivery.admission.owner.mismatch",
        privacy_tier=PrivacyTier.WORK,
    )
    submission.validate_profile(profile)
    # POLICY_WORK permits authority, but the owner path never grants any: a
    # work decision carrying cloud authority is not what the explicit tier
    # builds, so the re-derivation rejects it.
    mismatched = replace(
        submission,
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.WORK,
            reason=PrivacyReason.POLICY_WORK,
            policy_version="privacy-v1",
            authority=Authority(cloud=True, external_egress=False),
        ),
    )
    with pytest.raises(ValueError, match="does not match the local profile"):
        mismatched.validate_profile(profile)


def test_for_local_owner_rejects_an_unknown_explicit_tier() -> None:
    profile = _profile()
    with pytest.raises(ValueError, match="invalid privacy tier"):
        CaptureSubmission.for_local_owner(
            profile=profile,
            payload=TextPayload("Synthetic invalid tier owner capture"),
            delivery_id="delivery.admission.owner.invalid-tier",
            privacy_tier="synthetic-tier",
        )


def _manifest_file(tmp_path: Path, document: str) -> Path:
    path = tmp_path / "privacy-manifest.json"
    path.write_text(document, encoding="utf-8")
    return path


def test_privacy_manifest_loads_exact_relative_roots_and_tiers(tmp_path: Path) -> None:
    manifest = CapturePrivacyManifest.load(
        _manifest_file(tmp_path, json.dumps({"docs": "work", "notes/private": "secret"}))
    )
    assert manifest.tier_for("docs") is PrivacyTier.WORK
    assert manifest.tier_for("docs/synthetic-note.md") is PrivacyTier.WORK
    assert manifest.tier_for("notes/private") is PrivacyTier.SECRET
    assert manifest.tier_for("notes/private/deep/synthetic-note.md") is PrivacyTier.SECRET
    assert manifest.tier_for("documentation.md") is None
    assert manifest.tier_for("notes/other.md") is None

    empty = CapturePrivacyManifest.load(_manifest_file(tmp_path, "{}"))
    assert empty.tier_for("docs/synthetic-note.md") is None


@pytest.mark.parametrize(
    "document",
    [
        '{"docs": "synthetic-tier"}',
        '{"docs": 7}',
        '{"/absolute/synthetic": "work"}',
        '{"../outside": "work"}',
        '{"": "work"}',
        '{"docs/": "work"}',
        '{"./docs": "work"}',
        '{"docs//inner": "work"}',
        '{"docs/inner": "work", "docs": "secret"}',
        '{"docs": "work", "docs": "secret"}',
        '["docs"]',
        '"docs"',
    ],
)
def test_privacy_manifest_rejects_invalid_documents(tmp_path: Path, document: str) -> None:
    with pytest.raises(MarkdownImportFailure) as raised:
        CapturePrivacyManifest.load(_manifest_file(tmp_path, document))
    assert raised.value.code == "invalid_privacy_manifest"


def test_privacy_manifest_requires_an_absolute_readable_bounded_file(tmp_path: Path) -> None:
    with pytest.raises(MarkdownImportFailure) as raised:
        CapturePrivacyManifest.load("relative-manifest.json")
    assert raised.value.code == "invalid_privacy_manifest"
    with pytest.raises(MarkdownImportFailure) as raised:
        CapturePrivacyManifest.load(tmp_path / "missing-manifest.json")
    assert raised.value.code == "invalid_privacy_manifest"
    oversized = tmp_path / "oversized-manifest.json"
    oversized.write_text(
        json.dumps({f"docs/synthetic-{index}": "work" for index in range(6000)}),
        encoding="utf-8",
    )
    with pytest.raises(MarkdownImportFailure) as raised:
        CapturePrivacyManifest.load(oversized)
    assert raised.value.code == "invalid_privacy_manifest"


def _destination_authority(
    profile: LocalEngineContext,
    *,
    allowed_capture_tiers: frozenset[PrivacyTier],
    issuer_epoch: int = 1,
) -> EffectiveAuthority:
    return EffectiveAuthority(
        principal_id="synthetic-destination-principal",
        session_id="synthetic-destination-session",
        capabilities=frozenset(),
        space_ids=None,
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        allowed_capture_tiers=allowed_capture_tiers,
        brain_id=derive_brain_id(profile.tenant_id),
        issuer_epoch=issuer_epoch,
    )


def _destination_submission(
    profile: LocalEngineContext,
    *,
    delivery_id: str = "delivery.admission.destination",
    requested_tier: PrivacyTier | None = PrivacyTier.WORK,
    authority: EffectiveAuthority | None = None,
) -> CaptureSubmission:
    if authority is None:
        authority = _destination_authority(
            profile, allowed_capture_tiers=frozenset(set(PrivacyTier))
        )
    return CaptureSubmission.for_destination_bound(
        profile=profile,
        authority=authority,
        payload=TextPayload("Synthetic destination-bound capture"),
        delivery_id=delivery_id,
        requested_tier=requested_tier,
    )


def test_for_destination_bound_without_a_tier_defaults_to_unknown() -> None:
    profile = _profile()
    submission = _destination_submission(profile, requested_tier=None)
    assert submission.submission_path is CaptureSubmissionPath.DESTINATION_BOUND
    assert submission.requested_tier is PrivacyTier.UNKNOWN
    assert submission.privacy == _privacy(PrivacyTier.UNKNOWN)
    assert submission.capture_why is None
    assert submission.capture_why_origin is CaptureWhyOrigin.AUTOMATION_ABSENT
    submission.validate_profile(profile)


def test_for_destination_bound_accepts_every_tier_inside_the_policy_set() -> None:
    profile = _profile()
    for tier in PrivacyTier:
        submission = _destination_submission(profile, requested_tier=tier)
        assert submission.requested_tier is tier
        assert submission.privacy == _privacy(tier)
        assert submission.privacy.authority.cloud is False
        assert submission.privacy.authority.external_egress is False
        submission.validate_profile(profile)


def test_for_destination_bound_rejects_a_tier_outside_the_policy_set() -> None:
    profile = _profile()
    authority = _destination_authority(profile, allowed_capture_tiers=frozenset({PrivacyTier.WORK}))
    with pytest.raises(CaptureAdmissionError) as raised:
        _destination_submission(profile, requested_tier=PrivacyTier.PUBLIC, authority=authority)
    assert raised.value.result is CaptureAdmissionResult.TIER_NOT_PERMITTED
    assert raised.value.retryable is False


def test_for_destination_bound_rejects_owner_or_unbound_authority() -> None:
    profile = _profile()
    owner = EffectiveAuthority(
        principal_id="synthetic-owner-principal",
        session_id="synthetic-owner-session",
        capabilities=frozenset(),
        space_ids=None,
        owner=True,
        allowed_capture_tiers=frozenset({PrivacyTier.WORK}),
        brain_id=derive_brain_id(profile.tenant_id),
        issuer_epoch=1,
    )
    with pytest.raises(ValueError, match="owner"):
        _destination_submission(profile, authority=owner)
    unbound = EffectiveAuthority(
        principal_id="synthetic-unbound-principal",
        session_id="synthetic-unbound-session",
        capabilities=frozenset(),
        space_ids=None,
    )
    with pytest.raises(ValueError, match="destination"):
        _destination_submission(profile, authority=unbound)


def test_destination_bound_digest_binds_tier_brain_and_epoch() -> None:
    profile = _profile()
    work = _destination_submission(profile, requested_tier=PrivacyTier.WORK)
    work_repeat = _destination_submission(profile, requested_tier=PrivacyTier.WORK)
    personal = _destination_submission(profile, requested_tier=PrivacyTier.PERSONAL)
    later_epoch_authority = _destination_authority(
        profile, allowed_capture_tiers=frozenset(set(PrivacyTier)), issuer_epoch=2
    )
    later_epoch = _destination_submission(
        profile, requested_tier=PrivacyTier.WORK, authority=later_epoch_authority
    )
    assert work.request_sha256() == work_repeat.request_sha256()
    assert work.request_sha256() != personal.request_sha256()
    assert work.request_sha256() != later_epoch.request_sha256()
    value = work.request_value()
    assert "privacy" in value
    assert value["destination_brain_id"] == derive_brain_id(profile.tenant_id)
    assert value["issuer_epoch"] == 1
    owner = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("Synthetic owner digest boundary capture"),
        delivery_id="delivery.admission.owner.boundary",
    )
    assert "destination_brain_id" not in owner.request_value()


def test_destination_bound_request_sha256_helper_matches_submissions_without_an_authority() -> None:
    profile = _profile()
    authority = _destination_authority(profile, allowed_capture_tiers=frozenset(set(PrivacyTier)))
    brain_id = derive_brain_id(profile.tenant_id)
    payload = TextPayload("Synthetic destination-bound digest helper capture")
    titled = CaptureSubmission.for_destination_bound(
        profile=profile,
        authority=authority,
        payload=payload,
        delivery_id="delivery.admission.destination.helper-1",
        requested_tier=PrivacyTier.WORK,
        title="Synthetic helper title",
    )
    tierless = CaptureSubmission.for_destination_bound(
        profile=profile,
        authority=authority,
        payload=payload,
        delivery_id="delivery.admission.destination.helper-2",
        requested_tier=None,
    )
    assert (
        destination_bound_request_sha256(
            destination_brain_id=brain_id,
            issuer_epoch=1,
            tenant_id=profile.tenant_id,
            principal_id=authority.principal_id,
            payload=payload,
            requested_tier=PrivacyTier.WORK,
            title="Synthetic helper title",
        )
        == titled.request_sha256()
    )
    assert (
        destination_bound_request_sha256(
            destination_brain_id=brain_id,
            issuer_epoch=1,
            tenant_id=profile.tenant_id,
            principal_id=authority.principal_id,
            payload=payload,
        )
        == tierless.request_sha256()
    )
    other_delivery = CaptureSubmission.for_destination_bound(
        profile=profile,
        authority=authority,
        payload=payload,
        delivery_id="delivery.admission.destination.helper-3",
        requested_tier=PrivacyTier.WORK,
        title="Synthetic helper title",
    )
    assert other_delivery.request_sha256() == titled.request_sha256()


def test_destination_bound_submissions_carry_the_public_job_restrictions() -> None:
    profile = _profile()
    submission = _destination_submission(profile)
    assert submission.action.value == "quick"
    assert submission.space_id is None
    with pytest.raises(ValueError, match="public-job capture cannot route to a space"):
        replace(submission, space_id=f"space_{uuid4()}")
    with pytest.raises(ValueError, match="public-job source origin is not allowed"):
        replace(
            submission,
            source_origin=ContentOrigin.OWNER_AUTHORED,
            provenance=Provenance.create(
                source_ref=submission.source_reference,
                content_origin=ContentOrigin.OWNER_AUTHORED,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            ),
        )
    with pytest.raises(ValueError, match="destination binding"):
        replace(submission, submission_path=CaptureSubmissionPath.PUBLIC_JOB)


def test_destination_binding_is_refused_on_other_submission_paths() -> None:
    profile = _profile()
    owner = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("Synthetic owner binding refusal capture"),
        delivery_id="delivery.admission.owner.binding",
    )
    with pytest.raises(ValueError, match="destination binding"):
        replace(
            owner,
            destination_brain_id=derive_brain_id(profile.tenant_id),
            issuer_epoch=1,
        )
