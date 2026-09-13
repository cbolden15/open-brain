from __future__ import annotations

from dataclasses import dataclass

import pytest
from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    RawAssetRef,
    ValidationError,
)
from open_brain_engine.core.policy import BoundaryErrorCode
from open_brain_engine.providers.base import ProviderFailure

from open_brain_legacy.providers.transcription import (
    MAX_TRANSCRIPTION_MEDIA_BYTES,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionService,
)


def _privacy(*, cloud: bool = False) -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_CONFIRMED
        if cloud
        else PrivacyReason.PERSONAL_LOCAL_ONLY,
        policy_version="privacy-v1",
        authority=Authority(cloud=cloud, external_egress=False),
        confirmation_ref="confirmation.synthetic-001" if cloud else None,
    )


def _asset(*, media_type: str = "audio/mpeg", size: int = 1024) -> RawAssetRef:
    digest = "a" * 64
    return RawAssetRef.create(
        asset_id="asset_" + digest,
        sha256=digest,
        media_type=media_type,
        byte_length=size,
    )


def _request(*, max_output_bytes: int = 1024) -> TranscriptionRequest:
    return TranscriptionRequest.create(
        request_id="transcription.synthetic-001",
        asset=_asset(),
        timeout_seconds=30.0,
        max_output_bytes=max_output_bytes,
    )


@dataclass
class _Provider:
    response: TranscriptionResult | Exception | object
    calls: int = 0

    def transcribe(
        self, request: TranscriptionRequest, *, privacy: PrivacyDecision
    ) -> TranscriptionResult:
        del request, privacy
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response  # type: ignore[return-value]


def test_transcription_is_disabled_by_default_without_provider_construction() -> None:
    factory_calls = 0

    def factory() -> _Provider:
        nonlocal factory_calls
        factory_calls += 1
        return _Provider(TranscriptionResult(text="Synthetic", provider_name="local"))

    result = TranscriptionService(provider_factory=factory).transcribe(
        _request(), privacy=_privacy()
    )

    assert result.value is None
    assert result.error_code is BoundaryErrorCode.OPTIONAL_EXTRA_UNAVAILABLE
    assert factory_calls == 0


def test_enabled_fake_transcription_succeeds_and_maps_closed_failures() -> None:
    provider = _Provider(TranscriptionResult(text="Synthetic transcript", provider_name="local"))
    service = TranscriptionService(
        enabled=True,
        provider_factory=lambda: provider,
        cloud_provider=False,
    )

    success = service.transcribe(_request(), privacy=_privacy())
    provider.response = ProviderFailure(BoundaryErrorCode.PROVIDER_TIMEOUT)
    timeout = service.transcribe(_request(), privacy=_privacy())
    provider.response = object()
    malformed = service.transcribe(_request(), privacy=_privacy())

    assert success.value == TranscriptionResult(
        text="Synthetic transcript", provider_name="local"
    )
    assert success.error_code is None
    assert timeout.error_code is BoundaryErrorCode.PROVIDER_TIMEOUT
    assert malformed.error_code is BoundaryErrorCode.MALFORMED_RESPONSE


def test_transcription_enforces_media_type_size_and_output_bounds() -> None:
    with pytest.raises(ValidationError):
        TranscriptionRequest.create(
            request_id="transcription.synthetic-002",
            asset=_asset(media_type="image/jpeg"),
            timeout_seconds=30.0,
            max_output_bytes=1024,
        )
    with pytest.raises(ValidationError):
        TranscriptionRequest.create(
            request_id="transcription.synthetic-003",
            asset=_asset(size=MAX_TRANSCRIPTION_MEDIA_BYTES + 1),
            timeout_seconds=30.0,
            max_output_bytes=1024,
        )

    provider = _Provider(TranscriptionResult(text="x" * 5, provider_name="local"))
    result = TranscriptionService(
        enabled=True,
        provider_factory=lambda: provider,
        cloud_provider=False,
    ).transcribe(_request(max_output_bytes=4), privacy=_privacy())
    assert result.error_code is BoundaryErrorCode.OUTPUT_LIMIT


def test_cloud_transcription_requires_explicit_authority_before_factory_call() -> None:
    factory_calls = 0

    def factory() -> _Provider:
        nonlocal factory_calls
        factory_calls += 1
        return _Provider(TranscriptionResult(text="Synthetic", provider_name="cloud"))

    denied = TranscriptionService(
        enabled=True,
        provider_factory=factory,
        cloud_provider=True,
    ).transcribe(_request(), privacy=_privacy())
    allowed = TranscriptionService(
        enabled=True,
        provider_factory=factory,
        cloud_provider=True,
    ).transcribe(_request(), privacy=_privacy(cloud=True))

    assert denied.error_code is BoundaryErrorCode.CLOUD_AUTHORITY_REQUIRED
    assert allowed.error_code is None
    assert factory_calls == 1

