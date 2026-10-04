"""Whole-chain owner queue and completed capture replay, not backend authority."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from open_brain_engine.core.models import narrowest_tier

from .capture_recovery import CaptureRecoveryPlan
from .capture_replay import (
    _baseline_manifest,
    _preflight_captures,
    _require_completed_sources,
)
from .consent_contracts import EgressMode
from .contracts import CaptureCustodyReceipt, CaptureReceipt, CaptureSubmissionPath
from .custody_recovery import CaptureCustodyRecoveryPlan
from .custody_replay import _preflight, _rows
from .historical_recovery import require_historical_snapshot_settled
from .ingestion import _capture_receipt
from .journal_recovery import CaptureJournalRecoveryPlan
from .recovery_journal import RecoveryHead, RecoveryRecord, verify_recovery_chain
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine


def _operations(records: tuple[RecoveryRecord, ...]) -> tuple[
    tuple[CaptureCustodyRecoveryPlan, ...], tuple[CaptureRecoveryPlan, ...],
    tuple[CaptureJournalRecoveryPlan, ...],
]:
    cues: dict[str, CaptureCustodyRecoveryPlan] = {}
    captures: dict[str, CaptureRecoveryPlan] = {}
    journals: dict[str, CaptureJournalRecoveryPlan] = {}
    for record in records:
        if record.kind == "capture_custody":
            cue = CaptureCustodyRecoveryPlan.from_record(record)
            delivery = cue.receipt.delivery_id
            if delivery in cues:
                raise ValueError("duplicate original owner custody")
            cues[delivery] = cue
        elif record.kind == "capture":
            capture = CaptureRecoveryPlan.from_record(record)
            delivery = capture.envelope.submission.delivery_id
            if delivery not in cues or delivery in captures or (
                capture.envelope != cues[delivery].envelope
            ):
                raise ValueError("owner recovery allocation predecessor mismatch")
            if delivery in journals:
                # Journals without an allocation never precede one.
                raise ValueError("owner recovery allocation after journal history")
            captures[delivery] = capture
        elif record.kind == "capture_journal":
            journal = CaptureJournalRecoveryPlan.from_record(record)
            delivery = journal.custody.receipt.delivery_id
            if cues.get(delivery) != journal.custody or (
                journal.capture != captures.get(delivery)
            ):
                raise ValueError("owner recovery journal predecessor mismatch")
            previous = journals.get(delivery)
            if previous is not None and (
                previous.compacted or previous.tombstone is not None
                or journal.events[:len(previous.events)] != previous.events
                or len(journal.events) <= len(previous.events)
            ):
                raise ValueError("owner recovery journal history conflict")
            journals[delivery] = journal
        else:
            raise ValueError("unsupported owner recovery operation")
    # Every remaining shape is exact state restoration: an allocation without a
    # terminal journal resumes its bound stage machine and keeps its queue item
    # (the next drain writes the terminal event); a journal without an
    # allocation restores its exact non-terminal or discarded history. The
    # codec already binds accepted/duplicate terminals to their allocation.
    # Resuming an allocation the owner discarded would publish discarded
    # content, so that shape is refused before any write.
    for delivery, journal in journals.items():
        if journal.events[-1].event_kind == "discarded" and (
            journal.capture is not None or delivery in captures
        ):
            raise ValueError("owner recovery cannot replay a discarded allocation")
    # Authenticate the append chain unchanged; materialize a separate view in
    # original queue order, not callback completion order.
    return (
        tuple(sorted(cues.values(), key=lambda cue: cue.journal_sequence)),
        tuple(captures.values()), tuple(journals.values()),
    )


def _journal_preflight(
    connection: sqlite3.Connection, cues: tuple[CaptureCustodyRecoveryPlan, ...],
    journals: tuple[CaptureJournalRecoveryPlan, ...],
    captures: tuple[CaptureRecoveryPlan, ...] = (),
) -> tuple[CaptureCustodyRecoveryPlan, ...]:
    """``captures`` are the explicit plans the capture preflight matched exactly."""
    planned = {capture.envelope.submission.delivery_id for capture in captures}
    terminal = {plan.custody.receipt.delivery_id: plan for plan in journals}
    event_ids: dict[int, str] = {}
    item_ids: set[int] = set()
    ingestion_ids: set[str] = set()
    previous_item = previous_event = 0
    pending: list[CaptureCustodyRecoveryPlan] = []
    allocated: set[str] = set()
    for cue in cues:
        delivery = cue.receipt.delivery_id
        if cue.journal_sequence <= previous_item or cue.event_sequence <= previous_event:
            raise ValueError("owner recovery original custody order conflict")
        previous_item, previous_event = cue.journal_sequence, cue.event_sequence
        if cue.journal_sequence in item_ids or cue.receipt.ingestion_id in ingestion_ids:
            raise ValueError("owner recovery custody identity collision")
        item_ids.add(cue.journal_sequence)
        ingestion_ids.add(cue.receipt.ingestion_id)
        conflicts = connection.execute(
            "SELECT delivery_id FROM capture_ingestion_items "
            "WHERE journal_sequence=? OR ingestion_id=?",
            (cue.journal_sequence, cue.receipt.ingestion_id),
        ).fetchall()
        if any(row[0] != delivery for row in conflicts):
            raise ValueError("owner recovery retained custody collision")
        plan = terminal.get(delivery)
        events = (cue.event_sequence,) if plan is None else tuple(
            event.event_sequence for event in plan.events
        )
        for sequence in events:
            if sequence in event_ids:
                raise ValueError("owner recovery event identity collision")
            event_ids[sequence] = delivery
            row = connection.execute(
                "SELECT delivery_id FROM capture_ingestion_events WHERE event_sequence=?",
                (sequence,),
            ).fetchone()
            if row is not None and row[0] != delivery:
                raise ValueError("owner recovery retained event collision")
        item, event = _rows(cue)
        retained = connection.execute(
            "SELECT * FROM capture_ingestion_items WHERE delivery_id=?", (delivery,),
        ).fetchone()
        capture = connection.execute(
            "SELECT 1 FROM captures WHERE delivery_id=?", (delivery,),
        ).fetchone()
        if plan is None:
            if retained is None or capture is None:
                if retained is None and capture is not None and delivery in planned:
                    # Interrupted replay: the bound capture committed before
                    # its queue item. Its row was matched exactly by the
                    # capture preflight, so it is not a delivery collision.
                    allocated.add(delivery)
                pending.append(cue)
                continue
            # A restored pending allocation keeps its initial queue item beside
            # the resumed capture row; only the exact original rows may stand.
            body = connection.execute(
                "SELECT envelope_bytes FROM capture_ingestion_payloads WHERE delivery_id=?",
                (delivery,),
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM capture_ingestion_events WHERE delivery_id=?", (delivery,),
            ).fetchall()
            if dict(retained) != item or body is None or (
                body[0] != cue.envelope.to_bytes() or len(rows) != 1 or dict(rows[0]) != event
            ):
                raise ValueError("owner recovery retained allocation conflict")
            continue
        grave = connection.execute(
            "SELECT request_sha256,result_json,decided_at FROM capture_ingestion_tombstones "
            "WHERE delivery_id=?", (delivery,),
        ).fetchone()
        if grave is not None and (
            plan.tombstone is None or tuple(grave) != (
                plan.tombstone.request_sha256, plan.tombstone.result_json,
                plan.tombstone.decided_at,
            )
        ):
            raise ValueError("owner recovery retained tombstone conflict")
        if retained is None:
            if capture is None and grave is not None:
                # A compacted discard already restored; nothing remains to replay.
                pass
            elif capture is None:
                # Reuse existing high-water and original identity checks for
                # a missing terminal cue, even when final state is compacted.
                pending.append(cue)
            elif not plan.compacted and delivery in planned:
                # Interrupted replay of an allocated pending history.
                allocated.add(delivery)
                pending.append(cue)
            elif not plan.compacted:
                raise ValueError("owner recovery lost uncompacted journal")
            continue
        body = connection.execute(
            "SELECT envelope_bytes FROM capture_ingestion_payloads WHERE delivery_id=?",
            (delivery,),
        ).fetchone()
        original_events = [tuple(row) for row in connection.execute(
            "SELECT event_sequence,event_kind,attempt_number,receipt_json,recorded_at "
            "FROM capture_ingestion_events WHERE delivery_id=? ORDER BY event_sequence",
            (delivery,),
        )]
        expected = [(event.event_sequence, event.event_kind, event.attempt_number,
                     event.receipt_json, event.recorded_at) for event in plan.events]
        if dict(retained) != item or body is None or body[0] != cue.envelope.to_bytes() or (
            original_events != expected[:len(original_events)]
        ):
            raise ValueError("owner recovery retained journal conflict")
    # Only new cues reach this initial-only validator. Existing progressed
    # captures/journals were compared with their full exact plans above.
    return _preflight(connection, tuple(pending), allocated=frozenset(allocated))


def replay_owner_recovery_chain(
    engine: BrainEngine, records: tuple[RecoveryRecord, ...], *,
    expected_head: RecoveryHead, authority: EffectiveAuthority,
    dependency_payloads: tuple[bytes, ...] = (),
) -> tuple[CaptureCustodyReceipt | CaptureReceipt, ...]:
    """Recover initial queues and exact owner capture/journal histories.

    The caller authenticates latest head/baseline independently. All operations
    are preflighted together before existing capture stage machines run.
    Pending allocations, failed/quarantined/retried histories and discards are
    restored to their exact protected state; managed and control histories
    remain unsupported and refuse as a whole, never disappearing from the
    selected-source contract.
    """
    if type(authority) is not EffectiveAuthority or not authority.owner or (
        authority.egress_mode is not EgressMode.OWNER_LOCAL
        or authority.provider_id is not None
        or authority.principal_id != engine.profile.owner_actor_id
        or type(expected_head) is not RecoveryHead
    ):
        raise ValueError("owner recovery requires local owner")
    baseline = expected_head.baseline
    if authority.brain_id is not None and (
        authority.brain_id != baseline.brain_id or authority.issuer_epoch != baseline.issuer_epoch
    ):
        raise ValueError("owner recovery authority destination mismatch")
    limits = engine._admission_limits
    if type(records) is not tuple or any(type(record) is not RecoveryRecord for record in records):
        raise ValueError("invalid owner recovery records")
    if sum(len(record.to_bytes()) for record in records) > limits.max_journal_bytes:
        raise ValueError("owner recovery exceeds bounds")
    verify_recovery_chain(
        baseline, records, expected_head=expected_head, dependency_payloads=dependency_payloads,
    )
    cues, captures, journals = _operations(records)
    if len(cues) > limits.max_journal_items:
        raise ValueError("owner recovery exceeds item bounds")
    for cue in cues:
        if cue.envelope.submission.submission_path is not CaptureSubmissionPath.OWNER:
            raise ValueError("unsupported owner recovery submission")
        if len(cue.envelope.to_bytes()) > limits.max_journal_item_bytes:
            raise ValueError("owner recovery item exceeds bounds")
    with engine._writer_lease_bounded():
        engine._assert_root()
        if engine._validate_mutation_authority is not None:
            engine._validate_mutation_authority()
        paths = _baseline_manifest(engine, baseline)
        final_state = {
            plan.custody.receipt.delivery_id: plan.events[-1].event_kind for plan in journals
        }
        for cue in cues:
            # Quarantined or discarded originals are restored as-is; their
            # destination may no longer exist, so only the retained-privacy
            # boundary is rechecked. Everything else re-admits fully.
            if final_state.get(cue.receipt.delivery_id) in {"quarantined", "discarded"}:
                current_tier = engine._admitted_privacy(cue.envelope.submission).tier
            else:
                current_tier = engine._prepare_journal_submission(
                    cue.envelope.submission
                ).admitted_privacy.tier
            retained_tier = cue.envelope.admitted_privacy.tier
            if narrowest_tier(retained_tier, current_tier) != retained_tier:
                raise ValueError("owner recovery current privacy is narrower")
        with engine._store.connect() as connection:
            identity = connection.execute(
                "SELECT brain_id,issuer_epoch FROM brain_identity"
            ).fetchone()
            if identity is None or tuple(identity) != (baseline.brain_id, baseline.issuer_epoch):
                raise ValueError("owner recovery destination mismatch")
            require_historical_snapshot_settled(connection, engine.profile)
            _preflight_captures(connection, captures, paths)
            _require_completed_sources(engine, connection, captures)
            missing = _journal_preflight(connection, cues, journals, captures)
            count, size = connection.execute(
                "SELECT count(*),COALESCE(sum(byte_count),0) FROM capture_ingestion_items"
            ).fetchone()
            if count + len(missing) > limits.max_journal_items or (
                size + sum(len(cue.envelope.to_bytes()) for cue in missing)
                > limits.max_journal_bytes
            ):
                raise ValueError("owner recovery queue exceeds bounds")
        engine._refuse_on_storage_watermark()
        for capture in captures:
            engine._materialize_capture_locked(
                capture.envelope.submission, admitted_privacy=capture.envelope.admitted_privacy,
                recovery_plan=capture,
            )
        terminals = {plan.custody.receipt.delivery_id: plan for plan in journals}
        with engine._store.transaction() as connection:
            for cue in cues:
                delivery = cue.receipt.delivery_id
                plan = terminals.get(delivery)
                item, event = _rows(cue)
                compacted = plan is not None and plan.compacted
                present = connection.execute(
                    "SELECT 1 FROM capture_ingestion_items WHERE delivery_id=?", (delivery,),
                ).fetchone() is not None
                if cue in missing and not compacted:
                    connection.execute(
                        "INSERT INTO capture_ingestion_items VALUES(?,?,?,?,?,?,?,?)",
                        tuple(item.values()),
                    )
                    connection.execute(
                        "INSERT INTO capture_ingestion_payloads VALUES(?,?)",
                        (delivery, cue.envelope.to_bytes()),
                    )
                    connection.execute(
                        "INSERT INTO capture_ingestion_events VALUES(?,?,?,?,?,?)",
                        tuple(event.values()),
                    )
                    present = True
                # Terminal rows land before any deletion: the schema refuses to
                # drop custody that lacks a terminal event or tombstone.
                if plan is not None and present:
                    for event_row in plan.events[1:]:
                        connection.execute(
                            "INSERT OR IGNORE INTO capture_ingestion_events VALUES(?,?,?,?,?,?)",
                            (event_row.event_sequence, delivery, event_row.event_kind,
                             event_row.attempt_number, event_row.receipt_json,
                             event_row.recorded_at),
                        )
                if plan is not None and plan.tombstone is not None:
                    # Preflight proved any retained tombstone is identical.
                    connection.execute(
                        "INSERT OR IGNORE INTO capture_ingestion_tombstones VALUES(?,?,?,?)",
                        (delivery, plan.tombstone.request_sha256, plan.tombstone.result_json,
                         plan.tombstone.decided_at),
                    )
                if compacted:
                    connection.execute(
                        "DELETE FROM capture_ingestion_payloads WHERE delivery_id=?", (delivery,),
                    )
                    connection.execute(
                        "DELETE FROM capture_ingestion_items WHERE delivery_id=?", (delivery,),
                    )
            for table, high in (
                ("capture_ingestion_items", max((cue.journal_sequence for cue in cues), default=0)),
                ("capture_ingestion_events", max(max((
                    event.event_sequence for plan in journals for event in plan.events
                ), default=0), max((cue.event_sequence for cue in cues), default=0))),
            ):
                row = connection.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name=?", (table,),
                ).fetchone()
                if row is None:
                    connection.execute("INSERT INTO sqlite_sequence VALUES(?,?)", (table, high))
                elif row[0] < high:
                    connection.execute(
                        "UPDATE sqlite_sequence SET seq=? WHERE name=?", (high, table),
                    )
        result: list[CaptureCustodyReceipt | CaptureReceipt] = []
        for cue in cues:
            terminal = terminals.get(cue.receipt.delivery_id)
            receipt = None if terminal is None else _capture_receipt(
                terminal.events[-1].receipt_json
            )
            result.append(cue.receipt if receipt is None else receipt)
        return tuple(result)
