from dataclasses import dataclass, field

from open_brain_legacy._compat.open_brain.integrations.config import IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.ports import (
    Capability,
    ProviderSyncRequest,
    ReviewDisposition,
    ReviewWriteKind,
    ReviewWriteRequest,
    ReviewWriteResult,
)
from open_brain_legacy.integrations.messaging import (
    MessageBatch,
    MessageCandidate,
    MessageConfidence,
    MessagingIntegration,
)


@dataclass
class SyntheticMessageSource:
    calls: int = 0
    confidence: MessageConfidence = MessageConfidence.LOW
    fail: bool = False

    def fetch(self, request: ProviderSyncRequest) -> MessageBatch:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic provider failure detail")
        return MessageBatch(
            resource_ref=request.resource_ref,
            cursor_ref=request.cursor_ref,
            next_cursor_ref="cursor_002",
            candidates=(
                MessageCandidate(
                    message_ref="message_001",
                    content_ref="content_001",
                    confidence=self.confidence,
                ),
            ),
        )


@dataclass
class MemoryCursorStore:
    cursor: str | None = None
    processed: set[str | None] = field(default_factory=set)

    def current_cursor(self, resource_ref: str) -> str | None:
        assert resource_ref == "thread_001"
        return self.cursor

    def advance_cursor(
        self,
        resource_ref: str,
        *,
        expected_cursor: str | None,
        next_cursor: str,
    ) -> bool:
        assert resource_ref == "thread_001"
        if self.cursor != expected_cursor:
            return False
        self.processed.add(expected_cursor)
        self.cursor = next_cursor
        return True

    def cursor_was_processed(self, resource_ref: str, cursor_ref: str | None) -> bool:
        assert resource_ref == "thread_001"
        return cursor_ref in self.processed


@dataclass
class RecordingReviewWriter:
    requests: list[ReviewWriteRequest] = field(default_factory=list)
    fail: bool = False

    def submit(self, request: ReviewWriteRequest) -> ReviewWriteResult:
        if self.fail:
            raise RuntimeError("synthetic review failure detail")
        self.requests.append(request)
        return ReviewWriteResult(
            request_id=request.request_id,
            disposition=ReviewDisposition.QUEUED,
            review_id=request.review_id,
        )


def test_low_confidence_message_queues_one_review_proposal_for_duplicate_cursor() -> None:
    source = SyntheticMessageSource()
    cursors = MemoryCursorStore()
    reviews = RecordingReviewWriter()
    integration = MessagingIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.MESSAGING})),
        source=source,
        cursors=cursors,
        reviews=reviews,
    )
    request = ProviderSyncRequest(
        capability=Capability.MESSAGING,
        resource_ref="thread_001",
        cursor_ref=None,
        dry_run=False,
    )

    first = integration.sync(request)
    duplicate = integration.sync(request)

    assert first.to_dict() == {
        "capability": "messaging",
        "status": "completed",
        "created": 1,
        "updated": 0,
        "removed": 0,
        "next_cursor_ref": "cursor_002",
    }
    assert duplicate.to_dict() == {
        "capability": "messaging",
        "status": "completed",
        "created": 0,
        "updated": 0,
        "removed": 0,
        "next_cursor_ref": "cursor_002",
    }
    assert source.calls == 1
    assert cursors.cursor == "cursor_002"
    assert len(reviews.requests) == 1
    assert reviews.requests[0].kind is ReviewWriteKind.PROPOSAL
    assert reviews.requests[0].content_ref == "content_001"


def test_reorder_high_confidence_and_failures_stay_review_only_and_redacted() -> None:
    reordered_source = SyntheticMessageSource()
    reordered_reviews = RecordingReviewWriter()
    reordered = MessagingIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.MESSAGING})),
        source=reordered_source,
        cursors=MemoryCursorStore(cursor="cursor_002", processed={None}),
        reviews=reordered_reviews,
    ).sync(
        ProviderSyncRequest(
            capability=Capability.MESSAGING,
            resource_ref="thread_001",
            cursor_ref="cursor_999",
            dry_run=False,
        )
    )

    high_source = SyntheticMessageSource(confidence=MessageConfidence.HIGH)
    high_cursors = MemoryCursorStore()
    high_reviews = RecordingReviewWriter()
    high = MessagingIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.MESSAGING})),
        source=high_source,
        cursors=high_cursors,
        reviews=high_reviews,
    ).sync(
        ProviderSyncRequest(
            capability=Capability.MESSAGING,
            resource_ref="thread_001",
            cursor_ref=None,
            dry_run=False,
        )
    )

    provider_failure_source = SyntheticMessageSource(fail=True)
    provider_failure = MessagingIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.MESSAGING})),
        source=provider_failure_source,
        cursors=MemoryCursorStore(),
        reviews=RecordingReviewWriter(),
    ).sync(
        ProviderSyncRequest(
            capability=Capability.MESSAGING,
            resource_ref="thread_001",
            cursor_ref=None,
            dry_run=False,
        )
    )

    review_failure_cursors = MemoryCursorStore()
    review_failure = MessagingIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.MESSAGING})),
        source=SyntheticMessageSource(confidence=MessageConfidence.HIGH),
        cursors=review_failure_cursors,
        reviews=RecordingReviewWriter(fail=True),
    ).sync(
        ProviderSyncRequest(
            capability=Capability.MESSAGING,
            resource_ref="thread_001",
            cursor_ref=None,
            dry_run=False,
        )
    )

    disabled_source = SyntheticMessageSource(fail=True)
    disabled = MessagingIntegration(
        source=disabled_source,
        cursors=MemoryCursorStore(),
        reviews=RecordingReviewWriter(),
    ).sync(
        ProviderSyncRequest(
            capability=Capability.MESSAGING,
            resource_ref="thread_001",
            cursor_ref=None,
            dry_run=False,
        )
    )

    assert reordered.to_dict() == {
        "capability": "messaging",
        "status": "retryable",
        "created": 0,
        "updated": 0,
        "removed": 0,
        "next_cursor_ref": "cursor_002",
    }
    assert reordered_source.calls == 0
    assert reordered_reviews.requests == []
    assert high.status.value == "completed"
    assert high_cursors.cursor == "cursor_002"
    assert [request.kind for request in high_reviews.requests] == [ReviewWriteKind.PROPOSAL]
    assert provider_failure.to_dict() == {
        "capability": "messaging",
        "status": "retryable",
        "created": 0,
        "updated": 0,
        "removed": 0,
    }
    assert review_failure.to_dict() == provider_failure.to_dict()
    assert review_failure_cursors.cursor is None
    assert "failure detail" not in repr((provider_failure, review_failure))
    assert disabled.status.value == "unsupported"
    assert disabled_source.calls == 0
