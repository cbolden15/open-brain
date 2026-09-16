"""Explicit foreground Google Calendar sign-in, preview, and import commands."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from open_brain_engine.engine import (
    LocalEngineContext,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    open_local_engine,
)

from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.google_calendar_auth import GoogleCalendarAuthStore
from open_brain_connectors.runtime.google_calendar_contracts import (
    GoogleCalendarError,
    GoogleCalendarSelection,
)
from open_brain_connectors.runtime.google_calendar_provider import GoogleCalendarClient
from open_brain_connectors.runtime.google_calendar_sync import (
    GoogleCalendarSync,
    GoogleCalendarSyncStore,
)
from open_brain_connectors.runtime.source_host import existing_source_profile
from open_brain_connectors.runtime.source_intake import SourceRecordIntake


def configure_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="google_calendar_command", required=True)
    connect = commands.add_parser("connect", help="Sign in with a Desktop OAuth client.")
    connect.add_argument("--client-config", required=True)
    connect.add_argument("--credential-dir", required=True)
    connect.add_argument("--timeout-seconds", type=int, default=180)
    accounts = commands.add_parser("accounts", help="List saved opaque account identities.")
    accounts.add_argument("--credential-dir", required=True)
    calendars = commands.add_parser("calendars", help="Choose a calendar from authorized metadata.")
    calendars.add_argument("--credential-dir", required=True)
    calendars.add_argument("--connection-id", required=True)
    for verb in ("preview", "import", "status"):
        command = commands.add_parser(verb)
        command.add_argument("--connection-id", required=True)
        command.add_argument("--calendar-id", required=True)
        command.add_argument("--range-start", required=True)
        command.add_argument("--range-end", required=True)
        command.add_argument("--timezone", required=True)
        command.add_argument("--state-dir", required=True)
        command.add_argument("--brain-root", required=True)
        if verb == "preview":
            command.add_argument("--credential-dir", required=True)
        if verb == "import":
            command.add_argument("--preview-id", required=True)


def _client(parsed: argparse.Namespace) -> GoogleCalendarClient:
    def token() -> str:
        # Import and status consume only private local state. A network call from
        # either path is a contract violation, never a reason to request consent.
        if parsed.google_calendar_command not in {"preview", "calendars"}:
            raise GoogleCalendarError("google_calendar_network_not_requested")
        store = GoogleCalendarAuthStore(Path(parsed.credential_dir))
        return store.access_token(parsed.connection_id)

    return GoogleCalendarClient(parsed.connection_id, token)


def _destination(profile: LocalEngineContext) -> str:
    return hashlib.sha256(json.dumps([
        str(profile.root), profile.root_identity, profile.tenant_id,
    ], separators=(",", ":")).encode()).hexdigest()


class _Capture:
    def __init__(self, profile: LocalEngineContext) -> None:
        self.profile = profile
        self.sink: PublicJobCaptureSink | None = None

    def __call__(self, intake: SourceRecordIntake) -> None:
        try:
            if self.sink is None:
                tasks = open_local_engine(self.profile)
                actor = "actor_33333333-3333-4333-8333-333333333333"
                context = PublicJobCaptureContext.create(
                    profile=tasks.profile, actor_id=actor, role_claim={
                        "actor_id": actor, "tenant_id": tasks.profile.tenant_id,
                        "role_id": "role_33333333-3333-4333-8333-333333333333",
                        "role_claim_id": "role_claim_33333333-3333-4333-8333-333333333333",
                        "capabilities": ["capture.accept"],
                    },
                )
                self.sink = tasks.capture.public_job_sink(context)
            self.sink.submit(
                intake.payload(), delivery_id=intake.key.delivery_id(),
                source_origin="third_party", source_reference=intake.source_reference,
                provenance=intake.provenance(), privacy=intake.privacy,
                intent="reference", title=intake.title,
            )
        except (OSError, ValueError, RuntimeError) as error:
            raise GoogleCalendarError("google_calendar_import_failed") from error


def run(parsed: argparse.Namespace) -> dict[str, object]:
    command = parsed.google_calendar_command
    if command == "connect":
        account = GoogleCalendarAuthStore(Path(parsed.credential_dir)).connect(
            Path(parsed.client_config), timeout_seconds=parsed.timeout_seconds,
        )
        return {"status": "connected", "connection_id": account.connection_id,
                "expires_at_epoch": account.expires_at_epoch, "scopes": list(account.scopes)}
    if command == "accounts":
        accounts = GoogleCalendarAuthStore(Path(parsed.credential_dir)).accounts()
        return {"status": "ok", "accounts": [
            {"connection_id": account.connection_id, "expires_at_epoch": account.expires_at_epoch,
             "scopes": list(account.scopes)} for account in accounts
        ]}
    if command == "calendars":
        return {"status": "ok", "calendars": [
            {"calendar_id": item.calendar_id, "title": item.title, "timezone": item.timezone,
             "access_role": item.access_role} for item in _client(parsed).list_calendars()
        ]}
    selection = GoogleCalendarSelection(
        parsed.connection_id, parsed.calendar_id, parsed.range_start, parsed.range_end,
        parsed.timezone,
    )
    try:
        profile = existing_source_profile(Path(parsed.brain_root))
    except ConnectorContractError as error:
        raise GoogleCalendarError("google_calendar_brain_unavailable") from error
    sync = GoogleCalendarSync(
        _client(parsed), GoogleCalendarSyncStore(Path(parsed.state_dir)), _destination(profile),
    )
    if command == "preview":
        return sync.prepare(selection)
    if command == "import":
        return sync.apply(selection, parsed.preview_id, _Capture(profile))
    return sync.status(selection)
