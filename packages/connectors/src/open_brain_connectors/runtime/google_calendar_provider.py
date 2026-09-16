"""Bounded read-only Google Calendar API client and event normalization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import date, datetime, time
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from urllib.request import Request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from open_brain_engine.capture.redaction import has_redaction_finding

from open_brain_connectors.runtime.calendar import CalendarEventRecord
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.google_calendar_contracts import (
    MAX_EVENTS,
    MAX_PAGES,
    PAGE_SIZE,
    GoogleCalendarChange,
    GoogleCalendarError,
    GoogleCalendarMetadata,
    GoogleCalendarPage,
    GoogleCalendarSelection,
    instant,
    opaque_id,
)
from open_brain_connectors.runtime.google_calendar_http import bounded_urlopen

__all__ = ["GoogleCalendarClient", "GoogleCalendarTransport"]

_API_ROOT = "https://www.googleapis.com/calendar/v3"
_TIMEOUT_SECONDS = 15
_MAX_BODY_BYTES = 1_048_576
_MAX_TOKEN_BYTES = 8_192
_MAX_TITLE_CHARS = 200
_MAX_DESCRIPTION_CHARS = 60_000
_MAX_ATTENDEES = 50
_READ_ROLES = frozenset({"owner", "reader", "writer", "writerWithoutPrivateAccess"})
_CALENDAR_LINK_HOSTS = frozenset({"calendar.google.com", "www.google.com"})
_CREDENTIAL_QUERY_KEYS = frozenset(
    {"access_token", "authorization", "client_secret", "code", "key", "password", "token"}
)


class _Response(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class GoogleCalendarTransport(Protocol):
    """Injectable HTTP boundary used by synthetic provider tests."""

    def __call__(self, request: Request, *, timeout: int) -> _Response: ...


def _default_transport(request: Request, *, timeout: int) -> _Response:
    return bounded_urlopen(request, timeout=timeout, maximum=_MAX_BODY_BYTES)


class GoogleCalendarClient:
    """Read one authorized account through fixed Google Calendar endpoints."""

    def __init__(
        self,
        connection_id: str,
        access_token: Callable[[], str],
        *,
        transport: GoogleCalendarTransport | None = None,
    ) -> None:
        try:
            GoogleCalendarSelection(
                connection_id=connection_id,
                calendar_id="validation.invalid",
                range_start="2000-01-01T00:00:00Z",
                range_end="2000-01-02T00:00:00Z",
                timezone="UTC",
            )
        except GoogleCalendarError as error:
            raise GoogleCalendarError("google_calendar_invalid_connection") from error
        if not callable(access_token) or (transport is not None and not callable(transport)):
            raise GoogleCalendarError("google_calendar_invalid_client")
        self._connection_id = connection_id
        self._access_token = access_token
        self._transport = transport or _default_transport

    @property
    def connection_id(self) -> str:
        return self._connection_id

    def list_calendars(self) -> tuple[GoogleCalendarMetadata, ...]:
        calendars: list[GoogleCalendarMetadata] = []
        seen_page_tokens: set[str] = set()
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            query = {"maxResults": str(PAGE_SIZE), "minAccessRole": "reader"}
            if page_token is not None:
                query["pageToken"] = page_token
            payload = self._request_json(
                f"{_API_ROOT}/users/me/calendarList?{urlencode(query)}"
            )
            items = _required_items(payload, maximum=PAGE_SIZE)
            calendars.extend(_calendar_metadata(item) for item in items)
            if len(calendars) > MAX_EVENTS:
                raise GoogleCalendarError("google_calendar_response_too_large")
            page_token = _optional_token(payload.get("nextPageToken"))
            if page_token is None:
                return tuple(calendars)
            if page_token in seen_page_tokens:
                raise GoogleCalendarError("google_calendar_invalid_response")
            seen_page_tokens.add(page_token)
        raise GoogleCalendarError("google_calendar_response_too_large")

    def get_calendar(self, calendar_id: str) -> GoogleCalendarMetadata:
        native_id = _native_calendar_id(calendar_id)
        payload = self._request_json(
            f"{_API_ROOT}/users/me/calendarList/{quote(native_id, safe='')}"
        )
        return _calendar_metadata(payload, expected_id=native_id)

    def fetch_page(
        self,
        selection: GoogleCalendarSelection,
        *,
        page_token: str | None = None,
        sync_token: str | None = None,
    ) -> GoogleCalendarPage:
        if type(selection) is not GoogleCalendarSelection:
            raise GoogleCalendarError("google_calendar_invalid_selection")
        if selection.connection_id != self.connection_id:
            raise GoogleCalendarError("google_calendar_connection_mismatch")
        page_token = _optional_token(page_token)
        sync_token = _optional_token(sync_token)
        metadata = self.get_calendar(selection.calendar_id)
        if metadata.timezone != selection.timezone:
            raise GoogleCalendarError("google_calendar_timezone_changed")

        query = {
            "maxAttendees": str(_MAX_ATTENDEES),
            "maxResults": str(PAGE_SIZE),
            "showDeleted": "true",
            "singleEvents": "true",
            "timeZone": selection.timezone,
        }
        if sync_token is None:
            query.update({"timeMax": selection.range_end, "timeMin": selection.range_start})
        else:
            query["syncToken"] = sync_token
        if page_token is not None:
            query["pageToken"] = page_token
        payload = self._request_json(
            f"{_API_ROOT}/calendars/{quote(selection.calendar_id, safe='')}/events?"
            f"{urlencode(query)}"
        )
        response_timezone = payload.get("timeZone")
        if response_timezone is not None and response_timezone != selection.timezone:
            raise GoogleCalendarError("google_calendar_timezone_changed")
        items = _required_items(payload, maximum=PAGE_SIZE)
        changes = tuple(_event_change(item, selection) for item in items)
        next_page_token = _optional_token(payload.get("nextPageToken"))
        next_sync_token = _optional_token(payload.get("nextSyncToken"))
        if (next_page_token is None) == (next_sync_token is None):
            raise GoogleCalendarError("google_calendar_invalid_response")
        return GoogleCalendarPage(
            changes=changes,
            next_page_token=next_page_token,
            next_sync_token=next_sync_token,
        )

    def _request_json(self, url: str) -> Mapping[str, object]:
        try:
            token = self._access_token()
        except GoogleCalendarError:
            raise
        except Exception:
            raise GoogleCalendarError("google_calendar_auth_required") from None
        if (
            type(token) is not str
            or not token
            or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
            or any(character.isspace() or ord(character) < 33 for character in token)
        ):
            raise GoogleCalendarError("google_calendar_auth_required")
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        try:
            response = self._transport(request, timeout=_TIMEOUT_SECONDS)
            with closing(response):
                status = response.status
                body = response.read(_MAX_BODY_BYTES + 1)
        except HTTPError as error:
            _raise_http_error(error.code)
        except (OSError, TimeoutError, URLError) as error:
            raise GoogleCalendarError("google_calendar_unavailable") from error
        if type(status) is not int or not isinstance(body, bytes):
            raise GoogleCalendarError("google_calendar_invalid_response")
        if status != 200:
            _raise_http_error(status)
        if len(body) > _MAX_BODY_BYTES:
            raise GoogleCalendarError("google_calendar_response_too_large")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GoogleCalendarError("google_calendar_invalid_response") from error
        if not isinstance(decoded, Mapping):
            raise GoogleCalendarError("google_calendar_invalid_response")
        return cast(Mapping[str, object], decoded)


def _raise_http_error(status: int) -> None:
    if status == 401:
        raise GoogleCalendarError("google_calendar_auth_required")
    if status in {403, 404}:
        raise GoogleCalendarError("google_calendar_not_allowed")
    if status == 410:
        raise GoogleCalendarError("google_calendar_sync_expired")
    if status == 429 or 500 <= status <= 599:
        raise GoogleCalendarError("google_calendar_unavailable")
    raise GoogleCalendarError("google_calendar_invalid_response")


def _required_items(
    payload: Mapping[str, object], *, maximum: int
) -> Sequence[Mapping[str, object]]:
    items = payload.get("items", [])
    if (
        not isinstance(items, list)
        or len(items) > maximum
        or any(not isinstance(item, Mapping) for item in items)
    ):
        raise GoogleCalendarError("google_calendar_invalid_response")
    return cast(Sequence[Mapping[str, object]], items)


def _optional_token(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > _MAX_TOKEN_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GoogleCalendarError("google_calendar_invalid_response")
    return value


def _native_calendar_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value == "primary"
        or len(value) > 512
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise GoogleCalendarError("google_calendar_invalid_calendar")
    return value


def _calendar_metadata(
    value: Mapping[str, object], *, expected_id: str | None = None
) -> GoogleCalendarMetadata:
    native_id = _native_calendar_id(value.get("id"))
    title = _required_text(value.get("summary"), maximum=_MAX_TITLE_CHARS)
    timezone = _required_timezone(value.get("timeZone"))
    access_role = value.get("accessRole")
    if native_id != (expected_id or native_id) or access_role not in _READ_ROLES:
        raise GoogleCalendarError("google_calendar_not_allowed")
    return GoogleCalendarMetadata(
        calendar_id=native_id,
        title=title,
        timezone=timezone,
        access_role=access_role,
    )


def _event_change(
    value: Mapping[str, object], selection: GoogleCalendarSelection
) -> GoogleCalendarChange:
    native_id = _required_text(value.get("id"), maximum=1_024)
    event_id = opaque_id(native_id, "event")
    status = value.get("status")
    if status not in {"cancelled", "confirmed", "tentative"}:
        raise GoogleCalendarError("google_calendar_invalid_response")
    if status == "cancelled":
        revision_id = _revision_id(
            {"id": native_id, "status": status, "updated": _optional_text(value.get("updated"))}
        )
        return GoogleCalendarChange(
            event_id=event_id,
            revision_id=revision_id,
            record=None,
            reason="cancelled",
        )

    start = _event_time(value.get("start"), selection.timezone)
    end = _event_time(value.get("end"), selection.timezone)
    if end < start:
        raise GoogleCalendarError("google_calendar_invalid_response")
    range_start, range_end = instant(selection.range_start), instant(selection.range_end)
    overlaps = (
        range_start <= start < range_end
        if start == end
        else start < range_end and end > range_start
    )
    if not overlaps:
        revision_id = _revision_id(
            {
                "end": end.isoformat(timespec="seconds"),
                "id": native_id,
                "start": start.isoformat(timespec="seconds"),
                "status": status,
                "updated": _optional_text(value.get("updated")),
            }
        )
        return GoogleCalendarChange(
            event_id=event_id,
            revision_id=revision_id,
            record=None,
            reason="out_of_range",
        )

    title = _optional_text(value.get("summary"), maximum=_MAX_TITLE_CHARS) or "Calendar event"
    description = _optional_text(value.get("description"), maximum=_MAX_DESCRIPTION_CHARS)
    attendees_omitted = value.get("attendeesOmitted", False)
    if type(attendees_omitted) is not bool:
        raise GoogleCalendarError("google_calendar_invalid_response")
    if attendees_omitted:
        raise GoogleCalendarError("google_calendar_incomplete_event")
    attendees = _attendee_names(value.get("attendees"))
    meeting_link = _meeting_link(value)
    source_reference = _source_reference(value.get("htmlLink"))
    if any(
        has_redaction_finding(text)
        for text in (title, description, *attendees, meeting_link)
        if text is not None
    ):
        raise GoogleCalendarError("google_calendar_redaction_required")
    canonical = {
        "attendees": attendees,
        "description": description,
        "end": end.isoformat(timespec="seconds"),
        "id": native_id,
        "meeting_link": meeting_link,
        "source_reference": source_reference,
        "start": start.isoformat(timespec="seconds"),
        "status": status,
        "title": title,
    }
    revision_id = _revision_id(canonical)
    try:
        record = CalendarEventRecord(
            calendar_id=selection.resource_id,
            event_id=event_id,
            revision_id=revision_id,
            provider="google_calendar",
            title=title,
            start_time=start.isoformat(timespec="seconds"),
            end_time=end.isoformat(timespec="seconds"),
            timezone=selection.timezone,
            description=description,
            attendees=attendees,
            meeting_link=meeting_link,
        )
    except ConnectorContractError as error:
        raise GoogleCalendarError("google_calendar_invalid_response") from error
    return GoogleCalendarChange(
        event_id=event_id,
        revision_id=revision_id,
        record=record,
        source_reference=source_reference,
    )


def _event_time(value: object, default_timezone: str) -> datetime:
    if not isinstance(value, Mapping):
        raise GoogleCalendarError("google_calendar_invalid_response")
    date_time_value = value.get("dateTime")
    date_value = value.get("date")
    if (date_time_value is None) == (date_value is None):
        raise GoogleCalendarError("google_calendar_invalid_response")
    timezone_name = value.get("timeZone", default_timezone)
    timezone = _zoneinfo(timezone_name)
    try:
        if date_time_value is not None:
            text = _required_text(date_time_value, maximum=64)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=timezone)
            return parsed
        day = date.fromisoformat(_required_text(date_value, maximum=10))
        return datetime.combine(day, time.min, tzinfo=timezone)
    except ValueError as error:
        raise GoogleCalendarError("google_calendar_invalid_response") from error


def _required_timezone(value: object) -> str:
    text = _required_text(value, maximum=64)
    _zoneinfo(text)
    return text


def _zoneinfo(value: object) -> ZoneInfo:
    if type(value) is not str:
        raise GoogleCalendarError("google_calendar_invalid_response")
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise GoogleCalendarError("google_calendar_invalid_response") from error


def _required_text(value: object, *, maximum: int) -> str:
    text = _optional_text(value, maximum=maximum)
    if text is None:
        raise GoogleCalendarError("google_calendar_invalid_response")
    return text


def _optional_text(value: object, *, maximum: int = 64) -> str | None:
    if value is None:
        return None
    if type(value) is not str or "\x00" in value or len(value) > maximum:
        raise GoogleCalendarError("google_calendar_invalid_response")
    text = value.strip()
    return text or None


def _attendee_names(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_ATTENDEES:
        raise GoogleCalendarError("google_calendar_invalid_response")
    names: list[str] = []
    unnamed_count = 0
    for attendee in value:
        if not isinstance(attendee, Mapping):
            raise GoogleCalendarError("google_calendar_invalid_response")
        name = _optional_text(attendee.get("displayName"), maximum=200)
        if name is not None and name not in names:
            names.append(name)
        elif name is None:
            # Email addresses are deliberately not copied into capture text.
            unnamed_count += 1
    if unnamed_count:
        suffix = "attendee" if unnamed_count == 1 else "attendees"
        names.append(f"{unnamed_count} unnamed {suffix}")
    return tuple(names)


def _source_reference(value: object) -> str | None:
    link = _optional_text(value, maximum=2_048)
    if link is None:
        return None
    try:
        parsed = urlsplit(link)
        port = parsed.port
        query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=8)
    except ValueError as error:
        raise GoogleCalendarError("google_calendar_invalid_response") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _CALENDAR_LINK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path != "/calendar/event"
        or parsed.fragment
        or any(character.isspace() for character in link)
        or not any(key == "eid" and bool(item) for key, item in query)
        or any(key.lower() in _CREDENTIAL_QUERY_KEYS for key, _ in query)
    ):
        raise GoogleCalendarError("google_calendar_invalid_response")
    return link


def _meeting_link(value: Mapping[str, object]) -> str | None:
    link = _optional_text(value.get("hangoutLink"), maximum=2_048)
    if link is None:
        conference = value.get("conferenceData")
        if conference is not None:
            if not isinstance(conference, Mapping):
                raise GoogleCalendarError("google_calendar_invalid_response")
            entries = conference.get("entryPoints", [])
            if not isinstance(entries, list) or len(entries) > 16:
                raise GoogleCalendarError("google_calendar_invalid_response")
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise GoogleCalendarError("google_calendar_invalid_response")
                if entry.get("entryPointType") == "video":
                    link = _optional_text(entry.get("uri"), maximum=2_048)
                    break
    if link is not None and (
        not link.startswith("https://") or any(character.isspace() for character in link)
    ):
        raise GoogleCalendarError("google_calendar_invalid_response")
    return link


def _revision_id(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
