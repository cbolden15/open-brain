from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

from open_brain_engine.core.ids import ReviewId, canonical_json_bytes, validate_identifier
from open_brain_engine.core.models import ValidationError
from open_brain_engine.core.ports import Clock, PutDisposition, PutResult
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ApprovedIntentRecord,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewDecisionResult,
    ReviewState,
)
from open_brain_engine.storage.filesystem import DuplicateConflictError, StorageError
from open_brain_engine.storage.sqlite import (
    SchemaInspection,
    connect_database,
    connect_database_read_only,
)

from .maintenance import (
    ArchivedReview,
    ArchiveResult,
    CurationTarget,
    CurationTaxonomy,
    ReviewMaintenanceEvent,
    ReviewTargetEdit,
    closed_month,
    validate_month,
)


class ReviewStoreError(StorageError):
    """A review operation failed without exposing review content."""


@dataclass(frozen=True, slots=True)
class PendingReviewOutput:
    output_id: str
    approved_record: ApprovedIntentRecord
    created_at: datetime


_SHA256 = re.compile(r"[0-9a-f]{64}")
REVIEW_SCHEMA_VERSION = 2
_REVIEW_MIGRATIONS = (
    (
        1,
        "review-state",
        (
            """
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    aggregate_json TEXT NOT NULL,
    aggregate_sha256 TEXT NOT NULL CHECK (length(aggregate_sha256) = 64),
    created_at TEXT NOT NULL
)
""".strip(),
            """
CREATE TABLE IF NOT EXISTS review_events (
    decision_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(review_id),
    event_json TEXT NOT NULL,
    event_sha256 TEXT NOT NULL CHECK (length(event_sha256) = 64)
)
""".strip(),
            "CREATE INDEX review_events_review_id_idx ON review_events (review_id)",
            """
CREATE TABLE IF NOT EXISTS approved_intent_records (
    record_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE REFERENCES reviews(review_id),
    record_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64)
)
""".strip(),
            """
CREATE TABLE IF NOT EXISTS review_outbox (
    output_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL UNIQUE REFERENCES approved_intent_records(record_id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'delivered')),
    created_at TEXT NOT NULL,
    delivered_at TEXT
)
""".strip(),
            "CREATE INDEX review_outbox_pending_idx ON review_outbox (state, output_id)",
        ),
    ),
    (
        2,
        "review-maintenance",
        (
            """
CREATE TABLE IF NOT EXISTS review_curation_targets (
    review_id TEXT PRIMARY KEY REFERENCES reviews(review_id),
    target_json TEXT NOT NULL,
    target_sha256 TEXT NOT NULL CHECK (length(target_sha256) = 64),
    updated_at TEXT NOT NULL
)
""".strip(),
            """
CREATE TABLE IF NOT EXISTS review_maintenance_events (
    event_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(review_id),
    event_json TEXT NOT NULL,
    event_sha256 TEXT NOT NULL CHECK (length(event_sha256) = 64),
    occurred_at TEXT NOT NULL
)
""".strip(),
            "CREATE INDEX review_maintenance_review_idx "
            "ON review_maintenance_events (review_id, occurred_at, event_id)",
            """
CREATE TABLE IF NOT EXISTS review_archives (
    review_id TEXT PRIMARY KEY REFERENCES reviews(review_id),
    closed_month TEXT NOT NULL CHECK (length(closed_month) = 7),
    aggregate_json TEXT NOT NULL,
    aggregate_sha256 TEXT NOT NULL CHECK (length(aggregate_sha256) = 64),
    target_json TEXT,
    target_sha256 TEXT,
    archived_at TEXT NOT NULL,
    CHECK ((target_json IS NULL) = (target_sha256 IS NULL))
)
""".strip(),
            "CREATE INDEX review_archives_month_idx "
            "ON review_archives (closed_month, review_id)",
        ),
    ),
)


def _migration_checksum(version: int, name: str, statements: tuple[str, ...]) -> str:
    return sha256(
        canonical_json_bytes({"version": version, "name": name, "statements": list(statements)})
    ).hexdigest()


def inspect_review_schema(*, root: Path, database_name: str) -> SchemaInspection:
    """Inspect review migrations without creating tables or applying changes."""
    connection = connect_database_read_only(root=root, database_name=database_name)
    try:
        try:
            rows = connection.execute(
                "SELECT version, name, checksum "
                "FROM review_schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.Error:
            return SchemaInspection(version=0, valid=False)
        version = max((int(row["version"]) for row in rows), default=0)
        valid = (
            version == REVIEW_SCHEMA_VERSION
            and len(rows) == len(_REVIEW_MIGRATIONS)
            and all(
                int(row["version"]) == expected_version
                and row["name"] == name
                and row["checksum"] == _migration_checksum(
                    expected_version,
                    name,
                    statements,
                )
                for row, (expected_version, name, statements) in zip(
                    rows,
                    _REVIEW_MIGRATIONS,
                    strict=True,
                )
            )
        )
        return SchemaInspection(version=version, valid=valid)
    except (TypeError, ValueError, sqlite3.Error):
        raise ReviewStoreError("review schema inspection failed") from None
    finally:
        connection.close()


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReviewStoreError("invalid review timestamp")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReviewStoreError("invalid stored review timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise ReviewStoreError("invalid stored review timestamp") from None


def _aggregate_bytes(aggregate: ReviewAggregate) -> bytes:
    if not isinstance(aggregate, ReviewAggregate):
        raise ReviewStoreError("invalid review aggregate")
    try:
        verified = ReviewAggregate.from_dict(aggregate.to_dict())
    except (TypeError, ValueError):
        raise ReviewStoreError("invalid review aggregate") from None
    return canonical_json_bytes(verified.to_dict())


def _approved_record_bytes(record: ApprovedIntentRecord) -> bytes:
    if not isinstance(record, ApprovedIntentRecord):
        raise ReviewStoreError("invalid approved record")
    return canonical_json_bytes(record.to_dict())


def _event_bytes(aggregate: ReviewAggregate) -> bytes:
    if len(aggregate.events) != 1:
        raise ReviewStoreError("invalid review event")
    return canonical_json_bytes(aggregate.events[0].to_dict())


def _target_bytes(target: CurationTarget) -> bytes:
    if not isinstance(target, CurationTarget):
        raise ReviewStoreError("invalid curation target")
    try:
        verified = CurationTarget.from_dict(target.to_dict())
    except (TypeError, ValueError):
        raise ReviewStoreError("invalid curation target") from None
    return canonical_json_bytes(verified.to_dict())


def _maintenance_event_bytes(event: ReviewMaintenanceEvent) -> bytes:
    if not isinstance(event, ReviewMaintenanceEvent):
        raise ReviewStoreError("invalid review maintenance event")
    try:
        verified = ReviewMaintenanceEvent.from_dict(event.to_dict())
    except (TypeError, ValueError):
        raise ReviewStoreError("invalid review maintenance event") from None
    return canonical_json_bytes(verified.to_dict())


def _output_id(record_id: str) -> str:
    return "output_" + sha256(record_id.encode("utf-8")).hexdigest()


class SqliteReviewStore:
    def __init__(self, *, root: Path, database_name: str, clock: Clock) -> None:
        self._clock = clock
        self._connection = connect_database(root=root, database_name=database_name)
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SqliteReviewStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def create_if_absent(self, review: ReviewAggregate, *, payload_digest: str) -> PutResult:
        payload = _aggregate_bytes(review)
        digest = sha256(payload).hexdigest()
        if payload_digest != digest or not _SHA256.fullmatch(payload_digest):
            raise ReviewStoreError("invalid review payload digest")
        if review.proposal.state.value != "open":
            raise ReviewStoreError("review must be open")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                "INSERT INTO reviews(review_id, aggregate_json, aggregate_sha256, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(review_id) DO NOTHING",
                (
                    str(review.proposal.review_id),
                    payload.decode("utf-8"),
                    digest,
                    _timestamp(self._clock.now()),
                ),
            )
            if cursor.rowcount == 1:
                self._connection.commit()
                return PutResult(PutDisposition.CREATED, str(review.proposal.review_id), digest)
            row = self._connection.execute(
                "SELECT aggregate_json, aggregate_sha256 FROM reviews WHERE review_id = ?",
                (str(review.proposal.review_id),),
            ).fetchone()
            if (
                row is None
                or row["aggregate_json"] != payload.decode("utf-8")
                or row["aggregate_sha256"] != digest
            ):
                self._connection.rollback()
                raise DuplicateConflictError("immutable review conflict")
            self._connection.commit()
            return PutResult(PutDisposition.DUPLICATE, str(review.proposal.review_id), digest)
        except DuplicateConflictError:
            raise
        except (ReviewStoreError, sqlite3.Error):
            self._rollback()
            raise ReviewStoreError("review create failed") from None

    def get(self, review_id: ReviewId) -> ReviewAggregate | None:
        validated_id = self._review_id(review_id)
        try:
            row = self._connection.execute(
                "SELECT aggregate_json, aggregate_sha256 FROM reviews "
                "WHERE review_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM review_archives WHERE review_archives.review_id = reviews.review_id"
                ")",
                (validated_id,),
            ).fetchone()
            return None if row is None else self._decode_aggregate(row)
        except ReviewStoreError:
            raise
        except sqlite3.Error:
            raise ReviewStoreError("review read failed") from None

    def active_reviews(self) -> tuple[ReviewAggregate, ...]:
        """Return non-archived review aggregates in stable identifier order."""
        try:
            rows = self._connection.execute(
                "SELECT aggregate_json, aggregate_sha256 FROM reviews "
                "WHERE NOT EXISTS (SELECT 1 FROM review_archives "
                "WHERE review_archives.review_id = reviews.review_id) "
                "ORDER BY review_id"
            ).fetchall()
            return tuple(self._decode_aggregate(row) for row in rows)
        except ReviewStoreError:
            raise
        except sqlite3.Error:
            raise ReviewStoreError("review list failed") from None

    def decide(self, review_id: ReviewId, command: ReviewDecisionCommand) -> ReviewDecisionResult:
        validated_id = self._review_id(review_id)
        if not isinstance(command, ReviewDecisionCommand):
            raise ReviewStoreError("invalid review decision")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT aggregate_json, aggregate_sha256 FROM reviews "
                "WHERE review_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM review_archives WHERE review_archives.review_id = reviews.review_id"
                ")",
                (validated_id,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                raise ReviewStoreError("review not found")
            current = self._decode_aggregate(row)
            result = current.decide(command)
            if result.idempotent:
                self._verify_terminal_rows(result)
                self._connection.commit()
                return result
            self._persist_decision(result)
            self._connection.commit()
            return result
        except (DuplicateConflictError, ReviewStoreError, ValidationError):
            self._rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError):
            self._rollback()
            raise ReviewStoreError("review decision failed") from None

    def register_curation_target(self, target: CurationTarget) -> PutResult:
        payload = _target_bytes(target)
        digest = sha256(payload).hexdigest()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._active_review_row(str(target.review_id))
            if row is None:
                raise ReviewStoreError("review not found")
            aggregate = self._decode_aggregate(row)
            if (
                aggregate.proposal.state is not ReviewState.OPEN
                or aggregate.proposal.review_id != target.review_id
                or aggregate.proposal.privacy_tier is not target.source_privacy_tier
            ):
                raise ReviewStoreError("invalid curation review")
            cursor = self._connection.execute(
                "INSERT INTO review_curation_targets("
                "review_id, target_json, target_sha256, updated_at"
                ") VALUES (?, ?, ?, ?) ON CONFLICT(review_id) DO NOTHING",
                (
                    str(target.review_id),
                    payload.decode("utf-8"),
                    digest,
                    _timestamp(target.updated_at),
                ),
            )
            if cursor.rowcount == 1:
                self._connection.commit()
                return PutResult(PutDisposition.CREATED, str(target.review_id), digest)
            existing = self._connection.execute(
                "SELECT target_json, target_sha256 FROM review_curation_targets "
                "WHERE review_id = ?",
                (str(target.review_id),),
            ).fetchone()
            if (
                existing is None
                or existing["target_json"] != payload.decode("utf-8")
                or existing["target_sha256"] != digest
            ):
                raise DuplicateConflictError("immutable curation target conflict")
            self._connection.commit()
            return PutResult(PutDisposition.DUPLICATE, str(target.review_id), digest)
        except (DuplicateConflictError, ReviewStoreError, ValidationError):
            self._rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError):
            self._rollback()
            raise ReviewStoreError("curation target registration failed") from None

    def get_curation_target(self, review_id: ReviewId) -> CurationTarget | None:
        validated_id = self._review_id(review_id)
        try:
            row = self._connection.execute(
                "SELECT target_json, target_sha256 FROM review_curation_targets "
                "WHERE review_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM review_archives "
                "WHERE review_archives.review_id = review_curation_targets.review_id"
                ")",
                (validated_id,),
            ).fetchone()
            return None if row is None else self._decode_target(row)
        except ReviewStoreError:
            raise
        except sqlite3.Error:
            raise ReviewStoreError("curation target read failed") from None

    def edit_curation_target(
        self,
        review_id: ReviewId,
        command: ReviewTargetEdit,
        *,
        taxonomy: CurationTaxonomy,
        dry_run: bool,
    ) -> CurationTarget:
        validated_id = self._review_id(review_id)
        if (
            not isinstance(command, ReviewTargetEdit)
            or not isinstance(taxonomy, CurationTaxonomy)
            or type(dry_run) is not bool
        ):
            raise ReviewStoreError("invalid curation target edit")
        try:
            if not dry_run:
                self._connection.execute("BEGIN IMMEDIATE")
            review_row = self._active_review_row(validated_id)
            target_row = self._connection.execute(
                "SELECT target_json, target_sha256 FROM review_curation_targets "
                "WHERE review_id = ?",
                (validated_id,),
            ).fetchone()
            if review_row is None or target_row is None:
                raise ReviewStoreError("curation review not found")
            aggregate = self._decode_aggregate(review_row)
            current = self._decode_target(target_row)
            if (
                aggregate.proposal.state is not ReviewState.OPEN
                or aggregate.proposal.review_id != current.review_id
                or aggregate.proposal.privacy_tier is not current.source_privacy_tier
            ):
                raise ReviewStoreError("curation review is not editable")
            edited = current.edit(command, taxonomy=taxonomy)
            if dry_run:
                return edited
            payload = _target_bytes(edited)
            digest = sha256(payload).hexdigest()
            cursor = self._connection.execute(
                "UPDATE review_curation_targets "
                "SET target_json = ?, target_sha256 = ?, updated_at = ? "
                "WHERE review_id = ? AND target_sha256 = ?",
                (
                    payload.decode("utf-8"),
                    digest,
                    _timestamp(edited.updated_at),
                    validated_id,
                    target_row["target_sha256"],
                ),
            )
            if cursor.rowcount != 1:
                raise ReviewStoreError("curation target edit conflict")
            self._insert_maintenance_event(
                ReviewMaintenanceEvent.create(
                    review_id=edited.review_id,
                    action="edited",
                    occurred_at=command.occurred_at,
                    actor=command.actor,
                    evidence_sha256=digest,
                )
            )
            self._connection.commit()
            return edited
        except (ReviewStoreError, ValidationError):
            self._rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError):
            self._rollback()
            raise ReviewStoreError("curation target edit failed") from None

    def archive_reviews(
        self,
        *,
        before: str,
        occurred_at: datetime,
        dry_run: bool,
    ) -> ArchiveResult:
        if type(dry_run) is not bool:
            raise ReviewStoreError("invalid review archive request")
        try:
            cutoff = validate_month(before)
            archived_at = _timestamp(occurred_at)
        except (TypeError, ValueError, ValidationError):
            raise ReviewStoreError("invalid archive month") from None
        try:
            if not dry_run:
                self._connection.execute("BEGIN IMMEDIATE")
            rows = self._connection.execute(
                "SELECT reviews.review_id, reviews.aggregate_json, "
                "reviews.aggregate_sha256 FROM reviews "
                "WHERE NOT EXISTS (SELECT 1 FROM review_archives "
                "WHERE review_archives.review_id = reviews.review_id) "
                "ORDER BY reviews.review_id"
            ).fetchall()
            selected: list[tuple[ReviewAggregate, CurationTarget | None, str]] = []
            for row in rows:
                aggregate = self._decode_aggregate(row)
                month = closed_month(aggregate)
                if month is None or month >= cutoff or self._has_pending_output(aggregate):
                    continue
                target_row = self._connection.execute(
                    "SELECT target_json, target_sha256 FROM review_curation_targets "
                    "WHERE review_id = ?",
                    (str(aggregate.proposal.review_id),),
                ).fetchone()
                target = None if target_row is None else self._decode_target(target_row)
                selected.append((aggregate, target, month))
            result = ArchiveResult(
                len(selected), tuple(sorted({month for _aggregate, _target, month in selected}))
            )
            if dry_run:
                return result
            for aggregate, target, month in selected:
                aggregate_payload = _aggregate_bytes(aggregate)
                aggregate_digest = sha256(aggregate_payload).hexdigest()
                target_payload = None if target is None else _target_bytes(target)
                target_digest = (
                    None if target_payload is None else sha256(target_payload).hexdigest()
                )
                self._connection.execute(
                    "INSERT INTO review_archives("
                    "review_id, closed_month, aggregate_json, aggregate_sha256, "
                    "target_json, target_sha256, archived_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(aggregate.proposal.review_id),
                        month,
                        aggregate_payload.decode("utf-8"),
                        aggregate_digest,
                        None if target_payload is None else target_payload.decode("utf-8"),
                        target_digest,
                        archived_at,
                    ),
                )
                self._insert_maintenance_event(
                    ReviewMaintenanceEvent.create(
                        review_id=aggregate.proposal.review_id,
                        action="archived",
                        occurred_at=occurred_at,
                        actor=Actor(ActorKind.SYSTEM, "review-archive"),
                        evidence_sha256=aggregate_digest,
                    )
                )
            self._connection.commit()
            return result
        except (ReviewStoreError, ValidationError):
            self._rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError):
            self._rollback()
            raise ReviewStoreError("review archive failed") from None

    def get_archived(self, review_id: ReviewId) -> ArchivedReview | None:
        validated_id = self._review_id(review_id)
        try:
            row = self._connection.execute(
                "SELECT closed_month, aggregate_json, aggregate_sha256, "
                "target_json, target_sha256, archived_at "
                "FROM review_archives WHERE review_id = ?",
                (validated_id,),
            ).fetchone()
            return None if row is None else self._decode_archived(row)
        except ReviewStoreError:
            raise
        except sqlite3.Error:
            raise ReviewStoreError("archived review read failed") from None

    def maintenance_events(
        self, review_id: ReviewId
    ) -> tuple[ReviewMaintenanceEvent, ...]:
        validated_id = self._review_id(review_id)
        try:
            rows = self._connection.execute(
                "SELECT event_json, event_sha256 FROM review_maintenance_events "
                "WHERE review_id = ? ORDER BY occurred_at, event_id",
                (validated_id,),
            ).fetchall()
            return tuple(self._decode_maintenance_event(row) for row in rows)
        except ReviewStoreError:
            raise
        except sqlite3.Error:
            raise ReviewStoreError("review maintenance audit read failed") from None

    def pending_outputs(self, *, limit: int) -> tuple[PendingReviewOutput, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 1_000:
            raise ReviewStoreError("invalid output limit")
        try:
            rows = self._connection.execute(
                """
SELECT outbox.output_id, outbox.created_at, records.record_json, records.record_sha256,
       reviews.aggregate_json, reviews.aggregate_sha256
FROM review_outbox AS outbox
JOIN approved_intent_records AS records ON records.record_id = outbox.record_id
JOIN reviews ON reviews.review_id = records.review_id
WHERE outbox.state = 'pending'
  AND NOT EXISTS (
      SELECT 1 FROM review_archives WHERE review_archives.review_id = reviews.review_id
  )
ORDER BY outbox.output_id
LIMIT ?
""",
                (limit,),
            ).fetchall()
            return tuple(self._decode_pending_output(row) for row in rows)
        except ReviewStoreError:
            raise
        except sqlite3.Error:
            raise ReviewStoreError("review outbox read failed") from None

    def mark_output_delivered(self, output_id: str, *, delivered_at: datetime) -> None:
        self.mark_outputs_delivered((output_id,), delivered_at=delivered_at)

    def mark_outputs_delivered(
        self,
        output_ids: tuple[str, ...],
        *,
        delivered_at: datetime,
    ) -> None:
        if (
            not isinstance(output_ids, tuple)
            or not output_ids
            or len(set(output_ids)) != len(output_ids)
            or any(
                not isinstance(output_id, str)
                or not re.fullmatch(r"output_[0-9a-f]{64}", output_id)
                for output_id in output_ids
            )
        ):
            raise ReviewStoreError("invalid output identifier")
        timestamp = _timestamp(delivered_at)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            rows = self._connection.execute(
                "SELECT output_id, state, delivered_at FROM review_outbox "
                f"WHERE output_id IN ({','.join('?' for _ in output_ids)})",
                output_ids,
            ).fetchall()
            by_id = {str(row["output_id"]): row for row in rows}
            if set(by_id) != set(output_ids):
                raise ReviewStoreError("review output not found")
            for output_id in output_ids:
                row = by_id[output_id]
                if row["state"] == "pending":
                    self._connection.execute(
                        "UPDATE review_outbox "
                        "SET state = 'delivered', delivered_at = ? WHERE output_id = ?",
                        (timestamp, output_id),
                    )
                elif row["state"] != "delivered" or row["delivered_at"] is None:
                    raise ReviewStoreError("invalid review output state")
            self._connection.commit()
        except ReviewStoreError:
            self._rollback()
            raise
        except sqlite3.Error:
            self._rollback()
            raise ReviewStoreError("review output update failed") from None

    def _active_review_row(self, review_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                "SELECT aggregate_json, aggregate_sha256 FROM reviews "
                "WHERE review_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM review_archives "
                "WHERE review_archives.review_id = reviews.review_id"
                ")",
                (review_id,),
            ).fetchone(),
        )

    def _decode_target(self, row: sqlite3.Row) -> CurationTarget:
        try:
            payload = str(row["target_json"]).encode("utf-8")
            if sha256(payload).hexdigest() != row["target_sha256"]:
                raise ValueError
            value = json.loads(payload)
            target = CurationTarget.from_dict(value)
            if _target_bytes(target) != payload:
                raise ValueError
            return target
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ReviewStoreError("invalid stored curation target") from None

    def _insert_maintenance_event(self, event: ReviewMaintenanceEvent) -> None:
        payload = _maintenance_event_bytes(event)
        digest = sha256(payload).hexdigest()
        cursor = self._connection.execute(
            "INSERT INTO review_maintenance_events("
            "event_id, review_id, event_json, event_sha256, occurred_at"
            ") VALUES (?, ?, ?, ?, ?) ON CONFLICT(event_id) DO NOTHING",
            (
                event.event_id,
                str(event.review_id),
                payload.decode("utf-8"),
                digest,
                _timestamp(event.occurred_at),
            ),
        )
        if cursor.rowcount == 1:
            return
        row = self._connection.execute(
            "SELECT review_id, event_json, event_sha256, occurred_at "
            "FROM review_maintenance_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if (
            row is None
            or row["review_id"] != str(event.review_id)
            or row["event_json"] != payload.decode("utf-8")
            or row["event_sha256"] != digest
            or row["occurred_at"] != _timestamp(event.occurred_at)
        ):
            raise DuplicateConflictError("review maintenance audit conflict")

    def _decode_maintenance_event(self, row: sqlite3.Row) -> ReviewMaintenanceEvent:
        try:
            payload = str(row["event_json"]).encode("utf-8")
            if sha256(payload).hexdigest() != row["event_sha256"]:
                raise ValueError
            value = json.loads(payload)
            event = ReviewMaintenanceEvent.from_dict(value)
            if _maintenance_event_bytes(event) != payload:
                raise ValueError
            return event
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ReviewStoreError("invalid stored review maintenance event") from None

    def _has_pending_output(self, aggregate: ReviewAggregate) -> bool:
        if aggregate.proposal.state is not ReviewState.APPLIED:
            return False
        row = self._connection.execute(
            "SELECT outbox.state FROM review_outbox AS outbox "
            "JOIN approved_intent_records AS records ON records.record_id = outbox.record_id "
            "WHERE records.review_id = ?",
            (str(aggregate.proposal.review_id),),
        ).fetchone()
        return row is None or row["state"] != "delivered"

    def _decode_archived(self, row: sqlite3.Row) -> ArchivedReview:
        aggregate = self._decode_aggregate(row)
        month = validate_month(str(row["closed_month"]))
        target: CurationTarget | None = None
        if row["target_json"] is not None or row["target_sha256"] is not None:
            if row["target_json"] is None or row["target_sha256"] is None:
                raise ReviewStoreError("invalid archived review")
            target = self._decode_target(row)
        archived_at = _parse_timestamp(row["archived_at"])
        if closed_month(aggregate) != month:
            raise ReviewStoreError("invalid archived review")
        return ArchivedReview(aggregate, target, month, archived_at)

    def _migrate(self) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
CREATE TABLE IF NOT EXISTS review_schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
""".strip()
            )
            rows = self._connection.execute(
                "SELECT version, name, checksum FROM review_schema_migrations ORDER BY version"
            ).fetchall()
            versions = [int(row["version"]) for row in rows]
            if versions != list(range(1, len(versions) + 1)) or any(
                version > REVIEW_SCHEMA_VERSION for version in versions
            ):
                raise ReviewStoreError("review schema is unavailable")
            for version, name, statements in _REVIEW_MIGRATIONS:
                checksum = _migration_checksum(version, name, statements)
                if version <= len(rows):
                    row = rows[version - 1]
                    if row["name"] != name or row["checksum"] != checksum:
                        raise ReviewStoreError("review migration checksum mismatch")
                    continue
                for statement in statements:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO review_schema_migrations(version, name, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (version, name, checksum, _timestamp(self._clock.now())),
                )
            self._connection.commit()
        except ReviewStoreError:
            self._rollback()
            self._connection.close()
            raise
        except sqlite3.Error:
            self._rollback()
            self._connection.close()
            raise ReviewStoreError("review migration failed") from None

    def _persist_decision(self, result: ReviewDecisionResult) -> None:
        aggregate_payload = _aggregate_bytes(result.aggregate)
        aggregate_digest = sha256(aggregate_payload).hexdigest()
        cursor = self._connection.execute(
            "UPDATE reviews SET aggregate_json = ?, aggregate_sha256 = ? WHERE review_id = ?",
            (
                aggregate_payload.decode("utf-8"),
                aggregate_digest,
                str(result.aggregate.proposal.review_id),
            ),
        )
        if cursor.rowcount != 1:
            raise ReviewStoreError("review update failed")
        event_payload = _event_bytes(result.aggregate)
        self._connection.execute(
            "INSERT INTO review_events(decision_id, review_id, event_json, event_sha256) "
            "VALUES (?, ?, ?, ?)",
            (
                result.aggregate.events[0].decision_id,
                str(result.aggregate.proposal.review_id),
                event_payload.decode("utf-8"),
                sha256(event_payload).hexdigest(),
            ),
        )
        if result.approved_record is None:
            return
        record_payload = _approved_record_bytes(result.approved_record)
        self._connection.execute(
            "INSERT INTO approved_intent_records(record_id, review_id, record_json, record_sha256) "
            "VALUES (?, ?, ?, ?)",
            (
                result.approved_record.record_id,
                str(result.approved_record.review_id),
                record_payload.decode("utf-8"),
                sha256(record_payload).hexdigest(),
            ),
        )
        self._connection.execute(
            "INSERT INTO review_outbox(output_id, record_id, state, created_at, delivered_at) "
            "VALUES (?, ?, 'pending', ?, NULL)",
            (
                _output_id(result.approved_record.record_id),
                result.approved_record.record_id,
                _timestamp(self._clock.now()),
            ),
        )

    def _verify_terminal_rows(self, result: ReviewDecisionResult) -> None:
        row = self._connection.execute(
            "SELECT event_json, event_sha256 FROM review_events WHERE review_id = ?",
            (str(result.aggregate.proposal.review_id),),
        ).fetchone()
        event_payload = _event_bytes(result.aggregate)
        if (
            row is None
            or row["event_json"] != event_payload.decode("utf-8")
            or row["event_sha256"] != sha256(event_payload).hexdigest()
        ):
            raise ReviewStoreError("review audit conflict")
        if result.approved_record is None:
            return
        record_payload = _approved_record_bytes(result.approved_record)
        record = self._connection.execute(
            "SELECT record_json, record_sha256 FROM approved_intent_records WHERE record_id = ?",
            (result.approved_record.record_id,),
        ).fetchone()
        outbox = self._connection.execute(
            "SELECT record_id FROM review_outbox WHERE output_id = ?",
            (_output_id(result.approved_record.record_id),),
        ).fetchone()
        if (
            record is None
            or record["record_json"] != record_payload.decode("utf-8")
            or record["record_sha256"] != sha256(record_payload).hexdigest()
            or outbox is None
            or outbox["record_id"] != result.approved_record.record_id
        ):
            raise ReviewStoreError("review approval conflict")

    def _decode_aggregate(self, row: sqlite3.Row) -> ReviewAggregate:
        try:
            payload = str(row["aggregate_json"]).encode("utf-8")
            if sha256(payload).hexdigest() != row["aggregate_sha256"]:
                raise ValueError
            value = json.loads(payload)
            aggregate = ReviewAggregate.from_dict(value)
            if _aggregate_bytes(aggregate) != payload:
                raise ValueError
            return aggregate
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ReviewStoreError("invalid stored review") from None

    def _decode_pending_output(self, row: sqlite3.Row) -> PendingReviewOutput:
        aggregate = self._decode_aggregate(row)
        record = aggregate.approved_record
        if record is None:
            raise ReviewStoreError("invalid stored approval")
        payload = _approved_record_bytes(record)
        if (
            row["record_json"] != payload.decode("utf-8")
            or row["record_sha256"] != sha256(payload).hexdigest()
            or row["output_id"] != _output_id(record.record_id)
        ):
            raise ReviewStoreError("invalid stored approval")
        return PendingReviewOutput(
            str(row["output_id"]), record, _parse_timestamp(row["created_at"])
        )

    def _review_id(self, review_id: ReviewId) -> str:
        try:
            return validate_identifier(str(review_id), prefix="review_")
        except ValueError:
            raise ReviewStoreError("invalid review identifier") from None

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.rollback()
