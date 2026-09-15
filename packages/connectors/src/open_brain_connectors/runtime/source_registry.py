"""Strict source catalog and bounded preview values for D2 adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.source_intake import SourceRecordIntake

__all__ = [
    "D2_GITHUB_SOURCE",
    "D4_GITLAB_SOURCE",
    "D4_GMAIL_SOURCE",
    "D4_GOOGLE_DRIVE_SOURCE",
    "D4_AGENT_SESSION_SOURCE",
    "D4_JIRA_SOURCE",
    "D4_MICROSOFT_MAIL_SOURCE",
    "D4_SLACK_SOURCE",
    "D5_LOCAL_DOCUMENT_SOURCE",
    "D5_CALENDAR_SOURCE",
    "D5_WEB_CLIP_SOURCE",
    "SourceAuthMode",
    "SourceCatalog",
    "SourceConnectorDescriptor",
    "SourcePreviewPage",
    "SourcePreviewRecord",
    "SourceResourceSelection",
    "gitlab_source_descriptor",
    "github_source_descriptor",
    "gmail_source_descriptor",
    "google_drive_source_descriptor",
    "agent_session_source_descriptor",
    "jira_source_descriptor",
    "local_document_source_descriptor",
    "calendar_source_descriptor",
    "microsoft_mail_source_descriptor",
    "slack_source_descriptor",
    "web_clip_source_descriptor",
]

_CONNECTOR_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9 ._:/#()+,-]{0,119}")
_RESOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_URL_SCHEME = re.compile(r"https://[A-Za-z0-9.-]+(?:[/:?#][^\s\x00]*)?")
_MAX_PREVIEW_RECORDS = 25

D2_GITHUB_SOURCE = "github"
D4_GITLAB_SOURCE = "gitlab"
D4_GMAIL_SOURCE = "gmail"
D4_GOOGLE_DRIVE_SOURCE = "google_drive"
D4_AGENT_SESSION_SOURCE = "agent_session"
D4_JIRA_SOURCE = "jira"
D4_MICROSOFT_MAIL_SOURCE = "microsoft_mail"
D4_SLACK_SOURCE = "slack"
D5_LOCAL_DOCUMENT_SOURCE = "local_document"
D5_CALENDAR_SOURCE = "calendar"
D5_WEB_CLIP_SOURCE = "web_clip"


class SourceAuthMode(StrEnum):
    """Public onboarding modes surfaced before provider-specific auth starts."""

    DEVICE_FLOW = "device_flow"
    SESSION_ONLY = "session_only"


@dataclass(frozen=True, slots=True)
class SourceConnectorDescriptor:
    """One source adapter's user-selectable capabilities."""

    schema_version: int
    connector_name: str
    display_name: str
    auth_mode: SourceAuthMode
    resource_types: tuple[str, ...]
    content_types: tuple[str, ...]
    preview_limit: int
    public_onboarding: bool

    def __post_init__(self) -> None:
        try:
            auth_mode = SourceAuthMode(self.auth_mode)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid source descriptor") from error
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.connector_name) is not str
            or _CONNECTOR_NAME.fullmatch(self.connector_name) is None
            or type(self.display_name) is not str
            or _LABEL.fullmatch(self.display_name) is None
            or not isinstance(self.resource_types, tuple)
            or not isinstance(self.content_types, tuple)
            or not 1 <= len(self.resource_types) <= 16
            or not 1 <= len(self.content_types) <= 16
            or type(self.preview_limit) is not int
            or not 1 <= self.preview_limit <= _MAX_PREVIEW_RECORDS
            or type(self.public_onboarding) is not bool
        ):
            raise ConnectorContractError("invalid source descriptor")
        resource_types = _ordered_unique_labels(self.resource_types)
        content_types = _ordered_unique_labels(self.content_types)
        object.__setattr__(self, "auth_mode", auth_mode)
        object.__setattr__(self, "resource_types", resource_types)
        object.__setattr__(self, "content_types", content_types)

    @classmethod
    def from_dict(cls, value: object) -> SourceConnectorDescriptor:
        if not isinstance(value, dict) or set(value) != {
            "auth_mode",
            "connector_name",
            "content_types",
            "display_name",
            "preview_limit",
            "public_onboarding",
            "resource_types",
            "schema_version",
        }:
            raise ConnectorContractError("invalid source descriptor")
        resource_types = value["resource_types"]
        content_types = value["content_types"]
        if not isinstance(resource_types, list) or not isinstance(content_types, list):
            raise ConnectorContractError("invalid source descriptor")
        return cls(
            schema_version=cast(int, value["schema_version"]),
            connector_name=cast(str, value["connector_name"]),
            display_name=cast(str, value["display_name"]),
            auth_mode=cast(SourceAuthMode, value["auth_mode"]),
            resource_types=tuple(cast(list[str], resource_types)),
            content_types=tuple(cast(list[str], content_types)),
            preview_limit=cast(int, value["preview_limit"]),
            public_onboarding=cast(bool, value["public_onboarding"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "auth_mode": self.auth_mode.value,
            "connector_name": self.connector_name,
            "content_types": list(self.content_types),
            "display_name": self.display_name,
            "preview_limit": self.preview_limit,
            "public_onboarding": self.public_onboarding,
            "resource_types": list(self.resource_types),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class SourceResourceSelection:
    """A selected provider resource before preview or import."""

    connector_name: str
    connection_id: str
    resource_id: str
    resource_type: str

    def __post_init__(self) -> None:
        if (
            type(self.connector_name) is not str
            or _CONNECTOR_NAME.fullmatch(self.connector_name) is None
            or type(self.connection_id) is not str
            or _RESOURCE_ID.fullmatch(self.connection_id) is None
            or type(self.resource_id) is not str
            or _RESOURCE_ID.fullmatch(self.resource_id) is None
            or type(self.resource_type) is not str
            or _LABEL.fullmatch(self.resource_type) is None
        ):
            raise ConnectorContractError("invalid source resource selection")


@dataclass(frozen=True, slots=True)
class SourcePreviewRecord:
    """One bounded preview item. It is not durable Brain content."""

    connector_name: str
    connection_id: str
    resource_id: str
    delivery_id: str
    source_reference: str
    title: str | None
    content_type: str
    selected: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.delivery_id) is not str
            or not self.delivery_id.startswith("connector.")
            or type(self.connector_name) is not str
            or _CONNECTOR_NAME.fullmatch(self.connector_name) is None
            or type(self.connection_id) is not str
            or _RESOURCE_ID.fullmatch(self.connection_id) is None
            or type(self.resource_id) is not str
            or _RESOURCE_ID.fullmatch(self.resource_id) is None
            or type(self.source_reference) is not str
            or _URL_SCHEME.fullmatch(self.source_reference) is None
            or type(self.content_type) is not str
            or _LABEL.fullmatch(self.content_type) is None
            or type(self.selected) is not bool
            or (
                self.title is not None
                and (
                    type(self.title) is not str
                    or not self.title
                    or "\x00" in self.title
                    or len(self.title) > 200
                )
            )
        ):
            raise ConnectorContractError("invalid source preview record")

    @classmethod
    def from_intake(
        cls, intake: SourceRecordIntake, *, content_type: str, selected: bool = True
    ) -> SourcePreviewRecord:
        if type(intake) is not SourceRecordIntake:
            raise ConnectorContractError("invalid source preview record")
        return cls(
            connector_name=intake.key.connector_name,
            connection_id=intake.key.connection_id,
            resource_id=intake.key.resource_id,
            delivery_id=intake.key.delivery_id(),
            source_reference=intake.source_reference,
            title=intake.title,
            content_type=content_type,
            selected=selected,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "content_type": self.content_type,
            "delivery_id": self.delivery_id,
            "source_reference": self.source_reference,
            "title": self.title,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class SourcePreviewPage:
    """Bounded preview page returned before capture authority is issued."""

    selection: SourceResourceSelection
    records: tuple[SourcePreviewRecord, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.selection) is not SourceResourceSelection
            or not isinstance(self.records, tuple)
            or len(self.records) > _MAX_PREVIEW_RECORDS
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str
                    or _RESOURCE_ID.fullmatch(self.next_cursor) is None
                )
            )
        ):
            raise ConnectorContractError("invalid source preview page")
        if any(type(record) is not SourcePreviewRecord for record in self.records):
            raise ConnectorContractError("invalid source preview page")
        delivery_ids = [record.delivery_id for record in self.records]
        if len(delivery_ids) != len(set(delivery_ids)):
            raise ConnectorContractError("invalid source preview page")
        expected_prefix = f"connector.{self.selection.connector_name}."
        if any(
            record.connector_name != self.selection.connector_name
            or record.connection_id != self.selection.connection_id
            or record.resource_id != self.selection.resource_id
            or not record.delivery_id.startswith(expected_prefix)
            for record in self.records
        ):
            raise ConnectorContractError("invalid source preview page")

    def to_dict(self) -> dict[str, object]:
        return {
            "connector_name": self.selection.connector_name,
            "connection_id": self.selection.connection_id,
            "next_cursor": self.next_cursor,
            "records": [record.to_dict() for record in self.records],
            "resource_id": self.selection.resource_id,
            "resource_type": self.selection.resource_type,
            "schema_version": 1,
        }


class SourceCatalog:
    """Host-owned source catalog for explicit provider selection."""

    def __init__(self, descriptors: tuple[SourceConnectorDescriptor, ...]) -> None:
        if not isinstance(descriptors, tuple) or not descriptors:
            raise ConnectorContractError("invalid source catalog")
        if any(type(descriptor) is not SourceConnectorDescriptor for descriptor in descriptors):
            raise ConnectorContractError("invalid source catalog")
        names = [descriptor.connector_name for descriptor in descriptors]
        if len(names) != len(set(names)):
            raise ConnectorContractError("invalid source catalog")
        self._descriptors = tuple(
            sorted(descriptors, key=lambda descriptor: descriptor.connector_name)
        )

    def list(self) -> tuple[SourceConnectorDescriptor, ...]:
        return self._descriptors

    def require(self, connector_name: str) -> SourceConnectorDescriptor:
        if type(connector_name) is not str or _CONNECTOR_NAME.fullmatch(connector_name) is None:
            raise ConnectorContractError("invalid source descriptor")
        for descriptor in self._descriptors:
            if descriptor.connector_name == connector_name:
                return descriptor
        raise ConnectorContractError("source connector is not registered")


def github_source_descriptor() -> SourceConnectorDescriptor:
    """Return D2's planned GitHub source capability without importing a provider client."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D2_GITHUB_SOURCE,
        display_name="GitHub",
        auth_mode=SourceAuthMode.DEVICE_FLOW,
        resource_types=("repository",),
        content_types=("comment", "issue", "pull_request"),
        preview_limit=25,
        public_onboarding=True,
    )


def gitlab_source_descriptor() -> SourceConnectorDescriptor:
    """Return D4's planned GitLab source capability without importing a provider client."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D4_GITLAB_SOURCE,
        display_name="GitLab",
        auth_mode=SourceAuthMode.DEVICE_FLOW,
        resource_types=("project",),
        content_types=("comment", "issue", "merge_request"),
        preview_limit=25,
        public_onboarding=True,
    )


def gmail_source_descriptor() -> SourceConnectorDescriptor:
    """Return D4's planned Gmail source capability without importing a provider client."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D4_GMAIL_SOURCE,
        display_name="Gmail",
        auth_mode=SourceAuthMode.DEVICE_FLOW,
        resource_types=("mail_label",),
        content_types=("mail_message",),
        preview_limit=25,
        public_onboarding=True,
    )


def google_drive_source_descriptor() -> SourceConnectorDescriptor:
    """Return D4's planned Google Drive source capability without importing a provider client."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D4_GOOGLE_DRIVE_SOURCE,
        display_name="Google Drive",
        auth_mode=SourceAuthMode.DEVICE_FLOW,
        resource_types=("drive_file",),
        content_types=("drive_file",),
        preview_limit=25,
        public_onboarding=True,
    )


def agent_session_source_descriptor() -> SourceConnectorDescriptor:
    """Return D4's local agent-session capture capability."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D4_AGENT_SESSION_SOURCE,
        display_name="Agent Sessions",
        auth_mode=SourceAuthMode.SESSION_ONLY,
        resource_types=("local_project",),
        content_types=("session_summary", "session_transcript"),
        preview_limit=25,
        public_onboarding=False,
    )


def jira_source_descriptor() -> SourceConnectorDescriptor:
    """Return D4's planned Jira source capability without importing a provider client."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D4_JIRA_SOURCE,
        display_name="Jira",
        auth_mode=SourceAuthMode.DEVICE_FLOW,
        resource_types=("project",),
        content_types=("comment", "issue"),
        preview_limit=25,
        public_onboarding=True,
    )


def microsoft_mail_source_descriptor() -> SourceConnectorDescriptor:
    """Return D4's planned Microsoft 365 mail capability without importing a provider client."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D4_MICROSOFT_MAIL_SOURCE,
        display_name="Microsoft 365 Mail",
        auth_mode=SourceAuthMode.DEVICE_FLOW,
        resource_types=("mail_folder",),
        content_types=("mail_message",),
        preview_limit=25,
        public_onboarding=True,
    )


def slack_source_descriptor() -> SourceConnectorDescriptor:
    """Return D4's planned Slack channel capability without importing a provider client."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D4_SLACK_SOURCE,
        display_name="Slack",
        auth_mode=SourceAuthMode.DEVICE_FLOW,
        resource_types=("channel",),
        content_types=("message", "thread_reply"),
        preview_limit=25,
        public_onboarding=True,
    )


def local_document_source_descriptor() -> SourceConnectorDescriptor:
    """Return D5.1's explicit local document capture capability."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D5_LOCAL_DOCUMENT_SOURCE,
        display_name="Local Documents",
        auth_mode=SourceAuthMode.SESSION_ONLY,
        resource_types=("docx_file", "text_pdf"),
        content_types=("document_text",),
        preview_limit=25,
        public_onboarding=False,
    )


def calendar_source_descriptor() -> SourceConnectorDescriptor:
    """Return D5.2's selected calendar/date-range capture capability."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D5_CALENDAR_SOURCE,
        display_name="Calendars",
        auth_mode=SourceAuthMode.DEVICE_FLOW,
        resource_types=("google_calendar", "outlook_calendar"),
        content_types=("calendar_event",),
        preview_limit=25,
        public_onboarding=True,
    )


def web_clip_source_descriptor() -> SourceConnectorDescriptor:
    """Return D5.1's explicit browser-to-local web clip capability."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=D5_WEB_CLIP_SOURCE,
        display_name="Web Clips",
        auth_mode=SourceAuthMode.SESSION_ONLY,
        resource_types=("current_page", "selected_passage"),
        content_types=("page", "selected_passage"),
        preview_limit=25,
        public_onboarding=False,
    )


def _ordered_unique_labels(values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(
        type(value) is not str or _LABEL.fullmatch(value) is None for value in values
    ):
        raise ConnectorContractError("invalid source descriptor")
    if values != tuple(sorted(set(values))):
        raise ConnectorContractError("invalid source descriptor")
    return values
