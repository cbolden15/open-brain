"""Actual protected mixed owner closures recover without primary or sender."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureCustodyReceipt,
    CaptureFault,
    CaptureReceipt,
    CaptureSubmission,
    FilePayload,
    InjectedFault,
    TextPayload,
)
from open_brain_engine.engine.capture_recovery import CaptureRecoveryPlan
from open_brain_engine.engine.custody_recovery import (
    CaptureCustodyRecoveryPlan,
    emit_owner_custody_plan,
)
from open_brain_engine.engine.journal_recovery import CaptureJournalRecoveryPlan
from open_brain_engine.engine.owner_replay import replay_owner_recovery_chain
from open_brain_engine.engine.portability import restore_portable_clean
from open_brain_engine.engine.recovery_journal import RecoveryHead
from open_brain_engine.engine.recovery_protection import (
    RecoveryPlan,
    RecoveryProtectionEvidence,
    RecoveryProtectionGuard,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_custody_replay import _snapshot
from packages.app.tests.unit.engine.test_recovery_protection import (
    SyntheticPort,
)
from packages.app.tests.unit.engine.test_recovery_protection import (
    guarded as guarded,
)


def _restore(primary: BrainEngine, tmp_path: Path) -> tuple[BrainEngine, EffectiveAuthority]:
    owner = EffectiveAuthority(
        primary.profile.owner_actor_id, "recovery", frozenset(), None, owner=True,
    )
    primary.profile.root.rename(tmp_path / "unavailable-primary")
    target = tmp_path / "restored"
    restore_portable_clean(
        tmp_path / "baseline", target, import_id="import_" + str(uuid4()), authority=owner,
    )
    return BrainEngine.open(compile_single_user_local(target)), owner


@pytest.mark.parametrize("fault", [None, CaptureFault.AFTER_CAPTURE_RESERVATION,
                                  CaptureFault.AFTER_SOURCE_WRITE])
def test_actual_mixed_closure_restores_original_receipt_and_watermarks(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
    fault: CaptureFault | None,
) -> None:
    primary, port, _ = guarded
    originals = []
    for index in range(2):
        receipt = primary.capture.accept(
            TextPayload(f"Synthetic original {index}"), delivery_id=f"mixed.original.{index}"
        )
        assert isinstance(receipt, CaptureReceipt)
        originals.append(receipt)
    closure = port.lookup("mixed.original.1", timeout_seconds=5.0).closure
    with primary._store.connect() as connection:
        original_captures = tuple(tuple(row) for row in connection.execute(
            "SELECT * FROM captures ORDER BY delivery_id"
        ))
        original_sequences = tuple(tuple(row) for row in connection.execute(
            "SELECT name,seq FROM sqlite_sequence ORDER BY name"
        ))
    restored, owner = _restore(primary, tmp_path)
    if fault is not None:
        restored._faults.add(fault)
        with pytest.raises(InjectedFault):
            replay_owner_recovery_chain(
                restored, closure.records, expected_head=closure.expected_head, authority=owner,
            )
        restored = BrainEngine.open(restored.profile)
    assert replay_owner_recovery_chain(
        restored, closure.records, expected_head=closure.expected_head, authority=owner,
    ) == tuple(originals)
    with restored._store.connect() as connection:
        assert tuple(tuple(row) for row in connection.execute(
            "SELECT * FROM captures ORDER BY delivery_id"
        )) == original_captures
        assert tuple(tuple(row) for row in connection.execute(
            "SELECT name,seq FROM sqlite_sequence ORDER BY name"
        )) == original_sequences
    before = _snapshot(restored)
    assert replay_owner_recovery_chain(
        restored, closure.records, expected_head=closure.expected_head, authority=owner,
    ) == tuple(originals)
    assert _snapshot(restored) == before
    reopened = BrainEngine.open(restored.profile)
    assert _snapshot(reopened) == before
    for index, receipt in enumerate(originals):
        duplicate = reopened.capture.accept(
            TextPayload(f"Synthetic original {index}"), delivery_id=f"mixed.original.{index}"
        )
        assert isinstance(duplicate, CaptureReceipt)
        assert duplicate.duplicate and duplicate.capture_id == receipt.capture_id


def test_original_queue_order_can_differ_from_protection_callback_order(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary, port, _ = guarded
    first = CaptureSubmission.for_local_owner(
        profile=primary.profile, payload=TextPayload("Synthetic first cue"),
        delivery_id="mixed.queue.first",
    )
    second = CaptureSubmission.for_local_owner(
        profile=primary.profile, payload=TextPayload("Synthetic second cue"),
        delivery_id="mixed.queue.second",
    )
    original = port.protect
    delayed = True

    def protect(plan: RecoveryPlan, *, timeout_seconds: float) -> RecoveryProtectionEvidence:
        nonlocal delayed
        if delayed:
            delayed = False
            primary.ingestion.enqueue(second)
        return original(plan, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(port, "protect", protect)
    cue = primary.ingestion.enqueue(first)
    assert [plan.envelope.submission.delivery_id for plan in port.plans.values()] == [
        second.delivery_id, first.delivery_id,
    ]
    closure = port._evidence(next(iter(port.plans.values()))).closure
    restored, owner = _restore(primary, tmp_path)
    result = replay_owner_recovery_chain(
        restored, closure.records, expected_head=closure.expected_head, authority=owner,
    )
    assert result[0] == cue
    assert [receipt.delivery_id for receipt in result] == [first.delivery_id, second.delivery_id]


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_repeated_replay_refuses_damaged_completed_source_before_any_write(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
    damage: str,
) -> None:
    primary, port, _ = guarded
    primary.capture.accept(TextPayload("Synthetic original record"), delivery_id="mixed.file")
    closure = port.lookup("mixed.file", timeout_seconds=5.0).closure
    restored, owner = _restore(primary, tmp_path)
    replay_owner_recovery_chain(
        restored, closure.records, expected_head=closure.expected_head, authority=owner,
    )
    with restored._store.connect() as connection:
        relative = connection.execute("SELECT source_path FROM captures").fetchone()[0]
    path = restored.profile.root / relative
    raw = path.read_bytes()
    assert b"Synthetic original record" in raw
    if damage == "changed":
        path.write_bytes(raw.replace(b"Synthetic original record", b"Altered synthetic record!"))
    else:
        path.unlink()
    before = _snapshot(restored)
    with pytest.raises(ValueError):
        replay_owner_recovery_chain(
            restored, closure.records, expected_head=closure.expected_head, authority=owner,
        )
    assert _snapshot(restored) == before
    if damage == "changed":
        assert path.read_bytes() == raw.replace(
            b"Synthetic original record", b"Altered synthetic record!",
        )
    else:
        assert not path.exists()


@pytest.mark.parametrize("data", [b"", b"Synthetic original bytes"])
@pytest.mark.parametrize("damage", [None, "changed", "missing"])
def test_completed_file_replay_requires_original_blob(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
    data: bytes, damage: str | None,
) -> None:
    primary, port, _ = guarded
    payload = FilePayload("synthetic.bin", "application/octet-stream", data)
    original = primary.capture.accept(payload, delivery_id="mixed.blob")
    closure = port.lookup("mixed.blob", timeout_seconds=5.0).closure
    restored, owner = _restore(primary, tmp_path)
    assert replay_owner_recovery_chain(
        restored, closure.records, expected_head=closure.expected_head, authority=owner,
    ) == (original,)
    blob = restored.profile.root / f"sources/blobs/sha256/{payload.digest[:2]}/{payload.digest}"
    assert blob.read_bytes() == data
    if damage == "changed":
        blob.write_bytes(b"!" if not data else b"!" * len(data))
    elif damage == "missing":
        blob.unlink()
    before = _snapshot(restored)
    if damage is None:
        assert replay_owner_recovery_chain(
            restored, closure.records, expected_head=closure.expected_head, authority=owner,
        ) == (original,)
    else:
        with pytest.raises(ValueError):
            replay_owner_recovery_chain(
                restored, closure.records, expected_head=closure.expected_head, authority=owner,
            )
    assert _snapshot(restored) == before


def test_mixed_replay_retains_unallocated_original_queue_beside_compacted_captures(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard], tmp_path: Path,
) -> None:
    primary, port, _ = guarded
    original = primary.capture.accept(TextPayload("Synthetic terminal"), delivery_id="mixed.done")
    submission = CaptureSubmission.for_local_owner(
        profile=primary.profile, payload=TextPayload("Synthetic still queued"),
        delivery_id="mixed.queued",
    )
    cue = primary.ingestion.enqueue(submission)
    assert isinstance(cue, CaptureCustodyReceipt)
    closure = port.lookup("mixed.done", timeout_seconds=5.0).closure
    restored, owner = _restore(primary, tmp_path)
    assert replay_owner_recovery_chain(
        restored, closure.records, expected_head=closure.expected_head, authority=owner,
    ) == (original, cue)
    retained = emit_owner_custody_plan(
        restored, submission, baseline=closure.expected_head.baseline, authority=owner,
    )
    assert retained.receipt == cue
    before = _snapshot(restored)
    assert replay_owner_recovery_chain(
        restored, closure.records, expected_head=closure.expected_head, authority=owner,
    ) == (original, cue)
    assert _snapshot(restored) == before


@pytest.mark.parametrize("damage", [
    "prefix", "order", "later-kind", "nonowner", "privacy",
    "item-identity", "event-identity", "receipt-identity",
])
def test_malformed_later_mixed_history_refuses_before_any_write(
    guarded: tuple[BrainEngine, SyntheticPort, RecoveryProtectionGuard],
    tmp_path: Path, damage: str,
) -> None:
    primary, port, _ = guarded
    primary.capture.accept(TextPayload("Synthetic first"), delivery_id="mixed.first")
    primary.capture.accept(TextPayload("Synthetic later"), delivery_id="mixed.later")
    assert isinstance(port, SyntheticPort)
    closure = port.lookup("mixed.later", timeout_seconds=5.0).closure
    restored, owner = _restore(primary, tmp_path)
    records, head = closure.records, closure.expected_head
    if damage == "prefix":
        records = records[:-1]
    elif damage in {
        "order", "later-kind", "item-identity", "event-identity", "receipt-identity",
    }:
        changed = list(records)
        if damage == "order":
            changed[0], changed[1] = changed[1], changed[0]
        elif damage == "later-kind":
            changed[-1] = replace(changed[-1], kind="control")
        elif damage == "receipt-identity":
            first_capture = CaptureRecoveryPlan.from_record(changed[1])
            capture = CaptureRecoveryPlan.from_record(changed[4])
            capture = replace(capture, identities=replace(
                capture.identities,
                accepted_receipt_id=first_capture.identities.accepted_receipt_id,
            ))
            journal = replace(CaptureJournalRecoveryPlan.from_record(changed[5]), capture=capture)
            changed[4] = replace(changed[4], payload=capture.to_bytes())
            changed[5] = replace(changed[5], payload=journal.to_bytes())
        else:
            first_cue = CaptureCustodyRecoveryPlan.from_record(changed[0])
            cue = CaptureCustodyRecoveryPlan.from_record(changed[3])
            cue = replace(cue, journal_sequence=first_cue.journal_sequence) if (
                damage == "item-identity"
            ) else replace(cue, event_sequence=first_cue.event_sequence)
            journal = CaptureJournalRecoveryPlan.from_record(changed[5])
            events = journal.events if damage == "item-identity" else (
                replace(journal.events[0], event_sequence=cue.event_sequence), *journal.events[1:],
            )
            journal = replace(journal, custody=cue, events=events)
            changed[3] = replace(changed[3], payload=cue.to_bytes())
            changed[5] = replace(changed[5], payload=journal.to_bytes())
        predecessor = head.baseline.artifact_sha256
        records_list = []
        for index, record in enumerate(changed, 1):
            bound = replace(record, sequence=index, previous_sha256=predecessor)
            records_list.append(bound)
            predecessor = bound.record_sha256
        records = tuple(records_list)
        head = RecoveryHead(head.baseline, len(records), records[-1].record_sha256)
    elif damage == "nonowner":
        owner = replace(owner, owner=False)
    else:
        from open_brain_engine.core.models import PrivacyTier

        restored._boundary_classifier = lambda submission: (
            PrivacyTier.SECRET if submission.delivery_id == "mixed.later" else None
        )
    before = _snapshot(restored)
    # The exclusive writer lease records each acquisition time. Its metadata
    # is not retained knowledge or recovery state; all other files are checked.
    files_before = {path.relative_to(restored.profile.root): path.read_bytes()
                    for path in restored.profile.root.rglob("*")
                    if path.is_file() and "phase1.sqlite3" not in path.name
                    and ".open-brain-locks" not in path.parts}
    with pytest.raises(ValueError):
        replay_owner_recovery_chain(restored, records, expected_head=head, authority=owner)
    assert _snapshot(restored) == before
    assert {path.relative_to(restored.profile.root): path.read_bytes()
            for path in restored.profile.root.rglob("*")
            if path.is_file() and "phase1.sqlite3" not in path.name
            and ".open-brain-locks" not in path.parts} == files_before
