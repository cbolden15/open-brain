"""Gmail, Microsoft 365 mail, and Google Drive D4 adapter values."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TypedDict, cast

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
    D4_GMAIL_SOURCE,
    D4_GOOGLE_DRIVE_SOURCE,
    D4_MICROSOFT_MAIL_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "DriveFileCheckpoint",
    "DriveFileCheckpointStore",
    "DriveFilePage",
    "DriveFileRecord",
    "DriveSourceAdapter",
    "MailCheckpoint",
    "MailCheckpointStore",
    "MailPage",
    "MailPageStatus",
    "MailRecord",
    "GmailSourceAdapter",
    "MicrosoftMailSourceAdapter",
]

MailConnectorName = Literal["gmail", "microsoft_mail"]
DriveConnectorName = Literal["google_drive"]
MailContentType = Literal["mail_message"]
DriveContentType = Literal["drive_file"]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_HOST_URL = re.compile(r"https://[A-Za-z0-9.-]+(?:/[^\s\x00]*)?")
_RESOURCE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,255}")
_MAX_PREVIEW_RECORDS = 25


class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class MailPageStatus(StrEnum):
    """Host-observed result for one bounded mail/file page."""

    READY = "ready"
    RATE_LIMITED = "rate_limited"
    NEEDS_SIGN_IN = "needs_sign_in"
    NOT_ALLOWED = "not_allowed"
    UNSUPPORTED_ATTACHMENT = "unsupported_attachment"


@dataclass(frozen=True, slots=True)
class MailRecord:
    """A bounded provider mail message with attachments explicitly excluded."""

    connector_name: MailConnectorName
    resource_id: str
    message_id: str
    revision_id: str
    web_url: str
    subject: str
    body_text: str
    attachment_count: int = 0


@dataclass(frozen=True, slots=True)
class DriveFileRecord:
    """A bounded Google Drive text-like file with binary attachments excluded."""

    file_id: str
    revision_id: str
    web_url: str
    name: str
    text: str
    mime_type: str


@dataclass(frozen=True, slots=True)
class _SelectedPage:
    status: MailPageStatus
    preview: SourcePreviewPage | None = None
    retry_after_seconds: int | None = None
    connector_name: str = ""

    def __post_init__(self) -> None:
        try:
            status = MailPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError(f"invalid {self.connector_name} page") from error
        if status is MailPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage or self.retry_after_seconds is not None:
                raise ConnectorContractError(f"invalid {self.connector_name} page")
        elif (
            self.preview is not None
            or (
                self.retry_after_seconds is not None
                and (
                    type(self.retry_after_seconds) is not int
                    or not 1 <= self.retry_after_seconds <= 86_400
                )
            )
        ):
            raise ConnectorContractError(f"invalid {self.connector_name} page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class MailPage(_SelectedPage):
    connector_name: str = "mail"


@dataclass(frozen=True, slots=True)
class DriveFilePage(_SelectedPage):
    connector_name: str = D4_GOOGLE_DRIVE_SOURCE


@dataclass(frozen=True, slots=True)
class _SelectedCheckpoint:
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...] = ()

    @property
    def connector_name(self) -> str:
        return self.selection.connector_name

    def _validate_base(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.selection) is not SourceResourceSelection
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str
                    or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str
                or not value.startswith(f"connector.{self.connector_name}.")
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
            raise ConnectorContractError(f"invalid {self.connector_name} checkpoint")

    def _advance_base(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError(f"invalid {self.connector_name} checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        if tuple(committed_delivery_ids) != expected:
            raise ConnectorContractError(f"invalid {self.connector_name} checkpoint")
        revision_identities = tuple(committed_revision_identities)
        if len(revision_identities) != len(expected):
            raise ConnectorContractError(f"invalid {self.connector_name} checkpoint")
        existing_revisions = (
            dict(zip(self.committed_delivery_ids, self.committed_revision_identities, strict=True))
            if self.committed_revision_identities
            else {}
        )
        expected_set = set(expected)
        retained_delivery_ids = tuple(
            delivery_id
            for delivery_id in self.committed_delivery_ids
            if delivery_id not in expected_set
        )
        retained_revisions = tuple(
            existing_revisions[delivery_id]
            for delivery_id in retained_delivery_ids
            if delivery_id in existing_revisions
        )
        return (
            page.next_cursor,
            retained_delivery_ids + expected,
            retained_revisions + revision_identities,
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

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> _SelectedCheckpoint:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class MailCheckpoint(_SelectedCheckpoint):
    """Durable selected label/folder cursor committed after capture acknowledgements."""

    def __post_init__(self) -> None:
        self._validate_base()
        if self.connector_name not in {D4_GMAIL_SOURCE, D4_MICROSOFT_MAIL_SOURCE}:
            raise ConnectorContractError("invalid mail checkpoint")
        if self.selection.resource_type not in {"mail_label", "mail_folder"}:
            raise ConnectorContractError("invalid mail checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> MailCheckpoint:
        return cls(
            schema_version=1,
            selection=selection,
            next_cursor=None,
            committed_delivery_ids=(),
        )

    @classmethod
    def from_dict(cls, value: object) -> MailCheckpoint:
        parts = _checkpoint_parts(value, "invalid mail checkpoint")
        return cls(**parts)

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> MailCheckpoint:
        next_cursor, delivery_ids, revisions = self._advance_base(
            page,
            committed_delivery_ids=committed_delivery_ids,
            committed_revision_identities=committed_revision_identities,
        )
        return MailCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=next_cursor,
            committed_delivery_ids=delivery_ids,
            committed_revision_identities=revisions,
        )


@dataclass(frozen=True, slots=True)
class DriveFileCheckpoint(_SelectedCheckpoint):
    """Durable selected Drive file cursor committed after capture acknowledgements."""

    def __post_init__(self) -> None:
        self._validate_base()
        if (
            self.connector_name != D4_GOOGLE_DRIVE_SOURCE
            or self.selection.resource_type != "drive_file"
        ):
            raise ConnectorContractError("invalid google drive checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> DriveFileCheckpoint:
        return cls(
            schema_version=1,
            selection=selection,
            next_cursor=None,
            committed_delivery_ids=(),
        )

    @classmethod
    def from_dict(cls, value: object) -> DriveFileCheckpoint:
        parts = _checkpoint_parts(value, "invalid google drive checkpoint")
        return cls(**parts)

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> DriveFileCheckpoint:
        next_cursor, delivery_ids, revisions = self._advance_base(
            page,
            committed_delivery_ids=committed_delivery_ids,
            committed_revision_identities=committed_revision_identities,
        )
        return DriveFileCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=next_cursor,
            committed_delivery_ids=delivery_ids,
            committed_revision_identities=revisions,
        )


class _CheckpointStore:
    def __init__(self, root: Path, connector_names: set[str]) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid checkpoint store")
        self._root = root
        self._connector_names = connector_names

    def _path(self, selection: SourceResourceSelection) -> Path:
        if (
            type(selection) is not SourceResourceSelection
            or selection.connector_name not in self._connector_names
        ):
            raise ConnectorContractError("invalid checkpoint store")
        digest = SourceRecordKey(
            connector_name=selection.connector_name,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id=selection.resource_type,
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"{selection.connector_name}-{selection.resource_type}-{digest}.json"


class MailCheckpointStore(_CheckpointStore):
    """Goal-owned JSON checkpoint persistence for selected mail resources."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, {D4_GMAIL_SOURCE, D4_MICROSOFT_MAIL_SOURCE})

    def load(self, selection: SourceResourceSelection) -> MailCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return MailCheckpoint.initial(selection)
        return MailCheckpoint.from_dict(_read_checkpoint(path, "invalid mail checkpoint store"))

    def save(self, checkpoint: MailCheckpoint) -> Path:
        return _save_checkpoint(self, checkpoint, "invalid mail checkpoint store")


class DriveFileCheckpointStore(_CheckpointStore):
    """Goal-owned JSON checkpoint persistence for selected Drive files."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, {D4_GOOGLE_DRIVE_SOURCE})

    def load(self, selection: SourceResourceSelection) -> DriveFileCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return DriveFileCheckpoint.initial(selection)
        return DriveFileCheckpoint.from_dict(
            _read_checkpoint(path, "invalid google drive checkpoint store")
        )

    def save(self, checkpoint: DriveFileCheckpoint) -> Path:
        return _save_checkpoint(self, checkpoint, "invalid google drive checkpoint store")


class _MailSourceAdapter:
    connector_name: MailConnectorName
    resource_type: str

    def selection(self, *, connection_id: str, resource_id: str) -> SourceResourceSelection:
        _require_connection_id(connection_id)
        _require_resource_token(resource_id, f"invalid {self.connector_name} selection")
        return SourceResourceSelection(
            connector_name=self.connector_name,
            connection_id=connection_id,
            resource_id=f"{self.resource_type}:{resource_id}",
            resource_type=self.resource_type,
        )

    def page_from_rest(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> MailPage:
        records = tuple(self.record_from_rest(value) for value in _require_values(values))
        return MailPage(
            status=MailPageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
            connector_name=self.connector_name,
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[MailRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection, self.connector_name, self.resource_type)
        _require_record_count(records, f"invalid {self.connector_name} records")
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(
                    self.intake(selection, record, privacy=privacy),
                    content_type="mail_message",
                )
                for record in records
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: MailCheckpoint,
        page: MailPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[MailCheckpoint, ConnectorRunReceipt]:
        imported_checkpoint, receipt = _import_page(
            checkpoint,
            page,
            intakes,
            capture_sink,
            connector_name=self.connector_name,
            message=f"invalid {self.connector_name} import",
        )
        return imported_checkpoint, receipt

    def rate_limited_page(self, *, retry_after_seconds: int) -> MailPage:
        return MailPage(
            status=MailPageStatus.RATE_LIMITED,
            retry_after_seconds=retry_after_seconds,
            connector_name=self.connector_name,
        )

    def needs_sign_in_page(self) -> MailPage:
        return MailPage(status=MailPageStatus.NEEDS_SIGN_IN, connector_name=self.connector_name)

    def not_allowed_page(self) -> MailPage:
        return MailPage(status=MailPageStatus.NOT_ALLOWED, connector_name=self.connector_name)

    def unsupported_attachment_page(self) -> MailPage:
        return MailPage(
            status=MailPageStatus.UNSUPPORTED_ATTACHMENT,
            connector_name=self.connector_name,
        )

    def intake(
        self,
        selection: SourceResourceSelection,
        record: MailRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not MailRecord or record.connector_name != self.connector_name:
            raise ConnectorContractError(f"invalid {self.connector_name} record")
        _require_mail_record_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=self.connector_name,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=f"message:{record.message_id}",
                revision_id=f"updated:{record.revision_id}",
            ),
            url=record.web_url,
            title=record.subject,
            text=record.body_text,
            privacy=privacy,
        )

    def record_from_rest(self, value: Mapping[str, object]) -> MailRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError(f"invalid {self.connector_name} record")
        attachment_count = value.get("attachment_count", 0)
        if type(attachment_count) is not int or attachment_count < 0:
            raise ConnectorContractError(f"invalid {self.connector_name} record")
        return MailRecord(
            connector_name=self.connector_name,
            resource_id=_required_str(
                value.get("resource_id"),
                f"invalid {self.connector_name} record",
            ),
            message_id=_required_str(value.get("id"), f"invalid {self.connector_name} record"),
            revision_id=_required_str(
                value.get("revision_id", value.get("updated_at")),
                f"invalid {self.connector_name} record",
            ),
            web_url=_required_url(value.get("web_url"), f"invalid {self.connector_name} record"),
            subject=_required_str(value.get("subject"), f"invalid {self.connector_name} record"),
            body_text=_body_or_title(
                value.get("body_text"),
                value.get("subject"),
                f"invalid {self.connector_name} record",
            ),
            attachment_count=attachment_count,
        )


class GmailSourceAdapter(_MailSourceAdapter):
    connector_name: MailConnectorName = cast(MailConnectorName, D4_GMAIL_SOURCE)
    resource_type = "mail_label"


class MicrosoftMailSourceAdapter(_MailSourceAdapter):
    connector_name: MailConnectorName = cast(MailConnectorName, D4_MICROSOFT_MAIL_SOURCE)
    resource_type = "mail_folder"


class DriveSourceAdapter:
    """Convert host-mediated Google Drive file responses into D4 source values."""

    connector_name: DriveConnectorName = cast(DriveConnectorName, D4_GOOGLE_DRIVE_SOURCE)

    def file_selection(self, *, connection_id: str, file_id: str) -> SourceResourceSelection:
        _require_connection_id(connection_id)
        _require_resource_token(file_id, "invalid google drive selection")
        return SourceResourceSelection(
            connector_name=D4_GOOGLE_DRIVE_SOURCE,
            connection_id=connection_id,
            resource_id=f"drive_file:{file_id}",
            resource_type="drive_file",
        )

    def page_from_rest(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> DriveFilePage:
        records = tuple(self.record_from_rest(value) for value in _require_values(values))
        return DriveFilePage(
            status=MailPageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[DriveFileRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection, D4_GOOGLE_DRIVE_SOURCE, "drive_file")
        _require_record_count(records, "invalid google drive records")
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(
                    self.intake(selection, record, privacy=privacy),
                    content_type="drive_file",
                )
                for record in records
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: DriveFileCheckpoint,
        page: DriveFilePage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[DriveFileCheckpoint, ConnectorRunReceipt]:
        imported_checkpoint, receipt = _import_page(
            checkpoint,
            page,
            intakes,
            capture_sink,
            connector_name=D4_GOOGLE_DRIVE_SOURCE,
            message="invalid google drive import",
        )
        return imported_checkpoint, receipt

    def rate_limited_page(self, *, retry_after_seconds: int) -> DriveFilePage:
        return DriveFilePage(
            status=MailPageStatus.RATE_LIMITED,
            retry_after_seconds=retry_after_seconds,
        )

    def needs_sign_in_page(self) -> DriveFilePage:
        return DriveFilePage(status=MailPageStatus.NEEDS_SIGN_IN)

    def not_allowed_page(self) -> DriveFilePage:
        return DriveFilePage(status=MailPageStatus.NOT_ALLOWED)

    def unsupported_attachment_page(self) -> DriveFilePage:
        return DriveFilePage(status=MailPageStatus.UNSUPPORTED_ATTACHMENT)

    def intake(
        self,
        selection: SourceResourceSelection,
        record: DriveFileRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not DriveFileRecord:
            raise ConnectorContractError("invalid google drive record")
        _require_drive_record_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D4_GOOGLE_DRIVE_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=f"file:{record.file_id}",
                revision_id=f"revision:{record.revision_id}",
            ),
            url=record.web_url,
            title=record.name,
            text=record.text,
            privacy=privacy,
        )

    def record_from_rest(self, value: Mapping[str, object]) -> DriveFileRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid google drive record")
        mime_type = _required_str(value.get("mime_type"), "invalid google drive record")
        if not (
            mime_type.startswith("text/")
            or mime_type
            in {
                "application/vnd.google-apps.document",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/pdf",
            }
        ):
            raise ConnectorContractError("unsupported google drive file")
        return DriveFileRecord(
            file_id=_required_str(value.get("id"), "invalid google drive record"),
            revision_id=_required_str(value.get("revision_id"), "invalid google drive record"),
            web_url=_required_url(value.get("web_url"), "invalid google drive record"),
            name=_required_str(value.get("name"), "invalid google drive record"),
            text=_body_or_title(
                value.get("text"),
                value.get("name"),
                "invalid google drive record",
            ),
            mime_type=mime_type,
        )


def _import_page[CheckpointT: _SelectedCheckpoint](
    checkpoint: CheckpointT,
    page: _SelectedPage,
    intakes: Sequence[SourceRecordIntake],
    capture_sink: ConnectorCaptureSink,
    *,
    connector_name: str,
    message: str,
) -> tuple[CheckpointT, ConnectorRunReceipt]:
    if page.status is MailPageStatus.RATE_LIMITED:
        return checkpoint, ConnectorRunReceipt.failed(
            connector_name,
            ConnectorFailureCode.RUNTIME_FAILED,
        )
    if page.status in {
        MailPageStatus.NEEDS_SIGN_IN,
        MailPageStatus.NOT_ALLOWED,
        MailPageStatus.UNSUPPORTED_ATTACHMENT,
    }:
        return checkpoint, ConnectorRunReceipt.failed(
            connector_name,
            ConnectorFailureCode.NOT_ALLOWED,
        )
    if not isinstance(intakes, Sequence) or isinstance(intakes, str):
        raise ConnectorContractError(message)
    if type(capture_sink) is not ConnectorCaptureSink:
        raise ConnectorContractError(message)
    preview = page.preview
    if preview is None or preview.selection != checkpoint.selection:
        raise ConnectorContractError(message)
    selected = tuple(record for record in preview.records if record.selected)
    if not selected:
        return checkpoint, ConnectorRunReceipt.empty(
            connector_name,
            metadata_count=len(preview.records),
        )
    intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
    if (
        len(intake_by_delivery) != len(intakes)
        or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
        or any(
            intake.key.connector_name != preview.selection.connector_name
            or intake.key.connection_id != preview.selection.connection_id
            or intake.key.resource_id != preview.selection.resource_id
            or intake.source_reference != record.source_reference
            for record, intake in zip(selected, intakes, strict=True)
        )
    ):
        raise ConnectorContractError(message)
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
    committed_delivery_ids = set(checkpoint.committed_delivery_ids)
    if not committed_revisions:
        committed = tuple(
            record.delivery_id
            for record in selected
            if record.delivery_id in committed_delivery_ids
        )
        if committed and len(committed) != len(selected):
            raise ConnectorContractError(f"invalid {connector_name} checkpoint")
        changed_intakes = tuple(intakes)
    else:
        changed_intakes = tuple(
            intake
            for intake in intakes
            if committed_revisions.get(intake.key.delivery_id()) != intake.key.revision_identity()
        )
    if not changed_intakes:
        return checkpoint, ConnectorRunReceipt.empty(
            connector_name,
            metadata_count=len(preview.records),
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
            selection=preview.selection,
            records=selected,
            next_cursor=preview.next_cursor,
        ),
        committed_delivery_ids=tuple(record.delivery_id for record in selected),
        committed_revision_identities=tuple(intake.key.revision_identity() for intake in intakes),
    )
    return (
        cast(CheckpointT, advanced),
        ConnectorRunReceipt(
            connector_name=connector_name,
            outcome=ConnectorOutcome.COMPLETED,
            failure_code=None,
            discovered_count=len(preview.records),
            fetched_count=len(selected),
            extracted_count=len(selected),
            submitted_count=len(receipts),
            stubbed_count=0,
            created_count=sum(1 for receipt in receipts if not receipt.duplicate),
            duplicate_count=sum(1 for receipt in receipts if receipt.duplicate),
            checkpoint_committed=True,
            metadata_count=len(preview.records),
        ),
    )


def _checkpoint_parts(value: object, message: str) -> _CheckpointParts:
    fields = {
        "committed_delivery_ids",
        "committed_revision_identities",
        "connector_name",
        "connection_id",
        "next_cursor",
        "resource_id",
        "resource_type",
        "schema_version",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ConnectorContractError(message)
    committed = value["committed_delivery_ids"]
    committed_revisions = value["committed_revision_identities"]
    if not isinstance(committed, list) or not isinstance(committed_revisions, list):
        raise ConnectorContractError(message)
    return {
        "schema_version": _typed_int(value["schema_version"], message),
        "selection": SourceResourceSelection(
            connector_name=_typed_str(value["connector_name"], message),
            connection_id=_typed_str(value["connection_id"], message),
            resource_id=_typed_str(value["resource_id"], message),
            resource_type=_typed_str(value["resource_type"], message),
        ),
        "next_cursor": _optional_str(value["next_cursor"], message),
        "committed_delivery_ids": tuple(_typed_str(item, message) for item in committed),
        "committed_revision_identities": tuple(
            _typed_str(item, message) for item in committed_revisions
        ),
    }


def _read_checkpoint(path: Path, message: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConnectorContractError(message) from error


def _save_checkpoint(
    store: _CheckpointStore,
    checkpoint: _SelectedCheckpoint,
    message: str,
) -> Path:
    store._root.mkdir(parents=True, exist_ok=True)
    path = store._path(checkpoint.selection)
    payload = json.dumps(checkpoint.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    with NamedTemporaryFile(
        "w",
        delete=False,
        dir=store._root,
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
        raise ConnectorContractError(message) from error
    return path


def _require_values(values: Sequence[Mapping[str, object]]) -> Sequence[Mapping[str, object]]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid source records")
    return values


def _require_record_count(records: Sequence[object], message: str) -> None:
    if not isinstance(records, Sequence) or isinstance(records, str):
        raise ConnectorContractError(message)
    if len(records) > _MAX_PREVIEW_RECORDS:
        raise ConnectorContractError(message)


def _require_selection(
    selection: SourceResourceSelection,
    connector_name: str,
    resource_type: str,
) -> None:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != connector_name
        or selection.resource_type != resource_type
    ):
        raise ConnectorContractError(f"invalid {connector_name} selection")


def _require_connection_id(connection_id: str) -> None:
    if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
        raise ConnectorContractError("invalid source connection")


def _require_resource_token(value: str, message: str) -> None:
    if type(value) is not str or _RESOURCE_TOKEN.fullmatch(value) is None:
        raise ConnectorContractError(message)


def _required_str(value: object, message: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ConnectorContractError(message)
    return value.strip()


def _required_url(value: object, message: str) -> str:
    text = _required_str(value, message)
    if _HOST_URL.fullmatch(text) is None:
        raise ConnectorContractError(message)
    return text


def _typed_int(value: object, message: str) -> int:
    if type(value) is not int:
        raise ConnectorContractError(message)
    return value


def _typed_str(value: object, message: str) -> str:
    if type(value) is not str:
        raise ConnectorContractError(message)
    return value


def _optional_str(value: object, message: str) -> str | None:
    if value is None:
        return None
    return _typed_str(value, message)


def _body_or_title(body: object, title: object, message: str) -> str:
    if body is None:
        return _required_str(title, message)
    if type(body) is not str or "\x00" in body:
        raise ConnectorContractError(message)
    return body.strip() or _required_str(title, message)


def _resource_value(selection: SourceResourceSelection, prefix: str) -> str:
    if not selection.resource_id.startswith(f"{prefix}:"):
        raise ConnectorContractError(f"invalid {selection.connector_name} selection")
    return selection.resource_id.removeprefix(f"{prefix}:")


def _require_mail_record_matches_selection(
    selection: SourceResourceSelection,
    record: MailRecord,
) -> None:
    _require_selection(
        selection,
        record.connector_name,
        "mail_label" if record.connector_name == D4_GMAIL_SOURCE else "mail_folder",
    )
    if record.resource_id != _resource_value(selection, selection.resource_type):
        raise ConnectorContractError(f"invalid {record.connector_name} record")
    if record.web_url.startswith("https://") is False:
        raise ConnectorContractError(f"invalid {record.connector_name} record")


def _require_drive_record_matches_selection(
    selection: SourceResourceSelection,
    record: DriveFileRecord,
) -> None:
    _require_selection(selection, D4_GOOGLE_DRIVE_SOURCE, "drive_file")
    selected_file_id = _resource_value(selection, "drive_file")
    if record.file_id != selected_file_id:
        raise ConnectorContractError("invalid google drive record")
    if "drive.google.com" not in record.web_url:
        raise ConnectorContractError("invalid google drive record")
