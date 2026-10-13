"""Closed original owner journal history, not replay authority or durable protection."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .capture_recovery import MAX_CAPTURE_RECOVERY_BYTES, CaptureRecoveryPlan
from .contracts import CaptureSubmissionPath, JournalEnvelope
from .custody_recovery import CaptureCustodyRecoveryPlan
from .normalization import _optional_timestamp
from .recovery_journal import RecoveryBaseline, RecoveryRecord

_MAX_INTEGER = 2**63 - 1
_MAX_EVENTS = 100_000
_TERMINAL = frozenset({"accepted", "duplicate", "discarded"})
_EVENT_FIELDS = frozenset({
    "event_sequence", "event_kind", "attempt_number", "receipt_json", "recorded_at",
})
_TOMBSTONE_FIELDS = frozenset({"request_sha256", "result_json", "decided_at"})


def _timestamp(value: object) -> None:
    if type(value) is not str or len(value) > 32 or _optional_timestamp(value) != value:
        raise ValueError("invalid journal recovery timestamp")


def _object(raw: str) -> dict[str, object]:
    if type(raw) is not str or not 0 < len(raw) <= MAX_CAPTURE_RECOVERY_BYTES:
        raise ValueError("invalid journal recovery metadata")
    try:
        value = json.loads(raw)
        if type(value) is not dict or canonical(value).decode("utf-8") != raw:
            raise ValueError("invalid journal recovery canonical metadata")
        return value
    except UnicodeError, ValueError, TypeError, RecursionError:
        raise ValueError("invalid journal recovery metadata") from None


def _discard_result(raw: str) -> None:
    value = _object(raw)
    if (
        set(value) != {"status", "reason"} or value["status"] != "discarded"
        or type(value["reason"]) is not str or not 1 <= len(value["reason"]) <= 128
    ):
        raise ValueError("invalid journal recovery discard result")


@dataclass(frozen=True, slots=True)
class CaptureJournalRecoveryEvent:
    event_sequence: int
    event_kind: str
    attempt_number: int
    receipt_json: str
    recorded_at: str

    def __post_init__(self) -> None:
        if (
            type(self.event_sequence) is not int
            or not 1 <= self.event_sequence <= _MAX_INTEGER
            or type(self.attempt_number) is not int
            or not 0 <= self.attempt_number <= _MAX_INTEGER
            or type(self.event_kind) is not str
            or self.event_kind not in {"queued", "quarantined", "attempt_failed", *_TERMINAL}
        ):
            raise ValueError("invalid journal recovery event")
        _object(self.receipt_json)
        _timestamp(self.recorded_at)


@dataclass(frozen=True, slots=True)
class CaptureJournalRecoveryTombstone:
    request_sha256: str
    result_json: str
    decided_at: str

    def __post_init__(self) -> None:
        if type(self.request_sha256) is not str or (
            re.fullmatch(r"[0-9a-f]{64}", self.request_sha256) is None
        ):
            raise ValueError("invalid journal recovery tombstone digest")
        _discard_result(self.result_json)
        _timestamp(self.decided_at)


@dataclass(frozen=True, slots=True)
class CaptureJournalRecoveryPlan:
    custody: CaptureCustodyRecoveryPlan
    events: tuple[CaptureJournalRecoveryEvent, ...]
    capture: CaptureRecoveryPlan | None = None
    tombstone: CaptureJournalRecoveryTombstone | None = None
    compacted: bool = False

    def __post_init__(self) -> None:
        from .ingestion import _receipt_json

        if (
            type(self.custody) is not CaptureCustodyRecoveryPlan
            or type(self.events) is not tuple or not 1 <= len(self.events) <= _MAX_EVENTS
            or any(type(event) is not CaptureJournalRecoveryEvent for event in self.events)
            or self.capture is not None and type(self.capture) is not CaptureRecoveryPlan
            or self.tombstone is not None and type(self.tombstone) is not
            CaptureJournalRecoveryTombstone
            or type(self.compacted) is not bool
        ):
            raise ValueError("invalid journal recovery plan")
        if self.envelope.submission.submission_path is not CaptureSubmissionPath.OWNER:
            raise ValueError("journal recovery requires original owner custody")
        if self.capture is not None and (
            self.capture.baseline != self.baseline or self.capture.envelope != self.envelope
        ):
            raise ValueError("journal recovery capture binding mismatch")
        custody = self.custody
        if self.events[0] != CaptureJournalRecoveryEvent(
            custody.event_sequence, "queued", 0, _receipt_json(custody.receipt),
            custody.recorded_at,
        ):
            raise ValueError("journal recovery initial custody mismatch")
        previous = self.events[0]
        attempts = 0
        for event in self.events[1:]:
            if event.event_sequence <= previous.event_sequence or previous.event_kind in _TERMINAL:
                raise ValueError("invalid journal recovery event order")
            data = _object(event.receipt_json)
            kind = event.event_kind
            if kind == "queued":
                if (
                    previous.event_kind != "quarantined" or event.attempt_number != 0
                    or data != {"status": "queued"}
                ):
                    raise ValueError("invalid journal recovery retry")
            elif kind in {"attempt_failed", "quarantined"}:
                if (
                    previous.event_kind == "quarantined" or set(data) != {"status", "reason"}
                    or data["status"] != kind or type(data["reason"]) is not str
                    or data["reason"] not in {"invalid", "retryable"}
                    or kind == "attempt_failed" and data["reason"] != "retryable"
                ):
                    raise ValueError("invalid journal recovery failure")
                expected = attempts + (data["reason"] == "retryable")
                if event.attempt_number != expected:
                    raise ValueError("invalid journal recovery attempt order")
                attempts = expected
            elif kind in {"accepted", "duplicate"}:
                if previous.event_kind == "quarantined" or event.attempt_number != 0:
                    raise ValueError("invalid journal recovery terminal event")
                self._validate_capture_receipt(event)
            elif kind == "discarded":
                if previous.event_kind != "quarantined" or event.attempt_number != 0:
                    raise ValueError("invalid journal recovery discard event")
                _discard_result(event.receipt_json)
            previous = event
        final = self.events[-1]
        if self.compacted and final.event_kind not in _TERMINAL:
            raise ValueError("journal recovery compaction requires terminal history")
        if final.event_kind == "discarded":
            if self.tombstone is None or (
                self.tombstone.request_sha256 != custody.receipt.request_sha256
                or self.tombstone.result_json != final.receipt_json
            ):
                raise ValueError("journal recovery discard tombstone mismatch")
        elif self.tombstone is not None:
            raise ValueError("journal recovery tombstone requires discard")
        self.to_bytes()

    @property
    def baseline(self) -> RecoveryBaseline:
        return self.custody.baseline

    @property
    def envelope(self) -> JournalEnvelope:
        return self.custody.envelope

    def _validate_capture_receipt(self, event: CaptureJournalRecoveryEvent) -> None:
        from .ingestion import _capture_receipt, _receipt_json

        receipt = _capture_receipt(event.receipt_json)
        capture = self.capture
        if capture is None or receipt is None or _receipt_json(receipt) != event.receipt_json:
            raise ValueError("journal recovery terminal requires exact capture plan and receipt")
        submission = self.envelope.submission
        if (
            type(receipt.capture_id) is not str
            or receipt.capture_id != capture.identities.capture_id
            or type(receipt.duplicate) is not bool
            or receipt.duplicate != (event.event_kind == "duplicate")
            or type(receipt.payload_family) is not str
            or receipt.payload_family != submission.payload.family
            or type(receipt.state) is not str or receipt.state not in {"inbox", "published"}
            or type(receipt.enrichment_state) is not str
            or receipt.enrichment_state not in {"pending_enrichment", "enriched"}
            or receipt.space_id != submission.space_id
            or receipt.requested_tier != self.custody.receipt.requested_tier
            or receipt.final_admitted_tier != self.custody.receipt.final_admitted_tier
            or any(value is not None for value in (
                receipt.delivery_id, receipt.request_sha256, receipt.destination_brain_id,
                receipt.issuer_epoch,
            ))
        ):
            raise ValueError("journal recovery terminal receipt binding mismatch")
        if capture.identities.canonical:
            if (
                receipt.state != "published"
                or receipt.canonical_path != capture.identities.capture_id
            ):
                raise ValueError("journal recovery canonical receipt mismatch")
        elif receipt.state != "inbox" or receipt.canonical_path is not None:
            raise ValueError("journal recovery inbox receipt mismatch")

    def to_bytes(self) -> bytes:
        raw = canonical({
            "contract_version": "capture-journal-recovery-plan.v1",
            "custody": json.loads(self.custody.to_bytes()),
            "events": [asdict(event) for event in self.events],
            "capture": None if self.capture is None else json.loads(self.capture.to_bytes()),
            "tombstone": None if self.tombstone is None else asdict(self.tombstone),
            "compacted": self.compacted,
        })
        if len(raw) > MAX_CAPTURE_RECOVERY_BYTES:
            raise ValueError("journal recovery plan too large")
        return raw

    @classmethod
    def from_bytes(cls, raw: bytes) -> CaptureJournalRecoveryPlan:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_CAPTURE_RECOVERY_BYTES:
            raise ValueError("invalid journal recovery plan size")
        try:
            value = json.loads(raw.decode("utf-8"))
            if type(value) is not dict or set(value) != {
                "contract_version", "custody", "events", "capture", "tombstone", "compacted",
            } or value["contract_version"] != "capture-journal-recovery-plan.v1":
                raise ValueError("invalid journal recovery fields")
            events = value["events"]
            if type(events) is not list or not 1 <= len(events) <= _MAX_EVENTS or any(
                type(event) is not dict or set(event) != _EVENT_FIELDS for event in events
            ):
                raise ValueError("invalid journal recovery event fields")
            tombstone = value["tombstone"]
            if tombstone is not None and (
                type(tombstone) is not dict or set(tombstone) != _TOMBSTONE_FIELDS
            ):
                raise ValueError("invalid journal recovery tombstone fields")
            result = cls(
                custody=CaptureCustodyRecoveryPlan.from_bytes(canonical(value["custody"])),
                events=tuple(CaptureJournalRecoveryEvent(**event) for event in events),
                capture=None if value["capture"] is None else CaptureRecoveryPlan.from_bytes(
                    canonical(value["capture"])
                ),
                tombstone=None if tombstone is None else CaptureJournalRecoveryTombstone(
                    **tombstone
                ),
                compacted=value["compacted"],
            )
            if result.to_bytes() != raw:
                raise ValueError("invalid journal recovery canonical bytes")
            return result
        except UnicodeError, ValueError, TypeError, RecursionError:
            raise ValueError("invalid journal recovery plan") from None

    @classmethod
    def from_record(cls, record: RecoveryRecord) -> CaptureJournalRecoveryPlan:
        if type(record) is not RecoveryRecord or record.kind != "capture_journal":
            raise ValueError("invalid journal recovery record kind")
        plan = cls.from_bytes(record.payload)
        if plan.baseline != record.baseline:
            raise ValueError("journal recovery record baseline mismatch")
        return plan
