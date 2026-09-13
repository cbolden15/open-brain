from __future__ import annotations

from open_brain_engine.capture.models import ExtractionState, ExtractorKind, TranscriptState
from open_brain_engine.core.models import (
    Authority,
    ContentKind,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    SourceType,
)

from open_brain_legacy._compat.open_brain_connectors.capture.extractors import ExtractionRequest
from open_brain_legacy.capture.extractors.text import TextExtractor


def _privacy(*, external_egress: bool = True) -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=external_egress),
    )


def test_text_result_has_no_provider_or_persistence_dependencies() -> None:
    request = ExtractionRequest(
        capture_id="cap_" + "a" * 64,
        text="  Cafe\u0301 title  \r\nsecond line\r\n",
    )

    result = TextExtractor().extract(request, privacy=_privacy(external_egress=False))

    assert type(result).__name__ == "NormalizedExtraction"
    assert result.extractor is ExtractorKind.TEXT
    assert result.state is ExtractionState.COMPLETE
    assert result.source_type is SourceType.TEXT
    assert result.content_kind is ContentKind.OTHER
    assert result.metadata.title == "Café title"
    assert result.text == "  Café title  \nsecond line\n"
    assert result.transcript is None
    assert result.transcript_state is TranscriptState.NOT_APPLICABLE
    assert result.assets == ()
    assert result.failure is None


def test_text_title_is_deterministic_and_bounded() -> None:
    request = ExtractionRequest(capture_id="cap_" + "b" * 64, text="x" * 200 + "\nbody")

    result = TextExtractor().extract(request, privacy=_privacy())

    assert result.metadata.title == "x" * 80
    assert len(result.metadata.title or "") == 80
    assert result.text == "x" * 200 + "\nbody"
