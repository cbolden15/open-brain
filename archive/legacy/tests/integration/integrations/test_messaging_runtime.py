from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from open_brain_engine.core.ids import capture_id_for, review_id_for
from open_brain_engine.core.models import Intent
from open_brain_engine.storage.filesystem import RootConfinementError
from open_brain_engine.storage.sqlite import connect_database

from open_brain_legacy._compat.open_brain.integrations.config import IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.ports import (
    Capability,
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
)
from open_brain_legacy.integrations.messaging_runtime import (
    MessagingFailureStage,
    PersistentMessagingCursorStore,
    PersistentMessagingRuntime,
    SqliteMessageInbox,
    SqliteReviewProposalWriter,
)
from open_brain_legacy.review.store import SqliteReviewStore
from tests.unit.storage._factories import FixedClock


@dataclass
class SyntheticMessageSource:
    calls: int = 0
    fail: bool = False
    message_ref: str = "message_001"
    content_ref: str = "content_001"
    next_cursor_ref: str = "cursor_002"

    def fetch(self, request: ProviderSyncRequest) -> MessageBatch:
        self.calls += 1
        if self.fail:
            raise RuntimeError(
                "provider failure api key: synthetic-secret /private/provider/state.json"
            )
        return MessageBatch(
            resource_ref=request.resource_ref,
            cursor_ref=request.cursor_ref,
            next_cursor_ref=self.next_cursor_ref,
            candidates=(
                MessageCandidate(
                    message_ref=self.message_ref,
                    content_ref=self.content_ref,
                    confidence=MessageConfidence.HIGH,
                ),
            ),
        )


@dataclass
class RecordingReviewWriter:
    requests: list[ReviewWriteRequest] = field(default_factory=list)
    fail: bool = False

    def submit(self, request: ReviewWriteRequest) -> ReviewWriteResult:
        if self.fail:
            raise RuntimeError(
                "review failure bearer synthetic-token /private/review/runtime.log"
            )
        self.requests.append(request)
        return ReviewWriteResult(
            request_id=request.request_id,
            disposition=ReviewDisposition.QUEUED,
            review_id=request.review_id,
        )


def test_runtime_sync_stays_proposal_only_and_handles_duplicate_reorder_and_retry(
    tmp_path: Path,
) -> None:
    source = SyntheticMessageSource()
    reviews = RecordingReviewWriter()
    runtime = _runtime(tmp_path, source=source, reviews=reviews)
    request = ProviderSyncRequest(
        capability=Capability.MESSAGING,
        resource_ref="thread_001",
        cursor_ref=None,
        dry_run=False,
    )

    first = runtime.sync(request)
    duplicate = runtime.sync(request)
    reordered = runtime.sync(
        ProviderSyncRequest(
            capability=Capability.MESSAGING,
            resource_ref="thread_001",
            cursor_ref="cursor_999",
            dry_run=False,
        )
    )
    failed = _runtime(
        tmp_path / "failed",
        source=SyntheticMessageSource(fail=True),
        reviews=RecordingReviewWriter(),
    ).sync(request)

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
    assert reordered.status is SyncStatus.RETRYABLE
    assert reordered.next_cursor_ref == "cursor_002"
    assert failed.status is SyncStatus.RETRYABLE
    assert source.calls == 1
    assert [item.kind for item in reviews.requests] == [ReviewWriteKind.PROPOSAL]
    assert [item.content_ref for item in reviews.requests] == ["content_001"]


def test_runtime_cursor_state_persists_across_reopen_without_refetch(tmp_path: Path) -> None:
    request = ProviderSyncRequest(
        capability=Capability.MESSAGING,
        resource_ref="thread_001",
        cursor_ref=None,
        dry_run=False,
    )
    first_source = SyntheticMessageSource()
    runtime = _runtime(tmp_path, source=first_source, reviews=RecordingReviewWriter())

    first = runtime.sync(request)

    reopened_source = SyntheticMessageSource()
    reopened_runtime = _runtime(
        tmp_path,
        source=reopened_source,
        reviews=RecordingReviewWriter(),
    )
    duplicate = reopened_runtime.sync(request)

    assert first.status is SyncStatus.COMPLETED
    assert duplicate.status is SyncStatus.COMPLETED
    assert duplicate.created == 0
    assert duplicate.next_cursor_ref == "cursor_002"
    assert first_source.calls == 1
    assert reopened_source.calls == 0


def test_sqlite_inbox_routes_opaque_candidates_to_canonical_review_store(
    tmp_path: Path,
) -> None:
    inbox = SqliteMessageInbox(root=tmp_path, clock=FixedClock())
    batch = MessageBatch(
        resource_ref="thread_001",
        cursor_ref=None,
        next_cursor_ref="cursor_002",
        candidates=(
            MessageCandidate(
                message_ref="message_001",
                content_ref="content_001",
                confidence=MessageConfidence.HIGH,
            ),
        ),
    )
    inbox.enqueue(batch)
    inbox.enqueue(batch)
    runtime = PersistentMessagingRuntime(
        source=inbox,
        reviews=SqliteReviewProposalWriter(root=tmp_path, clock=FixedClock()),
        state=PersistentMessagingCursorStore(root=tmp_path, clock=FixedClock()),
        config=IntegrationConfig(live_adapters=frozenset({Capability.MESSAGING})),
    )
    request = ProviderSyncRequest(
        capability=Capability.MESSAGING,
        resource_ref="thread_001",
        cursor_ref=None,
        dry_run=False,
    )

    result = runtime.sync(request)
    capture_id = capture_id_for(
        {
            "identity_version": 1,
            "source": "messaging",
            "content_ref": "content_001",
        }
    )
    review_id = review_id_for(capture_id, Intent.ACTION_CANDIDATE.value)
    with SqliteReviewStore(
        root=tmp_path,
        database_name="review/review.sqlite3",
        clock=FixedClock(),
    ) as store:
        review = store.get(review_id)

    inbox_path = tmp_path / "integrations" / "message-inbox.sqlite3"
    cursor_path = tmp_path / "integrations" / "messaging-runtime.sqlite3"
    with sqlite3.connect(f"file:{inbox_path}?mode=ro", uri=True) as connection:
        inbox_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    with sqlite3.connect(f"file:{cursor_path}?mode=ro", uri=True) as connection:
        cursor_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert result.status is SyncStatus.COMPLETED
    assert result.created == 1
    assert review is not None
    assert str(review.proposal.review_id) == str(review_id)
    assert review.proposal.source_ref == "content_001"
    assert review.approved_record is None
    assert "message_batches" in inbox_tables
    assert "cursor_state" not in inbox_tables
    assert "cursor_state" in cursor_tables
    assert "message_batches" not in cursor_tables


def test_runtime_retries_when_persistent_state_is_malformed(tmp_path: Path) -> None:
    source = SyntheticMessageSource()
    runtime = _runtime(tmp_path, source=source, reviews=RecordingReviewWriter())
    request = ProviderSyncRequest(
        capability=Capability.MESSAGING,
        resource_ref="thread_001",
        cursor_ref=None,
        dry_run=False,
    )

    assert runtime.sync(request).status is SyncStatus.COMPLETED

    connection = connect_database(
        root=tmp_path,
        database_name="runtime/messaging.sqlite3",
    )
    try:
        connection.execute(
            "UPDATE cursor_state SET current_cursor = ? WHERE resource_ref = ?",
            ("bad/value", "thread_001"),
        )
    finally:
        connection.close()

    malformed = runtime.sync(request)
    failures = runtime.state.failures("thread_001")

    assert malformed.status is SyncStatus.RETRYABLE
    assert failures[-1].stage is MessagingFailureStage.CURSOR_STATE
    assert "bad/value" not in failures[-1].summary.text


@pytest.mark.parametrize(
    ("root_factory", "database_name", "message"),
    (
        (
            lambda path: path / "linked-root",
            "runtime/messaging.sqlite3",
            "unsafe storage root",
        ),
        (
            lambda path: path,
            "../escape.sqlite3",
            "unsafe relative path",
        ),
    ),
)
def test_runtime_store_refuses_unsafe_paths_and_symlinks(
    tmp_path: Path,
    root_factory: Callable[[Path], Path],
    database_name: str,
    message: str,
) -> None:
    actual_root = tmp_path / "actual-root"
    actual_root.mkdir(mode=0o700)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual_root, target_is_directory=True)

    store = PersistentMessagingCursorStore(
        root=root_factory(tmp_path),
        database_name=database_name,
    )

    with pytest.raises(RootConfinementError, match=message):
        store.current_cursor("thread_001")


def test_runtime_persists_redacted_provider_and_review_failures(tmp_path: Path) -> None:
    provider_runtime = _runtime(
        tmp_path / "provider",
        source=SyntheticMessageSource(fail=True),
        reviews=RecordingReviewWriter(),
    )
    review_runtime = _runtime(
        tmp_path / "review",
        source=SyntheticMessageSource(),
        reviews=RecordingReviewWriter(fail=True),
    )
    request = ProviderSyncRequest(
        capability=Capability.MESSAGING,
        resource_ref="thread_001",
        cursor_ref=None,
        dry_run=False,
    )

    provider_result = provider_runtime.sync(request)
    review_result = review_runtime.sync(request)
    provider_failure = provider_runtime.state.failures("thread_001")[-1]
    review_failure = review_runtime.state.failures("thread_001")[-1]

    assert provider_result.status is SyncStatus.RETRYABLE
    assert review_result.status is SyncStatus.RETRYABLE
    assert provider_failure.stage is MessagingFailureStage.SOURCE
    assert review_failure.stage is MessagingFailureStage.REVIEW
    for failure in (provider_failure, review_failure):
        assert failure.summary.text == "[redacted]"
        assert "synthetic-secret" not in failure.summary.text
        assert "synthetic-token" not in failure.summary.text
        assert "/private/" not in failure.summary.text


def _runtime(
    root: Path,
    *,
    source: SyntheticMessageSource,
    reviews: RecordingReviewWriter,
) -> PersistentMessagingRuntime:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return PersistentMessagingRuntime(
        source=source,
        reviews=reviews,
        state=PersistentMessagingCursorStore(
            root=root,
            database_name="runtime/messaging.sqlite3",
        ),
        config=IntegrationConfig(live_adapters=frozenset({Capability.MESSAGING})),
    )
