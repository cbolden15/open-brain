"""Owner-local initial custody replay, never a protection or authority grant."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from open_brain_engine.core.models import narrowest_tier

from .capture_replay import _baseline_manifest
from .consent_contracts import EgressMode
from .contracts import CaptureCustodyReceipt, CaptureSubmissionPath
from .custody_recovery import CaptureCustodyRecoveryPlan
from .ingestion import _receipt_json
from .recovery_journal import RecoveryHead, RecoveryRecord, verify_recovery_chain
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine


def _rows(plan: CaptureCustodyRecoveryPlan) -> tuple[dict[str, object], dict[str, object]]:
    submission = plan.envelope.submission
    return ({
        "journal_sequence": plan.journal_sequence,
        "ingestion_id": plan.receipt.ingestion_id,
        "delivery_id": submission.delivery_id,
        "request_sha256": submission.request_sha256(),
        "envelope_sha256": plan.envelope.sha256,
        "submission_path": submission.submission_path.value,
        "byte_count": len(plan.envelope.to_bytes()),
        "queued_at": plan.receipt.queued_at,
    }, {
        "event_sequence": plan.event_sequence,
        "delivery_id": submission.delivery_id,
        "event_kind": "queued",
        "attempt_number": 0,
        "receipt_json": _receipt_json(plan.receipt),
        "recorded_at": plan.recorded_at,
    })


def _preflight(
    connection: sqlite3.Connection, plans: tuple[CaptureCustodyRecoveryPlan, ...],
    *, allocated: frozenset[str] = frozenset(),
) -> tuple[CaptureCustodyRecoveryPlan, ...]:
    """``allocated`` deliveries already matched their capture row upstream."""
    item_high = max(
        connection.execute(
            "SELECT COALESCE(max(seq),0) FROM sqlite_sequence WHERE name='capture_ingestion_items'"
        ).fetchone()[0],
        connection.execute(
            "SELECT COALESCE(max(journal_sequence),0) FROM capture_ingestion_items"
        ).fetchone()[0],
    )
    event_high = max(
        connection.execute(
            "SELECT COALESCE(max(seq),0) FROM sqlite_sequence WHERE name='capture_ingestion_events'"
        ).fetchone()[0],
        connection.execute(
            "SELECT COALESCE(max(event_sequence),0) FROM capture_ingestion_events"
        ).fetchone()[0],
    )
    deliveries: set[str] = set()
    identities: set[str] = set()
    previous_item = previous_event = 0
    missing: list[CaptureCustodyRecoveryPlan] = []
    for plan in plans:
        delivery = plan.receipt.delivery_id
        if (
            delivery in deliveries or plan.receipt.ingestion_id in identities
            or plan.journal_sequence <= previous_item or plan.event_sequence <= previous_event
        ):
            raise ValueError("conflicting custody recovery order or identity")
        deliveries.add(delivery)
        identities.add(plan.receipt.ingestion_id)
        previous_item, previous_event = plan.journal_sequence, plan.event_sequence
        item, event = _rows(plan)
        if delivery in allocated:
            # The capture row was matched exactly upstream; its source rows must
            # still bind to this request, and owner captures never own intakes.
            alias = connection.execute(
                "SELECT evidence_sha256 FROM source_aliases WHERE delivery_id=?", (delivery,),
            ).fetchone()
            if alias is not None and alias[0] != plan.receipt.request_sha256:
                raise ValueError("custody recovery source binding collision")
        for table in (
            "captures", "capture_ingestion_tombstones", "source_aliases", "source_intakes"
        ):
            if table in {"captures", "source_aliases"} and delivery in allocated:
                continue
            if connection.execute(
                f"SELECT 1 FROM {table} WHERE delivery_id=?", (delivery,)
            ).fetchone() is not None:
                raise ValueError("custody recovery delivery collision")
        existing = connection.execute(
            "SELECT * FROM capture_ingestion_items WHERE delivery_id=?", (delivery,)
        ).fetchone()
        if existing is not None:
            payload = connection.execute(
                "SELECT envelope_bytes FROM capture_ingestion_payloads WHERE delivery_id=?",
                (delivery,),
            ).fetchone()
            events = connection.execute(
                "SELECT * FROM capture_ingestion_events WHERE delivery_id=?", (delivery,)
            ).fetchall()
            if (
                dict(existing) != item or payload is None
                or payload[0] != plan.envelope.to_bytes()
                or len(events) != 1 or dict(events[0]) != event
            ):
                raise ValueError("custody recovery conflicts with progressed custody")
            continue
        if plan.journal_sequence <= item_high or plan.event_sequence <= event_high:
            raise ValueError("custody recovery collides with retained order")
        if connection.execute(
            "SELECT 1 FROM capture_ingestion_items WHERE ingestion_id=? OR journal_sequence=?",
            (plan.receipt.ingestion_id, plan.journal_sequence),
        ).fetchone() is not None or connection.execute(
            "SELECT 1 FROM capture_ingestion_events WHERE event_sequence=?", (plan.event_sequence,)
        ).fetchone() is not None:
            raise ValueError("custody recovery identity collision")
        missing.append(plan)
    return tuple(missing)


def replay_owner_custody_chain(
    engine: BrainEngine, records: tuple[RecoveryRecord, ...], *,
    expected_head: RecoveryHead, authority: EffectiveAuthority,
    dependency_payloads: tuple[bytes, ...] = (),
) -> tuple[CaptureCustodyReceipt, ...]:
    """Atomically restore bounded original initial queues without draining them.

    The caller independently authenticates the latest head and baseline. This
    entrypoint supports only initial owner custody, not subsequent events or
    mixed mutation history. No remote/model grant, new identity or retargeting.
    """
    if (
        type(authority) is not EffectiveAuthority or not authority.owner
        or authority.egress_mode is not EgressMode.OWNER_LOCAL
        or authority.principal_id != engine.profile.owner_actor_id
        or type(expected_head) is not RecoveryHead
    ):
        raise ValueError("custody recovery requires local owner")
    baseline = expected_head.baseline
    if authority.brain_id is not None and (
        authority.brain_id != baseline.brain_id or authority.issuer_epoch != baseline.issuer_epoch
    ):
        raise ValueError("custody recovery authority destination mismatch")
    limits = engine._admission_limits
    if type(records) is not tuple or len(records) > limits.max_journal_items or (
        any(type(record) is not RecoveryRecord for record in records)
        or sum(len(record.payload) for record in records) > limits.max_journal_bytes
    ):
        raise ValueError("custody recovery request exceeds bounds")
    verify_recovery_chain(
        baseline, records, expected_head=expected_head, dependency_payloads=dependency_payloads
    )
    plans = tuple(CaptureCustodyRecoveryPlan.from_record(record) for record in records)
    if any(plan.envelope.submission.submission_path is not CaptureSubmissionPath.OWNER
           for plan in plans):
        raise ValueError("unsupported custody recovery operation")
    engine._assert_root()
    with engine._writer_lease_bounded():
        engine._assert_root()
        if engine._validate_mutation_authority is not None:
            engine._validate_mutation_authority()
        _baseline_manifest(engine, baseline)
        for plan in plans:
            current = engine._prepare_journal_submission(plan.envelope.submission)
            retained = plan.envelope.admitted_privacy
            if narrowest_tier(retained.tier, current.admitted_privacy.tier) != retained.tier:
                raise ValueError("custody recovery requires narrower current privacy")
            if len(plan.envelope.to_bytes()) > limits.max_journal_item_bytes:
                raise ValueError("custody recovery item exceeds bounds")
        with engine._store.transaction() as connection:
            identity = connection.execute(
                "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
            ).fetchone()
            if identity is None or tuple(identity) != (baseline.brain_id, baseline.issuer_epoch):
                raise ValueError("custody recovery destination mismatch")
            missing = _preflight(connection, plans)
            count, size = connection.execute(
                "SELECT count(*),COALESCE(sum(byte_count),0) FROM capture_ingestion_items"
            ).fetchone()
            if count + len(missing) > limits.max_journal_items or (
                size + sum(len(plan.envelope.to_bytes()) for plan in missing)
                > limits.max_journal_bytes
            ):
                raise ValueError("custody recovery queue exceeds bounds")
            if missing:
                engine._refuse_on_storage_watermark()
            for plan in missing:
                item, event = _rows(plan)
                # Closed typed operation, fixed schema columns and original
                # values only. SQLite advances both high-water marks normally.
                connection.execute(
                    "INSERT INTO capture_ingestion_items "
                    "(journal_sequence,ingestion_id,delivery_id,request_sha256,envelope_sha256,"
                    "submission_path,byte_count,queued_at) VALUES(?,?,?,?,?,?,?,?)",
                    tuple(item.values()),
                )
                connection.execute(
                    "INSERT INTO capture_ingestion_payloads VALUES(?,?)",
                    (plan.receipt.delivery_id, plan.envelope.to_bytes()),
                )
                connection.execute(
                    "INSERT INTO capture_ingestion_events "
                    "(event_sequence,delivery_id,event_kind,attempt_number,"
                    "receipt_json,recorded_at) "
                    "VALUES(?,?,?,?,?,?)", tuple(event.values()),
                )
    return tuple(plan.receipt for plan in plans)
