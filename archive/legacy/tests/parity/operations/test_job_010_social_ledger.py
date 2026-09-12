from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.core.ids import CaptureId, ReviewId
from open_brain_engine.core.models import Intent, PrivacyTier
from open_brain_engine.engine import LockScope
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ApprovedIntentRecord,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewProposal,
    ReviewState,
)

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.models import (
    DeploymentTarget,
    HostRole,
    JobState,
)
from open_brain_legacy.operations.writer_jobs import (
    ApprovalBinding,
    EffectCapability,
    EffectCommand,
    EffectReceipt,
    EffectRecord,
    JobRunDisposition,
    JobRunResult,
    PreparedEffect,
    ReplayJournal,
    ReviewBoundary,
    ScheduledEffect,
    WriterJobError,
    WriterJobInvocation,
    run_writer_job,
)


class MemoryJournal(ReplayJournal):
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], JobRunResult] = {}

    def completed(self, job_id: str, replay_key: str) -> JobRunResult | None:
        return self.runs.get((job_id, replay_key))

    def begin(self, job_id: str, replay_key: str, request_digest_sha256: str) -> None:
        return None

    def complete(self, result: JobRunResult) -> None:
        self.runs[(result.job_id, result.replay_key)] = result


class FailFirstCompletionJournal(MemoryJournal):
    def __init__(self) -> None:
        super().__init__()
        self.fail_completion = True

    def complete(self, result: JobRunResult) -> None:
        if self.fail_completion:
            self.fail_completion = False
            raise RuntimeError("synthetic crash before journal completion")
        super().complete(result)


class DurableLedgerCapability(EffectCapability):
    effect = ScheduledEffect.LEDGER_WRITE
    local_only = True
    dry_run = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.apply_calls = 0
        self.receipts: dict[tuple[str, str], EffectReceipt] = {}
        self.effects: dict[str, PreparedEffect] = {}

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        return self.receipts.get((job_id, replay_key))

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        receipt = EffectReceipt.from_command(command)
        self.receipts[(command.job_id, command.replay_key)] = receipt
        return receipt

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self.apply_calls += 1
        with (self.root / "ledger.jsonl").open("a") as stream:
            for record in command.prepared.records:
                stream.write(record.record_id + "\n")
        self.effects[receipt.effect_digest_sha256] = command.prepared

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        return self.effects.get(receipt.effect_digest_sha256)


class CrashAfterEffectLedgerCapability(DurableLedgerCapability):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail_after_effect = True

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        super().apply(command, receipt)
        if self.fail_after_effect:
            self.fail_after_effect = False
            raise RuntimeError("synthetic crash inside effect apply")


class MemoryReviewReader:
    def __init__(self, aggregates: tuple[ReviewAggregate, ...]) -> None:
        self.aggregates = {
            aggregate.proposal.review_id: aggregate for aggregate in aggregates
        }

    def get(self, review_id: ReviewId) -> ReviewAggregate | None:
        return self.aggregates.get(review_id)


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


class RecordingLedgerFlow:
    def __init__(self, *, record_count: int = 2) -> None:
        self.record_count = record_count
        self.invocations: list[WriterJobInvocation] = []

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        self.invocations.append(invocation)
        binding = invocation.approval_bindings[0]
        records = tuple(
            EffectRecord(
                record_id=f"ledger_record_{index}",
                digest_sha256=f"{index}" * 64,
                approval=binding,
            )
            for index in range(1, self.record_count + 1)
        )
        return PreparedEffect(effect=ScheduledEffect.LEDGER_WRITE, records=records)


def _approved_review(suffix: str) -> tuple[ReviewAggregate, ApprovedIntentRecord]:
    proposal = ReviewProposal.create(
        capture_id=CaptureId("cap_" + suffix * 64),
        source_ref=f"https://example.invalid/{suffix}",
        privacy_tier=PrivacyTier.WORK,
        proposed_intent=Intent.IDEA,
        proposal_reason="Synthetic ledger proposal",
        capture_why="Synthetic owner ledger statement",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        created_by=Actor(kind=ActorKind.SYSTEM, label="synthetic-router"),
    )
    decided = ReviewAggregate.create(proposal).decide(
        ReviewDecisionCommand.create(
            decision_id=f"decision-{suffix}",
            target_state=ReviewState.APPLIED,
            reason="Synthetic owner approval",
            occurred_at=datetime(2026, 8, 13, 1, tzinfo=UTC),
            actor=Actor(kind=ActorKind.OWNER, label="owner"),
        )
    )
    assert decided.approved_record is not None
    return decided.aggregate, decided.approved_record


def test_job_010_uses_canonical_lease_and_replays_nightly_ledger(tmp_path: Path) -> None:
    aggregate, approved = _approved_review("a")
    journal = MemoryJournal()
    lease = RecordingLease()
    capability = DurableLedgerCapability(tmp_path)
    application = RecordingLedgerFlow()
    job = get_job("JOB-010")

    first = run_writer_job(
        job_id="JOB-010",
        root=tmp_path,
        replay_key="ledger-nightly-2026-08-13",
        approved_records=(approved,),
        review_reader=MemoryReviewReader((aggregate,)),
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )
    replay = run_writer_job(
        job_id="JOB-010",
        root=tmp_path,
        replay_key="ledger-nightly-2026-08-13",
        approved_records=(approved,),
        review_reader=MemoryReviewReader((aggregate,)),
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )

    assert job.state is JobState.ENABLED
    assert job.command == ("open-brain", "ledger", "run", "--nightly", "--json")
    assert job.deployment_target is DeploymentTarget.CANONICAL_WRITER
    assert job.host_role is HostRole.WRITER
    assert application.invocations[0].review_boundary is ReviewBoundary.APPROVED_INPUTS_ONLY
    assert application.invocations[0].apply_review_decisions is False
    assert first.disposition is JobRunDisposition.APPLIED
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert len(application.invocations) == 1
    assert capability.apply_calls == 1
    receipt = capability.recover("JOB-010", "ledger-nightly-2026-08-13")
    assert receipt is not None
    assert receipt.approval_bindings == (ApprovalBinding.from_record(approved),)
    assert lease.scopes == [LockScope.SHARED_WRITER, LockScope.SHARED_WRITER]


def test_job_010_recovers_effect_after_crash_before_journal_completion(tmp_path: Path) -> None:
    aggregate, approved = _approved_review("a")
    reader = MemoryReviewReader((aggregate,))
    journal = FailFirstCompletionJournal()
    lease = RecordingLease()
    capability = DurableLedgerCapability(tmp_path)
    application = RecordingLedgerFlow(record_count=1)

    with pytest.raises(RuntimeError, match="synthetic crash"):
        run_writer_job(
            job_id="JOB-010",
            root=tmp_path,
            replay_key="ledger-crash-window",
            approved_records=(approved,),
            review_reader=reader,
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    replay = run_writer_job(
        job_id="JOB-010",
        root=tmp_path,
        replay_key="ledger-crash-window",
        approved_records=(approved,),
        review_reader=reader,
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )

    assert replay.disposition is JobRunDisposition.REPLAYED
    assert (tmp_path / "ledger.jsonl").read_text().splitlines() == ["ledger_record_1"]
    assert len(application.invocations) == 1
    assert capability.apply_calls == 1


def test_job_010_recovers_reserved_effect_after_crash_inside_apply(tmp_path: Path) -> None:
    aggregate, approved = _approved_review("a")
    reader = MemoryReviewReader((aggregate,))
    journal = MemoryJournal()
    lease = RecordingLease()
    capability = CrashAfterEffectLedgerCapability(tmp_path)
    application = RecordingLedgerFlow(record_count=1)

    with pytest.raises(RuntimeError, match="crash inside effect apply"):
        run_writer_job(
            job_id="JOB-010",
            root=tmp_path,
            replay_key="ledger-crash-inside-apply",
            approved_records=(approved,),
            review_reader=reader,
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    receipt = capability.recover("JOB-010", "ledger-crash-inside-apply")
    assert receipt is not None
    replay = run_writer_job(
        job_id="JOB-010",
        root=tmp_path,
        replay_key="ledger-crash-inside-apply",
        approved_records=(approved,),
        review_reader=reader,
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )

    assert replay.disposition is JobRunDisposition.REPLAYED
    assert (tmp_path / "ledger.jsonl").read_text().splitlines() == ["ledger_record_1"]
    assert len(application.invocations) == 1
    assert capability.apply_calls == 1


def test_job_010_rejects_replay_digest_conflict_from_durable_receipt(tmp_path: Path) -> None:
    aggregate_a, approved_a = _approved_review("a")
    aggregate_b, approved_b = _approved_review("b")
    reader = MemoryReviewReader((aggregate_a, aggregate_b))
    journal = FailFirstCompletionJournal()
    lease = RecordingLease()
    capability = DurableLedgerCapability(tmp_path)
    application = RecordingLedgerFlow(record_count=1)

    with pytest.raises(RuntimeError, match="synthetic crash"):
        run_writer_job(
            job_id="JOB-010",
            root=tmp_path,
            replay_key="ledger-receipt-conflict",
            approved_records=(approved_a,),
            review_reader=reader,
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    with pytest.raises(WriterJobError, match="replay conflict"):
        run_writer_job(
            job_id="JOB-010",
            root=tmp_path,
            replay_key="ledger-receipt-conflict",
            approved_records=(approved_b,),
            review_reader=reader,
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    assert (tmp_path / "ledger.jsonl").read_text().splitlines() == ["ledger_record_1"]
    assert len(application.invocations) == 1
    assert capability.apply_calls == 1
