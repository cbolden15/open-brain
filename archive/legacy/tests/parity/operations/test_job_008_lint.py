from pathlib import Path

import pytest

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.writer_jobs import (
    EffectCapability,
    EffectCommand,
    EffectReceipt,
    EffectRecord,
    JobRunResult,
    PreparedEffect,
    ReplayJournal,
    ScheduledEffect,
    WriterJobError,
    WriterJobInvocation,
    get_writer_job_spec,
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


class MemoryEffectCapability(EffectCapability):
    effect = ScheduledEffect.DIAGNOSTICS
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
        self.effects[receipt.effect_digest_sha256] = command.prepared

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        return self.effects.get(receipt.effect_digest_sha256)


class FixedApplication:
    def __init__(
        self,
        effect: ScheduledEffect,
        *,
        review_item_ids: tuple[str, ...] = (),
    ) -> None:
        self.effect = effect
        self.review_item_ids = review_item_ids
        self.invocations: list[WriterJobInvocation] = []

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        self.invocations.append(invocation)
        return PreparedEffect(
            effect=self.effect,
            records=(
                EffectRecord("diagnostic_1", "a" * 64),
                EffectRecord("diagnostic_2", "b" * 64),
                EffectRecord("diagnostic_3", "c" * 64),
            ),
            review_item_ids=self.review_item_ids,
        )


class ForbiddenContentWrite:
    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        (invocation.root / "content.md").write_text("synthetic forbidden content")
        return PreparedEffect(effect=ScheduledEffect.DIAGNOSTICS)


def test_job_008_allows_diagnostics_and_rejects_content_effects(tmp_path: Path) -> None:
    journal = MemoryJournal()
    capability = MemoryEffectCapability(tmp_path)
    diagnostics = FixedApplication(ScheduledEffect.DIAGNOSTICS)

    result = run_writer_job(
        job_id="JOB-008",
        root=tmp_path,
        replay_key="lint-2026-w33",
        journal=journal,
        application=diagnostics,
        effect_capability=capability,
    )

    assert get_writer_job_spec("JOB-008").command == get_job("JOB-008").command
    assert result.effect is ScheduledEffect.DIAGNOSTICS
    assert diagnostics.invocations[0].approved_records == ()

    with pytest.raises(WriterJobError, match="invalid effect"):
        run_writer_job(
            job_id="JOB-008",
            root=tmp_path,
            replay_key="lint-content-attempt",
            journal=journal,
            application=FixedApplication(ScheduledEffect.CURATION_PROMOTION),
            effect_capability=capability,
        )

    with pytest.raises(WriterJobError, match="diagnostics-only"):
        run_writer_job(
            job_id="JOB-008",
            root=tmp_path,
            replay_key="lint-queue-attempt",
            journal=journal,
            application=FixedApplication(
                ScheduledEffect.DIAGNOSTICS,
                review_item_ids=("review_attempt",),
            ),
            effect_capability=capability,
        )


def test_job_008_forbidden_content_write_fails_before_io(tmp_path: Path) -> None:
    capability = MemoryEffectCapability(tmp_path)

    with pytest.raises(WriterJobError, match="I/O capability"):
        run_writer_job(
            job_id="JOB-008",
            root=tmp_path,
            replay_key="lint-real-content-attempt",
            journal=MemoryJournal(),
            application=ForbiddenContentWrite(),
            effect_capability=capability,
        )

    assert capability.apply_calls == 0
    assert not (tmp_path / "content.md").exists()
