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
from urllib import parse

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
    "WorkspaceAuthProfile",
    "WorkspaceContentCheckpoint",
    "WorkspaceContentCheckpointStore",
    "WorkspaceContentPage",
    "WorkspaceContentPageStatus",
    "WorkspaceContentRecord",
    "WorkspaceContentSourceAdapter",
    "workspace_auth_profiles",
]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CONNECTOR = re.compile(r"(notion|confluence)")
_CONTENT_ID = re.compile(r"(?:notion|confluence):[A-Za-z0-9][A-Za-z0-9._:@/-]{0,180}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_MAX_PREVIEW_RECORDS = 25
_ARCHITECTURE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,180}")
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


@dataclass(frozen=True, slots=True)
class WorkspaceAuthProfile:
    """Reviewed D5.3 provider auth architecture without credentials or live tokens."""

    schema_version: int
    connector_name: str
    deployment: str
    oauth_architecture: str
    requested_scopes: tuple[str, ...]
    token_exchange_location: str
    client_secret_in_desktop_bundle: bool
    hosted_relay_authorized: bool
    owner_setup_required: bool
    public_onboarding_status: str

    def __post_init__(self) -> None:
        connector_name = _required_connector(self.connector_name)
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.deployment) is not str
            or not self.deployment
            or len(self.deployment) > 120
            or type(self.oauth_architecture) is not str
            or _ARCHITECTURE.fullmatch(self.oauth_architecture) is None
            or not isinstance(self.requested_scopes, tuple)
            or not self.requested_scopes
            or len(self.requested_scopes) > 8
            or any(
                type(scope) is not str or _SCOPE.fullmatch(scope) is None
                for scope in self.requested_scopes
            )
            or type(self.token_exchange_location) is not str
            or not self.token_exchange_location
            or len(self.token_exchange_location) > 160
            or type(self.client_secret_in_desktop_bundle) is not bool
            or type(self.hosted_relay_authorized) is not bool
            or type(self.owner_setup_required) is not bool
            or not self.owner_setup_required
            or type(self.public_onboarding_status) is not str
            or not self.public_onboarding_status
            or len(self.public_onboarding_status) > 200
        ):
            raise ConnectorContractError("invalid workspace auth profile")
        if connector_name == D5_NOTION_SOURCE and (
            self.oauth_architecture != "confidential_authorization_code"
            or self.client_secret_in_desktop_bundle
            or self.hosted_relay_authorized
        ):
            raise ConnectorContractError("invalid workspace auth profile")
        if connector_name == D5_CONFLUENCE_SOURCE and (
            self.oauth_architecture != "authorization_code_pkce"
            or self.client_secret_in_desktop_bundle
            or self.hosted_relay_authorized
        ):
            raise ConnectorContractError("invalid workspace auth profile")
        object.__setattr__(self, "connector_name", connector_name)

    def to_dict(self) -> dict[str, object]:
        return {
            "client_secret_in_desktop_bundle": self.client_secret_in_desktop_bundle,
            "connector_name": self.connector_name,
            "deployment": self.deployment,
            "hosted_relay_authorized": self.hosted_relay_authorized,
            "oauth_architecture": self.oauth_architecture,
            "owner_setup_required": self.owner_setup_required,
            "public_onboarding_status": self.public_onboarding_status,
            "requested_scopes": list(self.requested_scopes),
            "schema_version": self.schema_version,
            "token_exchange_location": self.token_exchange_location,
        }


def workspace_auth_profiles() -> tuple[WorkspaceAuthProfile, ...]:
    """Return the explicit supported D5.3 provider auth architecture."""

    return (
        WorkspaceAuthProfile(
            schema_version=1,
            connector_name=D5_NOTION_SOURCE,
            deployment="Notion public integration for selected pages and data sources",
            oauth_architecture="confidential_authorization_code",
            requested_scopes=("read_content", "read_user_without_email"),
            token_exchange_location=(
                "owner-authorized confidential component outside the desktop bundle"
            ),
            client_secret_in_desktop_bundle=False,
            hosted_relay_authorized=False,
            owner_setup_required=True,
            public_onboarding_status=(
                "architecture documented; live public OAuth is blocked until owner authorizes "
                "a confidential exchange component"
            ),
        ),
        WorkspaceAuthProfile(
            schema_version=1,
            connector_name=D5_CONFLUENCE_SOURCE,
            deployment="Atlassian Confluence Cloud OAuth 2.0 for selected spaces and pages",
            oauth_architecture="authorization_code_pkce",
            requested_scopes=(
                "read:content:confluence",
                "read:comment:confluence",
                "read:space:confluence",
            ),
            token_exchange_location="foreground local connector callback with PKCE",
            client_secret_in_desktop_bundle=False,
            hosted_relay_authorized=False,
            owner_setup_required=True,
            public_onboarding_status=(
                "cloud architecture ready for selected test-site consent; Confluence Data "
                "Center is out of this adapter"
            ),
        ),
    )


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
        all_records = tuple(self.record_from_item(value) for value in _require_values(values))
        selected_resource_children = tuple(
            record.content_id
            for record in all_records
            if (
                record.parent_id == selection.resource_id
                or record.content_id == selection.resource_id
            )
        )
        records = tuple(
            record
            for record in all_records
            if _matches_selected_record(
                selection,
                record,
                selected_ids,
                selected_resource_children=selected_resource_children,
            )
        )
        if not records:
            raise ConnectorContractError("invalid workspace contents")
        return WorkspaceContentPage(
            status=WorkspaceContentPageStatus.READY,
            preview=self._preview(
                selection,
                records,
                privacy=privacy,
                next_cursor=next_cursor,
                selected_resource_children=selected_resource_children,
            ),
        )

    def page_from_notion_response(
        self,
        selection: SourceResourceSelection,
        response: Mapping[str, object],
        *,
        privacy: PrivacyDecision,
        selected_content_ids: Sequence[str],
    ) -> WorkspaceContentPage:
        """Normalize one Notion API list response into selected workspace records."""

        _require_response_connector(selection, D5_NOTION_SOURCE)
        return self.page_from_items(
            selection,
            _notion_items(response),
            privacy=privacy,
            selected_content_ids=selected_content_ids,
            next_cursor=_notion_next_cursor(response),
        )

    def page_from_confluence_response(
        self,
        selection: SourceResourceSelection,
        response: Mapping[str, object],
        *,
        privacy: PrivacyDecision,
        selected_content_ids: Sequence[str],
    ) -> WorkspaceContentPage:
        """Normalize one Confluence Cloud REST v2 page into selected workspace records."""

        _require_response_connector(selection, D5_CONFLUENCE_SOURCE)
        return self.page_from_items(
            selection,
            _confluence_items(response),
            privacy=privacy,
            selected_content_ids=selected_content_ids,
            next_cursor=_confluence_next_cursor(response),
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[WorkspaceContentRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        return self._preview(
            selection,
            records,
            privacy=privacy,
            next_cursor=next_cursor,
            selected_resource_children=(),
        )

    def _preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[WorkspaceContentRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None,
        selected_resource_children: tuple[str, ...],
    ) -> SourcePreviewPage:
        _selection_connector(selection)
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid workspace contents")
        intakes = tuple(
            self._intake(
                selection,
                record,
                privacy=privacy,
                selected_resource_children=selected_resource_children,
            )
            for record in records
        )
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
        return self._intake(selection, record, privacy=privacy, selected_resource_children=())

    def _intake(
        self,
        selection: SourceResourceSelection,
        record: WorkspaceContentRecord,
        *,
        privacy: PrivacyDecision,
        selected_resource_children: tuple[str, ...],
    ) -> SourceRecordIntake:
        if type(record) is not WorkspaceContentRecord:
            raise ConnectorContractError("invalid workspace content")
        _require_record_matches_selection(
            selection,
            record,
            selected_resource_children=selected_resource_children,
        )
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


def _require_response_connector(selection: SourceResourceSelection, connector_name: str) -> None:
    if _selection_connector(selection) != connector_name:
        raise ConnectorContractError("invalid workspace content source selection")


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


def _value_str(value: object) -> str | None:
    if type(value) is str and value.strip():
        return value.strip()
    return None


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid workspace content")
    return value


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ConnectorContractError("invalid workspace content")
    return value


def _notion_items(response: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    values = response.get("results")
    if values is None and _value_str(response.get("object")) in {"page", "block", "comment"}:
        values = [response]
    return tuple(_notion_item(_mapping(value)) for value in _sequence(values))


def _notion_item(value: Mapping[str, object]) -> dict[str, object]:
    object_type = _value_str(value.get("object"))
    if object_type == "page":
        content_id = f"notion:page/{_required_raw_id(value.get('id'))}"
        parent_id = _notion_parent_id(value.get("parent"))
        content_type = "page"
        title = _notion_page_title(value)
        body = _notion_page_body(value)
    elif object_type == "block":
        content_id = f"notion:block/{_required_raw_id(value.get('id'))}"
        parent_id = _notion_parent_id(value.get("parent"))
        content_type = "block"
        title = _notion_block_title(value)
        body = _notion_block_body(value)
    elif object_type == "comment":
        content_id = f"notion:comment/{_required_raw_id(value.get('id'))}"
        parent_id = _notion_parent_id(value.get("parent"))
        content_type = "comment"
        body = _rich_text(value.get("rich_text")) or "Notion comment"
        title = "Notion comment"
    else:
        raise ConnectorContractError("invalid workspace content")
    edited = _value_str(value.get("last_edited_time"))
    revision = edited or _value_str(value.get("created_time")) or _required_raw_id(value.get("id"))
    return {
        "body": body,
        "connector_name": D5_NOTION_SOURCE,
        "content_id": content_id,
        "content_secret_scan": "clean",
        "content_type": content_type,
        "parent_id": parent_id,
        "revision_id": revision,
        "source_kind": D5_NOTION_SOURCE,
        "source_link": _value_str(value.get("url")),
        "title": title,
    }


def _notion_next_cursor(response: Mapping[str, object]) -> str | None:
    if response.get("has_more") is True:
        return _value_str(response.get("next_cursor"))
    return None


def _notion_parent_id(value: object) -> str | None:
    if value is None:
        return None
    parent = _mapping(value)
    parent_type = _value_str(parent.get("type"))
    if parent_type == "page_id":
        return f"notion:page/{_required_raw_id(parent.get('page_id'))}"
    if parent_type == "block_id":
        return f"notion:block/{_required_raw_id(parent.get('block_id'))}"
    if parent_type in {"database_id", "data_source_id"}:
        raw = parent.get(parent_type)
        return f"notion:data-source/{_required_raw_id(raw)}"
    return None


def _notion_page_title(value: Mapping[str, object]) -> str:
    properties = value.get("properties")
    if isinstance(properties, Mapping):
        for raw_property in properties.values():
            if isinstance(raw_property, Mapping):
                title = _rich_text(raw_property.get("title"))
                if title:
                    return title
    return "Notion page"


def _notion_page_body(value: Mapping[str, object]) -> str:
    lines: list[str] = []
    properties = value.get("properties")
    if isinstance(properties, Mapping):
        for raw_name, raw_property in properties.items():
            if not isinstance(raw_name, str) or not isinstance(raw_property, Mapping):
                continue
            text = _notion_property_text(raw_property)
            if text:
                lines.append(f"{raw_name}: {text}")
    return "\n".join(lines) if lines else _notion_page_title(value)


def _notion_property_text(value: Mapping[str, object]) -> str | None:
    property_type = _value_str(value.get("type"))
    if property_type in {"title", "rich_text"}:
        return _rich_text(value.get(property_type))
    if property_type in {"select", "status"} and isinstance(value.get(property_type), Mapping):
        return _value_str(_mapping(value[property_type]).get("name"))
    if property_type in {"url", "email", "phone_number", "number"}:
        raw = value.get(property_type)
        return str(raw) if raw not in (None, "") else None
    return None


def _notion_block_title(value: Mapping[str, object]) -> str:
    block_type = _value_str(value.get("type")) or "block"
    return f"Notion {block_type.replace('_', ' ')}"


def _notion_block_body(value: Mapping[str, object]) -> str:
    block_type = _value_str(value.get("type"))
    if block_type is not None and isinstance(value.get(block_type), Mapping):
        text = _rich_text(_mapping(value[block_type]).get("rich_text"))
        if text:
            return text
    return _notion_block_title(value)


def _rich_text(value: object) -> str | None:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return None
    text = "".join(
        fragment
        for item in value
        if isinstance(item, Mapping)
        for fragment in [_value_str(item.get("plain_text")) or ""]
    ).strip()
    return text or None


def _confluence_items(response: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    values = response.get("results")
    if values is None and (_value_str(response.get("id")) is not None):
        values = [response]
    return tuple(_confluence_item(_mapping(value), response) for value in _sequence(values))


def _confluence_item(
    value: Mapping[str, object], response: Mapping[str, object]
) -> dict[str, object]:
    raw_id = _required_raw_id(value.get("id"))
    raw_kind = _value_str(value.get("type")) or _value_str(value.get("subtype"))
    content_type = "comment" if raw_kind == "comment" else "page"
    content_id = f"confluence:{content_type}/{raw_id}"
    parent = _value_str(value.get("parentId")) or _value_str(value.get("pageId"))
    parent_id = f"confluence:page/{parent}" if parent and content_type == "comment" else None
    if content_type == "page":
        space = value.get("spaceId")
        parent_id = f"confluence:space/{_required_raw_id(space)}" if space is not None else None
    version = value.get("version")
    revision = (
        str(_mapping(version).get("number"))
        if isinstance(version, Mapping) and _mapping(version).get("number") is not None
        else _value_str(value.get("versionNumber")) or _required_raw_id(value.get("id"))
    )
    return {
        "body": _confluence_body(value),
        "connector_name": D5_CONFLUENCE_SOURCE,
        "content_id": content_id,
        "content_secret_scan": "clean",
        "content_type": content_type,
        "parent_id": parent_id,
        "revision_id": revision,
        "source_kind": D5_CONFLUENCE_SOURCE,
        "source_link": _confluence_link(value, response),
        "title": _value_str(value.get("title")) or "Confluence comment",
    }


def _confluence_body(value: Mapping[str, object]) -> str:
    body = value.get("body")
    if isinstance(body, Mapping):
        for key in ("storage", "view", "atlas_doc_format"):
            rendered = body.get(key)
            if isinstance(rendered, Mapping):
                text = _value_str(rendered.get("value"))
                if text:
                    return text
    return _value_str(value.get("title")) or "Confluence content"


def _confluence_link(
    value: Mapping[str, object], response: Mapping[str, object]
) -> str | None:
    links = value.get("_links")
    response_links = response.get("_links")
    if not isinstance(links, Mapping):
        return None
    webui = _value_str(links.get("webui"))
    if webui is None:
        return None
    base = (
        _value_str(links.get("base"))
        or (_value_str(response_links.get("base")) if isinstance(response_links, Mapping) else None)
    )
    if webui.startswith("https://"):
        return webui
    if base is not None and base.startswith("https://"):
        return parse.urljoin(base, webui)
    return None


def _confluence_next_cursor(response: Mapping[str, object]) -> str | None:
    links = response.get("_links")
    if not isinstance(links, Mapping):
        return None
    next_link = _value_str(links.get("next"))
    if next_link is None:
        return None
    parsed = parse.urlparse(next_link)
    query = parse.parse_qs(parsed.query)
    cursor = query.get("cursor", [None])[0]
    return _value_str(cursor)


def _required_raw_id(value: object) -> str:
    raw = _value_str(value)
    if raw is None:
        raise ConnectorContractError("invalid workspace content")
    cleaned = raw.strip().strip("{}")
    if not cleaned or any(character.isspace() for character in cleaned):
        raise ConnectorContractError("invalid workspace content")
    return cleaned


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


def _matches_selected_record(
    selection: SourceResourceSelection,
    record: WorkspaceContentRecord,
    selected_content_ids: tuple[str, ...],
    *,
    selected_resource_children: tuple[str, ...],
) -> bool:
    connector_name = _selection_connector(selection)
    if record.connector_name != connector_name:
        return False
    if record.content_id in selected_content_ids:
        return _record_within_selection(selection, record, selected_resource_children)
    if record.parent_id in selected_content_ids:
        return _record_within_selection(selection, record, selected_resource_children)
    return (
        record.parent_id == selection.resource_id
        or (
            selection.resource_type in {"page", "cloud_page"}
            and record.parent_id in selected_content_ids
        )
    )


def _record_within_selection(
    selection: SourceResourceSelection,
    record: WorkspaceContentRecord,
    selected_resource_children: tuple[str, ...],
) -> bool:
    if selection.resource_type in {"page", "cloud_page"}:
        return (
            record.content_id == selection.resource_id
            or record.parent_id == selection.resource_id
        )
    if record.content_id == selection.resource_id or record.parent_id == selection.resource_id:
        return True
    return record.content_type == "comment" and record.parent_id in selected_resource_children


def _require_record_matches_selection(
    selection: SourceResourceSelection,
    record: WorkspaceContentRecord,
    *,
    selected_resource_children: tuple[str, ...],
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
            and (
                record.content_type != "comment"
                or record.parent_id not in selected_resource_children
            )
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
