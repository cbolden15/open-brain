"""D5.3 adapter values for selected Notion and Confluence content."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypedDict

from open_brain_engine.capture.redaction import has_redaction_finding
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
    D5_CONFLUENCE_SOURCE,
    D5_NOTION_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "WorkspaceContentCheckpoint",
    "WorkspaceContentCheckpointStore",
    "WorkspaceContentPage",
    "WorkspaceContentPageStatus",
    "WorkspaceContentRecord",
    "WorkspaceContentSourceAdapter",
]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CONNECTOR = re.compile(r"(notion|confluence)")
_CONTENT_ID = re.compile(r"(?:notion|confluence):[A-Za-z0-9][A-Za-z0-9._:@/-]{0,180}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_MAX_PREVIEW_RECORDS = 25
_RESOURCE_TYPES = {
    D5_CONFLUENCE_SOURCE: {"cloud_page", "cloud_space"},
    D5_NOTION_SOURCE: {"data_source", "page"},
}
_CONTENT_TYPES = {
    D5_CONFLUENCE_SOURCE: {"comment", "page"},
    D5_NOTION_SOURCE: {"block", "comment", "page"},
}
_PROVIDER_HOSTS = {
    D5_CONFLUENCE_SOURCE: "confluence.local.openbrain.invalid",
    D5_NOTION_SOURCE: "notion.local.openbrain.invalid",
}


class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    observed_page_cursors: tuple[str, ...]
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class WorkspaceContentPageStatus(StrEnum):
    """Provider-observed result for one bounded selected workspace content page."""

    READY = "ready"
    NOT_ALLOWED = "not_allowed"
    TOKEN_INVALIDATED = "token_invalidated"
    UNSUPPORTED_RESOURCE = "unsupported_resource"


@dataclass(frozen=True, slots=True)
class WorkspaceContentRecord:
    """A selected Notion or Confluence page, block, or comment."""

    connector_name: str
    content_id: str
    revision_id: str
    content_type: str
    title: str
    body: str
    parent_id: str | None = None
    source_link: str | None = None

    def __post_init__(self) -> None:
        connector_name = _required_connector(self.connector_name)
        content_id = _required_content_id(self.content_id, connector_name)
        revision_id = _required_token(self.revision_id, "invalid workspace content")
        content_type = _required_content_type(self.content_type, connector_name)
        title = _body_or_fallback(self.title, "Selected workspace content")
        body = _required_body(self.body)
        parent_id = None if self.parent_id is None else _required_content_id(
            self.parent_id, connector_name
        )
        source_link = _optional_https_url(self.source_link)
        _require_redaction_clean(title)
        _require_redaction_clean(body)
        if source_link is not None:
            _require_redaction_clean(source_link)
        object.__setattr__(self, "connector_name", connector_name)
        object.__setattr__(self, "content_id", content_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "parent_id", parent_id)
        object.__setattr__(self, "source_link", source_link)


@dataclass(frozen=True, slots=True)
class WorkspaceContentPage:
    status: WorkspaceContentPageStatus
    preview: SourcePreviewPage | None = None

    def __post_init__(self) -> None:
        try:
            status = WorkspaceContentPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid workspace content page") from error
        if status is WorkspaceContentPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage:
                raise ConnectorContractError("invalid workspace content page")
        elif self.preview is not None:
            raise ConnectorContractError("invalid workspace content page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class WorkspaceContentCheckpoint:
    """Durable selected-workspace cursor committed after capture acknowledgements."""

    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...] = ()
    observed_page_cursors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        connector_name = _selection_connector(self.selection)
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.selection.resource_type not in _RESOURCE_TYPES[connector_name]
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str or not value.startswith(f"connector.{connector_name}.")
                for value in self.committed_delivery_ids
            )
            or len(self.committed_delivery_ids) != len(set(self.committed_delivery_ids))
            or not isinstance(self.committed_revision_identities, tuple)
            or any(
                type(value) is not str or _CURSOR.fullmatch(value) is None
                for value in self.committed_revision_identities
            )
            or not isinstance(self.observed_page_cursors, tuple)
            or any(
                type(value) is not str or _CURSOR.fullmatch(value) is None
                for value in self.observed_page_cursors
            )
            or len(self.observed_page_cursors) != len(set(self.observed_page_cursors))
            or (
                self.committed_revision_identities
                and len(self.committed_revision_identities) != len(self.committed_delivery_ids)
            )
        ):
            raise ConnectorContractError("invalid workspace content checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> WorkspaceContentCheckpoint:
        return cls(1, selection, None, ())

    @classmethod
    def from_dict(cls, value: object) -> WorkspaceContentCheckpoint:
        return cls(**_checkpoint_parts(value))

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> WorkspaceContentCheckpoint:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError("invalid workspace content checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        revisions = tuple(committed_revision_identities)
        if tuple(committed_delivery_ids) != expected or len(revisions) != len(expected):
            raise ConnectorContractError("invalid workspace content checkpoint")
        committed_by_delivery = dict(
            zip(
                self.committed_delivery_ids,
                self.committed_revision_identities,
                strict=True,
            )
        )
        for delivery_id, revision_identity in zip(expected, revisions, strict=True):
            committed_by_delivery[delivery_id] = revision_identity
        return WorkspaceContentCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=page.next_cursor,
            committed_delivery_ids=tuple(committed_by_delivery),
            committed_revision_identities=tuple(committed_by_delivery.values()),
            observed_page_cursors=_append_cursor(self.observed_page_cursors, page.next_cursor),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "committed_delivery_ids": list(self.committed_delivery_ids),
            "committed_revision_identities": list(self.committed_revision_identities),
            "connector_name": self.selection.connector_name,
            "connection_id": self.selection.connection_id,
            "next_cursor": self.next_cursor,
            "observed_page_cursors": list(self.observed_page_cursors),
            "resource_id": self.selection.resource_id,
            "resource_type": self.selection.resource_type,
            "schema_version": self.schema_version,
        }


class WorkspaceContentCheckpointStore:
    """Goal-owned JSON checkpoint persistence for selected D5.3 workspace content."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid workspace content checkpoint store")
        self._root = root

    def _path(self, selection: SourceResourceSelection) -> Path:
        connector_name = _selection_connector(selection)
        digest = SourceRecordKey(
            connector_name=connector_name,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id=f"workspace:{selection.resource_type}",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"workspace-content-{connector_name}-{digest}.json"

    def load(self, selection: SourceResourceSelection) -> WorkspaceContentCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return WorkspaceContentCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid workspace content checkpoint store") from error
        checkpoint = WorkspaceContentCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid workspace content checkpoint store")
        return checkpoint

    def save(self, checkpoint: WorkspaceContentCheckpoint) -> Path:
        if type(checkpoint) is not WorkspaceContentCheckpoint:
            raise ConnectorContractError("invalid workspace content checkpoint store")
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
            raise ConnectorContractError("invalid workspace content checkpoint store") from error
        return path


class WorkspaceContentSourceAdapter:
    """Convert selected Notion or Confluence payloads into D5.3 source values."""

    def resource_selection(
        self,
        *,
        connector_name: str,
        connection_id: str,
        resource_id: str,
        resource_type: str,
    ) -> SourceResourceSelection:
        connector_name = _required_connector(connector_name)
        if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
            raise ConnectorContractError("invalid workspace content source connection")
        if type(resource_id) is not str or not resource_id.startswith(f"{connector_name}:"):
            raise ConnectorContractError("invalid workspace content source selection")
        if resource_type not in _RESOURCE_TYPES[connector_name]:
            raise ConnectorContractError("invalid workspace content source selection")
        return SourceResourceSelection(
            connector_name=connector_name,
            connection_id=connection_id,
            resource_id=resource_id,
            resource_type=resource_type,
        )

    def page_from_items(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        selected_content_ids: Sequence[str],
        next_cursor: str | None = None,
    ) -> WorkspaceContentPage:
        selected_ids = _required_selected_content(selection, selected_content_ids)
        records = tuple(
            self.record_from_item(value)
            for value in _require_values(values)
            if _matches_selected_content(selection, value, selected_ids)
        )
        if not records:
            raise ConnectorContractError("invalid workspace contents")
        return WorkspaceContentPage(
            status=WorkspaceContentPageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[WorkspaceContentRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _selection_connector(selection)
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid workspace contents")
        intakes = tuple(self.intake(selection, record, privacy=privacy) for record in records)
        if len(intakes) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid workspace contents")
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(
                    intake,
                    content_type=record.content_type,
                )
                for record, intake in zip(records, intakes, strict=True)
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: WorkspaceContentCheckpoint,
        page: WorkspaceContentPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[WorkspaceContentCheckpoint, ConnectorRunReceipt]:
        connector_name = _selection_connector(checkpoint.selection)
        if page.status is WorkspaceContentPageStatus.UNSUPPORTED_RESOURCE:
            return checkpoint, ConnectorRunReceipt.failed(
                connector_name,
                ConnectorFailureCode.UNSUPPORTED_CAPABILITY,
            )
        if page.status in {
            WorkspaceContentPageStatus.NOT_ALLOWED,
            WorkspaceContentPageStatus.TOKEN_INVALIDATED,
        }:
            return checkpoint, ConnectorRunReceipt.failed(
                connector_name,
                ConnectorFailureCode.NOT_ALLOWED,
            )
        if (
            type(checkpoint) is not WorkspaceContentCheckpoint
            or type(page.preview) is not SourcePreviewPage
            or page.preview.selection != checkpoint.selection
            or not isinstance(intakes, Sequence)
            or isinstance(intakes, str)
            or type(capture_sink) is not ConnectorCaptureSink
        ):
            raise ConnectorContractError("invalid workspace content import")
        selected = tuple(record for record in page.preview.records if record.selected)
        if not selected:
            return checkpoint, ConnectorRunReceipt.empty(
                connector_name,
                metadata_count=len(page.preview.records),
            )
        intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
        if (
            len(intake_by_delivery) != len(intakes)
            or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
            or any(
                intake.key.connector_name != connector_name
                or intake.key.connection_id != page.preview.selection.connection_id
                or intake.key.resource_id != page.preview.selection.resource_id
                or intake.source_reference != record.source_reference
                for record, intake in zip(selected, intakes, strict=True)
            )
        ):
            raise ConnectorContractError("invalid workspace content import")
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
        selected_delivery_ids = tuple(record.delivery_id for record in selected)
        selected_revision_identities = tuple(intake.key.revision_identity() for intake in intakes)
        changed_intakes = tuple(
            intake
            for intake in intakes
            if committed_revisions.get(intake.key.delivery_id()) != intake.key.revision_identity()
        )
        if not changed_intakes:
            duplicate_only_known_page = all(
                delivery_id in checkpoint.committed_delivery_ids
                for delivery_id in selected_delivery_ids
            )
            stale_duplicate_page = (
                page.preview.next_cursor is None
                or page.preview.next_cursor in checkpoint.observed_page_cursors
            )
            if (
                selected_delivery_ids != checkpoint.committed_delivery_ids
                and (not duplicate_only_known_page or stale_duplicate_page)
            ):
                return checkpoint, ConnectorRunReceipt.empty(
                    connector_name,
                    metadata_count=len(page.preview.records),
                )
            advanced = checkpoint.advance(
                SourcePreviewPage(
                    selection=page.preview.selection,
                    records=selected,
                    next_cursor=page.preview.next_cursor,
                ),
                committed_delivery_ids=selected_delivery_ids,
                committed_revision_identities=selected_revision_identities,
            )
            if advanced == checkpoint:
                return checkpoint, ConnectorRunReceipt.empty(
                    connector_name,
                    metadata_count=len(page.preview.records),
                )
            return (
                advanced,
                ConnectorRunReceipt(
                    connector_name=connector_name,
                    outcome=ConnectorOutcome.COMPLETED,
                    failure_code=None,
                    discovered_count=len(page.preview.records),
                    fetched_count=len(selected),
                    extracted_count=len(selected),
                    submitted_count=0,
                    stubbed_count=0,
                    created_count=0,
                    duplicate_count=0,
                    checkpoint_committed=True,
                    metadata_count=len(page.preview.records),
                ),
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
                next_cursor=_advance_cursor(checkpoint, page.preview.next_cursor),
            ),
            committed_delivery_ids=selected_delivery_ids,
            committed_revision_identities=selected_revision_identities,
        )
        return (
            advanced,
            ConnectorRunReceipt(
                connector_name=connector_name,
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

    def not_allowed_page(self) -> WorkspaceContentPage:
        return WorkspaceContentPage(status=WorkspaceContentPageStatus.NOT_ALLOWED)

    def token_invalidated_page(self) -> WorkspaceContentPage:
        return WorkspaceContentPage(status=WorkspaceContentPageStatus.TOKEN_INVALIDATED)

    def unsupported_resource_page(self) -> WorkspaceContentPage:
        return WorkspaceContentPage(status=WorkspaceContentPageStatus.UNSUPPORTED_RESOURCE)

    def intake(
        self,
        selection: SourceResourceSelection,
        record: WorkspaceContentRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not WorkspaceContentRecord:
            raise ConnectorContractError("invalid workspace content")
        _require_record_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=selection.connector_name,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=record.content_id,
                revision_id=record.revision_id,
            ),
            url=_source_reference(record),
            title=record.title,
            text=_record_text(record),
            privacy=privacy,
        )

    def record_from_item(self, value: Mapping[str, object]) -> WorkspaceContentRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid workspace content")
        connector_name = _required_connector(value.get("connector_name"))
        if value.get("source_kind") != connector_name:
            raise ConnectorContractError("invalid workspace content")
        if value.get("content_secret_scan") != "clean":
            raise ConnectorContractError("invalid workspace content")
        return WorkspaceContentRecord(
            connector_name=connector_name,
            content_id=_required_content_id(value.get("content_id"), connector_name),
            revision_id=_required_token(value.get("revision_id"), "invalid workspace content"),
            content_type=_required_content_type(value.get("content_type"), connector_name),
            title=_body_or_fallback(value.get("title"), "Selected workspace content"),
            body=_required_body(value.get("body")),
            parent_id=_optional_content_id(value.get("parent_id"), connector_name),
            source_link=_optional_https_url(value.get("source_link")),
        )


def _checkpoint_parts(value: object) -> _CheckpointParts:
    required_fields = {
        "committed_delivery_ids",
        "committed_revision_identities",
        "connector_name",
        "connection_id",
        "next_cursor",
        "resource_id",
        "resource_type",
        "schema_version",
    }
    optional_fields = {"observed_page_cursors"}
    if (
        not isinstance(value, dict)
        or not required_fields.issubset(value)
        or not set(value).issubset(required_fields | optional_fields)
    ):
        raise ConnectorContractError("invalid workspace content checkpoint")
    committed = value["committed_delivery_ids"]
    committed_revisions = value["committed_revision_identities"]
    observed_cursors = value.get("observed_page_cursors", [])
    if (
        not isinstance(committed, list)
        or not isinstance(committed_revisions, list)
        or not isinstance(observed_cursors, list)
    ):
        raise ConnectorContractError("invalid workspace content checkpoint")
    return {
        "schema_version": _typed_int(value["schema_version"]),
        "selection": SourceResourceSelection(
            connector_name=_typed_str(value["connector_name"]),
            connection_id=_typed_str(value["connection_id"]),
            resource_id=_typed_str(value["resource_id"]),
            resource_type=_typed_str(value["resource_type"]),
        ),
        "next_cursor": _optional_str(value["next_cursor"]),
        "observed_page_cursors": tuple(_typed_str(item) for item in observed_cursors),
        "committed_delivery_ids": tuple(_typed_str(item) for item in committed),
        "committed_revision_identities": tuple(_typed_str(item) for item in committed_revisions),
    }


def _append_cursor(cursors: tuple[str, ...], next_cursor: str | None) -> tuple[str, ...]:
    if next_cursor is None or next_cursor in cursors:
        return cursors
    return (*cursors, next_cursor)


def _require_values(values: Sequence[Mapping[str, object]]) -> Sequence[Mapping[str, object]]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid workspace contents")
    return values


def _selection_connector(selection: SourceResourceSelection) -> str:
    if type(selection) is not SourceResourceSelection:
        raise ConnectorContractError("invalid workspace content source selection")
    connector_name = _required_connector(selection.connector_name)
    if selection.resource_type not in _RESOURCE_TYPES[connector_name]:
        raise ConnectorContractError("invalid workspace content source selection")
    return connector_name


def _required_connector(value: object) -> str:
    if type(value) is not str or _CONNECTOR.fullmatch(value) is None:
        raise ConnectorContractError("invalid workspace content source")
    return value


def _required_content_id(value: object, connector_name: str) -> str:
    if (
        type(value) is not str
        or _CONTENT_ID.fullmatch(value) is None
        or not value.startswith(f"{connector_name}:")
    ):
        raise ConnectorContractError("invalid workspace content")
    return value


def _optional_content_id(value: object, connector_name: str) -> str | None:
    if value is None:
        return None
    return _required_content_id(value, connector_name)


def _required_content_type(value: object, connector_name: str) -> str:
    if type(value) is not str or value not in _CONTENT_TYPES[connector_name]:
        raise ConnectorContractError("invalid workspace content")
    return value


def _required_token(value: object, error_code: str) -> str:
    if type(value) is not str or _CURSOR.fullmatch(value) is None:
        raise ConnectorContractError(error_code)
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _typed_str(value)


def _typed_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid workspace content checkpoint")
    return value


def _typed_str(value: object) -> str:
    if type(value) is not str:
        raise ConnectorContractError("invalid workspace content checkpoint")
    return value


def _body_or_fallback(value: object, fallback: str) -> str:
    if type(value) is str:
        stripped = value.strip()
        if stripped:
            return stripped[:200]
    return fallback


def _required_body(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ConnectorContractError("invalid workspace content")
    return value.strip()


def _optional_https_url(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value.startswith("https://")
        or "\x00" in value
        or any(character.isspace() for character in value)
    ):
        raise ConnectorContractError("invalid workspace content")
    return value


def _require_redaction_clean(value: str) -> None:
    if has_redaction_finding(value):
        raise ConnectorContractError("invalid workspace content")


def _required_selected_content(
    selection: SourceResourceSelection, selected_content_ids: Sequence[str]
) -> tuple[str, ...]:
    connector_name = _selection_connector(selection)
    if not isinstance(selected_content_ids, Sequence) or isinstance(selected_content_ids, str):
        raise ConnectorContractError("invalid workspace contents")
    selected = tuple(_required_content_id(value, connector_name) for value in selected_content_ids)
    if not selected or len(selected) != len(set(selected)):
        raise ConnectorContractError("invalid workspace contents")
    return selected


def _matches_selected_content(
    selection: SourceResourceSelection,
    value: Mapping[str, object],
    selected_content_ids: tuple[str, ...],
) -> bool:
    connector_name = _selection_connector(selection)
    content_id = value.get("content_id")
    parent_id = value.get("parent_id")
    return (
        value.get("connector_name") == connector_name
        and (
            content_id in selected_content_ids
            or (
                selection.resource_type in {"page", "cloud_page"}
                and parent_id in selected_content_ids
            )
        )
    )


def _require_record_matches_selection(
    selection: SourceResourceSelection, record: WorkspaceContentRecord
) -> None:
    connector_name = _selection_connector(selection)
    if (
        record.connector_name != connector_name
        or not record.content_id.startswith(f"{connector_name}:")
        or (
            selection.resource_type in {"page", "cloud_page"}
            and record.content_id != selection.resource_id
            and record.parent_id != selection.resource_id
        )
        or (
            selection.resource_type in {"data_source", "cloud_space"}
            and record.parent_id != selection.resource_id
            and record.content_id != selection.resource_id
        )
    ):
        raise ConnectorContractError("invalid workspace content")


def _source_reference(record: WorkspaceContentRecord) -> str:
    token = sha256(f"{record.connector_name}\x1f{record.content_id}".encode()).hexdigest()
    return f"https://{_PROVIDER_HOSTS[record.connector_name]}/{token}"


def _record_text(record: WorkspaceContentRecord) -> str:
    lines = [record.title, "", record.body]
    if record.source_link is not None:
        lines.extend(("", f"Source link: {record.source_link}"))
    return "\n".join(lines).strip()


def _advance_cursor(checkpoint: WorkspaceContentCheckpoint, next_cursor: str | None) -> str | None:
    if (
        next_cursor is not None
        and next_cursor in checkpoint.observed_page_cursors
        and next_cursor != checkpoint.next_cursor
    ):
        return checkpoint.next_cursor
    return next_cursor
