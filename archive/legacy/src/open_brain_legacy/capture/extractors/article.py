"""Bounded HTML article extraction through the outbound-fetch port."""

from __future__ import annotations

import unicodedata
from html.parser import HTMLParser

from open_brain_engine.capture.models import (
    ExtractionFailure,
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    TranscriptState,
)
from open_brain_engine.core.ids import canonicalize_source_url
from open_brain_engine.core.models import ContentKind, PrivacyDecision, SourceType
from open_brain_engine.core.ports import FetchRequest, OutboundFetcher

from open_brain_legacy._compat.open_brain_connectors.capture.extractors import ExtractionRequest

ArticleExtractionRequest = ExtractionRequest


class ArticleExtractor:
    """Fetch and parse only bounded HTML, returning no persistence side effects."""

    def __init__(self, fetcher: OutboundFetcher) -> None:
        self._fetcher = fetcher

    def extract(
        self,
        request: ExtractionRequest,
        *,
        privacy: PrivacyDecision,
    ) -> NormalizedExtraction:
        canonical_url = _canonical_url(request.url)
        supplied = _normalize_lines(request.text)
        if supplied.strip():
            if len(supplied.encode("utf-8")) > request.max_bytes:
                return _result(
                    request,
                    state=ExtractionState.FAILED,
                    failure=ExtractionFailure.BODY_LIMIT,
                    canonical_url=canonical_url,
                )
            return _result(
                request,
                state=ExtractionState.COMPLETE,
                failure=None,
                canonical_url=canonical_url,
                title=_title_for(supplied),
                text=supplied,
            )
        if not privacy.authority.external_egress:
            return _result(
                request,
                state=ExtractionState.REJECTED,
                failure=ExtractionFailure.PRIVACY_DENIED,
                canonical_url=canonical_url,
            )
        if canonical_url is None:
            return _result(
                request,
                state=ExtractionState.REJECTED,
                failure=ExtractionFailure.INVALID_INPUT,
                canonical_url=None,
            )

        fetch_request = FetchRequest(
            request_id=request.capture_id or "article-extract",
            url=canonical_url,
            timeout_seconds=request.timeout_seconds,
            max_bytes=request.max_bytes,
            max_redirects=request.max_redirects,
        )
        try:
            response = self._fetcher.fetch(fetch_request, privacy=privacy)
        except Exception:
            return _result(
                request,
                state=ExtractionState.FAILED,
                failure=ExtractionFailure.FETCH_FAILED,
                canonical_url=canonical_url,
            )
        if len(response.body) > request.max_bytes:
            return _result(
                request,
                state=ExtractionState.FAILED,
                failure=ExtractionFailure.BODY_LIMIT,
                canonical_url=canonical_url,
            )
        if not 200 <= response.status < 300 or not _is_html(response.media_type):
            return _result(
                request,
                state=ExtractionState.FAILED,
                failure=ExtractionFailure.FETCH_FAILED,
                canonical_url=canonical_url,
            )

        parser = _ArticleParser()
        parser.feed(response.body.decode("utf-8", errors="replace"))
        parser.close()
        text = _normalize_lines("\n".join(parser.visible_chunks).strip())
        title = unicodedata.normalize("NFC", parser.title.strip()) or _title_for(text)
        return _result(
            request,
            state=ExtractionState.COMPLETE if text else ExtractionState.NO_CONTENT,
            failure=None,
            canonical_url=canonical_url,
            title=title or "Article",
            text=text,
        )


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.visible_chunks: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized_tag = tag.lower()
        if normalized_tag == "title":
            self._in_title = True
        elif normalized_tag in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "title":
            self._in_title = False
        elif normalized_tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        elif data.strip():
            self.visible_chunks.append(data.strip())


def _canonical_url(value: str) -> str | None:
    try:
        return canonicalize_source_url(value)
    except ValueError:
        return None


def _is_html(media_type: str | None) -> bool:
    return media_type is not None and media_type.split(";", 1)[0].strip().lower() in {
        "text/html",
        "application/xhtml+xml",
    }


def _normalize_lines(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _title_for(text: str) -> str:
    return next((line.strip() for line in text.split("\n") if line.strip()), "Article")[:80]


def _result(
    request: ExtractionRequest,
    *,
    state: ExtractionState,
    failure: ExtractionFailure | None,
    canonical_url: str | None,
    title: str | None = None,
    text: str = "",
) -> NormalizedExtraction:
    return NormalizedExtraction.create(
        extractor=ExtractorKind.ARTICLE,
        state=state,
        source_type=SourceType.WEB,
        content_kind=ContentKind.ARTICLE,
        metadata=ExtractionMetadata.create(title=title, canonical_url=canonical_url),
        text=text,
        transcript=None,
        transcript_state=TranscriptState.NOT_APPLICABLE,
        assets=request.assets if failure is None else (),
        failure=failure,
    )
