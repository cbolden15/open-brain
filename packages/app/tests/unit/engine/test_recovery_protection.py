"""Synthetic port proves guard ordering, not independent backend durability."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine, CaptureReceipt, CaptureSubmission, TextPayload
from open_brain_engine.engine.custody_recovery import CaptureCustodyRecoveryPlan
from open_brain_engine.engine.journal_recovery import CaptureJournalRecoveryPlan
from open_brain_engine.engine.recovery_closure import RecoveryClosure, RecoveryClosureBounds
from open_brain_engine.engine.recovery_journal import RecoveryBaseline, RecoveryHead, RecoveryRecord
from open_brain_engine.engine.recovery_protection import (
    RecoveryPlan,
    RecoveryProtectionEvidence,
    RecoveryProtectionGuard,
    RecoveryProtectionPendingError,
    operation_sha256,
)
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local


class SyntheticPort:
    def __init__(self, baseline: RecoveryBaseline, files: tuple[tuple[str, bytes], ...]) -> None:
        self.baseline = baseline
        self.files = files
        self.records: list[RecoveryRecord] = []
        self.plans: dict[str, RecoveryPlan] = {}
        self.fail_kind: str | None = None
        self.fail_stage: int | None = None
        self.failure = "timeout"
        self.engine: BrainEngine | None = None
        self.stages: list[tuple[str, int | None]] = []

    def _evidence(self, plan: RecoveryPlan) -> RecoveryProtectionEvidence:
        records = tuple(self.records)
        return RecoveryProtectionEvidence(
            RecoveryClosure(
                bounds=RecoveryClosureBounds(4096, 16 * 1024 * 1024, 32, 16 * 1024 * 1024),
                baseline_files=self.files, records=records,
                expected_head=RecoveryHead(
                    self.baseline, len(records),
                    records[-1].record_sha256 if records else self.baseline.artifact_sha256,
                ),
            ), operation_sha256(plan), "synthetic-proof",
        )

    def protect(self, plan: RecoveryPlan, *, timeout_seconds: float) -> RecoveryProtectionEvidence:
        assert timeout_seconds == 5.0
        kind = "capture_journal" if isinstance(plan, CaptureJournalRecoveryPlan) else (
            "capture" if hasattr(plan, "identities") else "capture_custody"
        )
        if self.engine is not None:
            with self.engine._store.connect() as connection:
                row = connection.execute(
                    "SELECT stage FROM captures WHERE delivery_id=?",
                    (plan.envelope.submission.delivery_id,),
                ).fetchone()
                self.stages.append((kind, None if row is None else row[0]))
        failing = kind == self.fail_kind and (
            self.fail_stage is None or self.stages[-1][1] == self.fail_stage
        )
        if failing and self.failure == "timeout":
            raise TimeoutError()
        digest = operation_sha256(plan)
        if digest not in self.plans:
            self.records.append(RecoveryRecord(
                baseline=self.baseline, sequence=len(self.records) + 1,
                previous_sha256=self.records[-1].record_sha256 if self.records
                else self.baseline.artifact_sha256,
                kind=kind, payload=plan.to_bytes(),
            ))
            self.plans[digest] = plan
        evidence = self._evidence(plan)
        if failing:
            if self.failure == "wrong":
                return replace(evidence, operation_sha256="0" * 64)
            return replace(evidence, closure=replace(
                evidence.closure, records=(),
                expected_head=RecoveryHead(self.baseline, 0, self.baseline.artifact_sha256),
            ))
        return evidence

    def lookup(self, delivery_id: str, *, timeout_seconds: float) -> RecoveryProtectionEvidence:
        assert timeout_seconds == 5.0
        plan = next(plan for plan in self.plans.values()
                    if hasattr(plan, "identities")
                    and plan.envelope.submission.delivery_id == delivery_id)
        return self._evidence(plan)


@pytest.fixture
def guarded(tmp_path: Path) -> tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard]:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    archive = tmp_path / "baseline"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    files = tuple(sorted(validated_portable_snapshot(archive).files.items()))
    with engine._store.connect() as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    baseline = RecoveryBaseline(
        identity[0], identity[1], sha256(dict(files)["portable-manifest.json"]).hexdigest()
    )
    port = SyntheticPort(baseline, files)
    guard = RecoveryProtectionGuard(baseline, port)
    engine = BrainEngine.open(profile, recovery_protection_guard=guard)
    port.engine = engine
    return engine, port, guard


def test_guard_protects_original_cue_and_allocation_before_release(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    engine, port, _ = guarded
    receipt = engine.capture.accept(TextPayload("Synthetic owner"), delivery_id="owner.one")
    assert isinstance(receipt, CaptureReceipt)
    assert [record.kind for record in port.records] == [
        "capture_custody", "capture", "capture_journal",
    ]
    terminal = CaptureJournalRecoveryPlan.from_record(port.records[-1])
    assert [event.event_kind for event in terminal.events] == ["queued", "accepted"]
    assert terminal.compacted and terminal.capture is not None
    assert terminal.capture.identities.capture_id == receipt.capture_id
    assert port.stages[0] == ("capture_custody", None)
    assert next(stage for stage in port.stages if stage[0] == "capture") == ("capture", 0)
    assert port.stages[-1] == ("capture_journal", 3)
    with engine._store.connect() as connection:
        before = tuple(connection.execute("SELECT * FROM captures").fetchone())
        assert connection.execute(
            "SELECT count(*) FROM capture_ingestion_payloads"
        ).fetchone()[0] == 0
    duplicate = engine.capture.accept(TextPayload("Synthetic owner"), delivery_id="owner.one")
    assert isinstance(duplicate, CaptureReceipt) and duplicate.capture_id == receipt.capture_id
    assert duplicate.duplicate and len(port.records) == 3
    with engine._store.connect() as connection:
        assert tuple(connection.execute("SELECT * FROM captures").fetchone()) == before


@pytest.mark.parametrize("kind", ["capture_custody", "capture"])
@pytest.mark.parametrize("failure", ["timeout", "wrong", "missing"])
def test_failed_evidence_retains_body_and_retries_original_identity(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
    kind: str, failure: str,
) -> None:
    engine, port, guard = guarded
    port.fail_kind, port.failure = kind, failure
    with pytest.raises(RecoveryProtectionPendingError):
        engine.capture.accept(TextPayload("Synthetic retained"), delivery_id="owner.retry")
    with engine._store.connect() as connection:
        cue = tuple(connection.execute("SELECT * FROM capture_ingestion_items").fetchone())
        raw = connection.execute(
            "SELECT envelope_bytes FROM capture_ingestion_payloads"
        ).fetchone()[0]
        row = connection.execute("SELECT * FROM captures").fetchone()
        allocation = None if row is None else (row["capture_id"], row["accepted_receipt_id"])
        assert row is None or row["stage"] == 0
        assert connection.execute(
            "SELECT count(*) FROM capture_ingestion_events"
        ).fetchone()[0] == 1
    port.fail_kind = None
    reopened = BrainEngine.open(engine.profile, recovery_protection_guard=guard)
    port.engine = reopened
    receipt = reopened.capture.accept(TextPayload("Synthetic retained"), delivery_id="owner.retry")
    assert isinstance(receipt, CaptureReceipt)
    if allocation is not None:
        assert receipt.capture_id == allocation[0]
        with reopened._store.connect() as connection:
            assert connection.execute(
                "SELECT accepted_receipt_id FROM captures WHERE delivery_id='owner.retry'"
            ).fetchone()[0] == allocation[1]
    custody_plans = [plan for plan in port.plans.values()
                    if isinstance(plan, CaptureCustodyRecoveryPlan)]
    assert len(custody_plans) == 1
    assert custody_plans[0].receipt.ingestion_id == cue[1]
    assert custody_plans[0].envelope.to_bytes() == raw


def test_compacted_duplicate_cannot_change_unhashed_metadata(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    engine, _, guard = guarded
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=TextPayload("Synthetic original"),
        delivery_id="owner.original",
    )
    receipt = engine.capture.submit(submission)
    assert isinstance(receipt, CaptureReceipt)
    altered = replace(
        submission, source_reference="synthetic:changed",
        provenance=replace(submission.provenance, source_ref="synthetic:changed"),
    )
    assert altered.request_sha256() == submission.request_sha256()
    with pytest.raises(ValueError, match="does not match the local profile"):
        engine.capture.submit(altered)
    with pytest.raises(RecoveryProtectionPendingError, match="replay mismatch"):
        guard.validate_duplicate(engine, altered, receipt)


def test_terminal_failure_retains_original_body_until_recovered(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    engine, port, guard = guarded
    port.fail_kind, port.fail_stage = "capture", 3
    with pytest.raises(RecoveryProtectionPendingError):
        engine.capture.accept(TextPayload("Synthetic terminal"), delivery_id="owner.terminal")
    with engine._store.connect() as connection:
        row = connection.execute("SELECT capture_id,stage FROM captures").fetchone()
        assert row[1] == 3
        assert connection.execute(
            "SELECT count(*) FROM capture_ingestion_payloads"
        ).fetchone()[0] == 1
    port.fail_kind = None
    reopened = BrainEngine.open(engine.profile, recovery_protection_guard=guard)
    port.engine = reopened
    receipt = reopened.capture.accept(
        TextPayload("Synthetic terminal"), delivery_id="owner.terminal"
    )
    assert isinstance(receipt, CaptureReceipt) and receipt.capture_id == row[0]
    assert len(port.records) == 3
    with reopened._store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM capture_ingestion_payloads"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["timeout", "wrong", "missing"])
def test_terminal_journal_proof_failure_retains_exact_events_before_compaction(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], failure: str,
) -> None:
    engine, port, guard = guarded
    port.fail_kind, port.failure = "capture_journal", failure
    with pytest.raises(RecoveryProtectionPendingError):
        engine.capture.accept(TextPayload("Synthetic original events"), delivery_id="owner.events")
    with engine._store.connect() as connection:
        original_events = tuple(tuple(row) for row in connection.execute(
            "SELECT * FROM capture_ingestion_events ORDER BY event_sequence"
        ))
        original_capture = tuple(connection.execute("SELECT * FROM captures").fetchone())
        original_body = connection.execute(
            "SELECT envelope_bytes FROM capture_ingestion_payloads"
        ).fetchone()[0]
        assert len(original_events) == 2
    port.fail_kind = None
    reopened = BrainEngine.open(engine.profile, recovery_protection_guard=guard)
    port.engine = reopened
    proof = port.lookup("owner.events", timeout_seconds=5.0)
    journal = CaptureJournalRecoveryPlan.from_record(proof.closure.records[-1])
    assert tuple((event.event_sequence, "owner.events", event.event_kind,
                  event.attempt_number, event.receipt_json, event.recorded_at)
                 for event in journal.events) == original_events
    assert journal.envelope.to_bytes() == original_body
    with reopened._store.connect() as connection:
        assert tuple(connection.execute("SELECT * FROM captures").fetchone()) == original_capture
        assert connection.execute(
            "SELECT count(*) FROM capture_ingestion_payloads"
        ).fetchone()[0] == 0


def test_non_owner_ingress_refuses_before_enqueue(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission

    engine, _, _ = guarded
    with pytest.raises(ValueError, match="unsupported recovery-protected"):
        engine.capture.submit(_public_submission(engine.tasks))
    with engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM capture_ingestion_items").fetchone()[0] == 0


def test_stale_guard_refuses_before_open_writes(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
) -> None:
    engine, _, guard = guarded
    before = {path.relative_to(engine.profile.root): path.read_bytes()
              for path in engine.profile.root.rglob("*") if path.is_file()}
    stale = replace(guard, baseline=replace(guard.baseline, issuer_epoch=2))
    with pytest.raises(RecoveryProtectionPendingError, match="destination mismatch"):
        BrainEngine.open(engine.profile, recovery_protection_guard=stale)
    assert {path.relative_to(engine.profile.root): path.read_bytes()
            for path in engine.profile.root.rglob("*") if path.is_file()} == before
