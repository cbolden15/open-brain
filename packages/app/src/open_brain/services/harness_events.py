"""Bounded authored-message adapters for native harness lifecycle events."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

from open_brain_engine.capture.redaction import has_redaction_finding

from .harness_continuity import MAX_EVENT_BYTES, MAX_EVENT_MESSAGES
from .harness_memory import MemoryMessage, select_memories
from .harness_policy import _LOCAL_MARKING

_MAX_TRANSCRIPT = 8 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class HarnessEventError(ValueError):
    """Closed metadata errors; never include a path, transcript, or parse error."""

    def __init__(self, code: str) -> None:
        if code not in {
            "invalid_event",
            "identity_mismatch",
            "transcript_unavailable",
            "transcript_changed",
            "transcript_too_large",
            "unsupported_transcript",
            "credential_signal",
            "lineage_unavailable",
        }:
            raise ValueError("invalid event error")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HarnessCaptureEvent:
    client: str
    session_id: str
    project: Path
    messages: tuple[MemoryMessage, ...]
    timestamps: Mapping[int, str | None]
    local_only: bool = False
    parent_session_id: str | None = None
    session_timestamp: str | None = None
    origin_sessions: Mapping[str, str] = field(default_factory=dict)
    selection: tuple[tuple[int, int], ...] | None = None
    origin_ordinals: Mapping[str, int] = field(default_factory=dict)


def pi_parent_session_id(value: object) -> str | None:
    """Parse the installed SDK's parent filename; never open a parent transcript."""
    if value is None:
        return None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise HarnessEventError("lineage_unavailable")
    match = re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_"
        r"([A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?)\.jsonl",
        Path(value).name,
    )
    if match is None:
        raise HarnessEventError("lineage_unavailable")
    return match[1]


def capture_event(client: str, payload: object) -> HarnessCaptureEvent:
    """Bind native metadata before reading only its named transcript."""
    try:
        return _capture_event(client, payload)
    except TypeError, UnicodeError, OverflowError:
        raise HarnessEventError("invalid_event") from None


def _capture_event(client: str, payload: object) -> HarnessCaptureEvent:
    if not isinstance(client, str) or client not in {"codex", "claude-code", "pi", "hermes"}:
        raise HarnessEventError("invalid_event")
    if not isinstance(payload, dict):
        raise HarnessEventError("invalid_event")
    local_only = payload.get("local_only", False)
    if type(local_only) is not bool:
        raise HarnessEventError("invalid_event")
    session = payload.get("session_id")
    if not isinstance(session, str) or _IDENTIFIER.fullmatch(session) is None:
        raise HarnessEventError("invalid_event")
    project = _project(
        payload.get("project_path") if client in {"pi", "hermes"} else payload.get("cwd")
    )
    if client == "hermes":
        if any(key in payload for key in (
            "parent_session_id", "parent_session_path", "origin_sessions", "origin_ordinals",
        )):
            raise HarnessEventError("lineage_unavailable")
        messages, timestamps = _hermes_messages(payload)
        parent, session_time = None, None
    elif client == "pi":
        if payload.get("header_session_id", session) != session:
            raise HarnessEventError("identity_mismatch")
        messages, timestamps = _pi_messages(payload)
        parent = pi_parent_session_id(payload.get("parent_session_path"))
        supplied_parent = payload.get("parent_session_id")
        if supplied_parent is not None and (parent is None or supplied_parent != parent):
            raise HarnessEventError("lineage_unavailable")
        session_time = payload.get("session_timestamp")
    else:
        if payload.get("hook_event_name") not in ("Stop", "SessionEnd"):
            raise HarnessEventError("invalid_event")
        messages, timestamps, parent, session_time = _transcript(
            client, session, project, payload.get("transcript_path")
        )
    authored = "\n".join(message.text for message in messages if message.authored)
    return HarnessCaptureEvent(
        client,
        session,
        project,
        messages,
        timestamps,
        local_only or bool(_LOCAL_MARKING.search(authored)),
        parent,
        session_time if isinstance(session_time, str) else None,
    )


def _project(value: object) -> Path:
    if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
        raise HarnessEventError("invalid_event")
    try:
        path = Path(value).resolve(strict=True)
        if not path.is_dir():
            raise OSError
        return path
    except OSError, UnicodeError:
        raise HarnessEventError("invalid_event") from None


def _hermes_messages(
    payload: dict[str, object],
) -> tuple[tuple[MemoryMessage, ...], dict[int, str | None]]:
    """Accept canonical host messages; injected and tool records never enter custody."""
    raw = payload.get("messages")
    if not isinstance(raw, list) or len(raw) > MAX_EVENT_MESSAGES:
        raise HarnessEventError("invalid_event")
    authored: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, dict) or type(value.get("authored", True)) is not bool:
            raise HarnessEventError("invalid_event")
        if value.get("authored", True) is False or value.get("role") in ("tool", "system"):
            continue
        source_id = value.get("source_id")
        ordinal = value.get("ordinal")
        if (
            not isinstance(source_id, str)
            or _IDENTIFIER.fullmatch(source_id) is None
            or source_id in seen
            or type(ordinal) is not int
            or not 0 <= ordinal <= 2**53 - 1
        ):
            raise HarnessEventError("invalid_event")
        timestamp = value.get("timestamp")
        if timestamp is not None:
            try:
                if (
                    not isinstance(timestamp, str)
                    or len(timestamp) > 64
                    or datetime.fromisoformat(timestamp).tzinfo is None
                ):
                    raise ValueError
            except ValueError:
                raise HarnessEventError("invalid_event") from None
        seen.add(source_id)
        authored.append(value)
    return _pi_messages({"messages": authored})


def _pi_messages(
    payload: dict[str, object],
) -> tuple[tuple[MemoryMessage, ...], dict[int, str | None]]:
    raw = payload.get("messages")
    if not isinstance(raw, list) or len(raw) > MAX_EVENT_MESSAGES:
        raise HarnessEventError("invalid_event")
    messages: list[MemoryMessage] = []
    timestamps: dict[int, str | None] = {}
    seen: set[int] = set()
    for value in raw:
        if not isinstance(value, dict):
            raise HarnessEventError("invalid_event")
        role, text, ordinal = value.get("role"), value.get("text"), value.get("ordinal")
        authored = value.get("authored", True)
        source_id = value.get("source_id")
        if (
            not isinstance(role, str)
            or role not in {"user", "assistant"}
            or not isinstance(text, str)
            or "\0" in text
            or type(ordinal) is not int
            or ordinal < 0
            or ordinal in seen
            or type(authored) is not bool
            or source_id is not None
            and (not isinstance(source_id, str) or _IDENTIFIER.fullmatch(source_id) is None)
        ):
            raise HarnessEventError("invalid_event")
        seen.add(ordinal)
        if authored:
            _screen(text)
        messages.append(MemoryMessage(role, text, ordinal, authored, source_id))
        timestamp = value.get("timestamp")
        timestamps[ordinal] = timestamp if isinstance(timestamp, str) else None
    if sum(len(message.text.encode()) for message in messages) > MAX_EVENT_BYTES:
        raise HarnessEventError("transcript_too_large")
    return tuple(messages), timestamps


def _transcript(
    client: str,
    session: str,
    project: Path,
    value: object,
) -> tuple[tuple[MemoryMessage, ...], dict[int, str | None], str | None, str | None]:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise HarnessEventError("transcript_unavailable")
    try:
        descriptor = os.open(value, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
                raise HarnessEventError("transcript_unavailable")
            if before.st_size > _MAX_TRANSCRIPT:
                raise HarnessEventError("transcript_too_large")
            raw = stream.read(_MAX_TRANSCRIPT + 1)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise HarnessEventError("transcript_changed")
        if len(raw) > _MAX_TRANSCRIPT:
            raise HarnessEventError("transcript_too_large")
        lines = raw.splitlines()
    except HarnessEventError:
        raise
    except OSError, UnicodeError, ValueError:
        raise HarnessEventError("transcript_unavailable") from None
    messages: list[MemoryMessage] = []
    timestamps: dict[int, str | None] = {}
    bound = client == "claude-code"
    retained_bytes = 0
    occurrences: dict[str, int] = {}
    parent: str | None = None
    session_time: str | None = None
    credential = False
    for ordinal, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError, UnicodeError:
            if ordinal == len(lines) - 1 and not raw.endswith(b"\n"):
                break
            raise HarnessEventError("unsupported_transcript") from None
        if not isinstance(record, dict):
            raise HarnessEventError("unsupported_transcript")
        if client == "codex" and record.get("type") == "session_meta":
            metadata = record.get("payload")
            if not isinstance(metadata, dict):
                raise HarnessEventError("identity_mismatch")
            _bind(metadata.get("id"), metadata.get("cwd"), session, project)
            parent_value = metadata.get("forked_from_id")
            if parent_value is not None:
                if not isinstance(parent_value, str) or _IDENTIFIER.fullmatch(parent_value) is None:
                    raise HarnessEventError("lineage_unavailable")
                parent = parent_value
            native_time = metadata.get("timestamp", record.get("timestamp"))
            session_time = native_time if isinstance(native_time, str) else None
            bound = True
        elif client == "claude-code" and record.get("type") in {"user", "assistant"}:
            _bind(record.get("sessionId"), record.get("cwd"), session, project)
        message = _native_message(client, record, ordinal)
        if message is None:
            continue
        try:
            _screen(message.text)
        except HarnessEventError:
            credential = True
        item = record.get("payload")
        native_id = (
            record.get("uuid")
            if client == "claude-code"
            else (item.get("id") if isinstance(item, dict) else None)
        )
        if isinstance(native_id, str) and _IDENTIFIER.fullmatch(native_id):
            identity = "native:" + native_id
        else:
            # Codex response items do not always expose a native message ID.
            # This fallback survives inserted metadata/tool records and rotation
            # retaining the authored messages. Repeated identical messages use
            # occurrence order; dropping one needs an adapter continuity map.
            digest = sha256((message.role + "\0" + message.text).encode()).hexdigest()
            timestamp = record.get("timestamp")
            occurrence_key = digest + (timestamp if isinstance(timestamp, str) else "")
            occurrence = occurrences.get(occurrence_key, 0)
            occurrences[occurrence_key] = occurrence + 1
            if occurrence and select_memories([message]):
                raise HarnessEventError("lineage_unavailable")
            if isinstance(timestamp, str):
                stamp = sha256(timestamp.encode()).hexdigest()[:24]
                identity = f"stamped:{digest}:{stamp}:{occurrence}"
            else:
                if occurrence and select_memories([message]):
                    raise HarnessEventError("lineage_unavailable")
                identity = f"message:{digest}:{occurrence}"
        message = MemoryMessage(message.role, message.text, ordinal, True, identity)
        size = len(message.text.encode())
        if size > MAX_EVENT_BYTES:
            raise HarnessEventError("transcript_too_large")
        messages.append(message)
        retained_bytes += size
        timestamp = record.get("timestamp")
        timestamps[ordinal] = timestamp if isinstance(timestamp, str) else None
        # Refuse overflow visibly. A rolling window can lose a confidentiality
        # marking while retaining later facts, which would widen their policy.
        if len(messages) > MAX_EVENT_MESSAGES or retained_bytes > MAX_EVENT_BYTES:
            raise HarnessEventError("transcript_too_large")
    if not bound:
        raise HarnessEventError("identity_mismatch")
    if credential:
        raise HarnessEventError("credential_signal")
    return tuple(messages), timestamps, parent, session_time


def _bind(actual_session: object, actual_project: object, session: str, project: Path) -> None:
    if actual_session != session or _project(actual_project) != project:
        raise HarnessEventError("identity_mismatch")


def _native_message(client: str, record: dict[str, object], ordinal: int) -> MemoryMessage | None:
    if client == "claude-code":
        if record.get("type") not in {"user", "assistant"}:
            return None
        if record.get("isMeta") or record.get("isCompactSummary"):
            return None
        message = record.get("message")
    else:
        if record.get("type") != "response_item":
            return None
        message = record.get("payload")
        if not isinstance(message, dict) or message.get("type") != "message":
            return None
    if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
        return None
    role = cast(str, message["role"])
    content = message.get("content")
    if client == "codex" and role == "user":
        metadata = message.get("internal_chat_message_metadata_passthrough")
        if metadata is not None:
            kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
            if (
                not isinstance(content, list)
                or not isinstance(kinds, list)
                or len(content) != len(kinds)
                or any(not isinstance(kind, str) for kind in kinds)
            ):
                raise HarnessEventError("unsupported_transcript")
            content = [
                part for part, kind in zip(content, kinds, strict=True) if kind == "user.text"
            ]
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        allowed = (
            {"text"}
            if client == "claude-code"
            else {"input_text" if role == "user" else "output_text"}
        )
        text = "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") in allowed
            and isinstance(part.get("text"), str)
        )
    else:
        return None
    if not text.strip() or "\0" in text:
        return None
    return MemoryMessage(role, text, ordinal)


def _screen(text: str) -> None:
    text.encode("utf-8")
    if (
        has_redaction_finding(text)
        or re.search(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", text)
        or re.search(r"https?://[^\s/@:]+:[^\s/@]+@", text, re.I)
    ):
        raise HarnessEventError("credential_signal")
