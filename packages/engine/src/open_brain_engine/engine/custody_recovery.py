"""Original unreserved custody plans, not replay permission or durable protection."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .capture_recovery import MAX_CAPTURE_RECOVERY_BYTES, _allocated_id
from .consent_contracts import EgressMode
from .contracts import (
    CaptureCustodyReceipt,
    CaptureSubmission,
    CaptureSubmissionPath,
    JournalEnvelope,
    verify_capture_custody_receipt,
)
from .normalization import _timestamp
from .recovery_journal import RecoveryBaseline, RecoveryRecord
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine


@dataclass(frozen=True, slots=True)
class CaptureCustodyRecoveryPlan:
    baseline: RecoveryBaseline
    envelope: JournalEnvelope
    receipt: CaptureCustodyReceipt
    journal_sequence: int
    event_sequence: int
    recorded_at: str

    def __post_init__(self) -> None:
        if (
            type(self.baseline) is not RecoveryBaseline
            or type(self.envelope) is not JournalEnvelope
            or type(self.receipt) is not CaptureCustodyReceipt
        ):
            raise ValueError("invalid custody recovery plan")
        receipt = self.receipt
        submission = self.envelope.submission
        _allocated_id(receipt.ingestion_id, "ingestion")
        if (
            receipt.brain_id != self.baseline.brain_id
            or receipt.issuer_epoch != self.baseline.issuer_epoch
            or receipt.delivery_id != submission.delivery_id
            or receipt.request_sha256 != submission.request_sha256()
            or receipt.requested_tier != submission.requested_tier
            or receipt.final_admitted_tier != self.envelope.admitted_privacy.tier
            or receipt.protection_acknowledgement is not None
            or submission.destination_brain_id is not None and (
                submission.destination_brain_id != receipt.brain_id
                or submission.issuer_epoch != receipt.issuer_epoch
            )
        ):
            raise ValueError("custody recovery binding mismatch")
        for sequence in (self.journal_sequence, self.event_sequence):
            if type(sequence) is not int or not 1 <= sequence <= 9_223_372_036_854_775_807:
                raise ValueError("invalid custody recovery order")
        if type(self.recorded_at) is not str:
            raise ValueError("invalid custody recovery timestamp")
        try:
            if len(self.recorded_at) > 32 or (
                _timestamp(datetime.fromisoformat(self.recorded_at)) != self.recorded_at
            ):
                raise ValueError("invalid custody recovery timestamp")
        except ValueError:
            raise ValueError("invalid custody recovery timestamp") from None
        if len(self.to_bytes()) > MAX_CAPTURE_RECOVERY_BYTES:
            raise ValueError("custody recovery plan too large")

    def to_bytes(self) -> bytes:
        return canonical({
            "contract_version": "custody-recovery-plan.v1",
            "baseline": self.baseline.value(),
            "journal_base64": base64.b64encode(self.envelope.to_bytes()).decode("ascii"),
            "receipt": self.receipt.to_dict(),
            "journal_sequence": self.journal_sequence,
            "event_sequence": self.event_sequence,
            "recorded_at": self.recorded_at,
        })

    @classmethod
    def from_bytes(cls, raw: bytes) -> CaptureCustodyRecoveryPlan:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_CAPTURE_RECOVERY_BYTES:
            raise ValueError("invalid custody recovery plan size")
        try:
            value = json.loads(raw.decode("utf-8"))
            if type(value) is not dict or set(value) != {
                "contract_version", "baseline", "journal_base64", "receipt",
                "journal_sequence", "event_sequence", "recorded_at",
            } or value["contract_version"] != "custody-recovery-plan.v1":
                raise ValueError("invalid custody recovery fields")
            if type(value["baseline"]) is not dict or set(value["baseline"]) != {
                "brain_id", "issuer_epoch", "artifact_sha256",
            } or type(value["journal_base64"]) is not str:
                raise ValueError("invalid custody recovery fields")
            result = cls(
                RecoveryBaseline(**value["baseline"]),
                JournalEnvelope.from_bytes(
                    base64.b64decode(value["journal_base64"], validate=True)
                ),
                verify_capture_custody_receipt(canonical(value["receipt"])),
                value["journal_sequence"], value["event_sequence"], value["recorded_at"],
            )
            if result.to_bytes() != raw:
                raise ValueError("invalid custody recovery canonical bytes")
            return result
        except UnicodeError, ValueError, TypeError, RecursionError, binascii.Error:
            raise ValueError("invalid custody recovery plan") from None

    @classmethod
    def from_record(cls, record: RecoveryRecord) -> CaptureCustodyRecoveryPlan:
        if type(record) is not RecoveryRecord or record.kind != "capture_custody":
            raise ValueError("invalid custody recovery record kind")
        plan = cls.from_bytes(record.payload)
        if plan.baseline != record.baseline:
            raise ValueError("custody recovery record baseline mismatch")
        return plan


def emit_owner_custody_plan(
    engine: BrainEngine, submission: CaptureSubmission, *,
    baseline: RecoveryBaseline, authority: EffectiveAuthority,
) -> CaptureCustodyRecoveryPlan:
    """Read initial custody without draining; progressed custody needs later plans.

    Caller-provided baseline authentication, independent protection, and semantic
    replay are separate obligations. This seam is owner-local, not a model grant.
    """
    if (
        type(authority) is not EffectiveAuthority or not authority.owner
        or authority.egress_mode is not EgressMode.OWNER_LOCAL
        or authority.principal_id != engine.profile.owner_actor_id
        or type(baseline) is not RecoveryBaseline
    ):
        raise ValueError("custody plan emission requires local owner")
    if type(submission) is not CaptureSubmission or (
        submission.submission_path is not CaptureSubmissionPath.OWNER
        or submission.actor_id != engine.profile.owner_actor_id
        or submission.tenant_id != engine.profile.tenant_id
    ):
        raise ValueError("unsupported custody plan emission operation")
    if authority.brain_id is not None and (
        authority.brain_id != baseline.brain_id or authority.issuer_epoch != baseline.issuer_epoch
    ):
        raise ValueError("custody plan emission authority destination mismatch")
    engine._assert_root()
    with engine._writer_lease_bounded():
        engine._assert_root()
        with engine._store.connect() as connection:
            identity = connection.execute(
                "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
            ).fetchone()
            if identity is None or tuple(identity) != (baseline.brain_id, baseline.issuer_epoch):
                raise ValueError("custody plan emission destination mismatch")
            row = connection.execute(
                "SELECT i.*,p.envelope_bytes,e.event_sequence,e.recorded_at,e.receipt_json "
                "FROM capture_ingestion_items i "
                "JOIN capture_ingestion_payloads p USING(delivery_id) "
                "JOIN capture_ingestion_events e USING(delivery_id) "
                "WHERE i.delivery_id=? AND e.event_kind='queued' AND e.attempt_number=0 "
                "AND (SELECT count(*) FROM capture_ingestion_events "
                "WHERE delivery_id=i.delivery_id)=1 "
                "AND NOT EXISTS(SELECT 1 FROM captures WHERE delivery_id=i.delivery_id)",
                (submission.delivery_id,),
            ).fetchone()
            if row is None:
                raise ValueError("custody plan emission requires initial unreserved custody")
            envelope = JournalEnvelope.from_bytes(cast(bytes, row["envelope_bytes"]))
            event = json.loads(cast(str, row["receipt_json"]))
            if type(event) is not dict or set(event) != {"kind", "receipt"} or (
                event["kind"] != "custody"
            ):
                raise ValueError("invalid original custody event")
            receipt = verify_capture_custody_receipt(canonical(event["receipt"]))
            if (
                envelope.submission != submission or row["envelope_sha256"] != envelope.sha256
                or row["request_sha256"] != receipt.request_sha256
                or row["ingestion_id"] != receipt.ingestion_id
                or row["queued_at"] != receipt.queued_at
                or row["byte_count"] != len(envelope.to_bytes())
                or row["submission_path"] != submission.submission_path.value
                or canonical(event).decode() != row["receipt_json"]
            ):
                raise ValueError("custody plan emission original evidence mismatch")
            return CaptureCustodyRecoveryPlan(
                baseline, envelope, receipt, row["journal_sequence"], row["event_sequence"],
                row["recorded_at"],
            )
