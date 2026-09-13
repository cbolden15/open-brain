from __future__ import annotations

from dataclasses import dataclass

from open_brain_legacy._compat.open_brain.integrations.ports import (
    ReviewBoundWriter,
    ReviewDisposition,
    ReviewWriteKind,
    ReviewWriteRequest,
    ReviewWriteResult,
)
from open_brain_legacy.integrations.dev_workflows import (
    DevWorkflowFixtureJournal,
    DevWorkflowIntegration,
    FixtureJournalDisposition,
    FixtureJournalEntry,
    SessionSignal,
    SessionSignalDisposition,
    TimeoutClass,
    WorkWriteRequest,
)


@dataclass
class SyntheticReviewWriter:
    submitted: list[ReviewWriteRequest]
    failing_request_ids: frozenset[str]

    def submit(self, request: ReviewWriteRequest) -> ReviewWriteResult:
        self.submitted.append(request)
        if request.request_id in self.failing_request_ids:
            raise RuntimeError(
                "credential=synthetic-secret /synthetic/private raw-payload=synthetic"
            )
        return ReviewWriteResult(
            request_id=request.request_id,
            disposition=ReviewDisposition.QUEUED,
            review_id=request.review_id,
        )


def test_fixture_journal_is_capped_excluded_and_idempotent_with_timeout_classes() -> None:
    journal = DevWorkflowFixtureJournal(
        capacity=2,
        excluded_repository_ids=frozenset({"repository_excluded"}),
    )
    first = FixtureJournalEntry(
        entry_id="entry_first",
        repository_id="repository_allowed",
        timeout_class=TimeoutClass.SHORT,
    )

    assert journal.record(first).disposition is FixtureJournalDisposition.RECORDED
    assert journal.record(first).disposition is FixtureJournalDisposition.DUPLICATE
    assert journal.record(
        FixtureJournalEntry(
            entry_id="entry_excluded",
            repository_id="repository_excluded",
            timeout_class=TimeoutClass.LONG,
        )
    ).disposition is FixtureJournalDisposition.EXCLUDED
    assert journal.record(
        FixtureJournalEntry(
            entry_id="entry_second",
            repository_id="repository_allowed",
            timeout_class=TimeoutClass.LONG,
        )
    ).disposition is FixtureJournalDisposition.RECORDED
    assert journal.record(
        FixtureJournalEntry(
            entry_id="entry_overflow",
            repository_id="repository_allowed",
            timeout_class=TimeoutClass.SHORT,
        )
    ).disposition is FixtureJournalDisposition.CAPPED
    assert journal.entries == (
        first,
        FixtureJournalEntry(
            entry_id="entry_second",
            repository_id="repository_allowed",
            timeout_class=TimeoutClass.LONG,
        ),
    )


def test_session_signals_are_bounded_non_raising_idempotent_and_redacted() -> None:
    integration = DevWorkflowIntegration(
        allowed_repository_ids=frozenset({"repository_allowed"}),
        signal_capacity=1,
    )
    signal = SessionSignal(
        signal_id="signal_fixture",
        repository_id="repository_allowed",
        session_id="session_fixture",
    )

    assert (
        integration.record_session_signal(signal).disposition
        is SessionSignalDisposition.RECORDED
    )
    assert (
        integration.record_session_signal(signal).disposition
        is SessionSignalDisposition.DUPLICATE
    )
    failed = integration.record_session_signal(
        SessionSignal(
            signal_id="signal_other",
            repository_id="repository_other",
            session_id="session_fixture",
        )
    )

    assert failed.disposition is SessionSignalDisposition.FAILED
    assert "repository_other" not in str(failed)
    assert (
        integration.record_session_signal(object()).disposition
        is SessionSignalDisposition.FAILED
    )
    assert integration.session_signals == (signal,)


def test_work_writes_are_typed_confined_review_routed_idempotent_and_redacted() -> None:
    synthetic_writer = SyntheticReviewWriter(
        submitted=[], failing_request_ids=frozenset({"request_failure"})
    )
    writer: ReviewBoundWriter = synthetic_writer
    integration = DevWorkflowIntegration(
        allowed_repository_ids=frozenset({"repository_allowed"}),
        review_writer=writer,
        signal_capacity=1,
    )
    allowed = WorkWriteRequest(
        repository_id="repository_allowed",
        write=ReviewWriteRequest(
            request_id="request_allowed",
            kind=ReviewWriteKind.JOURNAL,
            content_ref="content_fixture",
            review_id="review_fixture",
        ),
    )

    assert integration.submit_work_write(allowed).disposition is ReviewDisposition.QUEUED
    assert integration.submit_work_write(allowed).disposition is ReviewDisposition.DUPLICATE
    assert synthetic_writer.submitted == [allowed.write]

    outside = WorkWriteRequest(
        repository_id="repository_outside",
        write=ReviewWriteRequest(
            request_id="request_outside",
            kind=ReviewWriteKind.PROPOSAL,
            content_ref="content_outside",
            review_id="review_outside",
        ),
    )
    assert integration.submit_work_write(outside).disposition is ReviewDisposition.BLOCKED
    assert synthetic_writer.submitted == [allowed.write]

    failure = WorkWriteRequest(
        repository_id="repository_allowed",
        write=ReviewWriteRequest(
            request_id="request_failure",
            kind=ReviewWriteKind.GOTCHA,
            content_ref="content_failure",
            review_id="review_failure",
        ),
    )
    failed = integration.submit_work_write(failure)

    assert failed.disposition is ReviewDisposition.BLOCKED
    assert "synthetic-secret" not in str(failed)
    assert "/synthetic/private" not in str(failed)
    assert "raw-payload" not in str(failed)
