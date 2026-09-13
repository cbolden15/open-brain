from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    RawAssetRef,
)
from open_brain_engine.core.policy import BoundaryErrorCode
from open_brain_engine.core.ports import TextModelRequest, TextModelResult
from open_brain_engine.providers.base import ProviderService

from open_brain_legacy.providers.transcription import (
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionService,
)


class _ProviderSpy:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: TextModelRequest, *, privacy: PrivacyDecision) -> TextModelResult:
        self.calls += 1
        return TextModelResult(text="safe", provider_name="cloud")


def test_denied_cloud_has_zero_factory_secret_and_provider_calls() -> None:
    factory_calls = 0
    secret_calls = 0
    provider = _ProviderSpy()

    def factory(credential: str) -> _ProviderSpy:
        nonlocal factory_calls
        assert credential == "synthetic"
        factory_calls += 1
        return provider

    def resolve_secret() -> str | None:
        nonlocal secret_calls
        secret_calls += 1
        return "synthetic"

    decision = PrivacyDecision.create(
        tier=PrivacyTier.WORK,
        reason=PrivacyReason.POLICY_WORK,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )
    request = TextModelRequest.create(
        request_id="request.security-001",
        purpose="synthetic",
        prompt="Synthetic prompt",
        timeout_seconds=1.0,
        max_output_bytes=64,
    )

    result = ProviderService(
        provider_name="cloud",
        cloud_enabled=True,
        local_factory=lambda: provider,
        cloud_factory=factory,
        resolve_cloud_secret=resolve_secret,
    ).complete(request, privacy=decision)

    assert result.value is None
    assert result.error_code is BoundaryErrorCode.CLOUD_AUTHORITY_REQUIRED
    assert factory_calls == 0
    assert secret_calls == 0
    assert provider.calls == 0


def test_authorized_cloud_secret_canary_is_blocked_before_construction() -> None:
    factory_calls = 0
    secret_calls = 0
    provider = _ProviderSpy()

    def factory(credential: str) -> _ProviderSpy:
        nonlocal factory_calls
        assert credential == "synthetic"
        factory_calls += 1
        return provider

    def resolve_secret() -> str | None:
        nonlocal secret_calls
        secret_calls += 1
        return "synthetic"

    canary = "api" + "_key=" + "A" * 32
    decision = PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_CONFIRMED,
        policy_version="privacy-v1",
        authority=Authority(cloud=True, external_egress=False),
        confirmation_ref="confirmation.synthetic-001",
    )
    request = TextModelRequest.create(
        request_id="request.security-002",
        purpose="synthetic",
        prompt=canary,
        timeout_seconds=1.0,
        max_output_bytes=64,
    )

    result = ProviderService(
        provider_name="cloud",
        cloud_enabled=True,
        local_factory=lambda: provider,
        cloud_factory=factory,
        resolve_cloud_secret=resolve_secret,
    ).complete(request, privacy=decision)

    assert result.value is None
    assert result.error_code is BoundaryErrorCode.PROVIDER_REJECTED
    assert factory_calls == 0
    assert secret_calls == 0
    assert provider.calls == 0
    assert canary not in repr(result)


def test_transcription_disabled_or_unauthorized_never_constructs_provider() -> None:
    factory_calls = 0
    digest = "a" * 64

    class TranscriptionSpy:
        def transcribe(
            self, request: TranscriptionRequest, *, privacy: PrivacyDecision
        ) -> TranscriptionResult:
            del request, privacy
            return TranscriptionResult(text="Synthetic", provider_name="cloud")

    def factory() -> TranscriptionSpy:
        nonlocal factory_calls
        factory_calls += 1
        return TranscriptionSpy()

    request = TranscriptionRequest.create(
        request_id="transcription.security-001",
        asset=RawAssetRef.create(
            asset_id="asset_" + digest,
            sha256=digest,
            media_type="audio/mpeg",
            byte_length=1024,
        ),
        timeout_seconds=30.0,
        max_output_bytes=1024,
    )
    decision = PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )

    disabled = TranscriptionService(
        provider_factory=factory,
        cloud_provider=True,
    ).transcribe(request, privacy=decision)
    unauthorized = TranscriptionService(
        enabled=True,
        provider_factory=factory,
        cloud_provider=True,
    ).transcribe(request, privacy=decision)

    assert disabled.value is unauthorized.value is None
    assert disabled.error_code is BoundaryErrorCode.OPTIONAL_EXTRA_UNAVAILABLE
    assert unauthorized.error_code is BoundaryErrorCode.CLOUD_AUTHORITY_REQUIRED
    assert factory_calls == 0
    assert TranscriptionResult.__module__ == "open_brain_legacy.providers.transcription"
