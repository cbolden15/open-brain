"""D4 adapter values for explicitly selected iMessage conversations."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TypedDict
from urllib.parse import quote

from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.connectors import (
    ConnectorCaptureSink,
    ConnectorContractError,
    ConnectorFailureCode,
    ConnectorOutcome,
    ConnectorRunReceipt,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import (
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "IMESSAGE_SOURCE",
    "ImessageCheckpoint",
    "ImessageCheckpointStore",
    "ImessagePage",
    "ImessagePageStatus",
    "ImessageRecord",
    "ImessageSourceAdapter",
]

ImessageContentType = Literal["message", "message_deleted"]

IMESSAGE_SOURCE = "imessage"

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_RESOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,255}")
_MAX_PREVIEW_RECORDS = 25
_REAL_MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"


class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class ImessagePageStatus(StrEnum):
    """Host-observed result for one bounded selected-conversation page."""

    READY = "ready"
    NOT_ALLOWED = "not_allowed"
    MISSING_DATABASE = "missing_database"
    UNSUPPORTED_SCHEMA = "unsupported_schema"


@dataclass(frozen=True, slots=True)
class ImessageRecord:
    """A single selected iMessage row with attachments intentionally excluded."""

    conversation_id: str
    message_id: str
    revision_id: str
    text: str
    title: str
    is_deleted: bool = False
    has_attachment: bool = False

    def __post_init__(self) -> None:
        conversation_id = _required_token(self.conversation_id, "invalid imessage record")
        message_id = _required_token(self.message_id, "invalid imessage record")
        revision_id = _required_token(self.revision_id, "invalid imessage record")
        title = _required_title(self.title)
        if type(self.is_deleted) is not bool or type(self.has_attachment) is not bool:
            raise ConnectorContractError("invalid imessage record")
        text = _text_or_deleted(self.text, self.is_deleted)
        object.__setattr__(self, "conversation_id", conversation_id)
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "title", title)


@dataclass(frozen=True, slots=True)
class ImessagePage:
    status: ImessagePageStatus
    preview: SourcePreviewPage | None = None

    def __post_init__(self) -> None:
        try:
            status = ImessagePageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid imessage page") from error
        if status is ImessagePageStatus.READY:
            if type(self.preview) is not SourcePreviewPage:
                raise ConnectorContractError("invalid imessage page")
        elif self.preview is not None:
            raise ConnectorContractError("invalid imessage page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class ImessageCheckpoint:
    """Durable selected-conversation cursor committed after capture acknowledgements."""

    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.selection) is not SourceResourceSelection
            or self.selection.connector_name != IMESSAGE_SOURCE
            or self.selection.resource_type != "conversation"
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str
                    or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str or not value.startswith("connector.imessage.")
                for value in self.committed_delivery_ids
            )
            or len(self.committed_delivery_ids) != len(set(self.committed_delivery_ids))
            or not isinstance(self.committed_revision_identities, tuple)
            or any(
                type(value) is not str or _CURSOR.fullmatch(value) is None
                for value in self.committed_revision_identities
            )
            or (
                self.committed_revision_identities
                and len(self.committed_revision_identities) != len(self.committed_delivery_ids)
            )
        ):
            raise ConnectorContractError("invalid imessage checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> ImessageCheckpoint:
        return cls(1, selection, None, ())

    @classmethod
    def from_dict(cls, value: object) -> ImessageCheckpoint:
        return cls(**_checkpoint_parts(value))

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> ImessageCheckpoint:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError("invalid imessage checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        revisions = tuple(committed_revision_identities)
        if tuple(committed_delivery_ids) != expected or len(revisions) != len(expected):
            raise ConnectorContractError("invalid imessage checkpoint")
        tracked = (
            dict(zip(self.committed_delivery_ids, self.committed_revision_identities, strict=True))
            if self.committed_revision_identities
            else {}
        )
        expected_set = set(expected)
        retained = tuple(
            (delivery_id, tracked[delivery_id])
            for delivery_id in self.committed_delivery_ids
            if delivery_id not in expected_set and delivery_id in tracked
        )
        return ImessageCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=page.next_cursor,
            committed_delivery_ids=tuple(delivery_id for delivery_id, _revision in retained)
            + expected,
            committed_revision_identities=tuple(revision for _delivery_id, revision in retained)
            + revisions,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "committed_delivery_ids": list(self.committed_delivery_ids),
            "committed_revision_identities": list(self.committed_revision_identities),
            "connector_name": self.selection.connector_name,
            "connection_id": self.selection.connection_id,
            "next_cursor": self.next_cursor,
            "resource_id": self.selection.resource_id,
            "resource_type": self.selection.resource_type,
            "schema_version": self.schema_version,
        }


class ImessageCheckpointStore:
    """Goal-owned JSON checkpoint persistence for selected iMessage conversations."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid imessage checkpoint store")
        self._root = root

    def _path(self, selection: SourceResourceSelection) -> Path:
        _require_selection(selection)
        digest = SourceRecordKey(
            connector_name=IMESSAGE_SOURCE,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id="conversation",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"imessage-{digest}.json"

    def load(self, selection: SourceResourceSelection) -> ImessageCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return ImessageCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid imessage checkpoint store") from error
        checkpoint = ImessageCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid imessage checkpoint store")
        return checkpoint

    def save(self, checkpoint: ImessageCheckpoint) -> Path:
        if type(checkpoint) is not ImessageCheckpoint:
            raise ConnectorContractError("invalid imessage checkpoint store")
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(checkpoint.selection)
        payload = json.dumps(checkpoint.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        with NamedTemporaryFile(
            "w",
            delete=False,
            dir=self._root,
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            handle.write(payload)
            temp_name = handle.name
        try:
            os.replace(temp_name, path)
        except OSError as error:
            Path(temp_name).unlink(missing_ok=True)
            raise ConnectorContractError("invalid imessage checkpoint store") from error
        return path


class ImessageSourceAdapter:
    """Convert explicitly selected local iMessage SQLite rows into D4 source values."""

    def conversation_selection(
        self,
        *,
        connection_id: str,
        database_path: Path,
        conversation_id: str,
        owner_permission_status: Literal["granted", "denied", "not_determined"],
    ) -> SourceResourceSelection:
        if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
            raise ConnectorContractError("invalid imessage source connection")
        _require_database_path(database_path)
        if owner_permission_status != "granted":
            raise ConnectorContractError("invalid imessage source permission")
        conversation = _required_token(conversation_id, "invalid imessage selection")
        return SourceResourceSelection(
            connector_name=IMESSAGE_SOURCE,
            connection_id=connection_id,
            resource_id=f"conversation:{conversation}",
            resource_type="conversation",
        )

    def page_from_sqlite(
        self,
        selection: SourceResourceSelection,
        *,
        database_path: Path,
        privacy: PrivacyDecision,
        limit: int = _MAX_PREVIEW_RECORDS,
        after_rowid: int | None = None,
    ) -> ImessagePage:
        _require_selection(selection)
        path = _require_database_path(database_path)
        if not path.exists():
            return self.missing_database_page()
        if type(limit) is not int or not 1 <= limit <= _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid imessage query")
        if after_rowid is not None and (type(after_rowid) is not int or after_rowid < 0):
            raise ConnectorContractError("invalid imessage query")
        try:
            records = self.records_from_sqlite(
                selection,
                database_path=path,
                limit=limit,
                after_rowid=after_rowid,
            )
        except PermissionError:
            return self.not_allowed_page()
        except sqlite3.DatabaseError:
            return self.unsupported_schema_page()
        if records is None:
            return self.unsupported_schema_page()
        if not records:
            return ImessagePage(
                status=ImessagePageStatus.READY,
                preview=SourcePreviewPage(selection=selection, records=(), next_cursor=None),
            )
        next_cursor = records[-1].message_id if len(records) == limit else None
        return ImessagePage(
            status=ImessagePageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
        )

    def records_from_sqlite(
        self,
        selection: SourceResourceSelection,
        *,
        database_path: Path,
        limit: int = _MAX_PREVIEW_RECORDS,
        after_rowid: int | None = None,
    ) -> tuple[ImessageRecord, ...] | None:
        _require_selection(selection)
        path = _require_database_path(database_path)
        if type(limit) is not int or not 1 <= limit <= _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid imessage query")
        if after_rowid is not None and (type(after_rowid) is not int or after_rowid < 0):
            raise ConnectorContractError("invalid imessage query")
        return _read_records(
            path,
            selection.resource_id.removeprefix("conversation:"),
            limit,
            after_rowid,
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[ImessageRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection)
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid imessage records")
        if len(records) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid imessage records")
        intakes = tuple(self.intake(selection, record, privacy=privacy) for record in records)
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(
                    intake,
                    content_type=("message_deleted" if record.is_deleted else "message"),
                )
                for record, intake in zip(records, intakes, strict=True)
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: ImessageCheckpoint,
        page: ImessagePage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[ImessageCheckpoint, ConnectorRunReceipt]:
        if page.status is ImessagePageStatus.MISSING_DATABASE:
            return checkpoint, ConnectorRunReceipt.failed(
                IMESSAGE_SOURCE,
                ConnectorFailureCode.NOT_DISCOVERED,
            )
        if page.status is ImessagePageStatus.UNSUPPORTED_SCHEMA:
            return checkpoint, ConnectorRunReceipt.failed(
                IMESSAGE_SOURCE,
                ConnectorFailureCode.UNSUPPORTED_CAPABILITY,
            )
        if page.status is not ImessagePageStatus.READY:
            return checkpoint, ConnectorRunReceipt.failed(
                IMESSAGE_SOURCE,
                ConnectorFailureCode.NOT_ALLOWED,
            )
        if (
            type(checkpoint) is not ImessageCheckpoint
            or type(page.preview) is not SourcePreviewPage
            or page.preview.selection != checkpoint.selection
            or not isinstance(intakes, Sequence)
            or isinstance(intakes, str)
            or type(capture_sink) is not ConnectorCaptureSink
        ):
            raise ConnectorContractError("invalid imessage import")
        selected = tuple(record for record in page.preview.records if record.selected)
        if not selected:
            return checkpoint, ConnectorRunReceipt.empty(
                IMESSAGE_SOURCE,
                metadata_count=len(page.preview.records),
            )
        intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
        if (
            len(intake_by_delivery) != len(intakes)
            or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
            or any(
                intake.key.connector_name != IMESSAGE_SOURCE
                or intake.key.connection_id != page.preview.selection.connection_id
                or intake.key.resource_id != page.preview.selection.resource_id
                or intake.source_reference != record.source_reference
                for record, intake in zip(selected, intakes, strict=True)
            )
        ):
            raise ConnectorContractError("invalid imessage import")
        committed_revisions = (
            dict(
                zip(
                    checkpoint.committed_delivery_ids,
                    checkpoint.committed_revision_identities,
                    strict=True,
                )
            )
            if checkpoint.committed_revision_identities
            else {}
        )
        changed_intakes = tuple(
            intake
            for intake in intakes
            if committed_revisions.get(intake.key.delivery_id()) != intake.key.revision_identity()
        )
        if not changed_intakes:
            return checkpoint, ConnectorRunReceipt.empty(
                IMESSAGE_SOURCE,
                metadata_count=len(page.preview.records),
            )
        receipts = tuple(
            capture_sink.submit(
                intake.payload(),
                delivery_id=intake.key.delivery_id(),
                source_origin="third_party",
                source_reference=intake.source_reference,
                provenance=intake.provenance(),
                privacy=intake.privacy,
                intent="reference",
                title=intake.title,
            )
            for intake in changed_intakes
        )
        advanced = checkpoint.advance(
            SourcePreviewPage(
                selection=page.preview.selection,
                records=selected,
                next_cursor=page.preview.next_cursor,
            ),
            committed_delivery_ids=tuple(record.delivery_id for record in selected),
            committed_revision_identities=tuple(
                intake.key.revision_identity() for intake in intakes
            ),
        )
        return (
            advanced,
            ConnectorRunReceipt(
                connector_name=IMESSAGE_SOURCE,
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=len(page.preview.records),
                fetched_count=len(selected),
                extracted_count=len(selected),
                submitted_count=len(receipts),
                stubbed_count=0,
                created_count=sum(1 for receipt in receipts if not receipt.duplicate),
                duplicate_count=sum(1 for receipt in receipts if receipt.duplicate),
                checkpoint_committed=True,
                metadata_count=len(page.preview.records),
            ),
        )

    def intake(
        self,
        selection: SourceResourceSelection,
        record: ImessageRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not ImessageRecord:
            raise ConnectorContractError("invalid imessage record")
        _require_record_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=IMESSAGE_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=f"message:{record.message_id}",
                revision_id=record.revision_id,
            ),
            url=_source_reference(record),
            title=record.title,
            text=record.text,
            privacy=privacy,
        )

    def not_allowed_page(self) -> ImessagePage:
        return ImessagePage(status=ImessagePageStatus.NOT_ALLOWED)

    def missing_database_page(self) -> ImessagePage:
        return ImessagePage(status=ImessagePageStatus.MISSING_DATABASE)

    def unsupported_schema_page(self) -> ImessagePage:
        return ImessagePage(status=ImessagePageStatus.UNSUPPORTED_SCHEMA)


def _read_records(
    path: Path,
    conversation_id: str,
    limit: int,
    after_rowid: int | None,
) -> tuple[ImessageRecord, ...] | None:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if not _has_supported_schema(connection):
            return None
        rows = connection.execute(
            """
            SELECT
              message.ROWID AS rowid,
              message.guid AS message_guid,
              message.text AS text,
              message.date AS message_date,
              message.date_edited AS date_edited,
              message.date_deleted AS date_deleted,
              message.cache_has_attachments AS cache_has_attachments,
              chat.guid AS chat_guid,
              COALESCE(chat.display_name, chat.guid) AS chat_title
            FROM chat
            JOIN chat_message_join ON chat_message_join.chat_id = chat.ROWID
            JOIN message ON message.ROWID = chat_message_join.message_id
            WHERE chat.guid = ? AND (? IS NULL OR message.ROWID > ?)
            ORDER BY message.ROWID ASC
            LIMIT ?
            """,
            (conversation_id, after_rowid, after_rowid, limit),
        ).fetchall()
    return tuple(_record_from_row(row) for row in rows)


def _has_supported_schema(connection: sqlite3.Connection) -> bool:
    expected = {
        "chat": {"ROWID", "guid", "display_name"},
        "message": {
            "ROWID",
            "guid",
            "text",
            "date",
            "date_edited",
            "date_deleted",
            "cache_has_attachments",
        },
        "chat_message_join": {"chat_id", "message_id"},
    }
    for table, columns in expected.items():
        found = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not columns.issubset(found):
            return False
    return True


def _record_from_row(row: sqlite3.Row) -> ImessageRecord:
    rowid = _row_int(row["rowid"])
    text = "" if row["text"] is None else str(row["text"])
    deleted = _row_int(row["date_deleted"]) > 0
    revision_material = "\x1f".join(
        (
            str(rowid),
            str(row["message_guid"]),
            text,
            str(row["message_date"]),
            str(row["date_edited"]),
            str(row["date_deleted"]),
        )
    )
    revision = f"row:{rowid}:sha256:{sha256(revision_material.encode('utf-8')).hexdigest()}"
    return ImessageRecord(
        conversation_id=str(row["chat_guid"]),
        message_id=f"row:{rowid}",
        revision_id=revision,
        text=text,
        title=f"iMessage {rowid} in {_required_title(str(row['chat_title']))}",
        is_deleted=deleted,
        has_attachment=_row_int(row["cache_has_attachments"]) > 0,
    )


def _checkpoint_parts(value: object) -> _CheckpointParts:
    current_fields = {
        "committed_delivery_ids",
        "committed_revision_identities",
        "connector_name",
        "connection_id",
        "next_cursor",
        "resource_id",
        "resource_type",
        "schema_version",
    }
    if not isinstance(value, dict) or set(value) != current_fields:
        raise ConnectorContractError("invalid imessage checkpoint")
    committed = value["committed_delivery_ids"]
    committed_revisions = value["committed_revision_identities"]
    if not isinstance(committed, list) or not isinstance(committed_revisions, list):
        raise ConnectorContractError("invalid imessage checkpoint")
    return {
        "schema_version": _typed_int(value["schema_version"]),
        "selection": SourceResourceSelection(
            connector_name=_typed_str(value["connector_name"]),
            connection_id=_typed_str(value["connection_id"]),
            resource_id=_typed_str(value["resource_id"]),
            resource_type=_typed_str(value["resource_type"]),
        ),
        "next_cursor": None if value["next_cursor"] is None else _typed_str(value["next_cursor"]),
        "committed_delivery_ids": tuple(_typed_str(item) for item in committed),
        "committed_revision_identities": tuple(_typed_str(item) for item in committed_revisions),
    }


def _require_selection(selection: SourceResourceSelection) -> None:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != IMESSAGE_SOURCE
        or selection.resource_type != "conversation"
        or not selection.resource_id.startswith("conversation:")
    ):
        raise ConnectorContractError("invalid imessage selection")


def _require_record_matches_selection(
    selection: SourceResourceSelection,
    record: ImessageRecord,
) -> None:
    _require_selection(selection)
    if selection.resource_id != f"conversation:{record.conversation_id}":
        raise ConnectorContractError("invalid imessage record")


def _require_database_path(database_path: Path) -> Path:
    if not isinstance(database_path, Path) or not database_path.is_absolute():
        raise ConnectorContractError("invalid imessage database path")
    try:
        resolved = database_path.resolve(strict=False)
    except OSError as error:
        raise ConnectorContractError("invalid imessage database path") from error
    if resolved == _REAL_MESSAGES_DB:
        raise ConnectorContractError("real imessage database requires separate consent gate")
    return resolved


def _required_token(value: object, message: str) -> str:
    if type(value) is not str or _RESOURCE.fullmatch(value) is None:
        raise ConnectorContractError(message)
    return value


def _required_title(value: object) -> str:
    if type(value) is not str:
        raise ConnectorContractError("invalid imessage record")
    title = value.strip()
    if not title or "\x00" in title or len(title) > 200:
        raise ConnectorContractError("invalid imessage record")
    return title


def _text_or_deleted(value: object, deleted: bool) -> str:
    if deleted:
        return "This iMessage was deleted in the selected conversation."
    if type(value) is not str:
        raise ConnectorContractError("invalid imessage record")
    text = value.strip()
    if not text or "\x00" in text:
        raise ConnectorContractError("invalid imessage record")
    return text


def _row_int(value: object) -> int:
    if value is None:
        return 0
    if type(value) is int:
        return value
    raise ConnectorContractError("invalid imessage record")


def _source_reference(record: ImessageRecord) -> str:
    return (
        "https://local.open-brain/imessage/conversations/"
        f"{quote(record.conversation_id, safe='')}/messages/{quote(record.message_id, safe='')}"
    )


def _typed_str(value: object) -> str:
    if type(value) is not str:
        raise ConnectorContractError("invalid imessage checkpoint")
    return value


def _typed_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid imessage checkpoint")
    return value
