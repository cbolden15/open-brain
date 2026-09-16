"""JSON command surface for host-mediated source onboarding and preview."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, NoReturn, cast
from urllib import parse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime import google_calendar_cli
from open_brain_connectors.runtime.agent_session import (
    AgentSessionCheckpointStore,
    AgentSessionSourceAdapter,
)
from open_brain_connectors.runtime.calendar import (
    CalendarCheckpointStore,
    CalendarSourceAdapter,
)
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.document_files import extract_selected_document
from open_brain_connectors.runtime.document_import import document_preview, import_document
from open_brain_connectors.runtime.github import (
    GitHubDeviceAuthSession,
    GitHubRepositoryCheckpointStore,
    GitHubSourceAdapter,
    GitHubUserTokenStore,
)
from open_brain_connectors.runtime.imessage import (
    ImessageCheckpointStore,
    ImessageSourceAdapter,
)
from open_brain_connectors.runtime.local_document import (
    LocalDocumentCheckpointStore,
    LocalDocumentSourceAdapter,
)
from open_brain_connectors.runtime.meeting_transcript import (
    MeetingTranscriptCheckpointStore,
    MeetingTranscriptSourceAdapter,
)
from open_brain_connectors.runtime.source_registry import (
    SourceCatalog,
    agent_session_source_descriptor,
    calendar_source_descriptor,
    confluence_source_descriptor,
    github_source_descriptor,
    gitlab_source_descriptor,
    gmail_source_descriptor,
    google_drive_source_descriptor,
    imessage_source_descriptor,
    jira_source_descriptor,
    local_document_source_descriptor,
    meeting_transcript_source_descriptor,
    microsoft_mail_source_descriptor,
    notion_source_descriptor,
    slack_source_descriptor,
    web_clip_source_descriptor,
)
from open_brain_connectors.runtime.web_clip import (
    WebClipCheckpointStore,
    WebClipSourceAdapter,
)
from open_brain_connectors.runtime.workspace_content import (
    WorkspaceContentCheckpointStore,
    WorkspaceContentSourceAdapter,
    workspace_auth_profiles,
)

__all__ = ["run_cli"]

_DEVICE_SESSION_REF = re.compile(r"session:github-device-flow/[0-9a-f]{32}")


class _UsageError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        raise _UsageError("invalid command")


def run_cli(argv: tuple[str, ...] | list[str] | None = None) -> int:
    """Run a connector command; only explicit document preview returns body text."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        parsed = _parser().parse_args(arguments)
        if parsed.command is None:
            raise _UsageError("invalid command")
        payload = _run(parsed)
    except _UsageError:
        _write_json({"error": {"code": "invalid_arguments"}, "status": "failed"})
        return 2
    except ConnectorContractError as error:
        _write_json({"error": {"code": str(error)}, "status": "failed"})
        return 78
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        _write_json({"error": {"code": "source_file_unavailable"}, "status": "failed"})
        return 78
    _write_json(payload)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="open-brain-source",
        description="Optional Open Brain source connector metadata and preview commands.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser("catalog", help="List available source connector descriptors.")

    google_calendar_cli.configure_parser(subparsers.add_parser(
        "google-calendar", help="Read-only Google Calendar connection and selected-range import.",
    ))

    github = subparsers.add_parser("github", help="GitHub source onboarding and preview.")
    github_subparsers = github.add_subparsers(dest="github_command", required=True)

    start_auth = github_subparsers.add_parser("start-device-flow")
    start_auth.add_argument("--client-id-env", required=True)
    start_auth.add_argument("--session-dir", required=True)

    complete_auth = github_subparsers.add_parser("complete-device-flow")
    complete_auth.add_argument("--client-id-env", required=True)
    complete_auth.add_argument("--session-dir", required=True)
    complete_auth.add_argument("--device-code-ref", required=True)
    complete_auth.add_argument("--credential-dir", required=True)

    refresh_auth = github_subparsers.add_parser("refresh-token")
    refresh_auth.add_argument("--client-id-env", required=True)
    refresh_auth.add_argument("--credential-dir", required=True)
    refresh_auth.add_argument("--credential-ref", required=True)

    auth_status = github_subparsers.add_parser("auth-status")
    auth_status.add_argument("--credential-dir", required=True)
    auth_status.add_argument("--credential-ref", required=True)

    revoke_auth = github_subparsers.add_parser("mark-revoked")
    revoke_auth.add_argument("--credential-dir", required=True)
    revoke_auth.add_argument("--credential-ref", required=True)

    auth = github_subparsers.add_parser("auth-session")
    auth.add_argument("--device-code-ref", required=True)
    auth.add_argument("--user-code", required=True)
    auth.add_argument("--verification-uri", required=True)
    auth.add_argument("--expires-in-seconds", required=True, type=int)
    auth.add_argument("--interval-seconds", required=True, type=int)

    connection = github_subparsers.add_parser("connection-ref")
    connection.add_argument("--connection-id", required=True)
    connection.add_argument("--account-login", required=True)
    connection.add_argument("--credential-ref", required=True)

    selection = github_subparsers.add_parser("select-repository")
    _add_repository_args(selection)

    listing = github_subparsers.add_parser("list-repositories")
    listing.add_argument("--input", required=True)
    listing.add_argument("--next-cursor")

    live_installations = github_subparsers.add_parser("list-installations")
    live_installations.add_argument("--credential-dir", required=True)
    live_installations.add_argument("--credential-ref", required=True)
    live_installations.add_argument("--next-cursor")

    live_listing = github_subparsers.add_parser("list-selected-repositories")
    live_listing.add_argument("--credential-dir", required=True)
    live_listing.add_argument("--credential-ref", required=True)
    live_listing.add_argument("--installation-id", required=True, type=int)
    live_listing.add_argument("--next-cursor")

    preview = github_subparsers.add_parser("preview-repository")
    _add_repository_args(preview)
    preview.add_argument("--input", required=True)
    preview.add_argument("--next-cursor")

    checkpoint = github_subparsers.add_parser("checkpoint")
    _add_repository_args(checkpoint)
    checkpoint.add_argument("--checkpoint-dir", required=True)

    agent_session = subparsers.add_parser(
        "agent-session",
        help="Explicit local agent-session selection and preview.",
    )
    agent_subparsers = agent_session.add_subparsers(
        dest="agent_session_command",
        required=True,
    )

    agent_selection = agent_subparsers.add_parser("select-project")
    _add_agent_project_args(agent_selection)

    agent_preview = agent_subparsers.add_parser("preview-events")
    _add_agent_project_args(agent_preview)
    agent_preview.add_argument("--input", required=True)
    agent_preview.add_argument("--include-transcripts", action="store_true")
    agent_preview.add_argument("--next-cursor")
    agent_preview.add_argument("--session-id", action="append", required=True)

    agent_checkpoint = agent_subparsers.add_parser("checkpoint")
    _add_agent_project_args(agent_checkpoint)
    agent_checkpoint.add_argument("--checkpoint-dir", required=True)

    local_document = subparsers.add_parser(
        "local-document",
        help="Explicit local document selection and preview.",
    )
    document_subparsers = local_document.add_subparsers(
        dest="local_document_command",
        required=True,
    )

    document_selection = document_subparsers.add_parser("select-file")
    _add_local_document_args(document_selection)

    document_preview = document_subparsers.add_parser("preview-file")
    _add_local_document_args(document_preview)
    document_preview.add_argument("--input", required=True)
    document_preview.add_argument("--next-cursor")
    document_preview.add_argument("--document-id", action="append", required=True)

    document_checkpoint = document_subparsers.add_parser("checkpoint")
    _add_local_document_args(document_checkpoint)
    document_checkpoint.add_argument("--checkpoint-dir", required=True)

    for verb in ("preview", "import"):
        file_command = document_subparsers.add_parser(
            verb, help="Read one selected PDF/DOCX; preview explicitly returns extracted text.",
        )
        file_command.add_argument("--file", required=True)
        file_command.add_argument("--title", required=True)
        file_command.add_argument("--connection-id", default="account:local-documents")
        if verb == "import":
            file_command.add_argument("--preview-id", required=True)
            file_command.add_argument("--brain-root", required=True)

    web_clip = subparsers.add_parser(
        "web-clip",
        help="Explicit browser-to-local web clip selection and preview.",
    )
    web_clip_subparsers = web_clip.add_subparsers(
        dest="web_clip_command",
        required=True,
    )

    web_clip_selection = web_clip_subparsers.add_parser("select-browser")
    _add_web_clip_args(web_clip_selection)

    web_clip_preview = web_clip_subparsers.add_parser("preview-clips")
    _add_web_clip_args(web_clip_preview)
    web_clip_preview.add_argument("--input", required=True)
    web_clip_preview.add_argument("--next-cursor")
    web_clip_preview.add_argument("--clip-id", action="append", required=True)

    web_clip_checkpoint = web_clip_subparsers.add_parser("checkpoint")
    _add_web_clip_args(web_clip_checkpoint)
    web_clip_checkpoint.add_argument("--checkpoint-dir", required=True)

    calendar = subparsers.add_parser(
        "calendar",
        help="Selected calendar date-range selection and preview.",
    )
    calendar_subparsers = calendar.add_subparsers(
        dest="calendar_command",
        required=True,
    )

    calendar_selection = calendar_subparsers.add_parser("select-calendar")
    _add_calendar_args(calendar_selection)

    calendar_preview = calendar_subparsers.add_parser("preview-events")
    _add_calendar_args(calendar_preview)
    calendar_preview.add_argument("--input", required=True)
    calendar_preview.add_argument("--range-start", required=True)
    calendar_preview.add_argument("--range-end", required=True)
    calendar_preview.add_argument("--next-cursor")
    calendar_preview.add_argument("--event-id", action="append", required=True)

    calendar_checkpoint = calendar_subparsers.add_parser("checkpoint")
    _add_calendar_args(calendar_checkpoint)
    calendar_checkpoint.add_argument("--checkpoint-dir", required=True)

    imessage = subparsers.add_parser(
        "imessage",
        help="Explicit selected-conversation iMessage preview.",
    )
    imessage_subparsers = imessage.add_subparsers(
        dest="imessage_command",
        required=True,
    )

    imessage_selection = imessage_subparsers.add_parser("select-conversation")
    _add_imessage_args(imessage_selection)

    imessage_preview = imessage_subparsers.add_parser("preview-messages")
    _add_imessage_args(imessage_preview)
    imessage_preview.add_argument("--next-cursor")
    imessage_preview.add_argument("--next-rowid", type=int)

    imessage_checkpoint = imessage_subparsers.add_parser("checkpoint")
    _add_imessage_args(imessage_checkpoint)
    imessage_checkpoint.add_argument("--checkpoint-dir", required=True)

    meeting_transcript = subparsers.add_parser(
        "meeting-transcript",
        help="Selected Zoom or Google Meet transcript selection and preview.",
    )
    meeting_subparsers = meeting_transcript.add_subparsers(
        dest="meeting_transcript_command",
        required=True,
    )

    meeting_selection = meeting_subparsers.add_parser("select-meeting")
    _add_meeting_transcript_args(meeting_selection)

    meeting_preview = meeting_subparsers.add_parser("preview-transcripts")
    _add_meeting_transcript_args(meeting_preview)
    meeting_preview.add_argument("--input", required=True)
    meeting_preview.add_argument("--next-cursor")
    meeting_preview.add_argument("--selected-meeting-id", action="append", required=True)

    meeting_checkpoint = meeting_subparsers.add_parser("checkpoint")
    _add_meeting_transcript_args(meeting_checkpoint)
    meeting_checkpoint.add_argument("--checkpoint-dir", required=True)

    workspace_content = subparsers.add_parser(
        "workspace-content",
        help="Selected Notion or Confluence page/data-source/space preview.",
    )
    workspace_subparsers = workspace_content.add_subparsers(
        dest="workspace_content_command",
        required=True,
    )

    workspace_selection = workspace_subparsers.add_parser("select-resource")
    _add_workspace_content_args(workspace_selection)

    workspace_preview = workspace_subparsers.add_parser("preview-content")
    _add_workspace_content_args(workspace_preview)
    workspace_preview.add_argument("--input", required=True)
    workspace_preview.add_argument("--next-cursor")
    workspace_preview.add_argument("--provider-format")
    workspace_preview.add_argument("--selected-content-id", action="append", required=True)

    workspace_checkpoint = workspace_subparsers.add_parser("checkpoint")
    _add_workspace_content_args(workspace_checkpoint)
    workspace_checkpoint.add_argument("--checkpoint-dir", required=True)

    workspace_subparsers.add_parser("auth-architecture")
    return parser


def _add_repository_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repository", required=True)


def _add_agent_project_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--project-id", required=True)


def _add_local_document_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--selected-document-id", required=True)
    parser.add_argument("--file-kind", required=True)


def _add_web_clip_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--browser-id", required=True)
    parser.add_argument("--clip-type", required=True)


def _add_calendar_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--calendar-id", required=True)
    parser.add_argument("--provider", required=True)


def _add_imessage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--permission-status", required=True)


def _add_meeting_transcript_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--meeting-id", required=True)
    parser.add_argument("--provider", required=True)


def _add_workspace_content_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--connector", required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--resource-type", required=True)


def _run(parsed: argparse.Namespace) -> dict[str, object]:
    if parsed.command == "google-calendar":
        return google_calendar_cli.run(parsed)
    if parsed.command == "catalog":
        catalog = SourceCatalog(
            (
                github_source_descriptor(),
                agent_session_source_descriptor(),
                calendar_source_descriptor(),
                confluence_source_descriptor(),
                gitlab_source_descriptor(),
                gmail_source_descriptor(),
                google_drive_source_descriptor(),
                imessage_source_descriptor(),
                jira_source_descriptor(),
                local_document_source_descriptor(),
                meeting_transcript_source_descriptor(),
                microsoft_mail_source_descriptor(),
                notion_source_descriptor(),
                slack_source_descriptor(),
                web_clip_source_descriptor(),
            )
        )
        return {
            "connectors": [descriptor.to_dict() for descriptor in catalog.list()],
            "schema_version": 1,
            "status": "ok",
        }
    if parsed.command == "agent-session":
        return _run_agent_session(parsed)
    if parsed.command == "local-document":
        return _run_local_document(parsed)
    if parsed.command == "web-clip":
        return _run_web_clip(parsed)
    if parsed.command == "calendar":
        return _run_calendar(parsed)
    if parsed.command == "imessage":
        return _run_imessage(parsed)
    if parsed.command == "meeting-transcript":
        return _run_meeting_transcript(parsed)
    if parsed.command == "workspace-content":
        return _run_workspace_content(parsed)
    if parsed.command != "github":
        raise _UsageError("invalid command")
    adapter = GitHubSourceAdapter()
    github_command = cast(str, parsed.github_command)
    if github_command == "start-device-flow":
        session = _start_github_device_flow(
            client_id_env=cast(str, parsed.client_id_env),
            session_dir=Path(cast(str, parsed.session_dir)),
            adapter=adapter,
        )
        return {
            "device_code_ref": session.device_code_ref,
            "expires_in_seconds": session.expires_in_seconds,
            "interval_seconds": session.interval_seconds,
            "schema_version": 1,
            "status": "needs_user_verification",
            "user_code": session.user_code,
            "verification_uri": session.verification_uri,
        }
    if github_command == "auth-session":
        session = adapter.device_auth_session(
            device_code_ref=_session_device_code_ref(cast(str, parsed.device_code_ref)),
            user_code=cast(str, parsed.user_code),
            verification_uri=cast(str, parsed.verification_uri),
            expires_in_seconds=cast(int, parsed.expires_in_seconds),
            interval_seconds=cast(int, parsed.interval_seconds),
        )
        return {
            "device_code_ref": session.device_code_ref,
            "expires_in_seconds": session.expires_in_seconds,
            "interval_seconds": session.interval_seconds,
            "schema_version": 1,
            "status": "needs_user_verification",
            "user_code": session.user_code,
            "verification_uri": session.verification_uri,
        }
    if github_command == "complete-device-flow":
        token = _complete_github_device_flow(
            client_id_env=cast(str, parsed.client_id_env),
            session_dir=Path(cast(str, parsed.session_dir)),
            device_code_ref=cast(str, parsed.device_code_ref),
            credential_dir=Path(cast(str, parsed.credential_dir)),
        )
        return token
    if github_command == "refresh-token":
        token = _refresh_github_token(
            client_id_env=cast(str, parsed.client_id_env),
            credential_dir=Path(cast(str, parsed.credential_dir)),
            credential_ref=cast(str, parsed.credential_ref),
        )
        return token
    if github_command == "auth-status":
        token_store = GitHubUserTokenStore(Path(cast(str, parsed.credential_dir)))
        if not token_store.metadata_exists(cast(str, parsed.credential_ref)):
            return {
                "credential_ref": cast(str, parsed.credential_ref),
                "needs_reauthentication": True,
                "refresh_available": False,
                "schema_version": 1,
                "status": "needs_sign_in",
            }
        token_session = token_store.load_metadata(cast(str, parsed.credential_ref))
        needs_reauthentication = token_session.is_expired and not token_session.can_refresh
        status = (
            "needs_sign_in"
            if needs_reauthentication
            else "needs_refresh"
            if token_session.is_expired
            else "ok"
        )
        return {
            "account_login": token_session.account_login,
            "credential_ref": token_session.credential_ref,
            "expires_at_epoch": token_session.expires_at_epoch,
            "needs_reauthentication": needs_reauthentication,
            "refresh_available": token_session.can_refresh,
            "refresh_expires_at_epoch": token_session.refresh_expires_at_epoch,
            "schema_version": 1,
            "scope": token_session.scope,
            "status": status,
            "token_type": token_session.token_type,
        }
    if github_command == "mark-revoked":
        token_store = GitHubUserTokenStore(Path(cast(str, parsed.credential_dir)))
        token_store.revoke_locally(cast(str, parsed.credential_ref))
        return {
            "credential_ref": cast(str, parsed.credential_ref),
            "needs_reauthentication": True,
            "schema_version": 1,
            "status": "needs_sign_in",
        }
    if github_command == "connection-ref":
        connection = adapter.connection_ref(
            connection_id=cast(str, parsed.connection_id),
            account_login=cast(str, parsed.account_login),
            credential_ref=cast(str, parsed.credential_ref),
            public_onboarding_proof=False,
        )
        return {
            "account_login": connection.account_login,
            "connection_id": connection.connection_id,
            "credential_ref": connection.credential_ref,
            "onboarding_mode": "device_flow",
            "public_onboarding_available": True,
            "schema_version": 1,
            "status": "ok",
        }
    if github_command == "select-repository":
        return {
            **_selection(parsed),
            "schema_version": 1,
            "status": "selected",
        }
    if github_command == "list-repositories":
        values = _read_json_list(Path(cast(str, parsed.input)))
        repository_list = adapter.repository_list_page(values, next_cursor=parsed.next_cursor)
        return {**repository_list.to_dict(), "status": repository_list.status.value}
    if github_command == "list-installations":
        return _list_github_app_installations(
            credential_dir=Path(cast(str, parsed.credential_dir)),
            credential_ref=cast(str, parsed.credential_ref),
            next_cursor=parsed.next_cursor,
        )
    if github_command == "list-selected-repositories":
        return _list_selected_github_repositories(
            credential_dir=Path(cast(str, parsed.credential_dir)),
            credential_ref=cast(str, parsed.credential_ref),
            installation_id=cast(int, parsed.installation_id),
            next_cursor=parsed.next_cursor,
            adapter=adapter,
        )
    if github_command == "preview-repository":
        selection = adapter.repository_selection(
            connection_id=cast(str, parsed.connection_id),
            owner=cast(str, parsed.owner),
            repository=cast(str, parsed.repository),
        )
        values = _read_json_list(Path(cast(str, parsed.input)))
        repository_page = adapter.repository_page_from_rest(
            selection,
            values,
            privacy=_public_provider_privacy(),
            next_cursor=parsed.next_cursor,
        )
        if repository_page.preview is None:
            raise ConnectorContractError("invalid github page")
        return {**repository_page.preview.to_dict(), "status": repository_page.status.value}
    if github_command == "checkpoint":
        selection = adapter.repository_selection(
            connection_id=cast(str, parsed.connection_id),
            owner=cast(str, parsed.owner),
            repository=cast(str, parsed.repository),
        )
        checkpoint = GitHubRepositoryCheckpointStore(Path(cast(str, parsed.checkpoint_dir))).load(
            selection
        )
        return {**checkpoint.to_dict(), "status": "ok"}
    raise _UsageError("invalid command")


def _run_agent_session(parsed: argparse.Namespace) -> dict[str, object]:
    adapter = AgentSessionSourceAdapter()
    command = cast(str, parsed.agent_session_command)
    if command == "select-project":
        selection = adapter.project_selection(
            connection_id=cast(str, parsed.connection_id),
            project_id=cast(str, parsed.project_id),
        )
        return {
            "connection_id": selection.connection_id,
            "connector_name": selection.connector_name,
            "resource_id": selection.resource_id,
            "resource_type": selection.resource_type,
            "schema_version": 1,
            "status": "selected",
        }
    if command == "preview-events":
        selection = adapter.project_selection(
            connection_id=cast(str, parsed.connection_id),
            project_id=cast(str, parsed.project_id),
        )
        events = _read_json_list(Path(cast(str, parsed.input)))
        page = adapter.page_from_events(
            selection,
            events,
            privacy=_local_agent_privacy(),
            include_transcripts=cast(bool, parsed.include_transcripts),
            selected_session_ids=tuple(cast(list[str], parsed.session_id)),
            next_cursor=parsed.next_cursor,
        )
        if page.preview is None:
            raise ConnectorContractError("invalid agent session page")
        return {**page.preview.to_dict(), "status": page.status.value}
    if command == "checkpoint":
        selection = adapter.project_selection(
            connection_id=cast(str, parsed.connection_id),
            project_id=cast(str, parsed.project_id),
        )
        checkpoint = AgentSessionCheckpointStore(Path(cast(str, parsed.checkpoint_dir))).load(
            selection
        )
        return {**checkpoint.to_dict(), "status": "ok"}
    raise _UsageError("invalid command")


def _run_local_document(parsed: argparse.Namespace) -> dict[str, object]:
    adapter = LocalDocumentSourceAdapter()
    command = cast(str, parsed.local_document_command)
    if command in {"preview", "import"}:
        connection_id = cast(str, parsed.connection_id)
        document = extract_selected_document(
            Path(cast(str, parsed.file)), title=cast(str, parsed.title),
            connection_id=connection_id,
        )
        if command == "preview":
            return document_preview(document, connection_id)
        return import_document(
            document, connection_id=connection_id, preview_id=cast(str, parsed.preview_id),
            brain_root=Path(cast(str, parsed.brain_root)),
        )
    if command == "select-file":
        selection = adapter.file_selection(
            connection_id=cast(str, parsed.connection_id),
            document_id=cast(str, parsed.selected_document_id),
            file_kind=cast(str, parsed.file_kind),
        )
        return {
            "connection_id": selection.connection_id,
            "connector_name": selection.connector_name,
            "resource_id": selection.resource_id,
            "resource_type": selection.resource_type,
            "schema_version": 1,
            "status": "selected",
        }
    if command == "preview-file":
        selection = adapter.file_selection(
            connection_id=cast(str, parsed.connection_id),
            document_id=cast(str, parsed.selected_document_id),
            file_kind=cast(str, parsed.file_kind),
        )
        documents = _read_json_list(Path(cast(str, parsed.input)))
        page = adapter.page_from_documents(
            selection,
            documents,
            privacy=_local_agent_privacy(),
            selected_document_ids=tuple(cast(list[str], parsed.document_id)),
            next_cursor=parsed.next_cursor,
        )
        if page.preview is None:
            raise ConnectorContractError("invalid local document page")
        return {**page.preview.to_dict(), "status": page.status.value}
    if command == "checkpoint":
        selection = adapter.file_selection(
            connection_id=cast(str, parsed.connection_id),
            document_id=cast(str, parsed.selected_document_id),
            file_kind=cast(str, parsed.file_kind),
        )
        checkpoint = LocalDocumentCheckpointStore(Path(cast(str, parsed.checkpoint_dir))).load(
            selection
        )
        return {**checkpoint.to_dict(), "status": "ok"}
    raise _UsageError("invalid command")


def _run_web_clip(parsed: argparse.Namespace) -> dict[str, object]:
    adapter = WebClipSourceAdapter()
    command = cast(str, parsed.web_clip_command)
    if command == "select-browser":
        selection = adapter.clip_selection(
            connection_id=cast(str, parsed.connection_id),
            browser_id=cast(str, parsed.browser_id),
            clip_type=cast(str, parsed.clip_type),
        )
        return {
            "connection_id": selection.connection_id,
            "connector_name": selection.connector_name,
            "resource_id": selection.resource_id,
            "resource_type": selection.resource_type,
            "schema_version": 1,
            "status": "selected",
        }
    if command == "preview-clips":
        selection = adapter.clip_selection(
            connection_id=cast(str, parsed.connection_id),
            browser_id=cast(str, parsed.browser_id),
            clip_type=cast(str, parsed.clip_type),
        )
        clips = _read_json_list(Path(cast(str, parsed.input)))
        page = adapter.page_from_clips(
            selection,
            clips,
            privacy=_local_agent_privacy(),
            selected_clip_ids=tuple(cast(list[str], parsed.clip_id)),
            next_cursor=parsed.next_cursor,
        )
        if page.preview is None:
            raise ConnectorContractError("invalid web clip page")
        return {**page.preview.to_dict(), "status": page.status.value}
    if command == "checkpoint":
        selection = adapter.clip_selection(
            connection_id=cast(str, parsed.connection_id),
            browser_id=cast(str, parsed.browser_id),
            clip_type=cast(str, parsed.clip_type),
        )
        checkpoint = WebClipCheckpointStore(Path(cast(str, parsed.checkpoint_dir))).load(selection)
        return {**checkpoint.to_dict(), "status": "ok"}
    raise _UsageError("invalid command")


def _run_calendar(parsed: argparse.Namespace) -> dict[str, object]:
    adapter = CalendarSourceAdapter()
    command = cast(str, parsed.calendar_command)
    if command == "select-calendar":
        selection = adapter.calendar_selection(
            connection_id=cast(str, parsed.connection_id),
            calendar_id=cast(str, parsed.calendar_id),
            provider=cast(str, parsed.provider),
        )
        return {
            "connection_id": selection.connection_id,
            "connector_name": selection.connector_name,
            "resource_id": selection.resource_id,
            "resource_type": selection.resource_type,
            "schema_version": 1,
            "status": "selected",
        }
    if command == "preview-events":
        selection = adapter.calendar_selection(
            connection_id=cast(str, parsed.connection_id),
            calendar_id=cast(str, parsed.calendar_id),
            provider=cast(str, parsed.provider),
        )
        events = _read_json_list(Path(cast(str, parsed.input)))
        page = adapter.page_from_events(
            selection,
            events,
            privacy=_local_agent_privacy(),
            selected_event_ids=tuple(cast(list[str], parsed.event_id)),
            range_start=cast(str, parsed.range_start),
            range_end=cast(str, parsed.range_end),
            next_cursor=parsed.next_cursor,
        )
        if page.preview is None:
            raise ConnectorContractError("invalid calendar page")
        return {**page.preview.to_dict(), "status": page.status.value}
    if command == "checkpoint":
        selection = adapter.calendar_selection(
            connection_id=cast(str, parsed.connection_id),
            calendar_id=cast(str, parsed.calendar_id),
            provider=cast(str, parsed.provider),
        )
        checkpoint = CalendarCheckpointStore(Path(cast(str, parsed.checkpoint_dir))).load(selection)
        return {**checkpoint.to_dict(), "status": "ok"}
    raise _UsageError("invalid command")


def _run_imessage(parsed: argparse.Namespace) -> dict[str, object]:
    adapter = ImessageSourceAdapter()
    command = cast(str, parsed.imessage_command)
    selection = adapter.conversation_selection(
        connection_id=cast(str, parsed.connection_id),
        database_path=Path(cast(str, parsed.database)),
        conversation_id=cast(str, parsed.conversation_id),
        owner_permission_status=cast(
            Literal["granted", "denied", "not_determined"],
            parsed.permission_status,
        ),
    )
    if command == "select-conversation":
        return {
            "connection_id": selection.connection_id,
            "connector_name": selection.connector_name,
            "resource_id": selection.resource_id,
            "resource_type": selection.resource_type,
            "schema_version": 1,
            "status": "selected",
        }
    if command == "preview-messages":
        page = adapter.page_from_sqlite(
            selection,
            database_path=Path(cast(str, parsed.database)),
            privacy=_local_agent_privacy(),
            after_rowid=_imessage_after_rowid(parsed.next_cursor, parsed.next_rowid),
        )
        if page.preview is None:
            return {
                "connector_name": selection.connector_name,
                "resource_id": selection.resource_id,
                "resource_type": selection.resource_type,
                "schema_version": 1,
                "status": page.status.value,
            }
        return {**page.preview.to_dict(), "status": page.status.value}
    if command == "checkpoint":
        checkpoint = ImessageCheckpointStore(Path(cast(str, parsed.checkpoint_dir))).load(
            selection
        )
        return {**checkpoint.to_dict(), "status": "ok"}
    raise _UsageError("invalid command")


def _imessage_after_rowid(next_cursor: object, next_rowid: object) -> int | None:
    if next_cursor is not None and next_rowid is not None:
        raise _UsageError("invalid command")
    if next_cursor is None:
        return cast(int | None, next_rowid)
    cursor = cast(str, next_cursor)
    if not cursor.startswith("row:"):
        raise _UsageError("invalid command")
    try:
        rowid = int(cursor.removeprefix("row:"))
    except ValueError as error:
        raise _UsageError("invalid command") from error
    if rowid < 0:
        raise _UsageError("invalid command")
    return rowid


def _run_meeting_transcript(parsed: argparse.Namespace) -> dict[str, object]:
    adapter = MeetingTranscriptSourceAdapter()
    command = cast(str, parsed.meeting_transcript_command)
    if command == "select-meeting":
        selection = adapter.meeting_selection(
            connection_id=cast(str, parsed.connection_id),
            meeting_id=cast(str, parsed.meeting_id),
            provider=cast(str, parsed.provider),
        )
        return {
            "connection_id": selection.connection_id,
            "connector_name": selection.connector_name,
            "resource_id": selection.resource_id,
            "resource_type": selection.resource_type,
            "schema_version": 1,
            "status": "selected",
        }
    if command == "preview-transcripts":
        selection = adapter.meeting_selection(
            connection_id=cast(str, parsed.connection_id),
            meeting_id=cast(str, parsed.meeting_id),
            provider=cast(str, parsed.provider),
        )
        transcripts = _read_json_list(Path(cast(str, parsed.input)))
        page = adapter.page_from_transcripts(
            selection,
            transcripts,
            privacy=_local_agent_privacy(),
            selected_meeting_ids=tuple(cast(list[str], parsed.selected_meeting_id)),
            next_cursor=parsed.next_cursor,
        )
        if page.preview is None:
            raise ConnectorContractError("invalid meeting transcript page")
        return {**page.preview.to_dict(), "status": page.status.value}
    if command == "checkpoint":
        selection = adapter.meeting_selection(
            connection_id=cast(str, parsed.connection_id),
            meeting_id=cast(str, parsed.meeting_id),
            provider=cast(str, parsed.provider),
        )
        checkpoint = MeetingTranscriptCheckpointStore(Path(cast(str, parsed.checkpoint_dir))).load(
            selection
        )
        return {**checkpoint.to_dict(), "status": "ok"}
    raise _UsageError("invalid command")


def _run_workspace_content(parsed: argparse.Namespace) -> dict[str, object]:
    adapter = WorkspaceContentSourceAdapter()
    command = cast(str, parsed.workspace_content_command)
    if command == "auth-architecture":
        return {
            "profiles": [profile.to_dict() for profile in workspace_auth_profiles()],
            "schema_version": 1,
            "status": "ok",
        }
    if command == "select-resource":
        selection = adapter.resource_selection(
            connector_name=cast(str, parsed.connector),
            connection_id=cast(str, parsed.connection_id),
            resource_id=cast(str, parsed.resource_id),
            resource_type=cast(str, parsed.resource_type),
        )
        return {
            "connection_id": selection.connection_id,
            "connector_name": selection.connector_name,
            "resource_id": selection.resource_id,
            "resource_type": selection.resource_type,
            "schema_version": 1,
            "status": "selected",
        }
    if command == "preview-content":
        selection = adapter.resource_selection(
            connector_name=cast(str, parsed.connector),
            connection_id=cast(str, parsed.connection_id),
            resource_id=cast(str, parsed.resource_id),
            resource_type=cast(str, parsed.resource_type),
        )
        selected_content_ids = tuple(cast(list[str], parsed.selected_content_id))
        provider_format = cast(str | None, parsed.provider_format)
        if provider_format is None:
            contents = _read_json_list(Path(cast(str, parsed.input)))
            page = adapter.page_from_items(
                selection,
                contents,
                privacy=_local_agent_privacy(),
                selected_content_ids=selected_content_ids,
                next_cursor=parsed.next_cursor,
            )
        else:
            response = _read_json_object(Path(cast(str, parsed.input)))
            if parsed.next_cursor is not None:
                raise _UsageError("invalid command")
            if provider_format == "notion-api":
                page = adapter.page_from_notion_response(
                    selection,
                    response,
                    privacy=_local_agent_privacy(),
                    selected_content_ids=selected_content_ids,
                )
            elif provider_format == "confluence-cloud-v2":
                page = adapter.page_from_confluence_response(
                    selection,
                    response,
                    privacy=_local_agent_privacy(),
                    selected_content_ids=selected_content_ids,
                )
            else:
                raise _UsageError("invalid command")
        if page.preview is None:
            raise ConnectorContractError("invalid workspace content page")
        return {**page.preview.to_dict(), "status": page.status.value}
    if command == "checkpoint":
        selection = adapter.resource_selection(
            connector_name=cast(str, parsed.connector),
            connection_id=cast(str, parsed.connection_id),
            resource_id=cast(str, parsed.resource_id),
            resource_type=cast(str, parsed.resource_type),
        )
        checkpoint = WorkspaceContentCheckpointStore(
            Path(cast(str, parsed.checkpoint_dir))
        ).load(selection)
        return {**checkpoint.to_dict(), "status": "ok"}
    raise _UsageError("invalid command")


def _selection(parsed: argparse.Namespace) -> dict[str, object]:
    selection = GitHubSourceAdapter().repository_selection(
        connection_id=cast(str, parsed.connection_id),
        owner=cast(str, parsed.owner),
        repository=cast(str, parsed.repository),
    )
    return {
        "connection_id": selection.connection_id,
        "connector_name": selection.connector_name,
        "resource_id": selection.resource_id,
        "resource_type": selection.resource_type,
    }


def _read_json_list(path: Path) -> Sequence[Mapping[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ConnectorContractError("invalid source input")
    return cast(Sequence[Mapping[str, object]], value)


def _read_json_object(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid source input")
    return cast(Mapping[str, object], value)


def _start_github_device_flow(
    *,
    client_id_env: str,
    session_dir: Path,
    adapter: GitHubSourceAdapter,
) -> GitHubDeviceAuthSession:
    client_id = _github_client_id(client_id_env)
    payload = parse.urlencode({"client_id": client_id}).encode("ascii")
    response = urlopen(  # noqa: S310 - fixed GitHub endpoint, no caller URL.
        Request(
            "https://github.com/login/device/code",
            data=payload,
            headers={"Accept": "application/json"},
            method="POST",
        ),
        timeout=15,
    )
    with response:
        decoded = json.loads(response.read().decode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise ConnectorContractError("invalid github auth session")
    device_code = _required_auth_str(decoded.get("device_code"))
    digest = hashlib.sha256(device_code.encode("utf-8")).hexdigest()
    session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    session_path = session_dir / f"github-device-flow-{digest[:32]}.json"
    session_payload = (
        json.dumps(
            {
                "app_type": "github_app",
                "device_code": device_code,
                "permission_model": "github_app_permissions",
                "schema_version": 1,
                "scope": "",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    with NamedTemporaryFile(
        "w",
        delete=False,
        dir=session_dir,
        encoding="utf-8",
        prefix=f".{session_path.name}.",
        suffix=".tmp",
    ) as handle:
        handle.write(session_payload)
        temp_name = handle.name
    Path(temp_name).chmod(0o600)
    try:
        os.replace(temp_name, session_path)
    except OSError:
        Path(temp_name).unlink(missing_ok=True)
        raise
    session_path.chmod(0o600)
    return adapter.device_auth_session(
        device_code_ref=f"session:github-device-flow/{digest[:32]}",
        user_code=_required_auth_str(decoded.get("user_code")),
        verification_uri=_required_auth_str(decoded.get("verification_uri")),
        expires_in_seconds=_required_auth_int(decoded.get("expires_in")),
        interval_seconds=_required_auth_int(decoded.get("interval")),
    )


def _complete_github_device_flow(
    *,
    client_id_env: str,
    session_dir: Path,
    device_code_ref: str,
    credential_dir: Path,
) -> dict[str, object]:
    session_path = _device_session_path(session_dir, _session_device_code_ref(device_code_ref))
    try:
        decoded_session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConnectorContractError("invalid github auth session") from error
    if not isinstance(decoded_session, Mapping):
        raise ConnectorContractError("invalid github auth session")
    response = _request_github_token(
        client_id_env=client_id_env,
        parameters={
            "device_code": _required_auth_str(decoded_session.get("device_code")),
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    status = _github_auth_error_status(response)
    if status is not None:
        return status
    return _persist_github_token(response, credential_dir=credential_dir)


def _refresh_github_token(
    *,
    client_id_env: str,
    credential_dir: Path,
    credential_ref: str,
) -> dict[str, object]:
    token_store = GitHubUserTokenStore(credential_dir)
    refresh_token = token_store.load_refresh_token(credential_ref)
    response = _request_github_token(
        client_id_env=client_id_env,
        parameters={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    status = _github_auth_error_status(response)
    if status is not None:
        if status["status"] == "needs_sign_in":
            token_store.revoke_locally(credential_ref)
        return status
    token_store.revoke_locally(credential_ref)
    return _persist_github_token(response, credential_dir=credential_dir)


def _list_github_app_installations(
    *,
    credential_dir: Path,
    credential_ref: str,
    next_cursor: str | None,
) -> dict[str, object]:
    response = _request_github_api(
        credential_dir=credential_dir,
        credential_ref=credential_ref,
        path="/user/installations",
        query={"per_page": "50", **({"page": next_cursor} if next_cursor else {})},
    )
    status = _github_api_error_status(response)
    if status is not None:
        return status
    installations = response.get("installations")
    if not isinstance(installations, list):
        raise ConnectorContractError("invalid github installation list")
    safe_installations: list[dict[str, object]] = []
    for installation in installations:
        if not isinstance(installation, Mapping):
            raise ConnectorContractError("invalid github installation list")
        account = installation.get("account")
        if not isinstance(account, Mapping):
            raise ConnectorContractError("invalid github installation list")
        safe_installations.append(
            {
                "account_login": _required_auth_str(account.get("login")),
                "id": _required_auth_int(installation.get("id")),
                "permissions": _safe_github_permissions(installation.get("permissions")),
                "repository_selection": _required_auth_str(
                    installation.get("repository_selection")
                ),
            }
        )
    return {
        "installations": safe_installations,
        "next_cursor": response.get("_next_cursor"),
        "schema_version": 1,
        "status": "ready",
    }


def _list_selected_github_repositories(
    *,
    credential_dir: Path,
    credential_ref: str,
    installation_id: int,
    next_cursor: str | None,
    adapter: GitHubSourceAdapter,
) -> dict[str, object]:
    if installation_id <= 0:
        raise ConnectorContractError("invalid github installation")
    response = _request_github_api(
        credential_dir=credential_dir,
        credential_ref=credential_ref,
        path=f"/user/installations/{installation_id}/repositories",
        query={"per_page": "50", **({"page": next_cursor} if next_cursor else {})},
        not_found_status="not_allowed",
    )
    status = _github_api_error_status(response)
    if status is not None:
        return status
    repositories = response.get("repositories")
    if not isinstance(repositories, list):
        raise ConnectorContractError("invalid github repository list")
    repository_list = adapter.repository_list_page(
        cast(Sequence[Mapping[str, object]], repositories),
        next_cursor=_optional_next_cursor(response.get("_next_cursor")),
        selected=True,
    )
    return {**repository_list.to_dict(), "status": repository_list.status.value}


def _request_github_token(
    *,
    client_id_env: str,
    parameters: Mapping[str, str],
) -> Mapping[str, object]:
    client_id = _github_client_id(client_id_env)
    payload = parse.urlencode({"client_id": client_id, **parameters}).encode("ascii")
    response = urlopen(  # noqa: S310 - fixed GitHub endpoint, no caller URL.
        Request(
            "https://github.com/login/oauth/access_token",
            data=payload,
            headers={"Accept": "application/json"},
            method="POST",
        ),
        timeout=15,
    )
    with response:
        decoded = json.loads(response.read().decode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise ConnectorContractError("invalid github token session")
    return decoded


def _request_github_api(
    *,
    credential_dir: Path,
    credential_ref: str,
    path: str,
    query: Mapping[str, str],
    not_found_status: str = "needs_sign_in",
) -> Mapping[str, object]:
    access_token = GitHubUserTokenStore(credential_dir).load_access_token(credential_ref)
    if not path.startswith("/"):
        raise ConnectorContractError("invalid github api path")
    encoded_query = parse.urlencode(query)
    url = f"https://api.github.com{path}"
    if encoded_query:
        url = f"{url}?{encoded_query}"
    try:
        response = urlopen(  # noqa: S310 - fixed GitHub API host and validated path.
            Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                method="GET",
            ),
            timeout=15,
        )
        with response:
            decoded = json.loads(response.read().decode("utf-8"))
            link = response.headers.get("Link")
    except HTTPError as error:
        return _github_http_error(error, not_found_status=not_found_status)
    if not isinstance(decoded, Mapping):
        raise ConnectorContractError("invalid github api response")
    return {**decoded, "_next_cursor": _next_cursor_from_link_header(link)}


def _github_http_error(error: HTTPError, *, not_found_status: str) -> dict[str, object]:
    retry_after = error.headers.get("Retry-After")
    if error.code == 401:
        return {"needs_reauthentication": True, "schema_version": 1, "status": "needs_sign_in"}
    if error.code == 404:
        if not_found_status == "not_allowed":
            return {"schema_version": 1, "status": "not_allowed"}
        return {"needs_reauthentication": True, "schema_version": 1, "status": "needs_sign_in"}
    if error.code == 403 and retry_after:
        try:
            retry_after_seconds = int(retry_after)
        except ValueError:
            retry_after_seconds = 60
        return {
            "retry_after_seconds": max(1, min(retry_after_seconds, 86_400)),
            "schema_version": 1,
            "status": "rate_limited",
        }
    if error.code == 403:
        return {"schema_version": 1, "status": "not_allowed"}
    raise ConnectorContractError("invalid github api response") from error


def _next_cursor_from_link_header(value: str | None) -> str | None:
    if value is None:
        return None
    for part in value.split(","):
        url_part, *parameter_parts = part.split(";")
        if not any(parameter.strip() == 'rel="next"' for parameter in parameter_parts):
            continue
        url = url_part.strip()
        if not (url.startswith("<") and url.endswith(">")):
            raise ConnectorContractError("invalid github api response")
        parsed = parse.urlparse(url[1:-1])
        query = parse.parse_qs(parsed.query)
        pages = query.get("page")
        if pages is None or len(pages) != 1:
            raise ConnectorContractError("invalid github api response")
        return _optional_next_cursor(pages[0])
    return None


def _optional_next_cursor(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.isdecimal() or int(value) <= 0:
        raise ConnectorContractError("invalid github api response")
    return value


def _github_api_error_status(decoded: Mapping[str, object]) -> dict[str, object] | None:
    status = decoded.get("status")
    if status in {"needs_sign_in", "rate_limited", "not_allowed"}:
        return dict(decoded)
    return None


def _persist_github_token(
    decoded: Mapping[str, object],
    *,
    credential_dir: Path,
) -> dict[str, object]:
    session = GitHubUserTokenStore(credential_dir).save_from_response(decoded)
    return {
        "credential_ref": session.credential_ref,
        "expires_at_epoch": session.expires_at_epoch,
        "needs_reauthentication": False,
        "refresh_expires_at_epoch": session.refresh_expires_at_epoch,
        "schema_version": 1,
        "scope": session.scope,
        "status": "authenticated",
        "token_type": session.token_type,
    }


def _github_auth_error_status(decoded: Mapping[str, object]) -> dict[str, object] | None:
    error = decoded.get("error")
    if error is None:
        return None
    if error == "authorization_pending":
        return {
            "next_poll_seconds": None,
            "schema_version": 1,
            "status": "authorization_pending",
        }
    if error == "slow_down":
        return {
            "next_poll_seconds": 5,
            "schema_version": 1,
            "status": "slow_down",
        }
    if error in {"expired_token", "access_denied", "bad_refresh_token"}:
        return {
            "needs_reauthentication": True,
            "schema_version": 1,
            "status": "needs_sign_in",
        }
    raise ConnectorContractError("invalid github token session")


def _required_auth_str(value: object) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ConnectorContractError("invalid github auth session")
    return value


def _required_auth_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid github auth session")
    return value


def _safe_github_permissions(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid github installation list")
    safe: dict[str, str] = {}
    for key, permission in value.items():
        if (
            type(key) is not str
            or key not in {"issues", "metadata", "pull_requests"}
            or permission != "read"
        ):
            raise ConnectorContractError("invalid github installation list")
        safe[key] = cast(str, permission)
    return safe


def _session_device_code_ref(value: str) -> str:
    if type(value) is not str or _DEVICE_SESSION_REF.fullmatch(value) is None:
        raise ConnectorContractError("invalid github auth session")
    return value


def _device_session_path(session_dir: Path, device_code_ref: str) -> Path:
    return session_dir / f"github-device-flow-{device_code_ref.rsplit('/', 1)[1]}.json"


def _github_client_id(client_id_env: str) -> str:
    if (
        not client_id_env.startswith("OPEN_BRAIN_")
        or not client_id_env.endswith("CLIENT_ID")
        or client_id_env not in os.environ
    ):
        raise ConnectorContractError("github public app registration required")
    client_id = os.environ[client_id_env]
    if not isinstance(client_id, str) or not client_id.strip() or "\x00" in client_id:
        raise ConnectorContractError("github public app registration required")
    return client_id


def _public_provider_privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": True},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "policy_public",
            "tier": "public",
        }
    )


def _local_agent_privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": False},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "personal_local_only",
            "tier": "personal",
        }
    )


def _write_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
