"""Project-local lifecycle hooks and bounded metadata queue for agent sessions.

This module deliberately never discovers client history.  A configured hook can enqueue
only the event metadata supplied by Claude Code or Codex for one selected project.  The
live source later reads the named, fresh transcript and extracts permitted text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, cast

from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_storage import PrivateJsonStore

_CLIENTS = frozenset({"claude_code", "codex"})
_EVENT_ID = re.compile(r"[a-f0-9]{32}")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_OWNER_ARGUMENT = "--owner open-brain-agent-session"
_MAX_HOOK_INPUT = 262_144
_MAX_QUEUE_EVENTS = 256
_QUEUE_FILE = re.compile(r"event-[a-f0-9]{32}\.json")
_OVERFLOW_FILE = "overflow.json"

HookClient = Literal["claude_code", "codex"]


class HookConfigError(ValueError):
    """A hook configuration cannot be safely previewed or changed."""


@dataclass(frozen=True, slots=True, repr=False)
class HookConfigPreview:
    """A preimage-bound local configuration change.

    ``document`` is intentionally excluded from repr because settings may contain
    unrelated local configuration.  It is only written after the preimage matches.
    """

    client: HookClient
    config_path: Path
    before_sha256: str
    document: dict[str, object]
    changed: bool
    action: Literal["apply", "uninstall"]


@dataclass(frozen=True, slots=True)
class QueuedSessionEvent:
    """Metadata for one hook event; no transcript text is stored in the queue."""

    event_id: str
    client: HookClient
    project_id: str
    project_path: str
    session_id: str
    transcript_path: str
    hook_event_name: str
    observed_mtime_ns: int
    transcript_device: int
    transcript_inode: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            _EVENT_ID.fullmatch(self.event_id) is None
            or self.client not in _CLIENTS
            or not _valid_project_id(self.project_id)
            or not _SESSION_ID.fullmatch(self.session_id)
            or self.hook_event_name not in {"Stop", "SessionEnd"}
            or type(self.observed_mtime_ns) is not int
            or self.observed_mtime_ns < 0
            or type(self.transcript_device) is not int
            or self.transcript_device < 0
            or type(self.transcript_inode) is not int
            or self.transcript_inode < 1
            or self.schema_version != 1
        ):
            raise HookConfigError("invalid agent session queue event")
        _required_absolute_path(self.project_path)
        _required_absolute_path(self.transcript_path)

    def to_dict(self) -> dict[str, object]:
        return {
            "client": self.client,
            "event_id": self.event_id,
            "hook_event_name": self.hook_event_name,
            "observed_mtime_ns": self.observed_mtime_ns,
            "project_id": self.project_id,
            "project_path": self.project_path,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "transcript_device": self.transcript_device,
            "transcript_inode": self.transcript_inode,
            "transcript_path": self.transcript_path,
        }

    @classmethod
    def from_dict(cls, value: object) -> QueuedSessionEvent:
        if not isinstance(value, dict) or set(value) != {
            "client",
            "event_id",
            "hook_event_name",
            "observed_mtime_ns",
            "project_id",
            "project_path",
            "schema_version",
            "session_id",
            "transcript_device",
            "transcript_inode",
            "transcript_path",
        }:
            raise HookConfigError("invalid agent session queue event")
        return cls(
            event_id=_required_str(value["event_id"]),
            client=cast(HookClient, _required_str(value["client"])),
            project_id=_required_str(value["project_id"]),
            project_path=_required_str(value["project_path"]),
            session_id=_required_str(value["session_id"]),
            transcript_path=_required_str(value["transcript_path"]),
            hook_event_name=_required_str(value["hook_event_name"]),
            observed_mtime_ns=_required_int(value["observed_mtime_ns"]),
            transcript_device=_required_int(value["transcript_device"]),
            transcript_inode=_required_int(value["transcript_inode"]),
            schema_version=_required_int(value["schema_version"]),
        )


@dataclass(slots=True)
class AgentSessionHookManager:
    """Preview, apply, and remove only Open Brain's selected-project hook entry."""

    queue_root: Path
    python_executable: str = field(default_factory=lambda: sys.executable)

    def preview_apply(self, client: HookClient, project_path: Path) -> HookConfigPreview:
        normalized_client = _required_client(client)
        project = _canonical_project(project_path)
        path = _config_path(normalized_client, project)
        before, document = _read_config(path)
        proposed = _copy_object(document)
        hooks = _hooks_mapping(proposed, create=True)
        if hooks is None:
            raise AssertionError("created hooks mapping is required")
        stop_entries = hooks.get("Stop")
        if stop_entries is None:
            stop_entries = []
            hooks["Stop"] = stop_entries
        if not isinstance(stop_entries, list):
            raise HookConfigError("invalid hook configuration")
        desired = _owned_hook_entry(
            normalized_client,
            project,
            self.queue_root,
            self.python_executable,
        )
        retained = [item for item in stop_entries if not _is_owned_entry(item)]
        changed = [*retained, desired] != stop_entries
        if changed:
            hooks["Stop"] = [*retained, desired]
        return HookConfigPreview(
            client=normalized_client,
            config_path=path,
            before_sha256=_digest(before),
            document=proposed,
            changed=changed,
            action="apply",
        )

    def preview_uninstall(self, client: HookClient, project_path: Path) -> HookConfigPreview:
        normalized_client = _required_client(client)
        project = _canonical_project(project_path)
        path = _config_path(normalized_client, project)
        before, document = _read_config(path)
        proposed = _copy_object(document)
        hooks = _hooks_mapping(proposed, create=False)
        changed = False
        if hooks is not None:
            stop_entries = hooks.get("Stop")
            if isinstance(stop_entries, list):
                retained = [item for item in stop_entries if not _is_owned_entry(item)]
                changed = len(retained) != len(stop_entries)
                if retained:
                    hooks["Stop"] = retained
                else:
                    hooks.pop("Stop", None)
            elif stop_entries is not None:
                raise HookConfigError("invalid hook configuration")
            if not hooks:
                proposed.pop("hooks", None)
        return HookConfigPreview(
            client=normalized_client,
            config_path=path,
            before_sha256=_digest(before),
            document=proposed,
            changed=changed,
            action="uninstall",
        )

    def apply(self, preview: HookConfigPreview) -> Path:
        if not isinstance(preview, HookConfigPreview) or preview.action != "apply":
            raise HookConfigError("invalid hook preview")
        return self._write_preview(preview)

    def uninstall(self, preview: HookConfigPreview) -> Path:
        if not isinstance(preview, HookConfigPreview) or preview.action != "uninstall":
            raise HookConfigError("invalid hook preview")
        return self._write_preview(preview)

    def _write_preview(self, preview: HookConfigPreview) -> Path:
        current, _ = _read_config(preview.config_path)
        if _digest(current) != preview.before_sha256:
            raise HookConfigError("hook configuration changed since preview")
        if not preview.changed:
            return preview.config_path
        _atomic_bytes_write(
            preview.config_path,
            _document_bytes(preview.document),
            expected_sha256=preview.before_sha256,
        )
        return preview.config_path

    @contextmanager
    def mutation(self, preview: HookConfigPreview) -> Iterator[None]:
        """Restore this exact hook preimage if the caller cannot save source state."""
        original, _ = _read_config(preview.config_path)
        if _digest(original) != preview.before_sha256:
            raise HookConfigError("hook configuration changed since preview")
        self.uninstall(preview) if preview.action == "uninstall" else self.apply(preview)
        try:
            yield
        except Exception:
            if preview.changed:
                expected = _digest(_document_bytes(preview.document))
                if original:
                    _atomic_bytes_write(preview.config_path, original, expected_sha256=expected)
                else:
                    current, _ = _read_config(preview.config_path)
                    if _digest(current) != expected:
                        raise HookConfigError("hook rollback preimage changed") from None
                    preview.config_path.unlink()
            raise


def project_id_for_path(project_path: Path) -> str:
    """Return the stable local-project identifier used by the existing adapter."""
    canonical = str(_canonical_project(project_path))
    return "project:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def enqueue_hook_event(
    queue_root: Path,
    *,
    client: HookClient,
    project_path: Path,
    payload: Mapping[str, object],
) -> bool:
    """Persist a bounded metadata event, returning false for malformed host input.

    Hooks call this best-effort function and deliberately suppress all failures so a
    capture outage cannot block the host client.
    """
    try:
        normalized_client = _required_client(client)
        project = _canonical_project(project_path)
        if not isinstance(payload, Mapping):
            return False
        cwd = _canonical_existing_path(_required_str(payload.get("cwd")))
        if cwd != project:
            return False
        session_id = _required_str(payload.get("session_id"))
        if _SESSION_ID.fullmatch(session_id) is None:
            return False
        transcript_path = _canonical_existing_path(_required_str(payload.get("transcript_path")))
        event_name = _required_str(payload.get("hook_event_name"))
        if event_name not in {"Stop", "SessionEnd"}:
            return False
        transcript_stat = transcript_path.stat()
        event = QueuedSessionEvent(
            event_id=uuid.uuid4().hex,
            client=normalized_client,
            project_id=project_id_for_path(project),
            project_path=str(project),
            session_id=session_id,
            transcript_path=str(transcript_path),
            hook_event_name=event_name,
            observed_mtime_ns=transcript_stat.st_mtime_ns,
            transcript_device=transcript_stat.st_dev,
            transcript_inode=transcript_stat.st_ino,
        )
        store = _queue_store(queue_root)
        with store.lock("queue"):
            events = _stored_events(store)
            if len(events) >= _MAX_QUEUE_EVENTS:
                _record_overflow(store)
                return False
            store.write(f"event-{event.event_id}.json", event.to_dict())
        return True
    except HookConfigError, LiveSourceError, OSError, TypeError, ValueError:
        return False


def queued_events(queue_root: Path) -> tuple[QueuedSessionEvent, ...]:
    """Read valid events only; malformed queue files are never interpreted as text."""
    try:
        store = _queue_store(queue_root)
        with store.lock("queue"):
            return _stored_events(store)
    except HookConfigError, LiveSourceError:
        return ()


def discard_queued_event(queue_root: Path, event_id: str) -> bool:
    """Explicitly discard one quarantined or unwanted queue event."""
    if type(event_id) is not str or _EVENT_ID.fullmatch(event_id) is None:
        return False
    try:
        store = _queue_store(queue_root)
        with store.lock("queue"):
            if store.read(f"event-{event_id}.json") is None:
                return False
            store.delete(f"event-{event_id}.json")
        return True
    except HookConfigError, LiveSourceError, OSError:
        return False


def queue_overflow_count(queue_root: Path) -> int:
    """Return metadata-only dropped-event count recorded after queue saturation."""
    try:
        store = _queue_store(queue_root)
        value = store.read(_OVERFLOW_FILE)
        if value is None:
            return 0
        if (
            not isinstance(value, dict)
            or set(value) != {"dropped_events", "schema_version"}
            or value["schema_version"] != 1
            or type(value["dropped_events"]) is not int
            or value["dropped_events"] < 1
        ):
            return 0
        return value["dropped_events"]
    except HookConfigError, LiveSourceError, OSError, UnicodeDecodeError, json.JSONDecodeError:
        return 0


def acknowledge_queued_events(
    queue_root: Path,
    *,
    client: HookClient,
    project_id: str,
    project_path: str,
    captured_event_ids: Sequence[str],
) -> None:
    """Delete only acknowledged events and their older same-session observations."""
    normalized_client = _required_client(client)
    if not _valid_project_id(project_id):
        raise HookConfigError("invalid agent session acknowledgement")
    canonical_project = str(_canonical_existing_path(project_path))
    if not isinstance(captured_event_ids, Sequence) or isinstance(captured_event_ids, str):
        raise HookConfigError("invalid agent session acknowledgement")
    acknowledged = tuple(captured_event_ids)
    if len(acknowledged) > 256 or any(
        type(event_id) is not str or _EVENT_ID.fullmatch(event_id) is None
        for event_id in acknowledged
    ):
        raise HookConfigError("invalid agent session acknowledgement")
    store = _queue_store(queue_root)
    with store.lock("queue"):
        events = _stored_events(store)
        watermarks = {
            event.session_id: (event.observed_mtime_ns, event.event_id)
            for event in events
            if event.event_id in acknowledged
            and event.client == normalized_client
            and event.project_id == project_id
            and event.project_path == canonical_project
        }
        for event in events:
            watermark = watermarks.get(event.session_id)
            if (
                watermark is not None
                and event.client == normalized_client
                and event.project_id == project_id
                and event.project_path == canonical_project
                and (event.observed_mtime_ns, event.event_id) <= watermark
            ):
                store.delete(f"event-{event.event_id}.json")
        if len(_stored_events(store)) < _MAX_QUEUE_EVENTS:
            store.delete(_OVERFLOW_FILE)


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Small hook command surface. It writes no transcript text to stdout or stderr."""
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    enqueue = subparsers.add_parser("enqueue", add_help=False)
    enqueue.add_argument("--root", required=True)
    enqueue.add_argument("--client", required=True, choices=sorted(_CLIENTS))
    enqueue.add_argument("--project", required=True)
    enqueue.add_argument("--owner", required=True)
    discard = subparsers.add_parser("discard", add_help=False)
    discard.add_argument("--root", required=True)
    discard.add_argument("--event-id", required=True)
    try:
        parsed = parser.parse_args(argv)
    except SystemExit:
        return 0
    if parsed.command == "enqueue":
        if parsed.owner != "open-brain-agent-session":
            return 0
        try:
            raw = sys.stdin.buffer.read(_MAX_HOOK_INPUT + 1)
            if len(raw) > _MAX_HOOK_INPUT:
                return 0
            value = json.loads(raw)
            if isinstance(value, dict):
                enqueue_hook_event(
                    Path(parsed.root),
                    client=cast(HookClient, parsed.client),
                    project_path=Path(parsed.project),
                    payload=value,
                )
        except OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError:
            return 0
        return 0
    discard_queued_event(Path(parsed.root), parsed.event_id)
    return 0


def _config_path(client: HookClient, project: Path) -> Path:
    if client == "claude_code":
        return project / ".claude" / "settings.local.json"
    return project / ".codex" / "hooks.json"


def _owned_hook_entry(
    client: HookClient,
    project: Path,
    queue_root: Path,
    python_executable: str,
) -> dict[str, object]:
    command = " ".join(
        shlex.quote(value)
        for value in (
            python_executable,
            "-m",
            "open_brain_connectors.runtime.agent_session_hooks",
            "enqueue",
            "--root",
            str(queue_root.resolve(strict=False)),
            "--client",
            client,
            "--project",
            str(project),
            "--owner",
            "open-brain-agent-session",
        )
    )
    # Both foreground clients can exit before a background Stop hook persists
    # its event. Wait only for the bounded metadata enqueue, never for import.
    handler: dict[str, object] = {"type": "command", "command": command, "timeout": 2}
    return {"hooks": [handler]}


def _is_owned_entry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    handlers = value.get("hooks")
    if not isinstance(handlers, list) or len(handlers) != 1 or not isinstance(handlers[0], dict):
        return False
    command = handlers[0].get("command")
    return type(command) is str and _OWNER_ARGUMENT in command


def _read_config(path: Path) -> tuple[bytes, dict[str, object]]:
    if path.exists():
        if path.is_symlink():
            raise HookConfigError("unsafe hook configuration path")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise HookConfigError("unsafe hook configuration path")
                raw = handle.read(_MAX_HOOK_INPUT + 1)
            if len(raw) > _MAX_HOOK_INPUT:
                raise HookConfigError("hook configuration too large")
            value = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HookConfigError("invalid hook configuration") from error
        if not isinstance(value, dict):
            raise HookConfigError("invalid hook configuration")
        return raw, cast(dict[str, object], value)
    return b"", {}


def _hooks_mapping(document: dict[str, object], *, create: bool) -> dict[str, object] | None:
    value = document.get("hooks")
    if value is None:
        if not create:
            return None
        created: dict[str, object] = {}
        document["hooks"] = created
        return created
    if not isinstance(value, dict):
        raise HookConfigError("invalid hook configuration")
    return cast(dict[str, object], value)


def _queue_store(root: Path) -> PrivateJsonStore:
    if not isinstance(root, Path) or not root.is_absolute():
        raise HookConfigError("invalid agent session queue")
    return PrivateJsonStore(root / "agent-session-queue")


def _stored_events(store: PrivateJsonStore) -> tuple[QueuedSessionEvent, ...]:
    events: list[QueuedSessionEvent] = []
    for name in store.names("event-"):
        if _QUEUE_FILE.fullmatch(name) is None:
            continue
        try:
            value = store.read(name)
            if value is not None:
                events.append(QueuedSessionEvent.from_dict(value))
        except HookConfigError, LiveSourceError:
            continue
    return tuple(sorted(events, key=lambda event: event.event_id))


def _record_overflow(store: PrivateJsonStore) -> None:
    value = store.read(_OVERFLOW_FILE)
    previous = 0
    if isinstance(value, dict) and type(value.get("dropped_events")) is int:
        previous = cast(int, value["dropped_events"])
    store.write(
        _OVERFLOW_FILE,
        {"dropped_events": min(previous + 1, 2_147_483_647), "schema_version": 1},
    )


def _document_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def _atomic_bytes_write(path: Path, payload: bytes, *, expected_sha256: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise HookConfigError("unsafe hook configuration path")
    temporary: str | None = None
    try:
        with NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            temporary = handle.name
        os.chmod(temporary, 0o600)
        current, _ = _read_config(path)
        if _digest(current) != expected_sha256:
            Path(temporary).unlink(missing_ok=True)
            raise HookConfigError("hook configuration changed since preview")
        os.replace(temporary, path)
    except (OSError, HookConfigError) as error:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        raise HookConfigError("hook configuration write failed") from error


def _canonical_project(path: Path) -> Path:
    return _canonical_existing_path(str(path))


def _canonical_existing_path(value: str) -> Path:
    _required_absolute_path(value)
    path = Path(value)
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise HookConfigError("invalid session path") from error


def _required_absolute_path(value: str) -> None:
    if type(value) is not str or "\x00" in value or not Path(value).is_absolute():
        raise HookConfigError("invalid session path")


def _required_client(value: object) -> HookClient:
    if value not in _CLIENTS:
        raise HookConfigError("invalid session client")
    return cast(HookClient, value)


def _required_str(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise HookConfigError("invalid session value")
    return value


def _required_int(value: object) -> int:
    if type(value) is not int:
        raise HookConfigError("invalid session value")
    return value


def _valid_project_id(value: str) -> bool:
    return bool(re.fullmatch(r"project:[a-f0-9]{16,64}", value))


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _copy_object(value: Mapping[str, object]) -> dict[str, object]:
    copied = json.loads(json.dumps(value))
    if not isinstance(copied, dict):
        raise AssertionError("object copy changed shape")
    return cast(dict[str, object], copied)


if __name__ == "__main__":  # pragma: no cover - exercised through the installed module command.
    raise SystemExit(run_cli())
