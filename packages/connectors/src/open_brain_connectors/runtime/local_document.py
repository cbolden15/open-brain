"""D5.1 adapter values for explicitly selected local documents."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TypedDict

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.connectors import (
    ConnectorCaptureSink,
    ConnectorContractError,
    ConnectorFailureCode,
    ConnectorOutcome,
    ConnectorRunReceipt,
    capture_outcome_is_duplicate,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import (
    D5_LOCAL_DOCUMENT_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "LocalDocumentCheckpoint",
    "LocalDocumentCheckpointStore",
    "LocalDocumentPage",
    "LocalDocumentPageStatus",
    "LocalDocumentRecord",
    "LocalDocumentSourceAdapter",
]

LocalDocumentContentType = Literal["document_text"]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_DOCUMENT_ID = re.compile(r"document:[0-9a-f]{16,64}")
_FILE_KIND = re.compile(r"(docx_file|text_pdf)")
_MAX_PREVIEW_RECORDS = 25


class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class LocalDocumentPageStatus(StrEnum):
    """Host-observed result for one bounded selected local document page."""

    READY = "ready"
    NOT_ALLOWED = "not_allowed"
    UNSUPPORTED_FORMAT = "unsupported_format"
    ENCRYPTED = "encrypted"


@dataclass(frozen=True, slots=True)
class LocalDocumentRecord:
    """A selected local document after bounded host extraction."""

    document_id: str
    revision_id: str
    file_kind: str
    title: str
    text: str
    source_reference: str | None = None

    def __post_init__(self) -> None:
        document_id = _required_document_id(self.document_id)
        revision_id = _required_token(self.revision_id, "invalid local document record")
        file_kind = _required_file_kind(self.file_kind)
        title = _body_or_fallback(self.title, "Selected local document")
        text = _body_or_fallback(self.text, title)
        _require_redaction_clean(title)
        _require_redaction_clean(text)
        if self.source_reference is not None:
            raise ConnectorContractError("invalid local document record")
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "file_kind", file_kind)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "source_reference", None)


@dataclass(frozen=True, slots=True)
class LocalDocumentPage:
    status: LocalDocumentPageStatus
    preview: SourcePreviewPage | None = None

    def __post_init__(self) -> None:
        try:
            status = LocalDocumentPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid local document page") from error
        if status is LocalDocumentPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage:
                raise ConnectorContractError("invalid local document page")
        elif self.preview is not None:
            raise ConnectorContractError("invalid local document page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class LocalDocumentCheckpoint:
    """Durable selected-document cursor committed after capture acknowledgements."""

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
            or self.selection.connector_name != D5_LOCAL_DOCUMENT_SOURCE
            or self.selection.resource_type not in {"docx_file", "text_pdf"}
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str or not value.startswith("connector.local_document.")
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
            raise ConnectorContractError("invalid local document checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> LocalDocumentCheckpoint:
        return cls(1, selection, None, ())

    @classmethod
    def from_dict(cls, value: object) -> LocalDocumentCheckpoint:
        return cls(**_checkpoint_parts(value))

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> LocalDocumentCheckpoint:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError("invalid local document checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        revisions = tuple(committed_revision_identities)
        if tuple(committed_delivery_ids) != expected or len(revisions) != len(expected):
            raise ConnectorContractError("invalid local document checkpoint")
        return LocalDocumentCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=page.next_cursor,
            committed_delivery_ids=expected,
            committed_revision_identities=revisions,
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


class LocalDocumentCheckpointStore:
    """Goal-owned JSON checkpoint persistence for selected local documents."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid local document checkpoint store")
        self._root = root

    def _path(self, selection: SourceResourceSelection) -> Path:
        _require_selection(selection)
        digest = SourceRecordKey(
            connector_name=D5_LOCAL_DOCUMENT_SOURCE,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id="document",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"local-document-{digest}.json"

    def load(self, selection: SourceResourceSelection) -> LocalDocumentCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return LocalDocumentCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid local document checkpoint store") from error
        checkpoint = LocalDocumentCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid local document checkpoint store")
        return checkpoint

    def save(self, checkpoint: LocalDocumentCheckpoint) -> Path:
        if type(checkpoint) is not LocalDocumentCheckpoint:
            raise ConnectorContractError("invalid local document checkpoint store")
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
            raise ConnectorContractError("invalid local document checkpoint store") from error
        return path


class LocalDocumentSourceAdapter:
    """Convert explicit local document payloads into D5.1 source values."""

    def file_selection(
        self,
        *,
        connection_id: str,
        document_id: str,
        file_kind: str,
    ) -> SourceResourceSelection:
        if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
            raise ConnectorContractError("invalid local document source connection")
        document_id = _required_document_id(document_id)
        file_kind = _required_file_kind(file_kind)
        return SourceResourceSelection(
            connector_name=D5_LOCAL_DOCUMENT_SOURCE,
            connection_id=connection_id,
            resource_id=document_id,
            resource_type=file_kind,
        )

    def page_from_documents(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        selected_document_ids: Sequence[str],
        next_cursor: str | None = None,
    ) -> LocalDocumentPage:
        selected_ids = _required_selected_documents(selected_document_ids)
        selected_values = tuple(
            value
            for value in _require_values(values)
            if _matches_selected_document(selection, value, selected_ids)
        )
        records = tuple(self.record_from_extracted(value) for value in selected_values)
        if not records:
            raise ConnectorContractError("invalid local document records")
        return LocalDocumentPage(
            status=LocalDocumentPageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[LocalDocumentRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection)
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid local document records")
        intakes = tuple(self.intake(selection, record, privacy=privacy) for record in records)
        if len(intakes) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid local document records")
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(intake, content_type="document_text")
                for intake in intakes
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: LocalDocumentCheckpoint,
        page: LocalDocumentPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[LocalDocumentCheckpoint, ConnectorRunReceipt]:
        if page.status in {
            LocalDocumentPageStatus.NOT_ALLOWED,
            LocalDocumentPageStatus.UNSUPPORTED_FORMAT,
            LocalDocumentPageStatus.ENCRYPTED,
        }:
            return checkpoint, ConnectorRunReceipt.failed(
                D5_LOCAL_DOCUMENT_SOURCE,
                ConnectorFailureCode.NOT_ALLOWED,
            )
        if (
            type(checkpoint) is not LocalDocumentCheckpoint
            or type(page.preview) is not SourcePreviewPage
            or page.preview.selection != checkpoint.selection
            or not isinstance(intakes, Sequence)
            or isinstance(intakes, str)
            or type(capture_sink) is not ConnectorCaptureSink
        ):
            raise ConnectorContractError("invalid local document import")
        selected = tuple(record for record in page.preview.records if record.selected)
        if not selected:
            return checkpoint, ConnectorRunReceipt.empty(
                D5_LOCAL_DOCUMENT_SOURCE,
                metadata_count=len(page.preview.records),
            )
        intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
        if (
            len(intake_by_delivery) != len(intakes)
            or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
            or any(
                intake.key.connector_name != D5_LOCAL_DOCUMENT_SOURCE
                or intake.key.connection_id != page.preview.selection.connection_id
                or intake.key.resource_id != page.preview.selection.resource_id
                or intake.source_reference != record.source_reference
                for record, intake in zip(selected, intakes, strict=True)
            )
        ):
            raise ConnectorContractError("invalid local document import")
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
                D5_LOCAL_DOCUMENT_SOURCE,
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
                connector_name=D5_LOCAL_DOCUMENT_SOURCE,
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=len(page.preview.records),
                fetched_count=len(selected),
                extracted_count=len(selected),
                submitted_count=len(receipts),
                stubbed_count=0,
                created_count=sum(
                    1 for receipt in receipts if not capture_outcome_is_duplicate(receipt)
                ),
                duplicate_count=sum(
                    1 for receipt in receipts if capture_outcome_is_duplicate(receipt)
                ),
                checkpoint_committed=True,
                metadata_count=len(page.preview.records),
            ),
        )

    def not_allowed_page(self) -> LocalDocumentPage:
        return LocalDocumentPage(status=LocalDocumentPageStatus.NOT_ALLOWED)

    def unsupported_format_page(self) -> LocalDocumentPage:
        return LocalDocumentPage(status=LocalDocumentPageStatus.UNSUPPORTED_FORMAT)

    def encrypted_page(self) -> LocalDocumentPage:
        return LocalDocumentPage(status=LocalDocumentPageStatus.ENCRYPTED)

    def intake(
        self,
        selection: SourceResourceSelection,
        record: LocalDocumentRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not LocalDocumentRecord:
            raise ConnectorContractError("invalid local document record")
        _require_record_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D5_LOCAL_DOCUMENT_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id="document_text",
                revision_id=record.revision_id,
            ),
            url=_source_reference(record),
            title=record.title,
            text=record.text,
            privacy=privacy,
        )

    def record_from_extracted(self, value: Mapping[str, object]) -> LocalDocumentRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid local document record")
        if value.get("source_kind") != "local_document":
            raise ConnectorContractError("invalid local document record")
        if "source_reference" in value and value["source_reference"] is not None:
            raise ConnectorContractError("invalid local document record")
        text = _body_or_fallback(value.get("text"), "Selected local document")
        title = _body_or_fallback(value.get("title"), "Selected local document")
        if value.get("text_secret_scan") != "clean":
            raise ConnectorContractError("invalid local document record")
        _require_redaction_clean(title)
        _require_redaction_clean(text)
        return LocalDocumentRecord(
            document_id=_required_document_id(value.get("document_id")),
            revision_id=_required_token(value.get("revision_id"), "invalid local document record"),
            file_kind=_required_file_kind(value.get("file_kind")),
            title=title,
            text=text,
        )


def _checkpoint_parts(value: object) -> _CheckpointParts:
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
        raise ConnectorContractError("invalid local document checkpoint")
    committed = value["committed_delivery_ids"]
    committed_revisions = value["committed_revision_identities"]
    if not isinstance(committed, list) or not isinstance(committed_revisions, list):
        raise ConnectorContractError("invalid local document checkpoint")
    return {
        "schema_version": _typed_int(value["schema_version"]),
        "selection": SourceResourceSelection(
            connector_name=_typed_str(value["connector_name"]),
            connection_id=_typed_str(value["connection_id"]),
            resource_id=_typed_str(value["resource_id"]),
            resource_type=_typed_str(value["resource_type"]),
        ),
        "next_cursor": _optional_str(value["next_cursor"]),
        "committed_delivery_ids": tuple(_typed_str(item) for item in committed),
        "committed_revision_identities": tuple(_typed_str(item) for item in committed_revisions),
    }


def _require_values(values: Sequence[Mapping[str, object]]) -> Sequence[Mapping[str, object]]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid local document records")
    return values


def _required_selected_documents(values: Sequence[str]) -> set[str]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid local document records")
    selected = tuple(_required_document_id(value) for value in values)
    if not selected:
        raise ConnectorContractError("invalid local document records")
    return set(selected)


def _matches_selected_document(
    selection: SourceResourceSelection,
    value: Mapping[str, object],
    selected_document_ids: set[str],
) -> bool:
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid local document record")
    document_id = _required_document_id(value.get("document_id"))
    if document_id != selection.resource_id or document_id not in selected_document_ids:
        return False
    return _required_file_kind(value.get("file_kind")) == selection.resource_type


def _require_selection(selection: SourceResourceSelection) -> None:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != D5_LOCAL_DOCUMENT_SOURCE
        or selection.resource_type not in {"docx_file", "text_pdf"}
        or _DOCUMENT_ID.fullmatch(selection.resource_id) is None
    ):
        raise ConnectorContractError("invalid local document selection")


def _require_record_matches_selection(
    selection: SourceResourceSelection,
    record: LocalDocumentRecord,
) -> None:
    _require_selection(selection)
    if record.document_id != selection.resource_id or record.file_kind != selection.resource_type:
        raise ConnectorContractError("invalid local document record")


def _source_reference(record: LocalDocumentRecord) -> str:
    document_token = record.document_id.removeprefix("document:")
    return f"https://local.openbrain.invalid/documents/{document_token}"


def _required_document_id(value: object) -> str:
    text = _required_token(value, "invalid local document record")
    if _DOCUMENT_ID.fullmatch(text) is None:
        raise ConnectorContractError("invalid local document record")
    return text


def _required_file_kind(value: object) -> str:
    text = _required_token(value, "invalid local document record")
    if _FILE_KIND.fullmatch(text) is None:
        raise ConnectorContractError("invalid local document record")
    return text


def _typed_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid local document checkpoint")
    return value


def _typed_str(value: object) -> str:
    if type(value) is not str:
        raise ConnectorContractError("invalid local document checkpoint")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _typed_str(value)


def _required_token(value: object, message: str) -> str:
    if type(value) is not str or _CURSOR.fullmatch(value) is None:
        raise ConnectorContractError(message)
    return value


def _body_or_fallback(body: object, fallback: str) -> str:
    if body is None:
        return fallback
    if type(body) is not str or "\x00" in body:
        raise ConnectorContractError("invalid local document record")
    return body.strip() or fallback


def _require_redaction_clean(body: str) -> None:
    try:
        redaction_found = has_redaction_finding(body)
    except ValueError as error:
        raise ConnectorContractError("invalid local document record") from error
    if redaction_found:
        raise ConnectorContractError("invalid local document record")
