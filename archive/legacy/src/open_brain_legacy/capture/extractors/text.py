"""Pure supplied-text extraction."""

from __future__ import annotations

import unicodedata

from open_brain_engine.capture.models import (
    ExtractionFailure,
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    TranscriptState,
)
from open_brain_engine.core.models import ContentKind, PrivacyDecision, SourceType

from open_brain_legacy._compat.open_brain_connectors.capture.extractors import ExtractionRequest

_TITLE_MAX_CHARACTERS = 80
TextExtractionRequest = ExtractionRequest


class TextExtractor:
    """Normalize supplied text without any external side effects."""

    def extract(
        self,
        request: ExtractionRequest,
        *,
        privacy: PrivacyDecision,
    ) -> NormalizedExtraction:
        del privacy
        text = _normalize_lines(request.text)
        title = _title_for(text)
        metadata = ExtractionMetadata.create(title=title)
        try:
            return NormalizedExtraction.create(
                extractor=ExtractorKind.TEXT,
                state=ExtractionState.COMPLETE,
                source_type=SourceType.TEXT,
                content_kind=ContentKind.OTHER,
                metadata=metadata,
                text=text,
                transcript=None,
                transcript_state=TranscriptState.NOT_APPLICABLE,
                assets=request.assets,
                failure=None,
            )
        except ValueError:
            return NormalizedExtraction.create(
                extractor=ExtractorKind.TEXT,
                state=ExtractionState.FAILED,
                source_type=SourceType.TEXT,
                content_kind=ContentKind.OTHER,
                metadata=metadata,
                text="",
                transcript=None,
                transcript_state=TranscriptState.NOT_APPLICABLE,
                assets=(),
                failure=ExtractionFailure.INVALID_INPUT,
            )


def _normalize_lines(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _title_for(text: str) -> str:
    first_line = next((line.strip() for line in text.split("\n") if line.strip()), "Note")
    return first_line[:_TITLE_MAX_CHARACTERS]
