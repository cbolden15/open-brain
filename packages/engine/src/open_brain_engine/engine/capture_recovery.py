"""Exact capture reservation plans, not an authority grant or recovery importer."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .contracts import CaptureAction, JournalEnvelope, TextPayload
from .normalization import _new_id, _timestamp
from .recovery_journal import RecoveryBaseline, RecoveryRecord

MAX_CAPTURE_RECOVERY_BYTES = 16 * 1024 * 1024
_OPTIONAL_IDS = (
    ("auto_proposal_id", "proposal"), ("auto_proposal_receipt_id", "receipt"),
    ("auto_decision_id", "decision"), ("auto_decision_receipt_id", "receipt"),
    ("page_id", "page"), ("publication_id", "publication"),
)
_IDENTITY_FIELDS = frozenset({
    "capture_id", "accepted_receipt_id", "accepted_at", "canonical",
    *(name for name, _ in _OPTIONAL_IDS),
})


def _allocated_id(value: object, prefix: str) -> None:
    if type(value) is not str or not value.startswith(prefix + "_"):
        raise ValueError("invalid capture recovery identity")
    try:
        identity = UUID(value[len(prefix) + 1:])
    except ValueError:
        raise ValueError("invalid capture recovery identity") from None
    if identity.version != 4 or str(identity) != value[len(prefix) + 1:]:
        raise ValueError("invalid capture recovery identity")


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptureReservationIdentity:
    capture_id: str
    accepted_receipt_id: str
    accepted_at: str
    canonical: bool
    auto_proposal_id: str | None = None
    auto_proposal_receipt_id: str | None = None
    auto_decision_id: str | None = None
    auto_decision_receipt_id: str | None = None
    page_id: str | None = None
    publication_id: str | None = None

    def __post_init__(self) -> None:
        _allocated_id(self.capture_id, "capture")
        _allocated_id(self.accepted_receipt_id, "receipt")
        if type(self.canonical) is not bool or type(self.accepted_at) is not str:
            raise ValueError("invalid capture recovery reservation")
        try:
            if len(self.accepted_at) > 32 or (
                _timestamp(datetime.fromisoformat(self.accepted_at)) != self.accepted_at
            ):
                raise ValueError("invalid capture recovery timestamp")
        except ValueError:
            raise ValueError("invalid capture recovery timestamp") from None
        allocated = [self.capture_id, self.accepted_receipt_id]
        for name, prefix in _OPTIONAL_IDS:
            value = getattr(self, name)
            if self.canonical:
                _allocated_id(value, prefix)
                allocated.append(value)
            elif value is not None:
                raise ValueError("invalid capture recovery canonical identities")
        if len(set(allocated)) != len(allocated):
            raise ValueError("duplicate capture recovery identity")

    @classmethod
    def allocate(cls, *, canonical: bool, accepted_at: str) -> CaptureReservationIdentity:
        return cls(
            capture_id=_new_id("capture"), accepted_receipt_id=_new_id("receipt"),
            accepted_at=accepted_at, canonical=canonical,
            auto_proposal_id=_new_id("proposal") if canonical else None,
            auto_proposal_receipt_id=_new_id("receipt") if canonical else None,
            auto_decision_id=_new_id("decision") if canonical else None,
            auto_decision_receipt_id=_new_id("receipt") if canonical else None,
            page_id=_new_id("page") if canonical else None,
            publication_id=_new_id("publication") if canonical else None,
        )


@dataclass(frozen=True, slots=True)
class CaptureRecoveryPlan:
    """Preserve original metadata and IDs; semantic replay still requires admission."""

    baseline: RecoveryBaseline
    envelope: JournalEnvelope
    identities: CaptureReservationIdentity

    def __post_init__(self) -> None:
        if (
            type(self.baseline) is not RecoveryBaseline
            or type(self.envelope) is not JournalEnvelope
            or type(self.identities) is not CaptureReservationIdentity
        ):
            raise ValueError("invalid capture recovery plan")
        submission = self.envelope.submission
        if self.identities.canonical != (submission.action is CaptureAction.CANONICAL_NOTE):
            raise ValueError("capture recovery action mismatch")
        if self.identities.canonical and (
            not isinstance(submission.payload, TextPayload) or submission.space_id is None
        ):
            raise ValueError("invalid capture recovery canonical destination")
        if submission.destination_brain_id is not None and (
            submission.destination_brain_id != self.baseline.brain_id
            or submission.issuer_epoch != self.baseline.issuer_epoch
        ):
            raise ValueError("capture recovery destination mismatch")
        if len(self.to_bytes()) > MAX_CAPTURE_RECOVERY_BYTES:
            raise ValueError("capture recovery plan too large")

    def to_bytes(self) -> bytes:
        return portable_canonical_json_bytes({
            "contract_version": "capture-recovery-plan.v1",
            "baseline": self.baseline.value(),
            "journal_base64": base64.b64encode(self.envelope.to_bytes()).decode("ascii"),
            "identities": asdict(self.identities),
        })

    @classmethod
    def from_record(cls, record: RecoveryRecord) -> CaptureRecoveryPlan:
        """Decode a typed operation; does not verify its chain or grant replay authority."""
        if type(record) is not RecoveryRecord or record.kind != "capture":
            raise ValueError("invalid capture recovery record kind")
        plan = cls.from_bytes(record.payload)
        if plan.baseline != record.baseline:
            raise ValueError("capture recovery record baseline mismatch")
        return plan

    @classmethod
    def from_bytes(cls, raw: bytes) -> CaptureRecoveryPlan:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_CAPTURE_RECOVERY_BYTES:
            raise ValueError("invalid capture recovery plan size")
        try:
            value = json.loads(raw.decode("utf-8"))
            if type(value) is not dict or set(value) != {
                "contract_version", "baseline", "journal_base64", "identities",
            } or value["contract_version"] != "capture-recovery-plan.v1":
                raise ValueError("invalid capture recovery fields")
            if type(value["baseline"]) is not dict or set(value["baseline"]) != {
                "brain_id", "issuer_epoch", "artifact_sha256",
            } or (
                type(value["identities"]) is not dict
                or set(value["identities"]) != _IDENTITY_FIELDS
            ):
                raise ValueError("invalid capture recovery fields")
            if type(value["journal_base64"]) is not str:
                raise ValueError("invalid capture recovery journal")
            result = cls(
                baseline=RecoveryBaseline(**value["baseline"]),
                envelope=JournalEnvelope.from_bytes(
                    base64.b64decode(value["journal_base64"], validate=True),
                ),
                identities=CaptureReservationIdentity(**value["identities"]),
            )
            if result.to_bytes() != raw:
                raise ValueError("invalid capture recovery canonical bytes")
            return result
        except UnicodeError, ValueError, TypeError, RecursionError, binascii.Error:
            raise ValueError("invalid capture recovery plan") from None
