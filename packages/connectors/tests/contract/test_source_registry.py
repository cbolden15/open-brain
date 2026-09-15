from typing import cast

import pytest

from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import (
    SourceAuthMode,
    SourceCatalog,
    SourceConnectorDescriptor,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
    agent_session_source_descriptor,
    calendar_source_descriptor,
    github_source_descriptor,
    gitlab_source_descriptor,
    gmail_source_descriptor,
    google_drive_source_descriptor,
    jira_source_descriptor,
    local_document_source_descriptor,
    microsoft_mail_source_descriptor,
    slack_source_descriptor,
    web_clip_source_descriptor,
)

from .test_source_intake import _privacy


def test_github_descriptor_declares_d2_public_onboarding_and_content_scope() -> None:
    descriptor = github_source_descriptor()

    assert descriptor.connector_name == "github"
    assert descriptor.display_name == "GitHub"
    assert descriptor.auth_mode is SourceAuthMode.DEVICE_FLOW
    assert descriptor.resource_types == ("repository",)
    assert descriptor.content_types == ("comment", "issue", "pull_request")
    assert descriptor.preview_limit == 25
    assert descriptor.public_onboarding is True
    assert SourceConnectorDescriptor.from_dict(descriptor.to_dict()) == descriptor


def test_source_catalog_lists_descriptors_without_import_or_capture_authority() -> None:
    catalog = SourceCatalog(
        (
            jira_source_descriptor(),
            agent_session_source_descriptor(),
            calendar_source_descriptor(),
            github_source_descriptor(),
            gitlab_source_descriptor(),
            microsoft_mail_source_descriptor(),
            local_document_source_descriptor(),
            google_drive_source_descriptor(),
            gmail_source_descriptor(),
            slack_source_descriptor(),
            web_clip_source_descriptor(),
        )
    )

    assert catalog.list()[0].connector_name == "agent_session"
    assert catalog.require("agent_session").auth_mode is SourceAuthMode.SESSION_ONLY
    assert catalog.require("github").auth_mode is SourceAuthMode.DEVICE_FLOW
    assert catalog.require("gitlab").content_types == ("comment", "issue", "merge_request")
    assert catalog.require("gmail").resource_types == ("mail_label",)
    assert catalog.require("google_drive").resource_types == ("drive_file",)
    assert catalog.require("jira").content_types == ("comment", "issue")
    assert catalog.require("local_document").resource_types == ("docx_file", "text_pdf")
    assert catalog.require("microsoft_mail").resource_types == ("mail_folder",)
    assert catalog.require("slack").content_types == ("message", "thread_reply")
    assert catalog.require("web_clip").content_types == ("page", "selected_passage")
    with pytest.raises(ConnectorContractError, match="source connector is not registered"):
        catalog.require("zoom")


def test_d4_descriptors_declare_public_project_onboarding_scope() -> None:
    gitlab = gitlab_source_descriptor()
    jira = jira_source_descriptor()

    assert gitlab.connector_name == "gitlab"
    assert gitlab.resource_types == ("project",)
    assert gitlab.auth_mode is SourceAuthMode.DEVICE_FLOW
    assert gitlab.public_onboarding is True
    assert jira.connector_name == "jira"
    assert jira.resource_types == ("project",)
    assert jira.auth_mode is SourceAuthMode.DEVICE_FLOW
    assert jira.public_onboarding is True


def test_d4_mail_and_drive_descriptors_declare_selected_resource_scopes() -> None:
    gmail = gmail_source_descriptor()
    drive = google_drive_source_descriptor()
    microsoft = microsoft_mail_source_descriptor()

    assert gmail.connector_name == "gmail"
    assert gmail.resource_types == ("mail_label",)
    assert gmail.content_types == ("mail_message",)
    assert gmail.auth_mode is SourceAuthMode.DEVICE_FLOW
    assert gmail.public_onboarding is True
    assert drive.connector_name == "google_drive"
    assert drive.resource_types == ("drive_file",)
    assert drive.content_types == ("drive_file",)
    assert microsoft.connector_name == "microsoft_mail"
    assert microsoft.resource_types == ("mail_folder",)
    assert microsoft.content_types == ("mail_message",)


def test_d4_slack_descriptor_declares_selected_channel_thread_scope() -> None:
    slack = slack_source_descriptor()

    assert slack.connector_name == "slack"
    assert slack.resource_types == ("channel",)
    assert slack.content_types == ("message", "thread_reply")
    assert slack.auth_mode is SourceAuthMode.DEVICE_FLOW
    assert slack.public_onboarding is True


def test_d4_agent_session_descriptor_declares_local_project_session_scope() -> None:
    descriptor = agent_session_source_descriptor()

    assert descriptor.connector_name == "agent_session"
    assert descriptor.display_name == "Agent Sessions"
    assert descriptor.auth_mode is SourceAuthMode.SESSION_ONLY
    assert descriptor.resource_types == ("local_project",)
    assert descriptor.content_types == ("session_summary", "session_transcript")
    assert descriptor.public_onboarding is False


def test_d5_1_descriptors_declare_explicit_local_selection_scope() -> None:
    document = local_document_source_descriptor()
    web_clip = web_clip_source_descriptor()

    assert document.connector_name == "local_document"
    assert document.display_name == "Local Documents"
    assert document.auth_mode is SourceAuthMode.SESSION_ONLY
    assert document.resource_types == ("docx_file", "text_pdf")
    assert document.content_types == ("document_text",)
    assert document.public_onboarding is False
    assert web_clip.connector_name == "web_clip"
    assert web_clip.display_name == "Web Clips"
    assert web_clip.auth_mode is SourceAuthMode.SESSION_ONLY
    assert web_clip.resource_types == ("current_page", "selected_passage")
    assert web_clip.content_types == ("page", "selected_passage")
    assert web_clip.public_onboarding is False


def test_d5_2_descriptor_declares_selected_calendar_scope() -> None:
    descriptor = calendar_source_descriptor()

    assert descriptor.connector_name == "calendar"
    assert descriptor.display_name == "Calendars"
    assert descriptor.auth_mode is SourceAuthMode.DEVICE_FLOW
    assert descriptor.resource_types == ("google_calendar", "outlook_calendar")
    assert descriptor.content_types == ("calendar_event",)
    assert descriptor.public_onboarding is True


def test_source_preview_page_is_bounded_metadata_only_and_selection_bound() -> None:
    selection = SourceResourceSelection(
        connector_name="github",
        connection_id="account:open-brain-test",
        resource_id="repo:cbolden15/open-brain-fixture",
        resource_type="repository",
    )
    page = SourcePreviewPage(
        selection=selection,
        records=(
            SourcePreviewRecord(
                connector_name="github",
                connection_id="account:open-brain-test",
                resource_id="repo:cbolden15/open-brain-fixture",
                delivery_id=_record("issue:42", "updated:2026-09-14T12:00:00Z").key.delivery_id(),
                source_reference="https://github.com/cbolden15/open-brain-fixture/issues/42",
                title="Synthetic issue:42",
                content_type="issue",
            ),
            SourcePreviewRecord(
                connector_name="github",
                connection_id="account:open-brain-test",
                resource_id="repo:cbolden15/open-brain-fixture",
                delivery_id=_record("pull:7", "sha:abc123").key.delivery_id(),
                source_reference="https://github.com/cbolden15/open-brain-fixture/issues/7",
                title="Synthetic pull:7",
                content_type="pull_request",
            ),
        ),
        next_cursor="cursor:page2",
    )

    encoded = page.to_dict()

    assert encoded["connector_name"] == "github"
    assert encoded["resource_type"] == "repository"
    records = cast(list[dict[str, object]], encoded["records"])
    assert len(records) == 2
    assert "Synthetic preview body" not in repr(encoded)
    assert all(
        set(record) == {"content_type", "delivery_id", "selected", "source_reference", "title"}
        for record in records
    )


def test_source_preview_rejects_duplicate_or_cross_resource_delivery_keys() -> None:
    selection = SourceResourceSelection(
        connector_name="github",
        connection_id="account:fixture",
        resource_id="repo:fixture/project",
        resource_type="repository",
    )
    record = SourcePreviewRecord(
        connector_name="github",
        connection_id="account:fixture",
        resource_id="repo:fixture/project",
        delivery_id=_record(
            "issue:1",
            "updated:1",
            connection_id="account:fixture",
            resource_id="repo:fixture/project",
        ).key.delivery_id(),
        source_reference="https://github.com/fixture/project/issues/1",
        title="Synthetic issue",
        content_type="issue",
    )

    with pytest.raises(ConnectorContractError, match="invalid source preview page"):
        SourcePreviewPage(selection=selection, records=(record, record))
    with pytest.raises(ConnectorContractError, match="invalid source preview page"):
        SourcePreviewPage(
            selection=selection,
            records=(
                SourcePreviewRecord(
                    connector_name="github",
                    connection_id="account:fixture",
                    resource_id="repo:other/project",
                    delivery_id=_record(
                        "issue:2",
                        "updated:2",
                        connection_id="account:fixture",
                        resource_id="repo:other/project",
                    ).key.delivery_id(),
                    source_reference="https://github.com/fixture/project/issues/2",
                    title="Synthetic issue",
                    content_type="issue",
                ),
            ),
        )


def test_source_preview_rejects_malformed_records_with_contract_error() -> None:
    selection = SourceResourceSelection(
        connector_name="github",
        connection_id="account:fixture",
        resource_id="repo:fixture/project",
        resource_type="repository",
    )

    with pytest.raises(ConnectorContractError, match="invalid source preview page"):
        SourcePreviewPage(
            selection=selection,
            records=(object(),),  # type: ignore[arg-type]
        )


def test_source_descriptor_rejects_unordered_or_malformed_shape() -> None:
    with pytest.raises(ConnectorContractError):
        SourceConnectorDescriptor(
            schema_version=1,
            connector_name="github",
            display_name="GitHub",
            auth_mode=SourceAuthMode.DEVICE_FLOW,
            resource_types=("repository",),
            content_types=("pull_request", "issue"),
            preview_limit=25,
            public_onboarding=True,
        )
    with pytest.raises(ConnectorContractError):
        SourceConnectorDescriptor(
            schema_version=1,
            connector_name="GitHub",
            display_name="GitHub",
            auth_mode=SourceAuthMode.DEVICE_FLOW,
            resource_types=("repository",),
            content_types=("issue",),
            preview_limit=0,
            public_onboarding=True,
        )
    with pytest.raises(ConnectorContractError):
        SourceConnectorDescriptor(
            schema_version=1,
            connector_name="github",
            display_name="GitHub",
            auth_mode=SourceAuthMode.DEVICE_FLOW,
            resource_types=("repository",),
            content_types=("issue",),
            preview_limit="25",  # type: ignore[arg-type]
            public_onboarding=True,
        )


def test_source_catalog_rejects_malformed_descriptors_with_contract_error() -> None:
    with pytest.raises(ConnectorContractError, match="invalid source catalog"):
        SourceCatalog((object(),))  # type: ignore[arg-type]


def _record(
    external_id: str,
    revision_id: str,
    *,
    connection_id: str = "account:open-brain-test",
    resource_id: str = "repo:cbolden15/open-brain-fixture",
) -> SourceRecordIntake:
    return SourceRecordIntake(
        key=SourceRecordKey(
            connector_name="github",
            connection_id=connection_id,
            resource_id=resource_id,
            external_id=external_id,
            revision_id=revision_id,
        ),
        url=(
            "https://github.com/cbolden15/open-brain-fixture/issues/"
            f"{external_id.rsplit(':', 1)[1]}"
        ),
        title=f"Synthetic {external_id}",
        text="Synthetic preview body from the disposable D2 fixture.",
        privacy=_privacy(),
    )
