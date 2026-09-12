from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from open_brain_engine.capture.models import ExtractionFailure, ExtractionState
from open_brain_engine.core.models import (
    Authority,
    ContentKind,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    RawAssetRef,
    SourceType,
)
from open_brain_engine.core.ports import FetchResponse

from open_brain_legacy._compat.open_brain_connectors.capture.extractors import ExtractionRequest
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeExtractionRequest,
    YouTubeExtractor,
)
from open_brain_legacy._compat.open_brain_connectors.capture.media import (
    DEFAULT_MEDIA_LIMITS,
    MediaCommand,
    MediaTool,
)
from open_brain_legacy.capture.extractors.article import ArticleExtractor
from open_brain_legacy.capture.extractors.social import (
    SocialExtractionRequest,
    SocialExtractor,
    SocialMediaResult,
)
from open_brain_legacy.capture.extractors.text import TextExtractor


def _privacy(*, egress: bool = True, cloud: bool = False) -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="test-v1",
        authority=Authority(cloud=cloud, external_egress=egress),
    )


def _asset(letter: str, size: int = 12) -> RawAssetRef:
    return RawAssetRef.create(
        asset_id="asset_" + letter * 64,
        sha256=letter * 64,
        media_type="image/jpeg",
        byte_length=size,
    )


@dataclass
class FakeFetcher:
    response: FetchResponse = field(
        default_factory=lambda: FetchResponse("https://example.test/", 200, "text/html", b"")
    )
    calls: list[object] = field(default_factory=list)

    def fetch(self, request: object, *, privacy: PrivacyDecision) -> FetchResponse:
        self.calls.append(request)
        return self.response


@dataclass
class FakeMediaAdapter:
    results: dict[MediaTool, SocialMediaResult] = field(default_factory=dict)
    calls: list[tuple[MediaTool, MediaCommand]] = field(default_factory=list)

    def download(self, url: str, *, tool: MediaTool, command: MediaCommand) -> SocialMediaResult:
        self.calls.append((tool, command))
        return self.results.get(tool, SocialMediaResult(used_tool=tool))


@pytest.mark.parametrize(
    ("metadata_type", "expected"),
    [
        ("event", ContentKind.EVENT),
        ("article", ContentKind.ARTICLE),
        ("product", ContentKind.PRODUCT),
        ("place", ContentKind.PLACE),
        ("post", ContentKind.POST),
        ("video.other", ContentKind.VIDEO),
        ("unrecognized", ContentKind.OTHER),
    ],
)
def test_generic_content_kind_uses_bounded_html_metadata(
    metadata_type: str, expected: ContentKind
) -> None:
    html = (
        f'<meta property="og:type" content="{metadata_type}"><title>Fallback title</title>'
    ).encode()
    fetcher = FakeFetcher(FetchResponse("https://example.test/x", 200, "text/html", html))

    result = SocialExtractor(fetcher=fetcher).extract(
        SocialExtractionRequest(
            url="https://example.test/x/instagram.com?host=twitter.com&kind=event"
        ),
        privacy=_privacy(),
    )

    assert result.source_type is SourceType.WEB
    assert result.content_kind is expected
    assert result.metadata.platform == "generic"
    assert result.metadata.title == "Fallback title"


def test_path_and_query_cannot_reclassify_a_generic_host() -> None:
    fetcher = FakeFetcher(
        FetchResponse("https://example.test/x", 200, "text/html", b"<title>Generic</title>")
    )

    result = SocialExtractor(fetcher=fetcher).extract(
        SocialExtractionRequest(
            url="https://example.test/instagram.com/video?host=eventbrite.com&kind=product"
        ),
        privacy=_privacy(),
    )

    assert result.source_type is SourceType.WEB
    assert result.content_kind is ContentKind.OTHER
    assert result.metadata.platform == "generic"


def test_known_social_and_event_hosts_have_closed_classification() -> None:
    extractor = SocialExtractor(fetcher=FakeFetcher())

    social = extractor.extract(
        SocialExtractionRequest(url="https://mobile.twitter.com/synthetic/status/1"),
        privacy=_privacy(),
    )
    event = extractor.extract(
        SocialExtractionRequest(url="https://www.eventbrite.com/e/synthetic"), privacy=_privacy()
    )

    assert (social.source_type, social.content_kind, social.metadata.platform) == (
        SourceType.SOCIAL,
        ContentKind.POST,
        "twitter",
    )
    assert (event.source_type, event.content_kind) == (SourceType.WEB, ContentKind.EVENT)


def test_social_uses_owner_supplied_share_text_without_egress() -> None:
    fetcher = FakeFetcher()

    result = SocialExtractor(fetcher=fetcher).extract(
        SocialExtractionRequest(
            url="https://x.com/synthetic/status/1",
            supplied_text="Synthetic owner-supplied post text",
        ),
        privacy=_privacy(egress=False),
    )

    assert result.state is ExtractionState.COMPLETE
    assert result.source_type is SourceType.SOCIAL
    assert result.content_kind is ContentKind.POST
    assert result.text == "Synthetic owner-supplied post text"
    assert fetcher.calls == []


def test_all_four_extractors_return_exact_normalized_results_without_persistence() -> None:
    article_html = b"<title>Article</title><p>Body</p>"
    social_html = b'<meta property="og:type" content="post"><title>Post</title>'
    article = ArticleExtractor(
        FakeFetcher(FetchResponse("https://example.test/article", 200, "text/html", article_html))
    ).extract(
        ExtractionRequest(
            capture_id="cap_" + "a" * 64,
            url="https://example.test/article",
        ),
        privacy=_privacy(),
    )
    text = TextExtractor().extract(
        ExtractionRequest(capture_id="cap_" + "b" * 64, text="Note"),
        privacy=_privacy(egress=False),
    )
    youtube = YouTubeExtractor().extract(
        YouTubeExtractionRequest(
            url="https://youtu.be/dQw4w9WgXcQ",
            supplied_transcript="Transcript",
        ),
        privacy=_privacy(),
    )
    social = SocialExtractor(
        fetcher=FakeFetcher(
            FetchResponse("https://example.test/post", 200, "text/html", social_html)
        )
    ).extract(
        SocialExtractionRequest(url="https://example.test/post"),
        privacy=_privacy(),
    )

    empty_metadata = {
        "title": None,
        "author": None,
        "published_at": None,
        "canonical_url": None,
        "platform": None,
        "video_id": None,
    }
    assert [result.to_dict() for result in (text, article, youtube, social)] == [
        {
            "extractor": "text",
            "state": "complete",
            "source_type": "text",
            "content_kind": "other",
            "metadata": {**empty_metadata, "title": "Note"},
            "text": "Note",
            "transcript": None,
            "transcript_state": "not_applicable",
            "assets": [],
            "failure": None,
        },
        {
            "extractor": "article",
            "state": "complete",
            "source_type": "web",
            "content_kind": "article",
            "metadata": {
                **empty_metadata,
                "title": "Article",
                "canonical_url": "https://example.test/article",
            },
            "text": "Body",
            "transcript": None,
            "transcript_state": "not_applicable",
            "assets": [],
            "failure": None,
        },
        {
            "extractor": "youtube",
            "state": "complete",
            "source_type": "youtube",
            "content_kind": "video",
            "metadata": {
                **empty_metadata,
                "platform": "youtube",
                "video_id": "dQw4w9WgXcQ",
            },
            "text": "",
            "transcript": "Transcript",
            "transcript_state": "supplied",
            "assets": [],
            "failure": None,
        },
        {
            "extractor": "social",
            "state": "complete",
            "source_type": "web",
            "content_kind": "post",
            "metadata": {
                **empty_metadata,
                "title": "Post",
                "canonical_url": "https://example.test/post",
                "platform": "generic",
            },
            "text": social_html.decode(),
            "transcript": None,
            "transcript_state": "not_applicable",
            "assets": [],
            "failure": None,
        },
    ]


def test_media_is_bounded_never_returns_paths_and_default_adapter_fails_closed() -> None:
    fetcher = FakeFetcher()
    good = FakeMediaAdapter(
        {MediaTool.YT_DLP: SocialMediaResult(assets=(_asset("a"),), used_tool=MediaTool.YT_DLP)}
    )
    accepted = SocialExtractor(fetcher=fetcher, media_adapter=good).extract(
        SocialExtractionRequest(url="https://instagram.com/p/synthetic", acquire_media=True),
        privacy=_privacy(),
    )
    assert accepted.assets == (_asset("a"),)
    assert [tool for tool, _ in good.calls] == [MediaTool.YT_DLP]
    assert good.calls[0][1].argv == ("yt-dlp", "--no-playlist")
    assert good.calls[0][1].environment == ()
    assert good.calls[0][1].limits == DEFAULT_MEDIA_LIMITS

    invalid = FakeMediaAdapter(
        {
            MediaTool.YT_DLP: SocialMediaResult(
                assets=(Path("/synthetic/not-durable"),),
                used_tool=MediaTool.YT_DLP,
            )
        }
    )
    rejected = SocialExtractor(fetcher=FakeFetcher(), media_adapter=invalid).extract(
        SocialExtractionRequest(url="https://instagram.com/p/synthetic", acquire_media=True),
        privacy=_privacy(),
    )
    unavailable = SocialExtractor(fetcher=FakeFetcher()).extract(
        SocialExtractionRequest(url="https://instagram.com/p/synthetic", acquire_media=True),
        privacy=_privacy(),
    )
    assert rejected.failure is ExtractionFailure.MALFORMED_TOOL_OUTPUT
    assert rejected.assets == ()
    assert unavailable.failure is ExtractionFailure.TOOL_UNAVAILABLE


def test_media_limits_hold_across_fallback_results() -> None:
    too_many = FakeMediaAdapter(
        {
            MediaTool.YT_DLP: SocialMediaResult(used_tool=MediaTool.YT_DLP),
            MediaTool.GALLERY_DL: SocialMediaResult(
                assets=tuple(_asset("012345678"[index]) for index in range(9)),
                used_tool=MediaTool.GALLERY_DL,
            ),
        }
    )
    too_large = FakeMediaAdapter(
        {
            MediaTool.YT_DLP: SocialMediaResult(
                assets=(_asset("f", size=50 * 1024 * 1024 + 1),),
                used_tool=MediaTool.YT_DLP,
            )
        }
    )

    count_result = SocialExtractor(fetcher=FakeFetcher(), media_adapter=too_many).extract(
        SocialExtractionRequest(url="https://instagram.com/p/synthetic", acquire_media=True),
        privacy=_privacy(),
    )
    bytes_result = SocialExtractor(fetcher=FakeFetcher(), media_adapter=too_large).extract(
        SocialExtractionRequest(url="https://instagram.com/p/synthetic", acquire_media=True),
        privacy=_privacy(),
    )

    assert count_result.failure is ExtractionFailure.MEDIA_LIMIT
    assert bytes_result.failure is ExtractionFailure.MEDIA_LIMIT
    assert [tool for tool, _ in too_many.calls] == [MediaTool.YT_DLP, MediaTool.GALLERY_DL]


def test_media_fallback_is_ordered_and_closed_failure_stops_fallback() -> None:
    fallback = FakeMediaAdapter(
        {
            MediaTool.YT_DLP: SocialMediaResult(used_tool=MediaTool.YT_DLP),
            MediaTool.GALLERY_DL: SocialMediaResult(
                assets=(_asset("b"),), used_tool=MediaTool.GALLERY_DL
            ),
        }
    )
    resource_failure = FakeMediaAdapter(
        {
            MediaTool.YT_DLP: SocialMediaResult(
                used_tool=MediaTool.YT_DLP,
                failure=ExtractionFailure.TOOL_RESOURCE_LIMIT,
            )
        }
    )
    request = SocialExtractionRequest(url="https://instagram.com/p/synthetic", acquire_media=True)

    fallback_result = SocialExtractor(fetcher=FakeFetcher(), media_adapter=fallback).extract(
        request, privacy=_privacy()
    )
    failed_result = SocialExtractor(fetcher=FakeFetcher(), media_adapter=resource_failure).extract(
        request, privacy=_privacy()
    )

    assert fallback_result.assets == (_asset("b"),)
    assert [tool for tool, _ in fallback.calls] == [
        MediaTool.YT_DLP,
        MediaTool.GALLERY_DL,
    ]
    assert failed_result.state is ExtractionState.FAILED
    assert failed_result.failure is ExtractionFailure.TOOL_RESOURCE_LIMIT
    assert [tool for tool, _ in resource_failure.calls] == [MediaTool.YT_DLP]


def test_transcription_is_executor_denied_without_fetch_media_or_executor_path() -> None:
    fetcher = FakeFetcher()
    media = FakeMediaAdapter(
        {MediaTool.YT_DLP: SocialMediaResult(assets=(_asset("a"),), used_tool=MediaTool.YT_DLP)}
    )
    extractor = SocialExtractor(fetcher=fetcher, media_adapter=media)
    request = SocialExtractionRequest(
        url="https://instagram.com/p/synthetic", acquire_media=True, transcribe_audio=True
    )

    denied = extractor.extract(request, privacy=_privacy())
    cloud_authorized = extractor.extract(request, privacy=_privacy(cloud=True))
    private = extractor.extract(request, privacy=_privacy(egress=False, cloud=True))

    assert denied.failure is ExtractionFailure.EXECUTOR_DENIED
    assert cloud_authorized.failure is ExtractionFailure.EXECUTOR_DENIED
    assert denied.transcript is None
    assert cloud_authorized.transcript is None
    assert private.failure is ExtractionFailure.PRIVACY_DENIED
    assert fetcher.calls == []
    assert media.calls == []
