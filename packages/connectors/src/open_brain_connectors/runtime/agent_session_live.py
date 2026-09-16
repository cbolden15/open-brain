"""Fresh, selected Claude Code and Codex session capture.

The source consumes only events previously submitted by its project-local hook.  It
does not enumerate client state, mutate checkpoints, or retain transcript text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from open_brain_connectors.runtime.agent_session import (
    AgentSessionRecord,
    AgentSessionSourceAdapter,
)
from open_brain_connectors.runtime.agent_session_hooks import (
    QueuedSessionEvent,
    acknowledge_queued_events,
    project_id_for_path,
    queue_overflow_count,
    queued_events,
)
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveSourceError,
    local_source_privacy,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import (
    D4_AGENT_SESSION_SOURCE,
    SourceResourceSelection,
)

_CLIENTS = frozenset({"claude_code", "codex"})
_MAX_TRANSCRIPT_BYTES = 524_288
_MAX_MESSAGES = 64
_MAX_TEXT = 65_536
_MAX_SUMMARY_MESSAGES = 4
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
_CREDENTIAL_URL = re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@")

SessionClient = Literal["claude_code", "codex"]


@dataclass(frozen=True, slots=True)
class _Message:
    role: Literal["user", "assistant"]
    text: str


class AgentSessionLiveSource:
    """Create bounded intakes from hook-enqueued, exact-project session events."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise LiveSourceError("source_invalid_value")
        self._root = root
        self._adapter = AgentSessionSourceAdapter()

    @staticmethod
    def project_id(project_path: Path) -> str:
        return project_id_for_path(project_path)

    @staticmethod
    def connection_id(client: SessionClient) -> str:
        if client not in _CLIENTS:
            raise LiveSourceError("source_invalid_options")
        return f"account:local:{client}"

    def selection(self, *, client: SessionClient, project_path: Path) -> SourceResourceSelection:
        normalized_client = _required_client(client)
        try:
            project_id = self.project_id(project_path)
        except ValueError as error:
            raise LiveSourceError("source_invalid_project") from error
        return SourceResourceSelection(
            connector_name=D4_AGENT_SESSION_SOURCE,
            connection_id=self.connection_id(normalized_client),
            resource_id=project_id,
            resource_type="local_project",
        )

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        """Return one batch and proposed state without consuming queued events."""
        client, project, capture_summary, capture_transcript = _options(options)
        _require_selection(selection, client, project)
        captured_ids = _checkpoint(checkpoint, client, selection.resource_id, project)
        if not capture_summary and not capture_transcript:
            disabled_checkpoint: dict[str, object] = {
                "captured_event_ids": list(captured_ids),
                "client": client,
                "project_id": selection.resource_id,
                "project_path": str(project),
                "schema_version": 1,
            }
            return LiveBatch(
                intakes=(),
                checkpoint=disabled_checkpoint,
            )
        candidates = _latest_events(
            queued_events(self._root),
            client=client,
            project_id=selection.resource_id,
            project_path=str(project),
            captured_ids=captured_ids,
        )
        accepted: list[tuple[QueuedSessionEvent, tuple[_Message, ...]]] = []
        notices: list[str] = ["session_queue_full"] if queue_overflow_count(self._root) else []
        for event in candidates:
            try:
                messages = _read_messages(event)
            except _SessionFormatError as error:
                notices.append(error.notice)
                continue
            if _contains_secret(messages):
                notices.append("session_quarantined_secret")
                continue
            accepted.append((event, messages))
        page = accepted[:25]
        intakes = tuple(
            intake
            for event, messages in page
            for intake in _intakes(
                self._adapter,
                selection,
                event,
                messages,
                capture_summary=capture_summary,
                capture_transcript=capture_transcript,
            )
        )
        # A transcript produces at most two intakes.  Avoid accepting more than
        # 12 sessions when both choices are enabled to honour LiveBatch's cap.
        if len(intakes) > 25:
            max_events = 12 if capture_summary and capture_transcript else 25
            page = accepted[:max_events]
            intakes = tuple(
                intake
                for event, messages in page
                for intake in _intakes(
                    self._adapter,
                    selection,
                    event,
                    messages,
                    capture_summary=capture_summary,
                    capture_transcript=capture_transcript,
                )
            )
        proposed_ids = tuple(event.event_id for event, _messages in page)
        proposed: dict[str, object] = {
            "captured_event_ids": list(_bounded_ids((*captured_ids, *proposed_ids))),
            "client": client,
            "project_id": selection.resource_id,
            "project_path": str(project),
            "schema_version": 1,
        }
        return LiveBatch(
            intakes=intakes,
            checkpoint=proposed,
            has_more=len(accepted) > len(page),
            notices=tuple(dict.fromkeys(notices))[:25],
        )

    def acknowledge(
        self,
        selection: SourceResourceSelection,
        checkpoint: dict[str, object],
    ) -> None:
        """Remove only events proved durable by the caller's committed checkpoint.

        The collector must call this after capture and checkpoint persistence succeeds,
        never from ``fetch``. Repeating it is safe and clears stale superseded events.
        """
        client, project_id, project, captured_ids = _acknowledgement_checkpoint(checkpoint)
        _require_selection(selection, client, project)
        acknowledge_queued_events(
            self._root,
            client=client,
            project_id=project_id,
            project_path=str(project),
            captured_event_ids=captured_ids,
        )


class _SessionFormatError(ValueError):
    def __init__(self, notice: str) -> None:
        super().__init__(notice)
        self.notice = notice


def _options(options: object) -> tuple[SessionClient, Path, bool, bool]:
    if not isinstance(options, dict) or set(options) != {
        "capture_summary",
        "capture_transcript",
        "client",
        "project_path",
    }:
        raise LiveSourceError("source_invalid_options")
    client = _required_client(options["client"])
    if (
        type(options["capture_summary"]) is not bool
        or type(options["capture_transcript"]) is not bool
    ):
        raise LiveSourceError("source_invalid_options")
    if not options["capture_summary"] and not options["capture_transcript"]:
        return client, _project_path(options["project_path"]), False, False
    return (
        client,
        _project_path(options["project_path"]),
        options["capture_summary"],
        options["capture_transcript"],
    )


def _project_path(value: object) -> Path:
    if type(value) is not str or "\x00" in value:
        raise LiveSourceError("source_invalid_options")
    path = Path(value)
    if not path.is_absolute():
        raise LiveSourceError("source_invalid_options")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise LiveSourceError("source_invalid_project") from error


def _require_selection(selection: object, client: SessionClient, project: Path) -> None:
    expected_project_id = project_id_for_path(project)
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != D4_AGENT_SESSION_SOURCE
        or selection.resource_type != "local_project"
        or selection.connection_id != AgentSessionLiveSource.connection_id(client)
        or selection.resource_id != expected_project_id
    ):
        raise LiveSourceError("source_selection_mismatch")


def _checkpoint(
    value: object,
    client: SessionClient,
    project_id: str,
    project: Path,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or set(value) != {
        "captured_event_ids",
        "client",
        "project_id",
        "project_path",
        "schema_version",
    }:
        raise LiveSourceError("source_invalid_checkpoint")
    event_ids = value["captured_event_ids"]
    if (
        value["schema_version"] != 1
        or value["client"] != client
        or value["project_id"] != project_id
        or value["project_path"] != str(project)
        or not isinstance(event_ids, list)
        or len(event_ids) > 256
        or any(
            type(event_id) is not str or not re.fullmatch(r"[a-f0-9]{32}", event_id)
            for event_id in event_ids
        )
    ):
        raise LiveSourceError("source_invalid_checkpoint")
    return tuple(cast(str, event_id) for event_id in event_ids)


def _acknowledgement_checkpoint(
    value: object,
) -> tuple[SessionClient, str, Path, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != {
        "captured_event_ids",
        "client",
        "project_id",
        "project_path",
        "schema_version",
    }:
        raise LiveSourceError("source_invalid_checkpoint")
    client = _required_client(value["client"])
    project_id = value["project_id"]
    if type(project_id) is not str or not re.fullmatch(r"project:[a-f0-9]{16,64}", project_id):
        raise LiveSourceError("source_invalid_checkpoint")
    project = _project_path(value["project_path"])
    return client, project_id, project, _checkpoint(value, client, project_id, project)


def _latest_events(
    events: Sequence[QueuedSessionEvent],
    *,
    client: SessionClient,
    project_id: str,
    project_path: str,
    captured_ids: Sequence[str],
) -> tuple[QueuedSessionEvent, ...]:
    selected: dict[str, QueuedSessionEvent] = {}
    for event in events:
        if (
            event.client != client
            or event.project_id != project_id
            or event.project_path != project_path
        ):
            continue
        current = selected.get(event.session_id)
        if current is None or (event.observed_mtime_ns, event.event_id) > (
            current.observed_mtime_ns,
            current.event_id,
        ):
            selected[event.session_id] = event
    captured = set(captured_ids)
    return tuple(
        event
        for event in sorted(
            selected.values(), key=lambda item: (item.observed_mtime_ns, item.event_id)
        )
        if event.event_id not in captured
    )


def _read_messages(event: QueuedSessionEvent) -> tuple[_Message, ...]:
    path = Path(event.transcript_path)
    try:
        if path.is_symlink() or not path.is_file():
            raise _SessionFormatError("session_transcript_unavailable")
        stat = path.stat()
        if stat.st_dev != event.transcript_device or stat.st_ino != event.transcript_inode:
            raise _SessionFormatError("session_transcript_changed")
        raw = path.read_bytes()
    except OSError as error:
        raise _SessionFormatError("session_transcript_unavailable") from error
    if len(raw) > _MAX_TRANSCRIPT_BYTES:
        raise _SessionFormatError("session_transcript_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _SessionFormatError("session_unsupported_format") from error
    messages: list[_Message] = []
    codex_meta_seen = False
    lines = text.splitlines()
    for position, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if position == len(lines) - 1 and not raw.endswith(b"\n"):
                break
            raise _SessionFormatError("session_unsupported_format") from None
        if event.client == "claude_code":
            _validate_claude_envelope(value, event)
            message = _claude_message(value)
        else:
            codex_meta_seen = _validate_codex_meta(value, event) or codex_meta_seen
            message = _codex_message(value)
        if message is not None:
            messages.append(message)
            if (
                len(messages) > _MAX_MESSAGES
                or sum(len(item.text) for item in messages) > _MAX_TEXT
            ):
                raise _SessionFormatError("session_transcript_too_large")
    if not messages or (event.client == "codex" and not codex_meta_seen):
        raise _SessionFormatError("session_unsupported_format")
    return tuple(messages)


def _validate_claude_envelope(value: object, event: QueuedSessionEvent) -> None:
    if not isinstance(value, dict) or value.get("type") not in {"user", "assistant"}:
        return
    if value.get("sessionId") != event.session_id or not _matches_project(value.get("cwd"), event):
        raise _SessionFormatError("session_identity_mismatch")


def _validate_codex_meta(value: object, event: QueuedSessionEvent) -> bool:
    if not isinstance(value, dict) or value.get("type") != "session_meta":
        return False
    payload = value.get("payload")
    if (
        not isinstance(payload, dict)
        or payload.get("id") != event.session_id
        or not _matches_project(payload.get("cwd"), event)
    ):
        raise _SessionFormatError("session_identity_mismatch")
    return True


def _matches_project(value: object, event: QueuedSessionEvent) -> bool:
    if type(value) is not str or "\x00" in value:
        return False
    try:
        return Path(value).resolve(strict=True) == Path(event.project_path)
    except OSError:
        return False


def _claude_message(value: object) -> _Message | None:
    if not isinstance(value, dict) or value.get("type") not in {"user", "assistant"}:
        return None
    message = value.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return None
    text = _content_text(message.get("content"), client="claude_code", role=cast(str, role))
    return None if text is None else _Message(cast(Literal["user", "assistant"], role), text)


def _codex_message(value: object) -> _Message | None:
    if not isinstance(value, dict) or value.get("type") != "response_item":
        return None
    payload = value.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return None
    text = _content_text(payload.get("content"), client="codex", role=cast(str, role))
    return None if text is None else _Message(cast(Literal["user", "assistant"], role), text)


def _content_text(content: object, *, client: SessionClient, role: str) -> str | None:
    if type(content) is str:
        return _clean_text(content)
    if not isinstance(content, list):
        return None
    allowed = (
        {"text"}
        if client == "claude_code"
        else ({"input_text"} if role == "user" else {"output_text"})
    )
    text_parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in allowed:
            continue
        text = item.get("text")
        if type(text) is str:
            cleaned = _clean_text(text)
            if cleaned is not None:
                text_parts.append(cleaned)
    return _clean_text("\n".join(text_parts))


def _clean_text(value: str) -> str | None:
    if "\x00" in value:
        return None
    result = value.strip()
    return result or None


def _contains_secret(messages: Sequence[_Message]) -> bool:
    joined = "\n".join(message.text for message in messages)
    if _PRIVATE_KEY.search(joined) or _CREDENTIAL_URL.search(joined):
        return True
    try:
        from open_brain_engine.capture.redaction import has_redaction_finding

        return has_redaction_finding(joined)
    except ValueError:
        return True


def _intakes(
    adapter: AgentSessionSourceAdapter,
    selection: SourceResourceSelection,
    event: QueuedSessionEvent,
    messages: Sequence[_Message],
    *,
    capture_summary: bool,
    capture_transcript: bool,
) -> tuple[SourceRecordIntake, ...]:
    transcript = "\n\n".join(f"{message.role}: {message.text}" for message in messages)
    summary_messages = messages[:_MAX_SUMMARY_MESSAGES]
    summary = "Extractive session summary:\n" + "\n".join(
        f"{message.role}: {message.text}" for message in summary_messages
    )
    revision = hashlib.sha256((event.event_id + "\x1f" + transcript).encode("utf-8")).hexdigest()
    record = AgentSessionRecord(
        project_id=selection.resource_id,
        session_id=event.session_id,
        revision_id=revision,
        client_name=event.client,
        summary=summary,
        transcript=transcript if capture_transcript else None,
        transcript_selected=capture_transcript,
    )
    all_intakes = adapter.intakes(selection, record, privacy=local_source_privacy())
    if capture_summary and capture_transcript:
        return all_intakes
    if capture_summary:
        return all_intakes[:1]
    if capture_transcript:
        return all_intakes[1:]
    return ()


def _bounded_ids(event_ids: Sequence[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(event_ids))
    return unique[-256:]


def _required_client(value: object) -> SessionClient:
    if value not in _CLIENTS:
        raise LiveSourceError("source_invalid_options")
    return cast(SessionClient, value)
