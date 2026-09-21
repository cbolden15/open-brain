from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

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
    narrowest_tier,
)
from open_brain_engine.engine.contracts import (
    AdmissionLimits,
    CaptureAdmissionError,
    CaptureAdmissionResult,
    CaptureSubmission,
    LocalEngineContext,
    PublicJobCaptureContext,
    TextPayload,
)
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
    "batch_too_large",
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
        limits.max_batch_items,
        limits.max_batch_bytes,
        limits.requests_per_minute_per_principal,
        limits.max_concurrent_admissions,
        limits.max_writer_waiters,
    )
    assert all(type(count) is int and count > 0 for count in counts)
    assert type(limits.storage_high_watermark_ratio) is float
    assert type(limits.storage_critical_watermark_ratio) is float
    assert limits.storage_high_watermark_ratio > 0.0
    assert limits.storage_high_watermark_ratio < limits.storage_critical_watermark_ratio < 1.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_envelope_bytes": 0},
        {"max_body_bytes": -1},
        {"max_batch_items": True},
        {"max_batch_bytes": "1024"},
        {"requests_per_minute_per_principal": 0},
        {"max_concurrent_admissions": None},
        {"max_writer_waiters": -5},
        {"storage_high_watermark_ratio": 0.0},
        {"storage_high_watermark_ratio": 1.0},
        {"storage_critical_watermark_ratio": 0.0},
        {"storage_critical_watermark_ratio": 1.0},
        {"storage_high_watermark_ratio": 0.9, "storage_critical_watermark_ratio": 0.9},
        {"storage_high_watermark_ratio": 0.95, "storage_critical_watermark_ratio": 0.9},
    ],
)
def test_admission_limits_reject_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AdmissionLimits(**overrides)  # type: ignore[arg-type]


def test_admission_limits_round_trip_and_reject_unknown_or_missing_keys() -> None:
    limits = AdmissionLimits(
        max_envelope_bytes=1024,
        max_body_bytes=512,
        max_batch_items=3,
        max_batch_bytes=2048,
        requests_per_minute_per_principal=7,
        max_concurrent_admissions=2,
        max_writer_waiters=4,
        storage_high_watermark_ratio=0.5,
        storage_critical_watermark_ratio=0.75,
    )
    encoded = limits.to_dict()
    assert AdmissionLimits.from_dict(encoded) == limits
    with pytest.raises(ValueError):
        AdmissionLimits.from_dict({**encoded, "unexpected": True})
    dropped = {key: value for key, value in encoded.items() if key != "max_batch_items"}
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
        CaptureAdmissionResult.BATCH_TOO_LARGE,
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
