from __future__ import annotations

import json
import sqlite3
import stat
from http.client import HTTPMessage
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import open_brain_connectors.runtime.source_cli as source_cli
from open_brain_connectors.runtime.source_cli import run_cli


def test_source_cli_catalog_exposes_registered_public_onboarding(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(("catalog",)) == 0
    payload = _json(capsys)

    assert payload["status"] == "ok"
    connectors = cast(list[dict[str, object]], payload["connectors"])
    assert connectors == [
        {
            "auth_mode": "session_only",
            "connector_name": "agent_session",
            "content_types": ["session_summary", "session_transcript"],
            "display_name": "Agent Sessions",
            "preview_limit": 25,
            "public_onboarding": False,
            "resource_types": ["local_project"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "calendar",
            "content_types": ["calendar_event"],
            "display_name": "Calendars",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["google_calendar", "outlook_calendar"],
            "schema_version": 1,
        },
        {
            "auth_mode": "auth_code_pkce",
            "connector_name": "confluence",
            "content_types": ["comment", "page"],
            "display_name": "Confluence",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["cloud_page", "cloud_space"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "github",
            "content_types": ["comment", "issue", "pull_request"],
            "display_name": "GitHub",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["repository"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "gitlab",
            "content_types": ["comment", "issue", "merge_request"],
            "display_name": "GitLab",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["project"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "gmail",
            "content_types": ["mail_message"],
            "display_name": "Gmail",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["mail_label"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "google_drive",
            "content_types": ["drive_file"],
            "display_name": "Google Drive",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["drive_file"],
            "schema_version": 1,
        },
        {
            "auth_mode": "session_only",
            "connector_name": "imessage",
            "content_types": ["message", "message_deleted"],
            "display_name": "iMessage",
            "preview_limit": 25,
            "public_onboarding": False,
            "resource_types": ["conversation"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "jira",
            "content_types": ["comment", "issue"],
            "display_name": "Jira",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["project"],
            "schema_version": 1,
        },
        {
            "auth_mode": "session_only",
            "connector_name": "local_document",
            "content_types": ["document_text"],
            "display_name": "Local Documents",
            "preview_limit": 25,
            "public_onboarding": False,
            "resource_types": ["docx_file", "text_pdf"],
            "schema_version": 1,
        },
        {
            "auth_mode": "session_only",
            "connector_name": "meeting_transcript",
            "content_types": ["meeting_transcript"],
            "display_name": "Meeting Transcripts",
            "preview_limit": 25,
            "public_onboarding": False,
            "resource_types": ["google_meet", "zoom"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "microsoft_mail",
            "content_types": ["mail_message"],
            "display_name": "Microsoft 365 Mail",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["mail_folder"],
            "schema_version": 1,
        },
        {
            "auth_mode": "confidential_oauth",
            "connector_name": "notion",
            "content_types": ["block", "comment", "page"],
            "display_name": "Notion",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["data_source", "page"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "slack",
            "content_types": ["message", "thread_reply"],
            "display_name": "Slack",
            "preview_limit": 25,
            "public_onboarding": True,
            "resource_types": ["channel"],
            "schema_version": 1,
        },
        {
            "auth_mode": "session_only",
            "connector_name": "web_clip",
            "content_types": ["page", "selected_passage"],
            "display_name": "Web Clips",
            "preview_limit": 25,
            "public_onboarding": False,
            "resource_types": ["current_page", "selected_passage"],
            "schema_version": 1,
        },
    ]


def test_source_cli_selects_agent_session_project(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "agent-session",
                "select-project",
                "--connection-id",
                "account:agent-fixture",
                "--project-id",
                "project:0123456789abcdef0123456789abcdef",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "connection_id": "account:agent-fixture",
        "connector_name": "agent_session",
        "resource_id": "project:0123456789abcdef0123456789abcdef",
        "resource_type": "local_project",
        "schema_version": 1,
        "status": "selected",
    }


def test_source_cli_selects_calendar(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "calendar",
                "select-calendar",
                "--connection-id",
                "account:calendar-fixture",
                "--calendar-id",
                "calendar:primary",
                "--provider",
                "google_calendar",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "connection_id": "account:calendar-fixture",
        "connector_name": "calendar",
        "resource_id": "calendar:primary",
        "resource_type": "google_calendar",
        "schema_version": 1,
        "status": "selected",
    }


def test_source_cli_selects_imessage_conversation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _write_imessage_database(tmp_path)

    assert (
        run_cli(
            (
                "imessage",
                "select-conversation",
                "--connection-id",
                "account:imessage-fixture",
                "--database",
                str(database),
                "--conversation-id",
                "chat-open-brain",
                "--permission-status",
                "granted",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "connection_id": "account:imessage-fixture",
        "connector_name": "imessage",
        "resource_id": "conversation:chat-open-brain",
        "resource_type": "conversation",
        "schema_version": 1,
        "status": "selected",
    }


def test_source_cli_previews_imessage_without_payload_bodies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _write_imessage_database(tmp_path)

    assert (
        run_cli(
            (
                "imessage",
                "preview-messages",
                "--connection-id",
                "account:imessage-fixture",
                "--database",
                str(database),
                "--conversation-id",
                "chat-open-brain",
                "--permission-status",
                "granted",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["connector_name"] == "imessage"
    assert payload["resource_id"] == "conversation:chat-open-brain"
    records = cast(list[dict[str, object]], payload["records"])
    assert [record["content_type"] for record in records] == ["message"]
    assert records[0]["title"] == "iMessage 1 in Open Brain Test"
    assert "must not print" not in repr(payload)


def test_source_cli_round_trips_imessage_preview_cursor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _write_imessage_database(tmp_path, message_count=26)
    base_args = (
        "imessage",
        "preview-messages",
        "--connection-id",
        "account:imessage-fixture",
        "--database",
        str(database),
        "--conversation-id",
        "chat-open-brain",
        "--permission-status",
        "granted",
    )

    assert run_cli(base_args) == 0
    first = _json(capsys)
    assert first["next_cursor"] == "row:25"

    assert run_cli((*base_args, "--next-cursor", first["next_cursor"])) == 0
    second = _json(capsys)

    assert second["next_cursor"] is None
    records = cast(list[dict[str, object]], second["records"])
    assert [record["title"] for record in records] == ["iMessage 26 in Open Brain Test"]
    assert "must not print" not in repr(second)


def test_source_cli_reports_imessage_checkpoint_without_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _write_imessage_database(tmp_path)

    assert (
        run_cli(
            (
                "imessage",
                "checkpoint",
                "--connection-id",
                "account:imessage-fixture",
                "--database",
                str(database),
                "--conversation-id",
                "chat-open-brain",
                "--permission-status",
                "granted",
                "--checkpoint-dir",
                str(tmp_path / "checkpoints"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ok"
    assert payload["connector_name"] == "imessage"
    assert payload["resource_id"] == "conversation:chat-open-brain"
    assert payload["resource_type"] == "conversation"
    assert payload["committed_delivery_ids"] == []
    assert payload["committed_revision_identities"] == []
    assert "must not print" not in repr(payload)


def test_source_cli_selects_meeting_transcript(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "meeting-transcript",
                "select-meeting",
                "--connection-id",
                "account:meeting-fixture",
                "--meeting-id",
                "meeting:weekly-sync",
                "--provider",
                "zoom",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "connection_id": "account:meeting-fixture",
        "connector_name": "meeting_transcript",
        "resource_id": "meeting:weekly-sync",
        "resource_type": "zoom",
        "schema_version": 1,
        "status": "selected",
    }


def test_source_cli_previews_meeting_transcripts_without_payload_bodies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "transcripts.json"
    source.write_text(
        json.dumps(
            [
                {
                    "meeting_id": "meeting:weekly-sync",
                    "provider": "zoom",
                    "revision_id": "rev-1",
                    "source_kind": "meeting_transcript",
                    "speaker_segments": ["Ada 00:01 Synthetic segment must not print."],
                    "started_at": "2026-09-15T15:00:00Z",
                    "title": "Weekly Sync",
                    "transcript": "Synthetic meeting body must not print.",
                    "transcript_id": "transcript:weekly-sync-v1",
                    "transcript_secret_scan": "clean",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "meeting-transcript",
                "preview-transcripts",
                "--connection-id",
                "account:meeting-fixture",
                "--meeting-id",
                "meeting:weekly-sync",
                "--provider",
                "zoom",
                "--input",
                str(source),
                "--next-cursor",
                "cursor:meeting2",
                "--selected-meeting-id",
                "meeting:weekly-sync",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "cursor:meeting2"
    assert payload["connector_name"] == "meeting_transcript"
    records = cast(list[dict[str, object]], payload["records"])
    assert [record["content_type"] for record in records] == ["meeting_transcript"]
    assert records[0]["title"] == "Weekly Sync"
    assert "must not print" not in repr(payload)


def test_source_cli_rejects_unselected_meeting_transcript_preview(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "transcripts.json"
    source.write_text(
        json.dumps(
            [
                {
                    "meeting_id": "meeting:weekly-sync",
                    "provider": "zoom",
                    "revision_id": "rev-1",
                    "source_kind": "meeting_transcript",
                    "started_at": "2026-09-15T15:00:00Z",
                    "title": "Weekly Sync",
                    "transcript": "Synthetic meeting body must not print.",
                    "transcript_id": "transcript:weekly-sync-v1",
                    "transcript_secret_scan": "clean",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "meeting-transcript",
                "preview-transcripts",
                "--connection-id",
                "account:meeting-fixture",
                "--meeting-id",
                "meeting:weekly-sync",
                "--provider",
                "zoom",
                "--input",
                str(source),
                "--selected-meeting-id",
                "meeting:weekly-sync",
                "--selected-meeting-id",
                "meeting:other",
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid meeting transcripts"}, "status": "failed"}
    assert "must not print" not in repr(payload)


def test_source_cli_rejects_blank_meeting_transcript_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "transcripts.json"
    source.write_text(
        json.dumps(
            [
                {
                    "meeting_id": "meeting:weekly-sync",
                    "provider": "zoom",
                    "revision_id": "rev-1",
                    "source_kind": "meeting_transcript",
                    "started_at": "2026-09-15T15:00:00Z",
                    "title": "Weekly Sync",
                    "transcript": " \n\t ",
                    "transcript_id": "transcript:weekly-sync-v1",
                    "transcript_secret_scan": "clean",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "meeting-transcript",
                "preview-transcripts",
                "--connection-id",
                "account:meeting-fixture",
                "--meeting-id",
                "meeting:weekly-sync",
                "--provider",
                "zoom",
                "--input",
                str(source),
                "--selected-meeting-id",
                "meeting:weekly-sync",
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid meeting transcript"}, "status": "failed"}


def test_source_cli_reports_meeting_transcript_checkpoint_without_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "meeting-transcript",
                "checkpoint",
                "--connection-id",
                "account:meeting-fixture",
                "--meeting-id",
                "meeting:weekly-sync",
                "--provider",
                "google_meet",
                "--checkpoint-dir",
                str(tmp_path / "checkpoints"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ok"
    assert payload["resource_id"] == "meeting:weekly-sync"
    assert payload["resource_type"] == "google_meet"
    assert payload["committed_delivery_ids"] == []
    assert payload["committed_revision_identities"] == []


def test_source_cli_selects_workspace_content_resource(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "workspace-content",
                "select-resource",
                "--connection-id",
                "account:notion-fixture",
                "--connector",
                "notion",
                "--resource-id",
                "notion:data-source/roadmap",
                "--resource-type",
                "data_source",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "connection_id": "account:notion-fixture",
        "connector_name": "notion",
        "resource_id": "notion:data-source/roadmap",
        "resource_type": "data_source",
        "schema_version": 1,
        "status": "selected",
    }


def test_source_cli_previews_workspace_content_without_payload_bodies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "workspace-content.json"
    source.write_text(
        json.dumps(
            [
                {
                    "body": "Synthetic Notion body must not print.",
                    "connector_name": "notion",
                    "content_id": "notion:block/decision-heading",
                    "content_secret_scan": "clean",
                    "content_type": "block",
                    "parent_id": "notion:page/weekly-plan",
                    "revision_id": "rev-1",
                    "source_kind": "notion",
                    "source_link": "https://notion.example.invalid/source/item",
                    "title": "Decision Heading",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "workspace-content",
                "preview-content",
                "--connection-id",
                "account:notion-fixture",
                "--connector",
                "notion",
                "--resource-id",
                "notion:page/weekly-plan",
                "--resource-type",
                "page",
                "--input",
                str(source),
                "--next-cursor",
                "cursor:notion2",
                "--selected-content-id",
                "notion:page/weekly-plan",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "cursor:notion2"
    assert payload["connector_name"] == "notion"
    assert payload["resource_id"] == "notion:page/weekly-plan"
    records = cast(list[dict[str, object]], payload["records"])
    assert [record["content_type"] for record in records] == ["block"]
    assert records[0]["title"] == "Decision Heading"
    assert "must not print" not in repr(payload)


def test_source_cli_previews_workspace_content_from_provider_response(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "notion-response.json"
    source.write_text(
        json.dumps(
            {
                "has_more": True,
                "next_cursor": "notion-cursor-2",
                "results": [
                    {
                        "id": "weekly-plan",
                        "last_edited_time": "2026-09-16T14:00:00Z",
                        "object": "page",
                        "properties": {
                            "Name": {
                                "type": "title",
                                "title": [{"plain_text": "Weekly Plan"}],
                            }
                        },
                        "url": "https://www.notion.so/workspace/weekly-plan",
                    },
                    {
                        "id": "decision-heading",
                        "last_edited_time": "2026-09-16T14:01:00Z",
                        "object": "block",
                        "parent": {"type": "page_id", "page_id": "weekly-plan"},
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"plain_text": "Synthetic provider body."}]
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "workspace-content",
                "preview-content",
                "--connection-id",
                "account:notion-fixture",
                "--connector",
                "notion",
                "--resource-id",
                "notion:page/weekly-plan",
                "--resource-type",
                "page",
                "--input",
                str(source),
                "--provider-format",
                "notion-api",
                "--selected-content-id",
                "notion:page/weekly-plan",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "notion-cursor-2"
    records = cast(list[dict[str, object]], payload["records"])
    assert [record["content_type"] for record in records] == ["page", "block"]
    assert "Synthetic provider body" not in repr(payload)


def test_source_cli_rejects_unselected_workspace_content_preview(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "workspace-content.json"
    source.write_text(
        json.dumps(
            [
                {
                    "body": "Synthetic Confluence body must not print.",
                    "connector_name": "confluence",
                    "content_id": "confluence:page/other",
                    "content_secret_scan": "clean",
                    "content_type": "page",
                    "parent_id": "confluence:space/ENG",
                    "revision_id": "rev-1",
                    "source_kind": "confluence",
                    "source_link": "https://confluence.example.invalid/source/item",
                    "title": "Other Page",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "workspace-content",
                "preview-content",
                "--connection-id",
                "account:confluence-fixture",
                "--connector",
                "confluence",
                "--resource-id",
                "confluence:page/runbook",
                "--resource-type",
                "cloud_page",
                "--input",
                str(source),
                "--selected-content-id",
                "confluence:page/runbook",
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid workspace contents"}, "status": "failed"}
    assert "must not print" not in repr(payload)


def test_source_cli_reports_workspace_content_checkpoint_without_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "workspace-content",
                "checkpoint",
                "--connection-id",
                "account:confluence-fixture",
                "--connector",
                "confluence",
                "--resource-id",
                "confluence:space/ENG",
                "--resource-type",
                "cloud_space",
                "--checkpoint-dir",
                str(tmp_path / "checkpoints"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ok"
    assert payload["connector_name"] == "confluence"
    assert payload["resource_id"] == "confluence:space/ENG"
    assert payload["resource_type"] == "cloud_space"
    assert payload["committed_delivery_ids"] == []
    assert payload["committed_revision_identities"] == []
    assert payload["observed_page_cursors"] == []


def test_source_cli_reports_workspace_auth_architecture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(("workspace-content", "auth-architecture")) == 0
    payload = _json(capsys)

    profiles = {
        cast(str, profile["connector_name"]): profile
        for profile in cast(list[dict[str, object]], payload["profiles"])
    }
    assert payload["status"] == "ok"
    assert profiles["notion"]["schema_version"] == 1
    assert profiles["notion"]["oauth_architecture"] == "confidential_authorization_code"
    assert profiles["notion"]["client_secret_in_desktop_bundle"] is False
    assert profiles["notion"]["hosted_relay_authorized"] is False
    assert profiles["notion"]["owner_setup_required"] is True
    assert "read_content" in cast(list[str], profiles["notion"]["requested_scopes"])
    assert "outside the desktop bundle" in cast(
        str,
        profiles["notion"]["token_exchange_location"],
    )
    assert set(profiles["notion"]) == {
        "client_secret_in_desktop_bundle",
        "connector_name",
        "deployment",
        "hosted_relay_authorized",
        "oauth_architecture",
        "owner_setup_required",
        "public_onboarding_status",
        "requested_scopes",
        "schema_version",
        "token_exchange_location",
    }
    assert profiles["confluence"]["oauth_architecture"] == "authorization_code_pkce"
    assert profiles["confluence"]["client_secret_in_desktop_bundle"] is False
    assert profiles["confluence"]["owner_setup_required"] is True


def test_source_cli_previews_calendar_events_without_payload_bodies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "events.json"
    source.write_text(
        json.dumps(
            [
                {
                    "attendees": ["Ada Example"],
                    "body_secret_scan": "clean",
                    "calendar_id": "calendar:primary",
                    "description": "Synthetic event body must not print.",
                    "end_time": "2026-09-15T10:30:00Z",
                    "event_id": "event:standup",
                    "provider": "google_calendar",
                    "revision_id": "rev-1",
                    "source_kind": "calendar_event",
                    "start_time": "2026-09-15T10:00:00Z",
                    "timezone": "America/Chicago",
                    "title": "Daily Standup",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "calendar",
                "preview-events",
                "--connection-id",
                "account:calendar-fixture",
                "--calendar-id",
                "calendar:primary",
                "--provider",
                "google_calendar",
                "--input",
                str(source),
                "--event-id",
                "event:standup",
                "--range-start",
                "2026-09-15T00:00:00Z",
                "--range-end",
                "2026-09-16T00:00:00Z",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["connector_name"] == "calendar"
    assert payload["resource_id"] == "calendar:primary"
    assert payload["resource_type"] == "google_calendar"
    assert payload["status"] == "ready"
    records = cast(list[dict[str, object]], payload["records"])
    assert records[0]["content_type"] == "calendar_event"
    assert records[0]["title"] == "Daily Standup"
    assert "must not print" not in json.dumps(payload)


def test_source_cli_previews_agent_session_events_without_payload_bodies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "agent-events.json"
    source.write_text(
        json.dumps(
            [
                {
                    "brain_result_reference": False,
                    "client_name": "codex",
                    "project_id": "project:0123456789abcdef0123456789abcdef",
                    "revision_id": "rev-1",
                    "session_id": "codex-session-1",
                    "source_kind": "agent_session",
                    "summary": "Synthetic summary body must not print.",
                    "summary_secret_scan": "clean",
                    "transcript": "Synthetic transcript body must not print.",
                    "transcript_secret_scan": "clean",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "agent-session",
                "preview-events",
                "--connection-id",
                "account:agent-fixture",
                "--project-id",
                "project:0123456789abcdef0123456789abcdef",
                "--input",
                str(source),
                "--include-transcripts",
                "--next-cursor",
                "cursor:agent2",
                "--session-id",
                "codex-session-1",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "cursor:agent2"
    records = cast(list[dict[str, object]], payload["records"])
    assert [record["content_type"] for record in records] == [
        "session_summary",
        "session_transcript",
    ]
    assert all(record["selected"] is True for record in records)
    assert "must not print" not in repr(payload)


def test_source_cli_reports_agent_session_checkpoint_without_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "agent-session",
                "checkpoint",
                "--connection-id",
                "account:agent-fixture",
                "--project-id",
                "project:0123456789abcdef0123456789abcdef",
                "--checkpoint-dir",
                str(tmp_path / "checkpoints"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ok"
    assert payload["resource_id"] == "project:0123456789abcdef0123456789abcdef"
    assert payload["committed_delivery_ids"] == []
    assert payload["committed_revision_identities"] == []
    assert "Synthetic" not in repr(payload)


def test_source_cli_selects_local_document_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "local-document",
                "select-file",
                "--connection-id",
                "account:local-doc-fixture",
                "--selected-document-id",
                "document:0123456789abcdef0123456789abcdef",
                "--file-kind",
                "text_pdf",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "connection_id": "account:local-doc-fixture",
        "connector_name": "local_document",
        "resource_id": "document:0123456789abcdef0123456789abcdef",
        "resource_type": "text_pdf",
        "schema_version": 1,
        "status": "selected",
    }


def test_source_cli_previews_local_document_without_payload_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "documents.json"
    source.write_text(
        json.dumps(
            [
                {
                    "document_id": "document:0123456789abcdef0123456789abcdef",
                    "file_kind": "text_pdf",
                    "revision_id": "rev-1",
                    "source_kind": "local_document",
                    "text": "Synthetic local document body must not print.",
                    "text_secret_scan": "clean",
                    "title": "Fixture PDF",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "local-document",
                "preview-file",
                "--connection-id",
                "account:local-doc-fixture",
                "--selected-document-id",
                "document:0123456789abcdef0123456789abcdef",
                "--file-kind",
                "text_pdf",
                "--input",
                str(source),
                "--next-cursor",
                "cursor:doc2",
                "--document-id",
                "document:0123456789abcdef0123456789abcdef",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "cursor:doc2"
    records = cast(list[dict[str, object]], payload["records"])
    assert [record["content_type"] for record in records] == ["document_text"]
    assert records[0]["selected"] is True
    assert "must not print" not in repr(payload)


def test_source_cli_rejects_local_document_secret_title_without_printing_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "documents.json"
    source.write_text(
        json.dumps(
            [
                {
                    "document_id": "document:0123456789abcdef0123456789abcdef",
                    "file_kind": "text_pdf",
                    "revision_id": "rev-1",
                    "source_kind": "local_document",
                    "text": "Synthetic local document body.",
                    "text_secret_scan": "clean",
                    "title": "access_token = synthetic-secret",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "local-document",
                "preview-file",
                "--connection-id",
                "account:local-doc-fixture",
                "--selected-document-id",
                "document:0123456789abcdef0123456789abcdef",
                "--file-kind",
                "text_pdf",
                "--input",
                str(source),
                "--document-id",
                "document:0123456789abcdef0123456789abcdef",
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {
        "error": {"code": "invalid local document record"},
        "status": "failed",
    }
    assert "synthetic-secret" not in repr(payload)


def test_source_cli_reports_local_document_checkpoint_without_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "local-document",
                "checkpoint",
                "--connection-id",
                "account:local-doc-fixture",
                "--selected-document-id",
                "document:0123456789abcdef0123456789abcdef",
                "--file-kind",
                "docx_file",
                "--checkpoint-dir",
                str(tmp_path / "checkpoints"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ok"
    assert payload["resource_id"] == "document:0123456789abcdef0123456789abcdef"
    assert payload["resource_type"] == "docx_file"
    assert payload["committed_delivery_ids"] == []
    assert payload["committed_revision_identities"] == []


def test_source_cli_selects_web_clip_browser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "web-clip",
                "select-browser",
                "--connection-id",
                "account:web-clip-fixture",
                "--browser-id",
                "browser:safari-fixture",
                "--clip-type",
                "selected_passage",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "connection_id": "account:web-clip-fixture",
        "connector_name": "web_clip",
        "resource_id": "browser:safari-fixture",
        "resource_type": "selected_passage",
        "schema_version": 1,
        "status": "selected",
    }


def test_source_cli_previews_web_clip_without_payload_body_or_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "web-clips.json"
    source.write_text(
        json.dumps(
            [
                {
                    "browser_id": "browser:safari-fixture",
                    "clip_id": "clip:0123456789abcdef0123456789abcdef",
                    "clip_type": "selected_passage",
                    "cookie_capture": False,
                    "history_scan": False,
                    "page_url": "https://example.invalid/page",
                    "revision_id": "rev-1",
                    "source_kind": "web_clip",
                    "text": "Synthetic web clip body must not print.",
                    "text_secret_scan": "clean",
                    "title": "Fixture Page",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "web-clip",
                "preview-clips",
                "--connection-id",
                "account:web-clip-fixture",
                "--browser-id",
                "browser:safari-fixture",
                "--clip-type",
                "selected_passage",
                "--input",
                str(source),
                "--next-cursor",
                "cursor:clip2",
                "--clip-id",
                "clip:0123456789abcdef0123456789abcdef",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "cursor:clip2"
    records = cast(list[dict[str, object]], payload["records"])
    assert [record["content_type"] for record in records] == ["selected_passage"]
    assert records[0]["selected"] is True
    assert "must not print" not in repr(payload)


def test_source_cli_rejects_web_clip_history_scan_without_printing_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "web-clips.json"
    source.write_text(
        json.dumps(
            [
                {
                    "browser_id": "browser:safari-fixture",
                    "clip_id": "clip:0123456789abcdef0123456789abcdef",
                    "clip_type": "selected_passage",
                    "cookie_capture": False,
                    "history_scan": True,
                    "page_url": "https://example.invalid/page",
                    "revision_id": "rev-1",
                    "source_kind": "web_clip",
                    "text": "Synthetic web clip body must not print.",
                    "text_secret_scan": "clean",
                    "title": "Fixture Page",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "web-clip",
                "preview-clips",
                "--connection-id",
                "account:web-clip-fixture",
                "--browser-id",
                "browser:safari-fixture",
                "--clip-type",
                "selected_passage",
                "--input",
                str(source),
                "--clip-id",
                "clip:0123456789abcdef0123456789abcdef",
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {
        "error": {"code": "invalid web clip record"},
        "status": "failed",
    }
    assert "must not print" not in repr(payload)


def test_source_cli_reports_web_clip_checkpoint_without_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "web-clip",
                "checkpoint",
                "--connection-id",
                "account:web-clip-fixture",
                "--browser-id",
                "browser:safari-fixture",
                "--clip-type",
                "current_page",
                "--checkpoint-dir",
                str(tmp_path / "checkpoints"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ok"
    assert payload["resource_id"] == "browser:safari-fixture"
    assert payload["resource_type"] == "current_page"
    assert payload["committed_delivery_ids"] == []
    assert payload["committed_revision_identities"] == []


def test_source_cli_requires_approved_github_public_client_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "start-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {
        "error": {"code": "github public app registration required"},
        "status": "failed",
    }


def test_source_cli_starts_github_public_device_flow_without_printing_device_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "device_code": "github-device-secret-material",
                    "expires_in": 900,
                    "interval": 5,
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                }
            ).encode("utf-8")

    observed: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: int) -> _Response:
        observed["request"] = request
        observed["timeout"] = timeout
        return _Response()

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "start-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "needs_user_verification"
    assert payload["device_code_ref"] != "github-device-secret-material"
    assert payload["verification_uri"] == "https://github.com/login/device"
    assert "github-device-secret-material" not in repr(payload)
    assert observed["timeout"] == 15
    request = cast(Request, observed["request"])
    assert request.full_url == "https://github.com/login/device/code"
    assert request.data == b"client_id=Iv1.fixturepublicclient"
    session_files = list((tmp_path / "sessions").glob("github-device-flow-*.json"))
    assert len(session_files) == 1
    assert stat.S_IMODE(session_files[0].stat().st_mode) == 0o600
    persisted = json.loads(session_files[0].read_text(encoding="utf-8"))
    assert persisted == {
        "app_type": "github_app",
        "device_code": "github-device-secret-material",
        "permission_model": "github_app_permissions",
        "schema_version": 1,
        "scope": "",
    }


def test_source_cli_completes_github_device_flow_without_printing_tokens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_device_session(
        tmp_path / "sessions",
        "0123456789abcdef0123456789abcdef",
        "github-device-secret-material",
    )

    observed: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: int) -> _TokenResponse:
        observed["request"] = request
        observed["timeout"] = timeout
        return _TokenResponse(
            {
                "access_token": "ghu_fixture_user_access_token",
                "expires_in": 28_800,
                "refresh_token": "ghr_fixture_refresh_token",
                "refresh_token_expires_in": 15_897_600,
                "scope": "",
                "token_type": "bearer",
            }
        )

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "complete-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
                "--device-code-ref",
                "session:github-device-flow/0123456789abcdef0123456789abcdef",
                "--credential-dir",
                str(tmp_path / "credentials"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "authenticated"
    assert str(payload["credential_ref"]).startswith("session:github-user-token/")
    assert payload["scope"] == ""
    assert "ghu_" not in repr(payload)
    assert "ghr_" not in repr(payload)
    request = cast(Request, observed["request"])
    assert request.full_url == "https://github.com/login/oauth/access_token"
    assert b"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in cast(
        bytes,
        request.data,
    )
    assert b"scope" not in cast(bytes, request.data)
    credential_files = list((tmp_path / "credentials").glob("github-user-token-*.json"))
    assert len(credential_files) == 1
    assert stat.S_IMODE(credential_files[0].stat().st_mode) == 0o600
    persisted = json.loads(credential_files[0].read_text(encoding="utf-8"))
    assert persisted["access_token"] == "ghu_fixture_user_access_token"
    assert persisted["refresh_token"] == "ghr_fixture_refresh_token"
    assert persisted["scope"] == ""


@pytest.mark.parametrize(
    ("error", "status"),
    (
        ("authorization_pending", "authorization_pending"),
        ("slow_down", "slow_down"),
        ("expired_token", "needs_sign_in"),
        ("access_denied", "needs_sign_in"),
    ),
)
def test_source_cli_device_flow_completion_models_polling_and_reauth_states(
    error: str,
    status: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_device_session(
        tmp_path / "sessions",
        "0123456789abcdef0123456789abcdef",
        "github-device-secret-material",
    )

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        assert timeout == 15
        return _TokenResponse({"error": error})

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "complete-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
                "--device-code-ref",
                "session:github-device-flow/0123456789abcdef0123456789abcdef",
                "--credential-dir",
                str(tmp_path / "credentials"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == status
    assert "github-device-secret-material" not in repr(payload)


def test_source_cli_refreshes_github_user_token_and_rotates_private_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_ref = _write_token_session(tmp_path / "credentials")

    def _urlopen(request: object, *, timeout: int) -> _TokenResponse:
        assert timeout == 15
        request_data = cast(bytes, cast(Request, request).data)
        assert b"grant_type=refresh_token" in request_data
        return _TokenResponse(
            {
                "access_token": "ghu_fixture_rotated_user_access_token",
                "expires_in": 28_800,
                "refresh_token": "ghr_fixture_rotated_refresh_token",
                "refresh_token_expires_in": 15_897_600,
                "scope": "",
                "token_type": "bearer",
            }
        )

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "refresh-token",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                first_ref,
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "authenticated"
    assert payload["credential_ref"] != first_ref
    assert not _token_session_path(tmp_path / "credentials", first_ref).exists()
    assert "ghu_" not in repr(payload)
    assert "ghr_" not in repr(payload)


def test_source_cli_bad_refresh_token_requires_sign_in_and_removes_local_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        assert timeout == 15
        return _TokenResponse({"error": "bad_refresh_token"})

    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")
    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "refresh-token",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "needs_reauthentication": True,
        "schema_version": 1,
        "status": "needs_sign_in",
    }
    assert not _token_session_path(tmp_path / "credentials", credential_ref).exists()


def test_source_cli_auth_status_and_local_revocation_are_metadata_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")

    assert (
        run_cli(
            (
                "github",
                "auth-status",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    status_payload = _json(capsys)
    assert status_payload["status"] == "ok"
    assert status_payload["credential_ref"] == credential_ref
    assert status_payload["refresh_available"] is True
    assert "ghu_" not in repr(status_payload)

    assert (
        run_cli(
            (
                "github",
                "mark-revoked",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    revoke_payload = _json(capsys)

    assert revoke_payload["status"] == "needs_sign_in"
    assert not _token_session_path(tmp_path / "credentials", credential_ref).exists()

    assert (
        run_cli(
            (
                "github",
                "auth-status",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    revoked_status_payload = _json(capsys)
    assert revoked_status_payload == {
        "credential_ref": credential_ref,
        "needs_reauthentication": True,
        "refresh_available": False,
        "schema_version": 1,
        "status": "needs_sign_in",
    }


def test_source_cli_auth_status_reports_refresh_before_reauthentication(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    refreshable_ref = _write_token_session(
        tmp_path / "refreshable",
        expires_at_epoch=1,
        refresh_expires_at_epoch=5_000_000_000,
    )
    assert (
        run_cli(
            (
                "github",
                "auth-status",
                "--credential-dir",
                str(tmp_path / "refreshable"),
                "--credential-ref",
                refreshable_ref,
            )
        )
        == 0
    )
    refreshable = _json(capsys)
    assert refreshable["status"] == "needs_refresh"
    assert refreshable["needs_reauthentication"] is False
    assert refreshable["refresh_available"] is True

    expired_ref = _write_token_session(
        tmp_path / "expired",
        expires_at_epoch=1,
        refresh_expires_at_epoch=1,
    )
    assert (
        run_cli(
            (
                "github",
                "auth-status",
                "--credential-dir",
                str(tmp_path / "expired"),
                "--credential-ref",
                expired_ref,
            )
        )
        == 0
    )
    expired = _json(capsys)
    assert expired["status"] == "needs_sign_in"
    assert expired["needs_reauthentication"] is True
    assert expired["refresh_available"] is False


def test_source_cli_rejects_legacy_oauth_scope_for_github_app_device_flow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPEN_BRAIN_GITHUB_CLIENT_ID", "Iv1.fixturepublicclient")

    assert (
        run_cli(
            (
                "github",
                "start-device-flow",
                "--client-id-env",
                "OPEN_BRAIN_GITHUB_CLIENT_ID",
                "--session-dir",
                str(tmp_path / "sessions"),
                "--scope",
                "public_repo",
            )
        )
        == 2
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid_arguments"}, "status": "failed"}
    assert "public_repo" not in repr(payload)


@pytest.mark.parametrize(
    "device_code_ref",
    (
        "ghp_plain_token_is_not_a_reference",
        "session:github-device-flow/ghp_plain_token_is_not_a_reference",
        "session:github-device-flow/github_pat_plain_token_is_not_a_reference",
        "session:github-device-flow/sk_live_plain_token_is_not_a_reference",
        "session:github-device-flow/raw-device-code",
    ),
)
def test_source_cli_auth_session_rejects_secret_shaped_device_refs(
    device_code_ref: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "auth-session",
                "--device-code-ref",
                device_code_ref,
                "--user-code",
                "ABCD-1234",
                "--verification-uri",
                "https://github.com/login/device",
                "--expires-in-seconds",
                "900",
                "--interval-seconds",
                "5",
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid github auth session"}, "status": "failed"}
    assert "ghp_" not in repr(payload)
    assert "github_pat_" not in repr(payload)


def test_source_cli_models_github_device_session_without_token_material(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "auth-session",
                "--device-code-ref",
                "session:github-device-flow/0123456789abcdef0123456789abcdef",
                "--user-code",
                "ABCD-1234",
                "--verification-uri",
                "https://github.com/login/device",
                "--expires-in-seconds",
                "900",
                "--interval-seconds",
                "5",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "needs_user_verification"
    assert payload["verification_uri"] == "https://github.com/login/device"
    assert "ghp_" not in repr(payload)


def test_source_cli_connection_ref_cannot_claim_live_onboarding_proof(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "connection-ref",
                "--connection-id",
                "account:fixture",
                "--account-login",
                "fixture",
                "--credential-ref",
                "keychain:open-brain/github/fixture",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "account_login": "fixture",
        "connection_id": "account:fixture",
        "credential_ref": "keychain:open-brain/github/fixture",
        "onboarding_mode": "device_flow",
        "public_onboarding_available": True,
        "schema_version": 1,
        "status": "ok",
    }
    assert "proof" not in repr(payload).lower()


@pytest.mark.parametrize(
    "credential_ref",
    (
        "session:ghu_plain_user_token_is_not_a_reference",
        "session:ghr_plain_refresh_token_is_not_a_reference",
        "session:github_pat_plain_token_is_not_a_reference",
        "keychain:open-brain/github/ghp_plain_token_is_not_a_reference",
    ),
)
def test_source_cli_connection_ref_rejects_secret_shaped_credential_refs(
    credential_ref: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "connection-ref",
                "--connection-id",
                "account:fixture",
                "--account-login",
                "fixture",
                "--credential-ref",
                credential_ref,
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid github connection"}, "status": "failed"}
    assert "ghu_" not in repr(payload)
    assert "github_pat_" not in repr(payload)


def test_source_cli_rejects_user_supplied_onboarding_proof_claim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "connection-ref",
                "--connection-id",
                "account:fixture",
                "--account-login",
                "fixture",
                "--credential-ref",
                "keychain:open-brain/github/fixture",
                "--public-onboarding-proof",
            )
        )
        == 2
    )
    payload = _json(capsys)

    assert payload == {"error": {"code": "invalid_arguments"}, "status": "failed"}
    assert "fixture" not in repr(payload)


def test_source_cli_previews_github_repository_from_host_payload_without_bodies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "github-page.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "number": 42,
                    "html_url": "https://github.com/fixture/project/issues/42",
                    "title": "Fixture issue",
                    "body": "Synthetic issue body that must stay out of preview JSON.",
                    "updated_at": "2026-09-14T12:00:00Z",
                },
                {
                    "id": 987,
                    "issue_url": "https://api.github.com/repos/fixture/project/issues/42",
                    "html_url": ("https://github.com/fixture/project/issues/42#issuecomment-987"),
                    "body": "Synthetic comment body that must stay out of preview JSON.",
                    "updated_at": "2026-09-14T12:05:00Z",
                },
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "github",
                "preview-repository",
                "--connection-id",
                "account:fixture",
                "--owner",
                "fixture",
                "--repository",
                "project",
                "--input",
                str(input_path),
                "--next-cursor",
                "cursor:page2",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "cursor:page2"
    assert payload["resource_id"] == "repo:fixture/project"
    content_types = [
        record["content_type"] for record in cast(list[dict[str, object]], payload["records"])
    ]
    assert content_types == [
        "issue",
        "comment",
    ]
    assert "must stay out" not in repr(payload)


def test_source_cli_lists_github_repositories_without_private_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "repositories.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "owner": {"login": "fixture"},
                    "name": "project",
                    "html_url": "https://github.com/fixture/project",
                    "private": True,
                    "description": "Private fixture description must stay local.",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            (
                "github",
                "list-repositories",
                "--input",
                str(input_path),
                "--next-cursor",
                "cursor:repositories2",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "cursor:repositories2"
    assert payload["repositories"] == [
        {
            "html_url": "https://github.com/fixture/project",
            "name": "project",
            "owner": "fixture",
            "selected": False,
        }
    ]
    assert "Private fixture description" not in repr(payload)


def test_source_cli_discovers_github_app_installations_with_private_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")
    observed: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: int) -> _TokenResponse:
        observed["request"] = request
        observed["timeout"] = timeout
        return _TokenResponse(
            {
                "installations": [
                    {
                        "account": {"login": "cbolden15"},
                        "id": 161812976,
                        "permissions": {
                            "issues": "read",
                            "metadata": "read",
                            "pull_requests": "read",
                        },
                        "repository_selection": "selected",
                    }
                ],
                "total_count": 1,
            },
            link_header=(
                '<https://api.github.com/user/installations?per_page=50&page=2>; rel="next"'
            ),
        )

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-installations",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {
        "installations": [
            {
                "account_login": "cbolden15",
                "id": 161812976,
                "permissions": {
                    "issues": "read",
                    "metadata": "read",
                    "pull_requests": "read",
                },
                "repository_selection": "selected",
            }
        ],
        "next_cursor": "2",
        "schema_version": 1,
        "status": "ready",
    }
    request = cast(Request, observed["request"])
    assert request.full_url == "https://api.github.com/user/installations?per_page=50"
    assert request.get_header("Authorization") == "Bearer ghu_fixture_user_access_token"
    assert "ghu_" not in repr(payload)


def test_source_cli_rejects_broader_github_app_installation_permissions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        assert timeout == 15
        return _TokenResponse(
            {
                "installations": [
                    {
                        "account": {"login": "cbolden15"},
                        "id": 161812976,
                        "permissions": {
                            "issues": "write",
                            "metadata": "read",
                            "pull_requests": "read",
                        },
                        "repository_selection": "selected",
                    }
                ],
                "total_count": 1,
            }
        )

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-installations",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 78
    )
    payload = _json(capsys)

    assert payload == {
        "error": {"code": "invalid github installation list"},
        "status": "failed",
    }


def test_source_cli_discovers_selected_private_repositories_without_private_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")
    observed: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: int) -> _TokenResponse:
        observed["request"] = request
        observed["timeout"] = timeout
        return _TokenResponse(
            {
                "repositories": [
                    {
                        "description": "Synthetic private description must stay local.",
                        "html_url": "https://github.com/cbolden15/open-brain-fixture",
                        "name": "open-brain-fixture",
                        "owner": {"login": "cbolden15"},
                        "private": True,
                    }
                ],
                "total_count": 1,
            },
            link_header=(
                "<https://api.github.com/user/installations/161812976/repositories?"
                'per_page=50&page=2>; rel="next"'
            ),
        )

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-selected-repositories",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
                "--installation-id",
                "161812976",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ready"
    assert payload["next_cursor"] == "2"
    assert payload["repositories"] == [
        {
            "html_url": "https://github.com/cbolden15/open-brain-fixture",
            "name": "open-brain-fixture",
            "owner": "cbolden15",
            "selected": True,
        }
    ]
    request = cast(Request, observed["request"])
    assert request.full_url == (
        "https://api.github.com/user/installations/161812976/repositories?per_page=50"
    )
    assert "Synthetic private description" not in repr(payload)
    assert "ghu_" not in repr(payload)


def test_source_cli_live_discovery_maps_revocation_and_rate_limit_states(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")
    calls = 0

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        nonlocal calls
        calls += 1
        headers = HTTPMessage()
        if calls == 1:
            raise HTTPError("", 401, "Unauthorized", headers, None)
        headers.add_header("Retry-After", "30")
        raise HTTPError("", 403, "Forbidden", headers, None)

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-installations",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    revoked = _json(capsys)
    assert revoked == {
        "needs_reauthentication": True,
        "schema_version": 1,
        "status": "needs_sign_in",
    }

    assert (
        run_cli(
            (
                "github",
                "list-installations",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
            )
        )
        == 0
    )
    limited = _json(capsys)
    assert limited == {
        "retry_after_seconds": 30,
        "schema_version": 1,
        "status": "rate_limited",
    }


def test_source_cli_selected_repository_404_reports_access_not_reauth(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_ref = _write_token_session(tmp_path / "credentials")

    def _urlopen(_request: object, *, timeout: int) -> _TokenResponse:
        raise HTTPError("", 404, "Not Found", HTTPMessage(), None)

    monkeypatch.setattr(source_cli, "urlopen", _urlopen)

    assert (
        run_cli(
            (
                "github",
                "list-selected-repositories",
                "--credential-dir",
                str(tmp_path / "credentials"),
                "--credential-ref",
                credential_ref,
                "--installation-id",
                "161812976",
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload == {"schema_version": 1, "status": "not_allowed"}


def test_source_cli_reports_selected_repository_checkpoint_without_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            (
                "github",
                "checkpoint",
                "--connection-id",
                "account:fixture",
                "--owner",
                "fixture",
                "--repository",
                "project",
                "--checkpoint-dir",
                str(tmp_path / "checkpoints"),
            )
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["status"] == "ok"
    assert payload["resource_id"] == "repo:fixture/project"
    assert payload["committed_delivery_ids"] == []
    assert "Synthetic" not in repr(payload)


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return cast(dict[str, object], json.loads(capsys.readouterr().out))


def _write_imessage_database(tmp_path: Path, *, message_count: int = 1) -> Path:
    database = tmp_path / "synthetic-chat.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE chat (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT NOT NULL UNIQUE,
              display_name TEXT
            );
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT NOT NULL UNIQUE,
              text TEXT,
              date INTEGER,
              date_edited INTEGER,
              date_deleted INTEGER,
              cache_has_attachments INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE chat_message_join (
              chat_id INTEGER NOT NULL,
              message_id INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO chat (ROWID, guid, display_name) VALUES (?, ?, ?)",
            (1, "chat-open-brain", "Open Brain Test"),
        )
        for rowid in range(1, message_count + 1):
            connection.execute(
                """
                INSERT INTO message
                  (ROWID, guid, text, date, date_edited, date_deleted, cache_has_attachments)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rowid,
                    f"message-{rowid}",
                    f"Synthetic iMessage body {rowid} must not print.",
                    1790000000 + rowid,
                    0,
                    0,
                    0,
                ),
            )
            connection.execute(
                "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
                (1, rowid),
            )
    return database


class _TokenResponse:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        link_header: str | None = None,
    ) -> None:
        self._payload = payload
        self.headers = HTTPMessage()
        if link_header is not None:
            self.headers.add_header("Link", link_header)

    def __enter__(self) -> _TokenResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _write_device_session(session_dir: Path, digest: str, device_code: str) -> None:
    session_dir.mkdir(parents=True)
    path = session_dir / f"github-device-flow-{digest}.json"
    path.write_text(
        json.dumps(
            {
                "app_type": "github_app",
                "device_code": device_code,
                "permission_model": "github_app_permissions",
                "schema_version": 1,
                "scope": "",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_token_session(
    credential_dir: Path,
    *,
    expires_at_epoch: int = 4_000_000_000,
    refresh_expires_at_epoch: int = 5_000_000_000,
) -> str:
    credential_dir.mkdir(parents=True)
    digest = "0123456789abcdef0123456789abcdef"
    path = credential_dir / f"github-user-token-{digest}.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "ghu_fixture_user_access_token",
                "account_login": None,
                "app_type": "github_app",
                "expires_at_epoch": expires_at_epoch,
                "permission_model": "github_app_permissions",
                "refresh_expires_at_epoch": refresh_expires_at_epoch,
                "refresh_token": "ghr_fixture_refresh_token",
                "schema_version": 1,
                "scope": "",
                "token_type": "bearer",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return f"session:github-user-token/{digest}"


def _token_session_path(credential_dir: Path, credential_ref: str) -> Path:
    return credential_dir / f"github-user-token-{credential_ref.rsplit('/', 1)[1]}.json"
