"""Optional owner guard for a trusted independent recovery adapter.

The adapter authenticates the latest head and independently retains the whole
closure before returning evidence. Checking this evidence is not itself proof
of independent durability. Deployment must enforce this configuration on every
writer; this provisional owner seam does not cover managed/control mutations.
Frozen envelope-only receipt-protection contracts remain unchanged.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol, cast

from .capture_recovery import CaptureRecoveryPlan, _capture_plan_from_connection
from .capture_replay import require_matching_capture
from .contracts import CaptureReceipt, CaptureSubmission, CaptureSubmissionPath, JournalEnvelope
from .custody_recovery import CaptureCustodyRecoveryPlan, _custody_plan_from_connection
from .journal_recovery import (
    CaptureJournalRecoveryEvent,
    CaptureJournalRecoveryPlan,
    CaptureJournalRecoveryTombstone,
)
from .recovery_closure import RecoveryClosure
from .recovery_journal import RecoveryBaseline

if TYPE_CHECKING:
    from .local import BrainEngine

RecoveryPlan = CaptureRecoveryPlan | CaptureCustodyRecoveryPlan | CaptureJournalRecoveryPlan


def operation_sha256(plan: RecoveryPlan) -> str:
    if type(plan) not in (
        CaptureRecoveryPlan, CaptureCustodyRecoveryPlan, CaptureJournalRecoveryPlan,
    ):
        raise ValueError("unsupported recovery protection plan")
    return sha256(b"open-brain-recovery-operation.v1\0" + plan.to_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class RecoveryProtectionEvidence:
    closure: RecoveryClosure
    operation_sha256: str
    protection_id: str

    def __post_init__(self) -> None:
        if type(self.closure) is not RecoveryClosure or (
            type(self.operation_sha256) is not str
            or len(self.operation_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.operation_sha256)
            or type(self.protection_id) is not str
            or not 0 < len(self.protection_id) <= 256
            or not self.protection_id.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in self.protection_id)
        ):
            raise ValueError("invalid recovery protection evidence")


class RecoveryProtectionPort(Protocol):
    """Trusted adapter: authenticate latest closure and retain it independently.

    Protect is idempotent by operation commitment. Lookup must fetch current
    authenticated evidence, not infer retention from local files or a prefix.
    Both calls must honor the supplied bounded deadline. Real backend restore,
    retention isolation and all mutation coverage require separate acceptance.
    """

    def protect(
        self, plan: RecoveryPlan, *, timeout_seconds: float,
    ) -> RecoveryProtectionEvidence: ...

    def lookup(
        self, delivery_id: str, *, timeout_seconds: float,
    ) -> RecoveryProtectionEvidence: ...


class RecoveryProtectionPendingError(RuntimeError):
    """No acknowledgement or body release; retain exact local custody for retry."""


@dataclass(frozen=True, slots=True)
class RecoveryProtectionGuard:
    baseline: RecoveryBaseline
    port: RecoveryProtectionPort
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if type(self.baseline) is not RecoveryBaseline or (
            not callable(getattr(self.port, "protect", None))
            or not callable(getattr(self.port, "lookup", None))
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 60
        ):
            raise ValueError("invalid recovery protection guard")

    def require_owner(self, engine: BrainEngine, submission: CaptureSubmission) -> None:
        if type(submission) is not CaptureSubmission or (
            submission.submission_path is not CaptureSubmissionPath.OWNER
            or submission.actor_id != engine.profile.owner_actor_id
            or submission.tenant_id != engine.profile.tenant_id
        ):
            raise ValueError("unsupported recovery-protected submission")

    def validate_identity(self, connection: sqlite3.Connection) -> None:
        identity = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
        ).fetchone()
        if identity is None or tuple(identity) != (
            self.baseline.brain_id, self.baseline.issuer_epoch
        ):
            raise RecoveryProtectionPendingError("recovery protection destination mismatch")

    def _checked(
        self, engine: BrainEngine, plan: RecoveryPlan, evidence: RecoveryProtectionEvidence,
    ) -> None:
        self.require_owner(engine, plan.envelope.submission)
        if type(evidence) is not RecoveryProtectionEvidence or (
            plan.baseline != self.baseline
            or evidence.closure.expected_head.baseline != self.baseline
            or evidence.operation_sha256 != operation_sha256(plan)
        ):
            raise ValueError("recovery protection evidence mismatch")
        matches = 0
        for record in evidence.closure.records:
            decoded: RecoveryPlan
            if record.kind == "capture":
                decoded = CaptureRecoveryPlan.from_record(record)
            elif record.kind == "capture_custody":
                decoded = CaptureCustodyRecoveryPlan.from_record(record)
            elif record.kind == "capture_journal":
                decoded = CaptureJournalRecoveryPlan.from_record(record)
            else:
                raise ValueError("unsupported protected recovery history")
            self.require_owner(engine, decoded.envelope.submission)
            if type(decoded) is type(plan) and decoded.to_bytes() == plan.to_bytes():
                matches += 1
        if matches != 1:
            raise ValueError("missing or ambiguous protected recovery operation")

    def _protected(self, engine: BrainEngine, plan: RecoveryPlan) -> RecoveryProtectionEvidence:
        try:
            evidence = self.port.protect(plan, timeout_seconds=float(self.timeout_seconds))
            self._checked(engine, plan, evidence)
        except Exception:
            raise RecoveryProtectionPendingError("recovery protection pending") from None
        return evidence

    def protect(self, engine: BrainEngine, plan: RecoveryPlan) -> None:
        self._protected(engine, plan)

    @staticmethod
    def _require_unique(evidence: RecoveryProtectionEvidence, delivery_id: str, kind: str) -> None:
        """One original cue/allocation per delivery: a changed local row cannot be re-protected."""
        matches = 0
        for record in evidence.closure.records:
            if record.kind != kind:
                continue
            delivery = (
                CaptureCustodyRecoveryPlan.from_record(record).receipt.delivery_id
                if kind == "capture_custody"
                else CaptureRecoveryPlan.from_record(record).envelope.submission.delivery_id
            )
            matches += delivery == delivery_id
        if matches != 1:
            raise ValueError("ambiguous protected recovery history")

    def capture_plan(self, engine: BrainEngine, delivery_id: str) -> CaptureRecoveryPlan:
        """Read original retained bytes, or fetch exact protected allocation proof."""
        try:
            with engine._store.connect() as connection:
                self.validate_identity(connection)
                retained = connection.execute(
                    "SELECT envelope_bytes FROM capture_ingestion_payloads WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if retained is not None:
                    submission = JournalEnvelope.from_bytes(cast(bytes, retained[0])).submission
                    self.require_owner(engine, submission)
                    return _capture_plan_from_connection(connection, submission, self.baseline)
                row = connection.execute(
                    "SELECT * FROM captures WHERE delivery_id=?", (delivery_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("original capture unavailable")
                evidence = self.port.lookup(
                    delivery_id, timeout_seconds=float(self.timeout_seconds)
                )
                if type(evidence) is not RecoveryProtectionEvidence:
                    raise ValueError("invalid recovery lookup")
                plans = [CaptureRecoveryPlan.from_record(record)
                         for record in evidence.closure.records if record.kind == "capture"]
                matching = [plan for plan in plans
                            if plan.envelope.submission.delivery_id == delivery_id]
                if len(matching) != 1:
                    raise ValueError("missing original capture proof")
                plan = matching[0]
                self._checked(engine, plan, evidence)
                require_matching_capture(row, plan)
                return plan
        except Exception:
            raise RecoveryProtectionPendingError("recovery protection replay pending") from None

    def protect_capture(self, engine: BrainEngine, delivery_id: str) -> None:
        self.protect(engine, self.capture_plan(engine, delivery_id))

    def protect_journal(self, engine: BrainEngine, delivery_id: str, *, compact: bool) -> None:
        """Protect the exact retained journal of a progressed or allocated item.

        The original cue is rebuilt from the item's first queued event, so a
        queue item that predates the protection chain (a baseline-existing
        item) is protected late rather than stalling forever. A changed local
        row produces a second cue/allocation for the delivery and is refused.
        ``compact=True`` commits a terminal history before local custody is
        deleted; ``compact=False`` protects a still-pending history.
        """
        try:
            with engine._store.connect() as connection:
                self.validate_identity(connection)
                body = connection.execute(
                    "SELECT envelope_bytes FROM capture_ingestion_payloads WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if body is None:
                    raise ValueError("journal protection requires retained custody")
                submission = JournalEnvelope.from_bytes(cast(bytes, body[0])).submission
                self.require_owner(engine, submission)
                custody = _custody_plan_from_connection(
                    connection, submission, self.baseline, initial_only=False,
                )
                reserved = connection.execute(
                    "SELECT 1 FROM captures WHERE delivery_id=?", (delivery_id,),
                ).fetchone()
                capture = None if reserved is None else _capture_plan_from_connection(
                    connection, submission, self.baseline,
                )
                events = tuple(CaptureJournalRecoveryEvent(
                    event_sequence=row["event_sequence"], event_kind=row["event_kind"],
                    attempt_number=row["attempt_number"], receipt_json=row["receipt_json"],
                    recorded_at=row["recorded_at"],
                ) for row in connection.execute(
                    "SELECT * FROM capture_ingestion_events WHERE delivery_id=? "
                    "ORDER BY event_sequence", (delivery_id,),
                ))
                grave = connection.execute(
                    "SELECT request_sha256,result_json,decided_at "
                    "FROM capture_ingestion_tombstones WHERE delivery_id=?", (delivery_id,),
                ).fetchone()
                tombstone = None if grave is None else CaptureJournalRecoveryTombstone(
                    request_sha256=grave["request_sha256"], result_json=grave["result_json"],
                    decided_at=grave["decided_at"],
                )
            if compact and events[-1].event_kind not in {"accepted", "duplicate", "discarded"}:
                raise ValueError("journal compaction requires terminal history")
            self._require_unique(self._protected(engine, custody), delivery_id, "capture_custody")
            if capture is not None:
                self._require_unique(self._protected(engine, capture), delivery_id, "capture")
            self.protect(engine, CaptureJournalRecoveryPlan(
                custody=custody, events=events, capture=capture, tombstone=tombstone,
                compacted=compact,
            ))
        except Exception:
            raise RecoveryProtectionPendingError("recovery protection journal pending") from None

    def protect_terminal_journal(self, engine: BrainEngine, delivery_id: str) -> None:
        """Commit original terminal events separately before deleting local custody."""
        self.protect_journal(engine, delivery_id, compact=True)

    def protect_pending(self, engine: BrainEngine, submission: CaptureSubmission) -> None:
        try:
            self.require_owner(engine, submission)
            with engine._store.connect() as connection:
                self.validate_identity(connection)
                reserved = connection.execute(
                    "SELECT 1 FROM captures WHERE delivery_id=?", (submission.delivery_id,),
                ).fetchone()
                progressed = connection.execute(
                    "SELECT count(*) FROM capture_ingestion_events WHERE delivery_id=?",
                    (submission.delivery_id,),
                ).fetchone()[0] > 1
                plan: RecoveryPlan | None = None
                if progressed:
                    pass
                elif reserved is not None:
                    plan = _capture_plan_from_connection(connection, submission, self.baseline)
                else:
                    plan = _custody_plan_from_connection(connection, submission, self.baseline)
            if plan is None:
                self.protect_journal(engine, submission.delivery_id, compact=False)
            else:
                self.protect(engine, plan)
        except Exception:
            raise RecoveryProtectionPendingError("recovery protection custody pending") from None

    def validate_duplicate(
        self, engine: BrainEngine, submission: CaptureSubmission, receipt: CaptureReceipt,
    ) -> None:
        plan = self.capture_plan(engine, submission.delivery_id)
        if plan.envelope.submission != submission or (
            plan.identities.capture_id != receipt.capture_id
        ):
            raise RecoveryProtectionPendingError("recovery protection replay mismatch")
        self.protect(engine, plan)
        with engine._store.connect() as connection:
            active = connection.execute(
                "SELECT 1 FROM capture_ingestion_items WHERE delivery_id=?",
                (submission.delivery_id,),
            ).fetchone()
        if active is not None:
            self.protect_terminal_journal(engine, submission.delivery_id)
        else:
            # Once local custody is compacted, allocation evidence alone cannot
            # prove the original terminal events. Require the independently
            # retained journal rather than inventing a replacement history.
            try:
                evidence = self.port.lookup(
                    submission.delivery_id, timeout_seconds=float(self.timeout_seconds),
                )
                self._checked(engine, plan, evidence)
                journals = [CaptureJournalRecoveryPlan.from_record(record)
                            for record in evidence.closure.records
                            if record.kind == "capture_journal"]
                matching = [journal for journal in journals
                            if journal.envelope.submission.delivery_id == submission.delivery_id]
                if len(matching) != 1 or matching[0].capture != plan or (
                    not matching[0].compacted
                    or matching[0].events[-1].event_kind not in {"accepted", "duplicate"}
                ):
                    raise ValueError("missing original terminal journal proof")
            except Exception:
                raise RecoveryProtectionPendingError(
                    "recovery protection journal pending",
                ) from None
