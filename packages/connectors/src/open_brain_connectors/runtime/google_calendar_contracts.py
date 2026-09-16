"""Shared values for the optional Google Calendar provider and sync host."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from open_brain_connectors.runtime.calendar import CalendarEventRecord
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.source_intake import SourceRecordIntake

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
MAX_PAGES = 20
MAX_EVENTS = 500
PAGE_SIZE = 25


class GoogleCalendarError(ConnectorContractError):
    """Closed error code; never include provider bodies or credentials."""


def instant(value: str) -> datetime:
    try:
        if (type(value) is not str or len(value) > 64
                or any(ord(character) < 32 for character in value)):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (ValueError, TypeError, AttributeError) as error:
        raise GoogleCalendarError("google_calendar_invalid_range") from error


def opaque_id(value: str, prefix: str) -> str:
    return prefix + ":" + hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class GoogleCalendarSelection:
    connection_id: str
    calendar_id: str
    range_start: str
    range_end: str
    timezone: str

    def __post_init__(self) -> None:
        if (type(self.connection_id) is not str or re.fullmatch(
                r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}", self.connection_id) is None):
            raise GoogleCalendarError("google_calendar_invalid_selection")
        if (type(self.calendar_id) is not str or not self.calendar_id
                or len(self.calendar_id) > 512 or self.calendar_id == "primary"
                or any(ord(c) < 33 or ord(c) == 127 for c in self.calendar_id)):
            raise GoogleCalendarError("google_calendar_invalid_selection")
        start, end = instant(self.range_start), instant(self.range_end)
        if not start < end or end - start > timedelta(days=366):
            raise GoogleCalendarError("google_calendar_invalid_range")
        try:
            if type(self.timezone) is not str or len(self.timezone) > 64:
                raise ValueError
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise GoogleCalendarError("google_calendar_invalid_timezone") from error

    @property
    def resource_id(self) -> str:
        return opaque_id(self.calendar_id, "calendar")

    def to_dict(self) -> dict[str, str]:
        """Private sync configuration. Do not log or emit this as a receipt."""
        return {"connection_id": self.connection_id, "calendar_id": self.calendar_id,
                "range_start": self.range_start, "range_end": self.range_end,
                "timezone": self.timezone}

    def identity(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class GoogleCalendarChange:
    # The provider hashes native IDs before they reach sync state or receipts.
    event_id: str
    revision_id: str
    record: CalendarEventRecord | None
    reason: Literal["updated", "cancelled", "out_of_range"] = "updated"
    source_reference: str | None = None


@dataclass(frozen=True, slots=True)
class GoogleCalendarPage:
    changes: tuple[GoogleCalendarChange, ...]
    next_page_token: str | None = None
    next_sync_token: str | None = None


@dataclass(frozen=True, slots=True)
class GoogleCalendarMetadata:
    calendar_id: str
    title: str
    timezone: str
    access_role: str


class GoogleCalendarProvider(Protocol):
    @property
    def connection_id(self) -> str: ...

    def fetch_page(
        self, selection: GoogleCalendarSelection, *, page_token: str | None = None,
        sync_token: str | None = None,
    ) -> GoogleCalendarPage: ...


class GoogleCalendarCapture(Protocol):
    def __call__(self, intake: SourceRecordIntake) -> None: ...
