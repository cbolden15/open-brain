from dataclasses import dataclass, field

from open_brain_legacy._compat.open_brain.integrations import Capability, IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.ports import (
    ProviderSyncRequest,
    ReviewDisposition,
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
from open_brain_legacy.operations.optional_jobs import compose_message_sync_job


@dataclass
class SyntheticSource:
    calls: int = 0
    fail: bool = False

    def fetch(self, request: ProviderSyncRequest) -> MessageBatch:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic provider failure detail")
        return MessageBatch(
            resource_ref=request.resource_ref,
            cursor_ref=request.cursor_ref,
            next_cursor_ref="cursor_next",
            candidates=(
                MessageCandidate("message_fixture", "content_fixture", MessageConfidence.LOW),
            ),
        )


@dataclass
class MemoryCursorStore:
    cursor: str | None = None
    processed: set[str | None] = field(default_factory=set)

    def current_cursor(self, resource_ref: str) -> str | None:
        return self.cursor

    def cursor_was_processed(self, resource_ref: str, cursor_ref: str | None) -> bool:
        return cursor_ref in self.processed

    def advance_cursor(
        self,
        resource_ref: str,
        *,
        expected_cursor: str | None,
        next_cursor: str,
    ) -> bool:
        if self.cursor != expected_cursor:
            return False
        self.processed.add(expected_cursor)
        self.cursor = next_cursor
        return True


class ReviewWriter:
    def submit(self, request: ReviewWriteRequest) -> ReviewWriteResult:
        return ReviewWriteResult(
            request_id=request.request_id,
            disposition=ReviewDisposition.QUEUED,
            review_id=request.review_id,
        )


def _integration(source: SyntheticSource, cursors: MemoryCursorStore) -> MessagingIntegration:
    return MessagingIntegration(
        source=source,
        cursors=cursors,
        reviews=ReviewWriter(),
        config=IntegrationConfig(live_adapters=frozenset({Capability.MESSAGING})),
    )


def test_job_021_composes_enabled_dry_run_cursor_sync_argv() -> None:
    request = ProviderSyncRequest(
        capability=Capability.MESSAGING,
        resource_ref="thread_fixture",
        cursor_ref="cursor_fixture",
        dry_run=True,
    )

    job = compose_message_sync_job(request)

    assert job.state is JobState.ENABLED
    assert job.command == (
        "open-brain",
        "messages",
        "sync",
        "--resource-ref=thread_fixture",
        "--cursor-ref=cursor_fixture",
        "--dry-run",
        "--json",
    )


def test_job_021_adapter_handles_duplicate_reorder_and_redacted_failure() -> None:
    request = ProviderSyncRequest(Capability.MESSAGING, "thread_fixture", None, False)
    source = SyntheticSource()
    cursors = MemoryCursorStore()
    integration = _integration(source, cursors)

    first = integration.sync(request)
    duplicate = integration.sync(request)
    reordered = integration.sync(
        ProviderSyncRequest(Capability.MESSAGING, "thread_fixture", "cursor_other", False)
    )
    failed = _integration(SyntheticSource(fail=True), MemoryCursorStore()).sync(request)

    assert first.status is SyncStatus.COMPLETED
    assert duplicate.status is SyncStatus.COMPLETED
    assert reordered.status is SyncStatus.RETRYABLE
    assert failed.status is SyncStatus.RETRYABLE
    assert source.calls == 1
    assert "failure detail" not in repr(failed)
