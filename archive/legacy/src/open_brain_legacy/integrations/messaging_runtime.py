"""Persistent runtime state for provider-neutral messaging synchronization."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import (
    CaptureId,
    canonical_json_bytes,
    capture_id_for,
    review_id_for,
)
from open_brain_engine.core.models import Intent, PrivacyTier
from open_brain_engine.core.ports import Clock
from open_brain_engine.review.models import Actor, ActorKind, ReviewAggregate, ReviewProposal
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    RootConfinementError,
    StorageError,
)
from open_brain_engine.storage.sqlite import (
    Migration,
    SchemaError,
    connect_database,
    connect_database_read_only,
    migrate,
)

from open_brain_legacy._compat.open_brain.integrations.config import IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.ports import (
    Capability,
    ProviderSyncRequest,
    ProviderSyncResult,
    RedactedText,
    RedactionPolicyVersion,
    RedactionReceipt,
    ReviewBoundWriter,
    ReviewDisposition,
    ReviewWriteKind,
    ReviewWriteRequest,
    ReviewWriteResult,
)
from open_brain_legacy.integrations.messaging import (
    MessageBatch,
    MessageCandidate,
    MessageConfidence,
    MessageSource,
    MessagingIntegration,
)
from open_brain_legacy.review.store import SqliteReviewStore

_OPAQUE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CURSOR_SENTINEL = "cursor_root"
_DEFAULT_DATABASE_NAME = PurePosixPath("integrations/messaging-runtime.sqlite3")
_DEFAULT_INBOX_DATABASE_NAME = PurePosixPath("integrations/message-inbox.sqlite3")
_CURSOR_SCHEMA_VERSION = 1
_INBOX_SCHEMA_VERSION = 1
_MAX_FAILURES = 32


class MessagingRuntimeStateError(StorageError):
    """Persistent messaging runtime state is malformed or unavailable."""


class MessagingFailureStage(StrEnum):
    """Stable redacted failure stages for messaging runtime persistence."""

    CURSOR_STATE = "cursor_state"
    SOURCE = "source"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class MessagingFailureRecord:
    """A redacted persistent failure record for one messaging sync attempt."""

    failure_id: str
    resource_ref: str
    cursor_ref: str | None
    stage: MessagingFailureStage
    summary: RedactedText
    occurred_at: datetime

    def __post_init__(self) -> None:
        if (
            _OPAQUE_ID_PATTERN.fullmatch(self.failure_id) is None
            or _OPAQUE_ID_PATTERN.fullmatch(self.resource_ref) is None
            or (
                self.cursor_ref is not None
                and _OPAQUE_ID_PATTERN.fullmatch(self.cursor_ref) is None
            )
            or not isinstance(self.stage, MessagingFailureStage)
            or not isinstance(self.summary, RedactedText)
            or not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("invalid messaging failure record")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))

    def to_dict(self) -> dict[str, object]:
        return {
            "failure_id": self.failure_id,
            "resource_ref": self.resource_ref,
            "stage": self.stage.value,
            "summary": self.summary.to_dict(),
            "occurred_at": _timestamp(self.occurred_at),
            **({"cursor_ref": self.cursor_ref} if self.cursor_ref is not None else {}),
        }


@dataclass(slots=True)
class PersistentMessagingCursorStore:
    """Root-confined durable cursor state with redacted failure persistence."""

    root: Path
    database_name: str | PurePosixPath = _DEFAULT_DATABASE_NAME
    clock: Clock = field(default_factory=lambda: _SystemClock())

    def current_cursor(self, resource_ref: str) -> str | None:
        _validate_opaque_id(resource_ref, "invalid messaging resource")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT current_cursor FROM cursor_state WHERE resource_ref = ?",
                (resource_ref,),
            ).fetchone()
            if row is None:
                return None
            return _decode_cursor(row["current_cursor"])
        except RootConfinementError:
            raise
        except MessagingRuntimeStateError as error:
            self._safe_record_failure(
                resource_ref=resource_ref,
                cursor_ref=None,
                stage=MessagingFailureStage.CURSOR_STATE,
                detail=str(error),
            )
            raise
        except (SchemaError, sqlite3.Error, TypeError, ValueError):
            self._safe_record_failure(
                resource_ref=resource_ref,
                cursor_ref=None,
                stage=MessagingFailureStage.CURSOR_STATE,
                detail="messaging cursor state unavailable",
            )
            raise MessagingRuntimeStateError("invalid messaging runtime state") from None
        finally:
            connection.close()

    def cursor_was_processed(self, resource_ref: str, cursor_ref: str | None) -> bool:
        _validate_opaque_id(resource_ref, "invalid messaging resource")
        _validate_optional_opaque_id(cursor_ref, "invalid messaging cursor")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT 1 FROM processed_cursors WHERE resource_ref = ? AND cursor_ref = ?",
                (resource_ref, _encode_cursor(cursor_ref)),
            ).fetchone()
            return row is not None
        except RootConfinementError:
            raise
        except (SchemaError, sqlite3.Error, TypeError, ValueError):
            self._safe_record_failure(
                resource_ref=resource_ref,
                cursor_ref=cursor_ref,
                stage=MessagingFailureStage.CURSOR_STATE,
                detail="messaging processed cursor lookup unavailable",
            )
            raise MessagingRuntimeStateError("invalid messaging runtime state") from None
        finally:
            connection.close()

    def advance_cursor(
        self,
        resource_ref: str,
        *,
        expected_cursor: str | None,
        next_cursor: str,
    ) -> bool:
        _validate_opaque_id(resource_ref, "invalid messaging resource")
        _validate_optional_opaque_id(expected_cursor, "invalid messaging cursor")
        _validate_opaque_id(next_cursor, "invalid messaging cursor")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_cursor FROM cursor_state WHERE resource_ref = ?",
                (resource_ref,),
            ).fetchone()
            current_cursor = None if row is None else _decode_cursor(row["current_cursor"])
            if current_cursor != expected_cursor:
                connection.rollback()
                return False
            timestamp = _timestamp(self.clock.now())
            connection.execute(
                """
                INSERT INTO cursor_state(resource_ref, current_cursor, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(resource_ref) DO UPDATE SET
                    current_cursor = excluded.current_cursor,
                    updated_at = excluded.updated_at
                """,
                (resource_ref, next_cursor, timestamp),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO processed_cursors(
                    resource_ref,
                    cursor_ref,
                    processed_at
                ) VALUES (?, ?, ?)
                """,
                (resource_ref, _encode_cursor(expected_cursor), timestamp),
            )
            connection.commit()
            return True
        except RootConfinementError:
            raise
        except MessagingRuntimeStateError as error:
            if connection.in_transaction:
                connection.rollback()
            self._safe_record_failure(
                resource_ref=resource_ref,
                cursor_ref=expected_cursor,
                stage=MessagingFailureStage.CURSOR_STATE,
                detail=str(error),
            )
            raise
        except (SchemaError, sqlite3.Error, TypeError, ValueError):
            if connection.in_transaction:
                connection.rollback()
            self._safe_record_failure(
                resource_ref=resource_ref,
                cursor_ref=expected_cursor,
                stage=MessagingFailureStage.CURSOR_STATE,
                detail="messaging cursor advance unavailable",
            )
            raise MessagingRuntimeStateError("invalid messaging runtime state") from None
        finally:
            connection.close()

    def record_failure(
        self,
        *,
        resource_ref: str,
        cursor_ref: str | None,
        stage: MessagingFailureStage,
        detail: str,
    ) -> MessagingFailureRecord:
        _validate_opaque_id(resource_ref, "invalid messaging resource")
        _validate_optional_opaque_id(cursor_ref, "invalid messaging cursor")
        if (
            not isinstance(stage, MessagingFailureStage)
            or not isinstance(detail, str)
            or not detail
        ):
            raise ValueError("invalid messaging failure")
        summary = RedactedText.redact(detail)
        occurred_at = self.clock.now()
        failure_id = _failure_id(
            resource_ref=resource_ref,
            cursor_ref=cursor_ref,
            stage=stage,
            occurred_at=occurred_at,
            summary=summary,
        )
        record = MessagingFailureRecord(
            failure_id=failure_id,
            resource_ref=resource_ref,
            cursor_ref=cursor_ref,
            stage=stage,
            summary=summary,
            occurred_at=occurred_at,
        )
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO failure_records(
                    failure_id,
                    resource_ref,
                    cursor_ref,
                    stage,
                    summary_text,
                    summary_source_digest,
                    summary_text_digest,
                    summary_redaction_count,
                    occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.failure_id,
                    record.resource_ref,
                    record.cursor_ref,
                    record.stage.value,
                    record.summary.text,
                    record.summary.receipt.source_digest,
                    record.summary.receipt.text_digest,
                    record.summary.receipt.redaction_count,
                    _timestamp(record.occurred_at),
                ),
            )
        except sqlite3.IntegrityError:
            pass
        except (SchemaError, sqlite3.Error, TypeError, ValueError):
            raise MessagingRuntimeStateError("invalid messaging runtime state") from None
        finally:
            connection.close()
        return record

    def failures(
        self,
        resource_ref: str,
        *,
        limit: int = 16,
    ) -> tuple[MessagingFailureRecord, ...]:
        _validate_opaque_id(resource_ref, "invalid messaging resource")
        if type(limit) is not int or limit < 1 or limit > _MAX_FAILURES:
            raise ValueError("invalid messaging failure limit")
        connection = self._connect_read_only()
        try:
            rows = connection.execute(
                """
                SELECT
                    failure_id,
                    resource_ref,
                    cursor_ref,
                    stage,
                    summary_text,
                    summary_source_digest,
                    summary_text_digest,
                    summary_redaction_count,
                    occurred_at
                FROM failure_records
                WHERE resource_ref = ?
                ORDER BY rowid ASC
                LIMIT ?
                """,
                (resource_ref, limit),
            ).fetchall()
            return tuple(_row_to_failure_record(row) for row in rows)
        except (SchemaError, sqlite3.Error, TypeError, ValueError):
            raise MessagingRuntimeStateError("invalid messaging runtime state") from None
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = connect_database(root=self.root, database_name=self.database_name)
        migrate(
            connection,
            clock=self.clock,
            migrations=_CURSOR_MIGRATIONS,
            schema_version=_CURSOR_SCHEMA_VERSION,
        )
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        connection = connect_database_read_only(
            root=self.root,
            database_name=self.database_name,
        )
        _validate_schema(
            connection,
            schema_version=_CURSOR_SCHEMA_VERSION,
            migrations=_CURSOR_MIGRATIONS,
        )
        return connection

    def _safe_record_failure(
        self,
        *,
        resource_ref: str,
        cursor_ref: str | None,
        stage: MessagingFailureStage,
        detail: str,
    ) -> None:
        try:
            self.record_failure(
                resource_ref=resource_ref,
                cursor_ref=cursor_ref,
                stage=stage,
                detail=detail,
            )
        except (RootConfinementError, StorageError, ValueError):
            return


@dataclass(slots=True)
class PersistentMessagingRuntime:
    """Message sync runtime with durable cursors and redacted failure records."""

    source: MessageSource
    reviews: ReviewBoundWriter
    state: PersistentMessagingCursorStore
    config: IntegrationConfig = IntegrationConfig()

    def sync(self, request: ProviderSyncRequest) -> ProviderSyncResult:
        integration = MessagingIntegration(
            source=_FailureRecordingSource(self.source, self.state),
            cursors=self.state,
            reviews=_FailureRecordingReviewWriter(
                writer=self.reviews,
                state=self.state,
                resource_ref=request.resource_ref,
                cursor_ref=request.cursor_ref,
            ),
            config=self.config,
        )
        return integration.sync(request)


@dataclass(slots=True)
class SqliteMessageInbox:
    """Immutable opaque message batches written by a separately owned ingress boundary."""

    root: Path
    database_name: str | PurePosixPath = _DEFAULT_INBOX_DATABASE_NAME
    clock: Clock = field(default_factory=lambda: _SystemClock())

    def enqueue(self, batch: MessageBatch) -> None:
        if not isinstance(batch, MessageBatch):
            raise ValueError("invalid messaging batch")
        payload = canonical_json_bytes(
            [
                {
                    "message_ref": candidate.message_ref,
                    "content_ref": candidate.content_ref,
                    "confidence": candidate.confidence.value,
                }
                for candidate in batch.candidates
            ]
        )
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO message_batches(
                    resource_ref,
                    cursor_ref,
                    next_cursor_ref,
                    candidates_json,
                    candidates_sha256,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.resource_ref,
                    _encode_cursor(batch.cursor_ref),
                    batch.next_cursor_ref,
                    payload.decode("utf-8"),
                    sha256(payload).hexdigest(),
                    _timestamp(self.clock.now()),
                ),
            )
        except sqlite3.IntegrityError:
            row = connection.execute(
                """
                SELECT next_cursor_ref, candidates_json, candidates_sha256
                FROM message_batches
                WHERE resource_ref = ? AND cursor_ref = ?
                """,
                (batch.resource_ref, _encode_cursor(batch.cursor_ref)),
            ).fetchone()
            if (
                row is None
                or row["next_cursor_ref"] != batch.next_cursor_ref
                or row["candidates_json"] != payload.decode("utf-8")
                or row["candidates_sha256"] != sha256(payload).hexdigest()
            ):
                raise DuplicateConflictError("immutable messaging batch conflict") from None
        except (SchemaError, sqlite3.Error, TypeError, ValueError):
            raise MessagingRuntimeStateError("invalid messaging runtime state") from None
        finally:
            connection.close()

    def fetch(self, request: ProviderSyncRequest) -> MessageBatch:
        if (
            not isinstance(request, ProviderSyncRequest)
            or request.capability is not Capability.MESSAGING
        ):
            raise ValueError("invalid messaging sync request")
        connection = self._connect_read_only()
        try:
            row = connection.execute(
                """
                SELECT next_cursor_ref, candidates_json, candidates_sha256
                FROM message_batches
                WHERE resource_ref = ? AND cursor_ref = ?
                """,
                (request.resource_ref, _encode_cursor(request.cursor_ref)),
            ).fetchone()
            if row is None:
                raise MessagingRuntimeStateError("messaging batch unavailable")
            payload = str(row["candidates_json"]).encode("utf-8")
            if sha256(payload).hexdigest() != row["candidates_sha256"]:
                raise MessagingRuntimeStateError("invalid messaging runtime state")
            values = json.loads(payload)
            if type(values) is not list:
                raise MessagingRuntimeStateError("invalid messaging runtime state")
            candidates = tuple(
                MessageCandidate(
                    message_ref=value["message_ref"],
                    content_ref=value["content_ref"],
                    confidence=MessageConfidence(value["confidence"]),
                )
                for value in values
                if type(value) is dict
                and set(value) == {"message_ref", "content_ref", "confidence"}
            )
            if len(candidates) != len(values):
                raise MessagingRuntimeStateError("invalid messaging runtime state")
            return MessageBatch(
                resource_ref=request.resource_ref,
                cursor_ref=request.cursor_ref,
                next_cursor_ref=row["next_cursor_ref"],
                candidates=candidates,
            )
        except MessagingRuntimeStateError:
            raise
        except (SchemaError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            raise MessagingRuntimeStateError("invalid messaging runtime state") from None
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = connect_database(root=self.root, database_name=self.database_name)
        migrate(
            connection,
            clock=self.clock,
            migrations=_INBOX_MIGRATIONS,
            schema_version=_INBOX_SCHEMA_VERSION,
        )
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        connection = connect_database_read_only(
            root=self.root,
            database_name=self.database_name,
        )
        _validate_schema(
            connection,
            schema_version=_INBOX_SCHEMA_VERSION,
            migrations=_INBOX_MIGRATIONS,
        )
        return connection


@dataclass(frozen=True, slots=True)
class SqliteReviewProposalWriter:
    """Create canonical open review proposals from opaque messaging references."""

    root: Path
    clock: Clock
    database_name: str = "review/review.sqlite3"

    def submit(self, request: ReviewWriteRequest) -> ReviewWriteResult:
        if not isinstance(request, ReviewWriteRequest):
            raise ValueError("invalid review write request")
        if request.kind is not ReviewWriteKind.PROPOSAL:
            return ReviewWriteResult(
                request.request_id,
                ReviewDisposition.BLOCKED,
                request.review_id,
            )
        capture_id = _messaging_capture_id(request.content_ref)
        expected_review_id = str(
            review_id_for(capture_id, Intent.ACTION_CANDIDATE.value)
        )
        if request.review_id != expected_review_id:
            return ReviewWriteResult(
                request.request_id,
                ReviewDisposition.BLOCKED,
                request.review_id,
            )
        aggregate = ReviewAggregate.create(
            ReviewProposal.create(
                capture_id=capture_id,
                source_ref=request.content_ref,
                privacy_tier=PrivacyTier.PERSONAL,
                proposed_intent=Intent.ACTION_CANDIDATE,
                proposal_reason="Message-derived action requires owner review",
                capture_why="Review an opaque message-derived action candidate",
                created_at=self.clock.now(),
                created_by=Actor(ActorKind.SYSTEM, "messaging-runtime"),
                review_id=request.review_id,
            )
        )
        payload = canonical_json_bytes(aggregate.to_dict())
        with SqliteReviewStore(
            root=self.root,
            database_name=self.database_name,
            clock=self.clock,
        ) as store:
            result = store.create_if_absent(
                aggregate,
                payload_digest=sha256(payload).hexdigest(),
            )
        disposition = {
            "created": ReviewDisposition.QUEUED,
            "duplicate": ReviewDisposition.DUPLICATE,
        }[result.disposition.value]
        return ReviewWriteResult(request.request_id, disposition, request.review_id)


@dataclass(frozen=True, slots=True)
class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _FailureRecordingSource:
    source: MessageSource
    state: PersistentMessagingCursorStore

    def fetch(self, request: ProviderSyncRequest) -> MessageBatch:
        try:
            return self.source.fetch(request)
        except Exception as error:
            self.state._safe_record_failure(
                resource_ref=request.resource_ref,
                cursor_ref=request.cursor_ref,
                stage=MessagingFailureStage.SOURCE,
                detail=str(error) or "messaging source failure",
            )
            raise


@dataclass(frozen=True, slots=True)
class _FailureRecordingReviewWriter:
    writer: ReviewBoundWriter
    state: PersistentMessagingCursorStore
    resource_ref: str
    cursor_ref: str | None

    def submit(self, request: ReviewWriteRequest) -> ReviewWriteResult:
        try:
            return self.writer.submit(request)
        except Exception as error:
            self.state._safe_record_failure(
                resource_ref=self.resource_ref,
                cursor_ref=self.cursor_ref,
                stage=MessagingFailureStage.REVIEW,
                detail=str(error) or "messaging review failure",
            )
            raise


def _row_to_failure_record(row: sqlite3.Row) -> MessagingFailureRecord:
    summary = _restore_redacted_text(
        text=row["summary_text"],
        source_digest=row["summary_source_digest"],
        text_digest=row["summary_text_digest"],
        redaction_count=int(row["summary_redaction_count"]),
    )
    return MessagingFailureRecord(
        failure_id=row["failure_id"],
        resource_ref=row["resource_ref"],
        cursor_ref=row["cursor_ref"],
        stage=MessagingFailureStage(row["stage"]),
        summary=summary,
        occurred_at=_parse_timestamp(row["occurred_at"]),
    )


def _restore_redacted_text(
    *,
    text: str,
    source_digest: str,
    text_digest: str,
    redaction_count: int,
) -> RedactedText:
    receipt = object.__new__(RedactionReceipt)
    object.__setattr__(receipt, "policy_version", RedactionPolicyVersion.V1)
    object.__setattr__(receipt, "source_digest", source_digest)
    object.__setattr__(receipt, "text_digest", text_digest)
    object.__setattr__(receipt, "redaction_count", redaction_count)
    value = object.__new__(RedactedText)
    object.__setattr__(value, "text", text)
    object.__setattr__(value, "receipt", receipt)
    if not receipt.verifies_text(text):
        raise MessagingRuntimeStateError("invalid messaging runtime state")
    return value


def _failure_id(
    *,
    resource_ref: str,
    cursor_ref: str | None,
    stage: MessagingFailureStage,
    occurred_at: datetime,
    summary: RedactedText,
) -> str:
    digest = sha256(
        (
            f"{resource_ref}\0{cursor_ref or ''}\0{stage.value}\0"
            f"{_timestamp(occurred_at.astimezone(UTC))}\0{summary.receipt.source_digest}"
        ).encode()
    ).hexdigest()
    return f"message_failure_{digest[:40]}"


def _validate_schema(
    connection: sqlite3.Connection,
    *,
    schema_version: int,
    migrations: tuple[Migration, ...],
) -> None:
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    except (TypeError, ValueError, sqlite3.Error):
        raise MessagingRuntimeStateError("invalid messaging runtime state") from None
    if version != schema_version or len(rows) != len(migrations):
        raise MessagingRuntimeStateError("invalid messaging runtime state")
    for row, migration_item in zip(rows, migrations, strict=True):
        if (
            int(row["version"]) != migration_item.version
            or row["name"] != migration_item.name
            or row["checksum"] != migration_item.checksum
        ):
            raise MessagingRuntimeStateError("invalid messaging runtime state")


def _validate_opaque_id(value: str, message: str) -> None:
    if not isinstance(value, str) or _OPAQUE_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(message)


def _validate_optional_opaque_id(value: str | None, message: str) -> None:
    if value is not None:
        _validate_opaque_id(value, message)


def _encode_cursor(cursor_ref: str | None) -> str:
    return _CURSOR_SENTINEL if cursor_ref is None else cursor_ref


def _messaging_capture_id(content_ref: str) -> CaptureId:
    _validate_opaque_id(content_ref, "invalid messaging content reference")
    return capture_id_for(
        {
            "identity_version": 1,
            "source": "messaging",
            "content_ref": content_ref,
        }
    )


def _decode_cursor(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MessagingRuntimeStateError("invalid messaging runtime state")
    if value == _CURSOR_SENTINEL:
        return None
    if _OPAQUE_ID_PATTERN.fullmatch(value) is None:
        raise MessagingRuntimeStateError("invalid messaging runtime state")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MessagingRuntimeStateError("invalid messaging runtime state")
    normalized = value.astimezone(UTC)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise MessagingRuntimeStateError("invalid messaging runtime state")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)
    except ValueError:
        raise MessagingRuntimeStateError("invalid messaging runtime state") from None


_STATE_STATEMENTS = (
    """
    CREATE TABLE cursor_state (
        resource_ref TEXT PRIMARY KEY,
        current_cursor TEXT,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE processed_cursors (
        resource_ref TEXT NOT NULL,
        cursor_ref TEXT NOT NULL,
        processed_at TEXT NOT NULL,
        PRIMARY KEY (resource_ref, cursor_ref)
    )
    """.strip(),
    """
    CREATE TABLE failure_records (
        failure_id TEXT PRIMARY KEY,
        resource_ref TEXT NOT NULL,
        cursor_ref TEXT,
        stage TEXT NOT NULL,
        summary_text TEXT NOT NULL,
        summary_source_digest TEXT NOT NULL,
        summary_text_digest TEXT NOT NULL,
        summary_redaction_count INTEGER NOT NULL,
        occurred_at TEXT NOT NULL
    )
    """.strip(),
    "CREATE INDEX failure_records_resource_idx ON failure_records (resource_ref, occurred_at)",
)
_INBOX_STATEMENTS = (
    """
    CREATE TABLE message_batches (
        resource_ref TEXT NOT NULL,
        cursor_ref TEXT NOT NULL,
        next_cursor_ref TEXT NOT NULL,
        candidates_json TEXT NOT NULL,
        candidates_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (resource_ref, cursor_ref)
    )
    """.strip(),
)
_CURSOR_MIGRATIONS = (
    Migration(
        version=1,
        name="messaging_runtime",
        checksum=sha256(
            canonical_json_bytes(
                {
                    "version": 1,
                    "name": "messaging_runtime",
                    "statements": list(_STATE_STATEMENTS),
                }
            )
        ).hexdigest(),
        statements=_STATE_STATEMENTS,
    ),
)
_INBOX_MIGRATIONS = (
    Migration(
        version=1,
        name="messaging_inbox",
        checksum=sha256(
            canonical_json_bytes(
                {
                    "version": 1,
                    "name": "messaging_inbox",
                    "statements": list(_INBOX_STATEMENTS),
                }
            )
        ).hexdigest(),
        statements=_INBOX_STATEMENTS,
    ),
)
