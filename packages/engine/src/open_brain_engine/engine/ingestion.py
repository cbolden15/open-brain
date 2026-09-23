"""Brain-owned durable capture ingress journal.

The journal is deliberately small: it owns pre-writer custody and replay, while
``captures`` remains the canonical materialization stage machine.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier

from .contracts import (
    CaptureCustodyReceipt,
    CaptureFault,
    CaptureOutcome,
    CaptureReceipt,
    CaptureSubmission,
    InjectedFault,
    JournalEnvelope,
)
from .normalization import _timestamp

if TYPE_CHECKING:
    from .local import BrainEngine


class JournalCapacityError(ValueError):
    """The Brain retained every existing journal item and has no ingress capacity."""


@dataclass(frozen=True, slots=True)
class IngestionStatus:
    """Owner-only, metadata-only active journal state."""

    delivery_id: str
    ingestion_id: str
    state: str
    attempt_number: int
    queued_at: str


def _receipt_json(receipt: CaptureCustodyReceipt | CaptureReceipt) -> str:
    if isinstance(receipt, CaptureCustodyReceipt):
        value: dict[str, object] = {"kind": "custody", "receipt": receipt.to_dict()}
    else:
        value = {
            "kind": "capture",
            "receipt": {
                "capture_id": receipt.capture_id,
                "payload_family": receipt.payload_family,
                "state": receipt.state,
                "enrichment_state": receipt.enrichment_state,
                "space_id": receipt.space_id,
                "canonical_path": receipt.canonical_path,
                "duplicate": receipt.duplicate,
                "requested_tier": receipt.requested_tier.value,
                "final_admitted_tier": receipt.final_admitted_tier.value,
                "delivery_id": receipt.delivery_id,
                "request_sha256": receipt.request_sha256,
                "destination_brain_id": receipt.destination_brain_id,
                "issuer_epoch": receipt.issuer_epoch,
            },
        }
    return portable_canonical_json_bytes(value).decode("utf-8")


def _capture_receipt(value: str) -> CaptureReceipt | None:
    try:
        wrapped = json.loads(value)
        receipt = wrapped["receipt"]
        if wrapped["kind"] != "capture" or not isinstance(receipt, dict):
            return None
        return CaptureReceipt(
            capture_id=cast(str, receipt["capture_id"]),
            payload_family=cast(str, receipt["payload_family"]),
            state=cast(str, receipt["state"]),
            enrichment_state=cast(str, receipt["enrichment_state"]),
            space_id=cast(str | None, receipt["space_id"]),
            canonical_path=cast(str | None, receipt["canonical_path"]),
            duplicate=cast(bool, receipt["duplicate"]),
            requested_tier=PrivacyTier(cast(str, receipt["requested_tier"])),
            final_admitted_tier=PrivacyTier(cast(str, receipt["final_admitted_tier"])),
            delivery_id=cast(str | None, receipt["delivery_id"]),
            request_sha256=cast(str | None, receipt["request_sha256"]),
            destination_brain_id=cast(str | None, receipt["destination_brain_id"]),
            issuer_epoch=cast(int | None, receipt["issuer_epoch"]),
        )
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        return None


class IngestionJournal:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def known(self, submission: CaptureSubmission) -> bool:
        """Whether a delivery already has durable replay state (no content read)."""
        connection = self._engine._store.connect()
        try:
            return self._identity_row(connection, submission.delivery_id) is not None
        finally:
            connection.close()

    def enqueue(self, submission: CaptureSubmission) -> CaptureOutcome:
        """Commit custody before any writer work, returning a stable replay result."""
        envelope = self._engine._prepare_journal_submission(submission)
        request_sha = submission.request_sha256()
        body = envelope.to_bytes()
        if len(body) > self._engine._admission_limits.max_journal_item_bytes:
            raise JournalCapacityError("journal capacity exceeded")
        new_delivery = False
        with self._engine._store.transaction() as connection:
            existing = self._identity_row(connection, submission.delivery_id)
            if existing is not None:
                outcome = self._replay_existing(connection, existing, submission)
            else:
                self._engine._refuse_on_storage_watermark()
                self._enforce_capacity(connection, len(body))
                identity = connection.execute(
                    "SELECT brain_id, issuer_epoch FROM brain_identity WHERE singleton = 1"
                ).fetchone()
                if identity is None:
                    raise RuntimeError("brain identity unavailable")
                queued_at = _timestamp(self._engine._clock())
                custody = CaptureCustodyReceipt(
                    ingestion_id="ingestion_" + str(uuid4()),
                    brain_id=cast(str, identity["brain_id"]),
                    issuer_epoch=cast(int, identity["issuer_epoch"]),
                    delivery_id=submission.delivery_id,
                    request_sha256=request_sha,
                    requested_tier=submission.requested_tier,
                    final_admitted_tier=envelope.admitted_privacy.tier,
                    queued_at=queued_at,
                )
                connection.execute(
                    """
                    INSERT INTO capture_ingestion_items (
                        ingestion_id, delivery_id, request_sha256, envelope_sha256,
                        submission_path, byte_count, queued_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        custody.ingestion_id,
                        submission.delivery_id,
                        request_sha,
                        envelope.sha256,
                        submission.submission_path.value,
                        len(body),
                        queued_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO capture_ingestion_payloads "
                    "(delivery_id, envelope_bytes) VALUES (?, ?)",
                    (submission.delivery_id, body),
                )
                self._append_event(connection, submission.delivery_id, "queued", 0, custody)
                outcome = custody
                new_delivery = True
        if new_delivery:
            self._engine._fault(CaptureFault.AFTER_JOURNAL_COMMIT)
        return outcome

    def drain_locked(self) -> tuple[CaptureReceipt, ...]:
        """Materialize one bounded oldest-first batch while the engine writer fence is held."""
        limits = self._engine._admission_limits
        self._compact_terminal_items()
        connection = self._engine._store.connect()
        try:
            rows = tuple(
                connection.execute(
                    """
                    SELECT journal_sequence, delivery_id, envelope_sha256, byte_count
                    FROM capture_ingestion_pending AS pending
                    WHERE COALESCE(
                        (SELECT event_kind FROM capture_ingestion_events
                         WHERE delivery_id = pending.delivery_id
                         ORDER BY event_sequence DESC LIMIT 1),
                        'queued'
                    ) <> 'quarantined'
                    ORDER BY journal_sequence
                    LIMIT ?
                    """,
                    (limits.max_journal_batch_items,),
                )
            )
        finally:
            connection.close()
        receipts: list[CaptureReceipt] = []
        used = 0
        for row in rows:
            size = cast(int, row["byte_count"])
            if used + size > limits.max_journal_batch_bytes:
                break
            used += size
            result = self._drain_one(
                cast(str, row["delivery_id"]), cast(str, row["envelope_sha256"])
            )
            if result is not None:
                receipts.append(result)
        return tuple(receipts)

    def _compact_terminal_items(self) -> None:
        connection = self._engine._store.connect()
        try:
            deliveries = tuple(
                cast(str, row["delivery_id"])
                for row in connection.execute(
                    """
                    SELECT item.delivery_id
                    FROM capture_ingestion_items AS item
                    WHERE (SELECT event_kind FROM capture_ingestion_events
                           WHERE delivery_id = item.delivery_id
                           ORDER BY event_sequence DESC LIMIT 1)
                          IN ('accepted', 'duplicate')
                    ORDER BY item.journal_sequence
                    """
                )
            )
        finally:
            connection.close()
        for delivery_id in deliveries:
            self.compact(delivery_id)

    def _drain_one(self, delivery_id: str, expected_digest: str) -> CaptureReceipt | None:
        connection = self._engine._store.connect()
        try:
            row = connection.execute(
                """
                SELECT item.request_sha256, payload.envelope_bytes,
                       (SELECT max(attempt_number) FROM capture_ingestion_events
                        WHERE delivery_id = item.delivery_id) AS attempts
                FROM capture_ingestion_items AS item
                JOIN capture_ingestion_payloads AS payload USING (delivery_id)
                WHERE item.delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        raw = cast(bytes, row["envelope_bytes"])
        try:
            if sha256(raw).hexdigest() != expected_digest:
                raise ValueError("digest mismatch")
            envelope = JournalEnvelope.from_bytes(raw)
            submission = envelope.submission
            receipt = self._engine._materialize_capture_locked(
                submission, admitted_privacy=envelope.admitted_privacy
            )
            self._engine._fault(CaptureFault.AFTER_CANONICAL_COMPLETION)
        except InjectedFault:
            raise
        except ValueError:
            self._terminal_metadata(
                delivery_id, "quarantined", cast(int, row["attempts"]), "invalid"
            )
            return None
        except Exception:
            attempts = cast(int, row["attempts"]) + 1
            event = (
                "quarantined"
                if attempts >= self._engine._admission_limits.max_journal_attempts
                else "attempt_failed"
            )
            self._terminal_metadata(delivery_id, event, attempts, "retryable")
            return None
        self._terminal_receipt(
            delivery_id, "duplicate" if receipt.duplicate else "accepted", receipt
        )
        return receipt

    def _terminal_metadata(self, delivery_id: str, event: str, attempts: int, reason: str) -> None:
        with self._engine._store.transaction() as connection:
            self._append_event(
                connection, delivery_id, event, attempts, {"status": event, "reason": reason}
            )

    def _terminal_receipt(self, delivery_id: str, event: str, receipt: CaptureReceipt) -> None:
        with self._engine._store.transaction() as connection:
            self._append_event(connection, delivery_id, event, 0, receipt)
        self._engine._fault(CaptureFault.AFTER_JOURNAL_TERMINAL_EVENT)
        self.compact(delivery_id)

    def compact(self, delivery_id: str) -> None:
        """Remove active terminal custody only after ``captures`` owns replay."""
        with self._engine._store.transaction() as connection:
            row = connection.execute(
                "SELECT request_sha256 FROM capture_ingestion_items WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return
            canonical = connection.execute(
                "SELECT 1 FROM captures WHERE delivery_id = ? AND request_sha256 = ?",
                (delivery_id, row["request_sha256"]),
            ).fetchone()
            if canonical is None:
                return
            connection.execute(
                "DELETE FROM capture_ingestion_payloads WHERE delivery_id = ?", (delivery_id,)
            )
            connection.execute(
                "DELETE FROM capture_ingestion_items WHERE delivery_id = ?", (delivery_id,)
            )

    def status(self, *, limit: int = 100) -> tuple[IngestionStatus, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid journal status limit")
        connection = self._engine._store.connect()
        try:
            rows = tuple(
                connection.execute(
                    """
                SELECT pending.delivery_id, pending.ingestion_id, pending.queued_at,
                       event.event_kind, event.attempt_number
                FROM capture_ingestion_pending AS pending
                JOIN capture_ingestion_events AS event ON event.event_sequence = (
                    SELECT max(event_sequence) FROM capture_ingestion_events
                    WHERE delivery_id = pending.delivery_id
                )
                ORDER BY pending.journal_sequence LIMIT ?
                """,
                    (limit,),
                )
            )
        finally:
            connection.close()
        return tuple(
            IngestionStatus(
                delivery_id=cast(str, row["delivery_id"]),
                ingestion_id=cast(str, row["ingestion_id"]),
                state=cast(str, row["event_kind"]),
                attempt_number=cast(int, row["attempt_number"]),
                queued_at=cast(str, row["queued_at"]),
            )
            for row in rows
        )

    def retry(self, delivery_id: str) -> None:
        with self._engine._store.transaction() as connection:
            state = self._latest_event(connection, delivery_id)
            if state != "quarantined":
                raise ValueError("journal item is not quarantined")
            self._append_event(connection, delivery_id, "queued", 0, {"status": "queued"})

    def discard(self, delivery_id: str, *, reason: str) -> None:
        if not isinstance(reason, str) or not 1 <= len(reason) <= 128:
            raise ValueError("invalid discard reason")
        with self._engine._store.transaction() as connection:
            item = connection.execute(
                "SELECT request_sha256 FROM capture_ingestion_items WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if item is None or self._latest_event(connection, delivery_id) != "quarantined":
                raise ValueError("journal item is not quarantined")
            result = portable_canonical_json_bytes(
                {"status": "discarded", "reason": reason}
            ).decode("utf-8")
            self._append_event(connection, delivery_id, "discarded", 0, json.loads(result))
            connection.execute(
                "INSERT INTO capture_ingestion_tombstones VALUES (?, ?, ?, ?)",
                (delivery_id, item["request_sha256"], result, _timestamp(self._engine._clock())),
            )
            connection.execute(
                "DELETE FROM capture_ingestion_payloads WHERE delivery_id = ?", (delivery_id,)
            )
            connection.execute(
                "DELETE FROM capture_ingestion_items WHERE delivery_id = ?", (delivery_id,)
            )

    def _identity_row(self, connection: sqlite3.Connection, delivery_id: str) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT 'item' AS location, request_sha256, delivery_id "
            "FROM capture_ingestion_items WHERE delivery_id = ? "
            "UNION ALL SELECT 'capture', request_sha256, delivery_id "
            "FROM captures WHERE delivery_id = ? "
            "UNION ALL SELECT 'tombstone', request_sha256, delivery_id "
            "FROM capture_ingestion_tombstones WHERE delivery_id = ?",
            (delivery_id, delivery_id, delivery_id),
        ).fetchall()
        if len(row) > 1:
            locations = {cast(str, item["location"]) for item in row}
            digests = {cast(str, item["request_sha256"]) for item in row}
            if locations == {"item", "capture"} and len(digests) == 1:
                return next(
                    cast(sqlite3.Row, item) for item in row if item["location"] == "item"
                )
            raise RuntimeError("ambiguous journal replay state")
        return None if not row else cast(sqlite3.Row, row[0])

    def _replay_existing(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        submission: CaptureSubmission,
    ) -> CaptureOutcome:
        if cast(str, row["request_sha256"]) != submission.request_sha256():
            from .capture import DeliveryConflict

            raise DeliveryConflict()
        location = cast(str, row["location"])
        delivery_id = cast(str, row["delivery_id"])
        if location == "capture":
            receipt = self._engine._receipt_for_delivery(delivery_id)
            if receipt is None:
                raise RuntimeError("canonical replay unavailable")
            # ``captures`` retains the admitted decision, while the request
            # digest proves this is the exact original submission. Preserve
            # that submission's requested tier on replay so a boundary
            # narrowing does not make the receipt appear to describe a
            # different request after journal compaction.
            return replace(
                receipt,
                duplicate=True,
                requested_tier=submission.requested_tier,
            )
        if location == "tombstone":
            raise ValueError("delivery discarded")
        event = connection.execute(
            "SELECT receipt_json FROM capture_ingestion_events WHERE delivery_id = ? "
            "ORDER BY event_sequence DESC LIMIT 1",
            (delivery_id,),
        ).fetchone()
        if event is None:
            raise RuntimeError("journal event unavailable")
        capture = _capture_receipt(cast(str, event["receipt_json"]))
        if capture is not None:
            return capture
        queued = connection.execute(
            "SELECT receipt_json FROM capture_ingestion_events "
            "WHERE delivery_id = ? AND event_kind = 'queued' "
            "ORDER BY event_sequence LIMIT 1",
            (delivery_id,),
        ).fetchone()
        if queued is None:
            raise RuntimeError("journal custody unavailable")
        wrapped = json.loads(cast(str, queued["receipt_json"]))
        from .contracts import verify_capture_custody_receipt

        return verify_capture_custody_receipt(cast(dict[str, object], wrapped["receipt"]))

    def _enforce_capacity(self, connection: sqlite3.Connection, bytes_to_add: int) -> None:
        count, total = connection.execute(
            "SELECT count(*), coalesce(sum(byte_count), 0) FROM capture_ingestion_items"
        ).fetchone()
        limits = self._engine._admission_limits
        if (
            int(count) >= limits.max_journal_items
            or int(total) + bytes_to_add > limits.max_journal_bytes
        ):
            raise JournalCapacityError("journal capacity exceeded")

    def _append_event(
        self,
        connection: sqlite3.Connection,
        delivery_id: str,
        event: str,
        attempts: int,
        receipt: object,
    ) -> None:
        if isinstance(receipt, (CaptureReceipt, CaptureCustodyReceipt)):
            value = _receipt_json(receipt)
        else:
            value = portable_canonical_json_bytes(receipt).decode("utf-8")
        connection.execute(
            "INSERT INTO capture_ingestion_events "
            "(delivery_id, event_kind, attempt_number, receipt_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (delivery_id, event, attempts, value, _timestamp(self._engine._clock())),
        )

    @staticmethod
    def _latest_event(connection: sqlite3.Connection, delivery_id: str) -> str | None:
        row = connection.execute(
            "SELECT event_kind FROM capture_ingestion_events WHERE delivery_id = ? "
            "ORDER BY event_sequence DESC LIMIT 1",
            (delivery_id,),
        ).fetchone()
        return None if row is None else cast(str, row["event_kind"])


__all__ = ["IngestionJournal", "IngestionStatus", "JournalCapacityError"]
