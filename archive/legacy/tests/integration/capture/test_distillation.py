from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from open_brain_engine.capture.models import (
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    TranscriptState,
)
from open_brain_engine.core.models import (
    Authority,
    ContentKind,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    SourceType,
)
from open_brain_engine.core.policy import BoundaryErrorCode
from open_brain_engine.core.ports import TextModelRequest, TextModelResult
from open_brain_engine.providers.base import ProviderFailure, ProviderService

from open_brain_legacy.capture.distillation import (
    DistillationInput,
    DistillationService,
    FilesystemDistillationStore,
)


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.WORK,
        reason=PrivacyReason.POLICY_WORK,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _input() -> DistillationInput:
    extraction = NormalizedExtraction.create(
        extractor=ExtractorKind.YOUTUBE,
        state=ExtractionState.COMPLETE,
        source_type=SourceType.YOUTUBE,
        content_kind=ContentKind.VIDEO,
        metadata=ExtractionMetadata.create(title="Synthetic source", platform="youtube"),
        text="",
        transcript="Synthetic transcript",
        transcript_state=TranscriptState.ACQUIRED,
        assets=(),
        failure=None,
    )
    return DistillationInput.create(
        capture_id="cap_" + "a" * 64,
        capture_why="Compare this with the synthetic project plan",
        extraction=extraction,
    )


@dataclass
class _Provider:
    response: TextModelResult | Exception
    calls: int = 0
    request: TextModelRequest | None = None

    def complete(self, request: TextModelRequest, *, privacy: PrivacyDecision) -> TextModelResult:
        del privacy
        self.calls += 1
        self.request = request
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _service(tmp_path: Path, provider: _Provider) -> DistillationService:
    provider_service = ProviderService(
        provider_name="local",
        cloud_enabled=False,
        local_factory=lambda: provider,
        cloud_factory=lambda credential: provider,
        resolve_cloud_secret=lambda: None,
    )
    return DistillationService(
        store=FilesystemDistillationStore(tmp_path / "distilled"),
        provider=provider_service,
    )


def test_typed_distillation_preserves_owner_reason_and_replay_skips_provider(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        TextModelResult(
            text=json.dumps(
                {
                    "title": "Synthetic distilled title",
                    "summary": "A bounded synthetic summary.",
                    "topics": ["capture", "testing"],
                }
            ),
            provider_name="local",
        )
    )
    service = _service(tmp_path, provider)

    first = service.distill(_input(), privacy=_privacy())
    replay = service.distill(_input(), privacy=_privacy())

    assert first.error_code is None and first.value is not None
    assert first.value.capture_why == _input().capture_why
    assert first.value.content_kind is ContentKind.VIDEO
    assert first.value.topics == ("capture", "testing")
    assert first.value.provider_name == "local"
    assert set(first.value.to_dict()) == {
        "schema_version",
        "capture_id",
        "capture_why",
        "content_kind",
        "title",
        "summary",
        "topics",
        "provider_name",
    }
    assert replay == first
    assert provider.calls == 1
    assert provider.request is not None
    assert _input().capture_why in provider.request.prompt


def test_distillation_canonicalizes_unsorted_duplicate_topics(tmp_path: Path) -> None:
    provider = _Provider(
        TextModelResult(
            text=json.dumps(
                {
                    "title": "Synthetic distilled title",
                    "summary": "A bounded synthetic summary.",
                    "topics": ["testing", "capture", "testing"],
                }
            ),
            provider_name="local",
        )
    )

    result = _service(tmp_path, provider).distill(_input(), privacy=_privacy())

    assert result.error_code is None
    assert result.value is not None
    assert result.value.topics == ("capture", "testing")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("not-json", BoundaryErrorCode.MALFORMED_RESPONSE),
        (
            json.dumps(
                {"title": "Synthetic", "summary": "Summary", "topics": [], "extra": True}
            ),
            BoundaryErrorCode.MALFORMED_RESPONSE,
        ),
    ],
)
def test_distillation_rejects_non_schema_provider_output(
    tmp_path: Path, response: str, expected: BoundaryErrorCode
) -> None:
    result = _service(
        tmp_path,
        _Provider(TextModelResult(text=response, provider_name="local")),
    ).distill(_input(), privacy=_privacy())

    assert result.value is None
    assert result.error_code is expected


def test_distillation_maps_provider_timeout_and_is_disabled_without_provider(
    tmp_path: Path,
) -> None:
    timeout = _service(
        tmp_path,
        _Provider(ProviderFailure(BoundaryErrorCode.PROVIDER_TIMEOUT)),
    ).distill(_input(), privacy=_privacy())
    disabled = DistillationService(
        store=FilesystemDistillationStore(tmp_path / "disabled")
    ).distill(_input(), privacy=_privacy())

    assert timeout.value is None
    assert timeout.error_code is BoundaryErrorCode.PROVIDER_TIMEOUT
    assert disabled.value is None
    assert disabled.error_code is BoundaryErrorCode.OPTIONAL_EXTRA_UNAVAILABLE
