from __future__ import annotations

from dataclasses import dataclass

from open_brain_engine.capture.models import (
    ExtractionFailure,
    ExtractionState,
    ExtractorKind,
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
from open_brain_engine.core.ports import FetchRequest, FetchResponse

from open_brain_legacy._compat.open_brain_connectors.capture.extractors import ExtractionRequest
from open_brain_legacy.capture.extractors.article import ArticleExtractor


def _privacy(*, external_egress: bool = True) -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=external_egress),
    )


@dataclass
class FakeFetcher:
    body: bytes
    calls: int = 0
    requests: list[FetchRequest] | None = None

    def __post_init__(self) -> None:
        self.requests = []

    def fetch(self, request: FetchRequest, *, privacy: PrivacyDecision) -> FetchResponse:
        assert privacy.authority.external_egress is True
        self.calls += 1
        assert self.requests is not None
        self.requests.append(request)
        return FetchResponse(
            final_url=request.url,
            status=200,
            media_type="text/html; charset=utf-8",
            body=self.body,
        )


def test_article_extractor_fetches_bounded_html_and_returns_exact_normalized_extraction() -> None:
    fetcher = FakeFetcher(
        b"""<html><head><title>  Synthetic Article </title><style>hidden css</style>
        <script>secret script</script></head><body><p>Visible paragraph.</p>
        <div>Another line.</div></body></html>"""
    )
    request = ExtractionRequest(
        capture_id="cap_" + "a" * 64,
        url="HTTPS://Example.Test/article",
        timeout_seconds=2.5,
        max_bytes=512,
        max_redirects=1,
    )

    result = ArticleExtractor(fetcher).extract(request, privacy=_privacy())

    assert type(result).__name__ == "NormalizedExtraction"
    assert result.extractor is ExtractorKind.ARTICLE
    assert result.state is ExtractionState.COMPLETE
    assert result.source_type is SourceType.WEB
    assert result.content_kind is ContentKind.ARTICLE
    assert result.metadata.title == "Synthetic Article"
    assert result.metadata.canonical_url == "https://example.test/article"
    assert result.text == "Visible paragraph.\nAnother line."
    assert "hidden css" not in result.text
    assert "secret script" not in result.text
    assert result.transcript_state is TranscriptState.NOT_APPLICABLE
    assert result.assets == ()
    assert result.failure is None
    assert fetcher.calls == 1
    assert fetcher.requests == [
        FetchRequest(
            request_id="cap_" + "a" * 64,
            url="https://example.test/article",
            timeout_seconds=2.5,
            max_bytes=512,
            max_redirects=1,
            allowed_cookie_domains=(),
        )
    ]


def test_article_privacy_denial_prevents_fetcher_invocation() -> None:
    fetcher = FakeFetcher(b"<title>Should not fetch</title><p>body</p>")
    request = ExtractionRequest(capture_id="cap_" + "b" * 64, url="https://example.test/article")

    result = ArticleExtractor(fetcher).extract(
        request,
        privacy=_privacy(external_egress=False),
    )

    assert result.extractor is ExtractorKind.ARTICLE
    assert result.state is ExtractionState.REJECTED
    assert result.failure is ExtractionFailure.PRIVACY_DENIED
    assert result.text == ""
    assert result.metadata.canonical_url == "https://example.test/article"
    assert fetcher.calls == 0


def test_article_uses_owner_supplied_share_text_without_egress() -> None:
    fetcher = FakeFetcher(b"<title>Must not fetch</title>")
    request = ExtractionRequest(
        capture_id="cap_" + "e" * 64,
        url="https://example.test/article",
        text="Synthetic owner-supplied article text",
    )

    result = ArticleExtractor(fetcher).extract(
        request,
        privacy=_privacy(external_egress=False),
    )

    assert result.state is ExtractionState.COMPLETE
    assert result.text == "Synthetic owner-supplied article text"
    assert result.metadata.canonical_url == "https://example.test/article"
    assert fetcher.calls == 0


def test_article_body_limit_returns_closed_failure() -> None:
    fetcher = FakeFetcher(b"x" * 513)
    request = ExtractionRequest(
        capture_id="cap_" + "c" * 64,
        url="https://example.test/article",
        max_bytes=512,
    )

    result = ArticleExtractor(fetcher).extract(request, privacy=_privacy())

    assert result.state is ExtractionState.FAILED
    assert result.failure is ExtractionFailure.BODY_LIMIT
    assert result.text == ""


def test_article_without_visible_text_returns_no_content() -> None:
    fetcher = FakeFetcher(
        b"<html><head><title>Empty</title></head><body><style>hidden</style></body></html>"
    )
    request = ExtractionRequest(capture_id="cap_" + "d" * 64, url="https://example.test/empty")

    result = ArticleExtractor(fetcher).extract(request, privacy=_privacy())

    assert result.state is ExtractionState.NO_CONTENT
    assert result.failure is None
    assert result.metadata.title == "Empty"
    assert result.text == ""
