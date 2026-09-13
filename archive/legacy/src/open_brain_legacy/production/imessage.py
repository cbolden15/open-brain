"""Local-only, cursor-safe production iMessage capture ingress."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import subprocess
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import sleep as _sleep
from typing import Protocol, cast

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.engine import PublicJobCaptureSink

from open_brain_legacy.operations.capture_jobs import get_capture_job

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_HISTORY_BYTES = 2 * 1024 * 1024
_MAX_MESSAGES = 50
_MAX_TEXT_BYTES = 256 * 1024
_IMSG_CANDIDATES = (
    Path("/opt/homebrew/bin/imsg"),
    Path("/usr/local/bin/imsg"),
    Path("/usr/bin/imsg"),
)


class ImessageConfigError(ValueError):
    """The local iMessage configuration or cursor is unavailable or malformed."""


@dataclass(frozen=True, slots=True)
class ImessageConfig:
    chat_id: str = field(repr=False)
    allowed_senders: tuple[str, ...] = field(repr=False)

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> ImessageConfig:
        try:
            decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
            value = _mapping(decoded)
            senders = value["allowed_senders"]
            if (
                set(value) != {"schema_version", "chat_id", "allowed_senders"}
                or value["schema_version"] != 1
                or not isinstance(senders, list)
                or not 1 <= len(senders) <= 20
            ):
                raise ImessageConfigError("invalid private iMessage config")
            chat_id = _private_identifier(value["chat_id"])
            normalized_senders = tuple(_private_identifier(item).casefold() for item in senders)
            result = cls(chat_id=chat_id, allowed_senders=normalized_senders)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            if isinstance(error, ImessageConfigError):
                raise
            raise ImessageConfigError("invalid private iMessage config") from error
        if (
            tuple(sorted(normalized_senders)) != normalized_senders
            or len(set(normalized_senders)) != len(normalized_senders)
            or result.canonical_bytes() != payload
        ):
            raise ImessageConfigError("invalid private iMessage config")
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "chat_id": self.chat_id,
                "allowed_senders": list(self.allowed_senders),
            }
        )


class ImessageHistoryClient(Protocol):
    def history(self, *, chat_id: str, after_rowid: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ProductionImessageResult:
    scanned_count: int
    created_count: int
    duplicate_count: int
    cursor_rowid: int


@dataclass(frozen=True, slots=True)
class _Message:
    rowid: int
    chat_id: str = field(repr=False)
    sender: str = field(repr=False)
    text: str = field(repr=False)
    captured_at: datetime | None = field(repr=False)
    from_self: bool
    guid: str = field(repr=False)


class SubprocessImessageHistoryClient:
    def __init__(self, *, executable: Path) -> None:
        try:
            resolved = executable.resolve(strict=True)
        except OSError as error:
            raise ImessageConfigError("iMessage history capability unavailable") from error
        if (
            not executable.is_absolute()
            or not resolved.is_file()
            or not os.access(resolved, os.X_OK)
        ):
            raise ImessageConfigError("iMessage history capability unavailable")
        self._executable = str(resolved)

    def history(self, *, chat_id: str, after_rowid: int) -> bytes:
        if not isinstance(chat_id, str) or not isinstance(after_rowid, int) or after_rowid < 0:
            raise ImessageConfigError("invalid iMessage history request")
        try:
            result = subprocess.run(
                (
                    self._executable,
                    "history",
                    "--chat-id",
                    chat_id,
                    "--limit",
                    str(_MAX_MESSAGES),
                    "--json",
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
                env={"HOME": str(Path.home())},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ImessageConfigError("iMessage history capability unavailable") from error
        if result.returncode != 0 or len(result.stdout) > _MAX_HISTORY_BYTES:
            raise ImessageConfigError("iMessage history capability unavailable")
        return result.stdout


class ProductionImessageIngress:
    def __init__(
        self,
        *,
        config: ImessageConfig,
        state_root: Path,
        sink: PublicJobCaptureSink,
        history_client: ImessageHistoryClient,
    ) -> None:
        if (
            not isinstance(config, ImessageConfig)
            or not isinstance(state_root, Path)
            or not state_root.is_absolute()
            or not isinstance(sink, PublicJobCaptureSink)
            or not callable(getattr(history_client, "history", None))
        ):
            raise ValueError("invalid production iMessage ingress")
        self._config = config
        self._sink = sink
        self._history = history_client
        self._cursor = _CursorStore(state_root / "imessage-ingress")

    def run_once(self) -> ProductionImessageResult:
        with self._cursor.locked():
            prior = self._cursor.load()
            messages = _parse_history(
                self._history.history(
                    chat_id=self._config.chat_id,
                    after_rowid=prior,
                )
            )
            current = prior
            created = 0
            duplicates = 0
            application = get_capture_job("JOB-005")
            for message in messages:
                if message.rowid <= prior:
                    continue
                if not self._allowed(message):
                    current = max(current, message.rowid)
                    continue
                append = application.submit(
                    sink=self._sink,
                    envelope=_capture_envelope(message, self._config),
                )
                current = max(current, message.rowid)
                if append.disposition.value == "created":
                    created += 1
                else:
                    duplicates += 1
            if current != prior:
                self._cursor.save(current)
        return ProductionImessageResult(
            scanned_count=sum(message.rowid > prior for message in messages),
            created_count=created,
            duplicate_count=duplicates,
            cursor_rowid=current,
        )

    def run_forever(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = _sleep,
        idle_seconds: float = 15.0,
        failure_seconds: float = 30.0,
    ) -> None:
        """Poll until stopped, preserving the cursor across transient failures."""
        if (
            should_stop is not None
            and not callable(should_stop)
            or not callable(sleep)
            or isinstance(idle_seconds, bool)
            or not isinstance(idle_seconds, (int, float))
            or not 0 < idle_seconds <= 3_600
            or isinstance(failure_seconds, bool)
            or not isinstance(failure_seconds, (int, float))
            or not 0 < failure_seconds <= 3_600
        ):
            raise ValueError("invalid iMessage keepalive policy")
        stop = should_stop or (lambda: False)
        while not stop():
            delay = float(idle_seconds)
            try:
                self.run_once()
            except Exception:
                delay = float(failure_seconds)
            if not stop():
                sleep(delay)

    def _allowed(self, message: _Message) -> bool:
        return (
            message.chat_id == self._config.chat_id
            and message.sender.casefold() in self._config.allowed_senders
            and not message.from_self
            and bool(message.text.strip())
        )


class _CursorStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "cursor.json"
        _ensure_owner_directory(root)

    @contextmanager
    def locked(self) -> Iterator[None]:
        descriptor = os.open(self._root / ".cursor.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load(self) -> int:
        if not self._path.exists():
            return 0
        try:
            metadata = self._path.lstat()
            payload = self._path.read_bytes()
            value = _mapping(
                json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
            )
            rowid = value["last_rowid"]
        except (KeyError, OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ImessageConfigError("invalid iMessage cursor") from None
        expected = canonical_json_bytes({"schema_version": 1, "last_rowid": rowid})
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
            or not isinstance(rowid, int)
            or isinstance(rowid, bool)
            or rowid < 0
            or payload != expected
        ):
            raise ImessageConfigError("invalid iMessage cursor")
        return rowid

    def save(self, rowid: int) -> None:
        if not isinstance(rowid, int) or isinstance(rowid, bool) or rowid < 0:
            raise ValueError("invalid iMessage cursor")
        temporary = self._root / ("." + secrets.token_hex(16) + ".tmp")
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            payload = canonical_json_bytes({"schema_version": 1, "last_rowid": rowid})
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()


def load_private_imessage_config(path: Path) -> ImessageConfig:
    payload = _read_owner_file(path, maximum=_MAX_CONFIG_BYTES)
    return ImessageConfig.from_canonical_bytes(payload)


def compose_production_imessage_ingress(
    *,
    config_path: Path,
    state_root: Path,
    sink: PublicJobCaptureSink,
    history_client: ImessageHistoryClient | None = None,
) -> ProductionImessageIngress:
    selected = history_client
    if selected is None:
        executable = next(
            (
                candidate
                for candidate in _IMSG_CANDIDATES
                if candidate.is_file() and os.access(candidate, os.X_OK)
            ),
            None,
        )
        if executable is None:
            raise ImessageConfigError("iMessage history capability unavailable")
        selected = SubprocessImessageHistoryClient(executable=executable)
    return ProductionImessageIngress(
        config=load_private_imessage_config(config_path),
        state_root=state_root,
        sink=sink,
        history_client=selected,
    )


def _parse_history(payload: bytes) -> tuple[_Message, ...]:
    if not isinstance(payload, bytes) or len(payload) > _MAX_HISTORY_BYTES:
        raise ImessageConfigError("invalid iMessage history")
    raw_items: list[object] = []
    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        raw_items.extend(_message_items(decoded))
    except json.JSONDecodeError:
        try:
            for line in payload.decode("utf-8").splitlines():
                if line.strip():
                    raw_items.extend(
                        _message_items(
                            json.loads(line, object_pairs_hook=_unique_object)
                        )
                    )
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ImessageConfigError("invalid iMessage history") from error
    if len(raw_items) > _MAX_MESSAGES:
        raise ImessageConfigError("invalid iMessage history")
    try:
        messages = tuple(
            sorted(
                (_message(item) for item in raw_items),
                key=lambda item: item.rowid,
            )
        )
    except (TypeError, ValueError) as error:
        raise ImessageConfigError("invalid iMessage history") from error
    if len({message.rowid for message in messages}) != len(messages):
        raise ImessageConfigError("invalid iMessage history")
    return messages


def _message_items(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        messages = value.get("messages")
        return messages if isinstance(messages, list) else [value]
    raise ImessageConfigError("invalid iMessage history")


def _message(value: object) -> _Message:
    item = _mapping(value)
    rowid = item.get("rowid", item.get("rowId", item.get("id")))
    if not isinstance(rowid, int) or isinstance(rowid, bool) or rowid < 1:
        raise ImessageConfigError("invalid iMessage history")
    chat_id = _private_identifier(
        item.get("chat_id", item.get("chatId", item.get("chat", "")))
    )
    sender = _private_identifier(
        item.get("sender", item.get("handle", item.get("from", "")))
    )
    raw_text = item.get("text", item.get("body", item.get("message", "")))
    if not isinstance(raw_text, str):
        raise ImessageConfigError("invalid iMessage history")
    text = unicodedata.normalize("NFC", raw_text).replace("\r\n", "\n").replace("\r", "\n")
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES or "\x00" in text:
        raise ImessageConfigError("invalid iMessage history")
    timestamp = item.get("timestamp", item.get("date", item.get("time")))
    captured_at = None if timestamp is None else _timestamp(timestamp)
    from_self = bool(
        item.get("is_from_me", item.get("isFromMe", item.get("fromMe", False)))
    )
    guid_value = item.get("guid", "")
    guid = guid_value if isinstance(guid_value, str) else ""
    return _Message(rowid, chat_id, sender, text, captured_at, from_self, guid)


def _capture_envelope(message: _Message, config: ImessageConfig) -> CaptureEnvelope:
    if message.captured_at is None:
        raise ImessageConfigError("invalid iMessage history")
    identity = sha256(
        canonical_json_bytes(
            {
                "chat_id": config.chat_id,
                "sender": message.sender,
                "rowid": message.rowid,
                "guid": message.guid,
            }
        )
    ).hexdigest()[:16]
    text_digest = sha256(message.text.encode("utf-8")).hexdigest()
    return CaptureEnvelope.create(
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        source_url=None,
        title=None,
        shared_text=message.text,
        captured_at=message.captured_at,
        capture_why="Retain this owner message context " + identity,
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.INTEGRATION,
        provenance=Provenance.create(
            source_ref="urn:open-brain:text:sha256:" + text_digest,
            content_origin=ContentOrigin.OWNER_AUTHORED,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=PrivacyDecision.create(
            tier=PrivacyTier.PERSONAL,
            reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )


def _read_owner_file(path: Path, *, maximum: int) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ImessageConfigError("invalid private iMessage config")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= maximum
        ):
            raise ImessageConfigError("invalid private iMessage config")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(maximum + 1)
    except ImessageConfigError:
        raise
    except OSError as error:
        raise ImessageConfigError("invalid private iMessage config") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > maximum:
        raise ImessageConfigError("invalid private iMessage config")
    return payload


def _ensure_owner_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise ImessageConfigError("invalid iMessage state root") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ImessageConfigError("invalid iMessage state root")


def _private_identifier(value: object) -> str:
    if not isinstance(value, str):
        raise ImessageConfigError("invalid private iMessage config")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > 512
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ImessageConfigError("invalid private iMessage config")
    return normalized


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ImessageConfigError("invalid iMessage history")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ImessageConfigError("invalid iMessage history") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ImessageConfigError("invalid iMessage history")
    return result.astimezone(UTC)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ImessageConfigError("invalid private iMessage config")
    return cast(Mapping[str, object], value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ImessageConfigError("invalid private iMessage config")
        value[key] = item
    return value


__all__ = [
    "ImessageConfig",
    "ImessageConfigError",
    "ImessageHistoryClient",
    "ProductionImessageIngress",
    "ProductionImessageResult",
    "SubprocessImessageHistoryClient",
    "compose_production_imessage_ingress",
    "load_private_imessage_config",
]
