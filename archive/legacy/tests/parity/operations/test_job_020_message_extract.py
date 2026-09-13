from dataclasses import dataclass, field

from open_brain_legacy._compat.open_brain.integrations import Capability, IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.ports import (
    ProviderSyncRequest,
    ReviewDisposition,
    ReviewWriteKind,
    ReviewWriteRequest,
    ReviewWriteResult,
    SyncStatus,
)
from open_brain_legacy.integrations.messaging import (
    MessageBatch,
    MessageCandidate,
    MessageConfidence,
    MessagingIntegration,
)
from open_brain_legacy.operations.models import JobState
from open_brain_legacy.operations.optional_jobs import compose_message_extract_job


@dataclass
class SyntheticSource:
    def fetch(self, request: ProviderSyncRequest) -> MessageBatch:
        return MessageBatch(
            resource_ref=request.resource_ref,
            cursor_ref=request.cursor_ref,
            next_cursor_ref="cursor_next",
            candidates=(
                MessageCandidate("message_fixture", "content_fixture", MessageConfidence.HIGH),
            ),
        )


@dataclass
class MemoryCursorStore:
    cursor: str | None = None

    def current_cursor(self, resource_ref: str) -> str | None:
        return self.cursor

    def cursor_was_processed(self, resource_ref: str, cursor_ref: str | None) -> bool:
        return False

    def advance_cursor(
        self,
        resource_ref: str,
        *,
        expected_cursor: str | None,
        next_cursor: str,
    ) -> bool:
        if self.cursor != expected_cursor:
            return False
        self.cursor = next_cursor
        return True


@dataclass
class RecordingReviewWriter:
    requests: list[ReviewWriteRequest] = field(default_factory=list)

    def submit(self, request: ReviewWriteRequest) -> ReviewWriteResult:
        self.requests.append(request)
        return ReviewWriteResult(
            request_id=request.request_id,
            disposition=ReviewDisposition.QUEUED,
            review_id=request.review_id,
        )


def test_job_020_extracts_only_review_proposals_and_never_tasks() -> None:
    request = ProviderSyncRequest(
        capability=Capability.MESSAGING,
        resource_ref="thread_fixture",
        cursor_ref=None,
        dry_run=False,
    )
    job = compose_message_extract_job(request)

    assert job.state is JobState.ENABLED
    assert job.command == (
        "open-brain",
        "messages",
        "extract",
        "--resource-ref=thread_fixture",
        "--json",
        "--review-actions",
    )
    assert not any("task" in argument or "apply" in argument for argument in job.command)

    reviews = RecordingReviewWriter()
    result = MessagingIntegration(
        source=SyntheticSource(),
        cursors=MemoryCursorStore(),
        reviews=reviews,
        config=IntegrationConfig(live_adapters=frozenset({Capability.MESSAGING})),
    ).sync(request)

    assert result.status is SyncStatus.COMPLETED
    assert [review.kind for review in reviews.requests] == [ReviewWriteKind.PROPOSAL]
    assert not hasattr(reviews.requests[0], "task_id")
