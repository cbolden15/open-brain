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


class CurationCapability(EffectCapability):
    effect = ScheduledEffect.CURATION_PROMOTION
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
        (self.root / "promoted.txt").write_text(
            "\n".join(record.record_id for record in command.prepared.records)
        )
        self.effects[receipt.effect_digest_sha256] = command.prepared

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        return self.effects.get(receipt.effect_digest_sha256)


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


class CurationApplication:
    def __init__(self) -> None:
        self.invocations: list[WriterJobInvocation] = []

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        self.invocations.append(invocation)
        return PreparedEffect(
            effect=ScheduledEffect.CURATION_PROMOTION,
            records=(
                EffectRecord(
                    "curated_record",
                    "a" * 64,
                    approval=invocation.approval_bindings[0],
                ),
            ),
            review_item_ids=("review_followup",),
        )


class MismatchedApprovalApplication:
    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        return PreparedEffect(
            effect=ScheduledEffect.CURATION_PROMOTION,
            records=(
                EffectRecord(
                    "curated_unapproved",
                    "b" * 64,
                    approval=ApprovalBinding(
                        record_id="intent_unapproved",
                        review_id="review_unapproved",
                        record_digest_sha256="c" * 64,
                    ),
                ),
            ),
        )


def _approved_review(suffix: str) -> tuple[ReviewAggregate, ApprovedIntentRecord]:
    proposal = ReviewProposal.create(
        capture_id=CaptureId("cap_" + suffix * 64),
        source_ref=f"https://example.invalid/{suffix}",
        privacy_tier=PrivacyTier.WORK,
        proposed_intent=Intent.ACTION_CANDIDATE,
        proposal_reason="Synthetic curation proposal",
        capture_why="Synthetic owner curation statement",
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


def test_job_012_promotes_only_approved_inputs_and_replays_journal(tmp_path: Path) -> None:
    aggregate, approved = _approved_review("a")
    journal = MemoryJournal()
    lease = RecordingLease()
    capability = CurationCapability(tmp_path)
    application = CurationApplication()

    first = run_writer_job(
        job_id="JOB-012",
        root=tmp_path,
        replay_key="curation-2026-08-13",
        approved_records=(approved,),
        review_reader=MemoryReviewReader((aggregate,)),
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )
    replay = run_writer_job(
        job_id="JOB-012",
        root=tmp_path,
        replay_key="curation-2026-08-13",
        approved_records=(approved,),
        review_reader=MemoryReviewReader((aggregate,)),
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )

    assert get_job("JOB-012").command == (
        "open-brain",
        "curation",
        "run",
        "--day=yesterday",
        "--json",
    )
    assert first.approved_inputs_applied == 1
    assert first.review_items_queued == 1
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert application.invocations[0].apply_review_decisions is False
    receipt = capability.recover("JOB-012", "curation-2026-08-13")
    assert receipt is not None
    assert receipt.approval_bindings == (ApprovalBinding.from_record(approved),)
    assert lease.scopes == [LockScope.SHARED_WRITER, LockScope.SHARED_WRITER]


def test_job_012_rejects_mismatched_approval_effect_binding_before_io(
    tmp_path: Path,
) -> None:
    aggregate, approved = _approved_review("a")
    capability = CurationCapability(tmp_path)

    with pytest.raises(WriterJobError, match="unapproved approval binding"):
        run_writer_job(
            job_id="JOB-012",
            root=tmp_path,
            replay_key="curation-mismatched-approval",
            approved_records=(approved,),
            review_reader=MemoryReviewReader((aggregate,)),
            journal=MemoryJournal(),
            application=MismatchedApprovalApplication(),
            effect_capability=capability,
            lease=RecordingLease(),
        )

    assert capability.apply_calls == 0
    assert not (tmp_path / "promoted.txt").exists()


def test_job_012_requires_exact_review_store_record_before_io(tmp_path: Path) -> None:
    _, approved = _approved_review("a")
    different_aggregate, _ = _approved_review("b")
    capability = CurationCapability(tmp_path)

    with pytest.raises(WriterJobError, match="review-store read-back"):
        run_writer_job(
            job_id="JOB-012",
            root=tmp_path,
            replay_key="curation-store-mismatch",
            approved_records=(approved,),
            review_reader=MemoryReviewReader((different_aggregate,)),
            journal=MemoryJournal(),
            application=CurationApplication(),
            effect_capability=capability,
            lease=RecordingLease(),
        )

    assert capability.apply_calls == 0
    assert not (tmp_path / "promoted.txt").exists()
