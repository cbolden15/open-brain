"""Read-only, provider-neutral mail cursor and agenda boundaries."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from open_brain_legacy._compat.open_brain.integrations.config import IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.ports import (
    Capability,
    ProviderSyncRequest,
    ProviderSyncResult,
    RedactedText,
    SyncStatus,
)

_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MAX_AGENDA_ITEMS = 8
_MAX_SOURCE_EVENTS = 64
_MAX_TITLE_LENGTH = 256
_MAX_DESCRIPTION_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class MailPage:
    """Opaque mail item references returned by an injected read-only provider."""

    item_refs: tuple[str, ...]
    next_cursor_ref: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.item_refs, tuple)
            or not self.item_refs
            or len(self.item_refs) > _MAX_SOURCE_EVENTS
            or len(set(self.item_refs)) != len(self.item_refs)
            or any(not _is_opaque_id(item_ref) for item_ref in self.item_refs)
            or (
                self.next_cursor_ref is not None
                and not _is_opaque_id(self.next_cursor_ref)
            )
        ):
            raise ValueError("invalid mail page")


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """Provider input whose description is deliberately omitted from agenda output."""

    event_id: str
    starts_at: datetime
    ends_at: datetime
    title: str
    description: str

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.event_id)
            or not isinstance(self.starts_at, datetime)
            or not isinstance(self.ends_at, datetime)
            or self.ends_at <= self.starts_at
            or not _is_bounded_text(self.title, maximum=_MAX_TITLE_LENGTH, allow_empty=False)
            or not _is_bounded_text(
                self.description,
                maximum=_MAX_DESCRIPTION_LENGTH,
                allow_empty=True,
            )
        ):
            raise ValueError("invalid calendar event")


@dataclass(frozen=True, slots=True)
class AgendaItem:
    """Bounded public agenda data with no description field."""

    event_id: str
    starts_at: datetime
    ends_at: datetime
    title: RedactedText

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.event_id)
            or not isinstance(self.starts_at, datetime)
            or not isinstance(self.ends_at, datetime)
            or self.ends_at <= self.starts_at
            or not isinstance(self.title, RedactedText)
        ):
            raise ValueError("invalid agenda item")


class DurableCursorStore:
    """Atomically persist opaque cursors only; provider data never reaches disk."""

    def __init__(self, *, path: Path) -> None:
        if not isinstance(path, Path) or not path.name:
            raise ValueError("invalid cursor path")
        self._path = path

    def read(self, *, resource_ref: str) -> str | None:
        if not _is_opaque_id(resource_ref):
            raise ValueError("invalid resource reference")
        with self._locked():
            state = self._read_state()
            return state.get(resource_ref)

    def advance(
        self,
        *,
        resource_ref: str,
        expected_cursor_ref: str | None,
        cursor_ref: str | None,
    ) -> bool:
        if not _is_opaque_id(resource_ref) or (
            expected_cursor_ref is not None and not _is_opaque_id(expected_cursor_ref)
        ) or (
            cursor_ref is not None and not _is_opaque_id(cursor_ref)
        ):
            raise ValueError("invalid cursor update")
        with self._locked():
            state = self._read_state()
            if state.get(resource_ref) != expected_cursor_ref:
                return False
            if cursor_ref is None:
                state.pop(resource_ref, None)
            else:
                state[resource_ref] = cursor_ref
            self._write_state(state)
            return True

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self._path.parent / f".{self._path.name}.lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
        except OSError as error:
            raise ValueError("cursor storage unavailable") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_state(self) -> dict[str, str]:
        try:
            payload = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise ValueError("cursor storage unavailable") from error
        try:
            state = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("invalid cursor storage") from error
        if not isinstance(state, dict) or any(
            not _is_opaque_id(resource_ref) or not _is_opaque_id(cursor_ref)
            for resource_ref, cursor_ref in state.items()
        ):
            raise ValueError("invalid cursor storage")
        return dict(state)

    def _write_state(self, state: dict[str, str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_path = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._path)
        except OSError as error:
            raise ValueError("cursor storage unavailable") from error


class MailCalendarIntegration:
    """Local-only adapter: it never creates, updates, or deletes provider data."""

    def __init__(self, *, config: IntegrationConfig, cursor_store: DurableCursorStore) -> None:
        if not isinstance(config, IntegrationConfig) or not isinstance(
            cursor_store, DurableCursorStore
        ):
            raise ValueError("invalid mail calendar integration")
        self._config = config
        self._cursor_store = cursor_store

    def sync_mail(
        self,
        *,
        request: ProviderSyncRequest,
        page: MailPage,
    ) -> ProviderSyncResult:
        """Advance a local opaque cursor after a supplied read-only page is accepted."""
        if not isinstance(request, ProviderSyncRequest) or not isinstance(page, MailPage):
            raise ValueError("invalid mail sync input")
        if request.capability is not Capability.MAIL_CALENDAR:
            raise ValueError("invalid mail sync capability")
        try:
            stored_cursor = self._cursor_store.read(resource_ref=request.resource_ref)
        except ValueError:
            return _sync_result(status=SyncStatus.RETRYABLE, next_cursor_ref=None)
        if not self._config.live_adapter_enabled(Capability.MAIL_CALENDAR):
            return _sync_result(status=SyncStatus.UNSUPPORTED, next_cursor_ref=stored_cursor)
        if request.cursor_ref != stored_cursor:
            return _sync_result(status=SyncStatus.RETRYABLE, next_cursor_ref=stored_cursor)
        if request.dry_run:
            return _sync_result(
                status=SyncStatus.DRY_RUN,
                created=len(page.item_refs),
                next_cursor_ref=page.next_cursor_ref,
            )
        try:
            advanced = self._cursor_store.advance(
                resource_ref=request.resource_ref,
                expected_cursor_ref=request.cursor_ref,
                cursor_ref=page.next_cursor_ref,
            )
            if not advanced:
                current_cursor = self._cursor_store.read(resource_ref=request.resource_ref)
                return _sync_result(
                    status=SyncStatus.RETRYABLE,
                    next_cursor_ref=current_cursor,
                )
        except ValueError:
            return _sync_result(status=SyncStatus.RETRYABLE, next_cursor_ref=stored_cursor)
        return _sync_result(
            status=SyncStatus.COMPLETED,
            created=len(page.item_refs),
            next_cursor_ref=page.next_cursor_ref,
        )

    def agenda(
        self,
        *,
        events: Iterable[CalendarEvent],
        limit: int = _MAX_AGENDA_ITEMS,
    ) -> tuple[AgendaItem, ...]:
        """Return a capped, title-only agenda from injected provider data."""
        if type(limit) is not int or not 1 <= limit <= _MAX_AGENDA_ITEMS:
            raise ValueError("invalid agenda limit")
        if not self._config.live_adapter_enabled(Capability.MAIL_CALENDAR):
            return ()
        supplied_events = tuple(events)
        if len(supplied_events) > _MAX_SOURCE_EVENTS or any(
            not isinstance(event, CalendarEvent) for event in supplied_events
        ):
            raise ValueError("invalid agenda events")
        return tuple(
            AgendaItem(
                event_id=event.event_id,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                title=RedactedText.redact(event.title),
            )
            for event in sorted(
                supplied_events,
                key=lambda event: (event.starts_at, event.event_id),
            )[:limit]
        )


def _sync_result(
    *,
    status: SyncStatus,
    created: int = 0,
    next_cursor_ref: str | None,
) -> ProviderSyncResult:
    return ProviderSyncResult(
        capability=Capability.MAIL_CALENDAR,
        status=status,
        created=created,
        updated=0,
        removed=0,
        next_cursor_ref=next_cursor_ref,
    )


def _is_opaque_id(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None


def _is_bounded_text(value: object, *, maximum: int, allow_empty: bool) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and not any(ord(character) < 32 and character not in "\n\t" for character in value)
    )
