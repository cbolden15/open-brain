from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from open_brain_connectors.runtime.google_calendar_contracts import (
    GoogleCalendarError,
    GoogleCalendarSelection,
    opaque_id,
)
from open_brain_connectors.runtime.google_calendar_provider import GoogleCalendarClient


@dataclass
class _Response:
    body: bytes
    status: int = 200
    closed: bool = False

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]

    def close(self) -> None:
        self.closed = True


class _Transport:
    def __init__(self, responses: Sequence[Mapping[str, object] | tuple[int, object]]) -> None:
        self._responses = list(responses)
        self.requests: list[Request] = []
        self.timeouts: list[int] = []
        self.returned: list[_Response] = []

    def __call__(self, request: Request, *, timeout: int) -> _Response:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self._responses:
            raise AssertionError("unexpected request")
        queued = self._responses.pop(0)
        status, value = queued if isinstance(queued, tuple) else (200, queued)
        response = _Response(json.dumps(value).encode("utf-8"), status=status)
        self.returned.append(response)
        return response


def _metadata(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "accessRole": "reader",
        "id": "team@example.invalid",
        "summary": "Synthetic Team Calendar",
        "timeZone": "America/Chicago",
    }
    value.update(overrides)
    return value


def _selection(**overrides: object) -> GoogleCalendarSelection:
    value: dict[str, object] = {
        "calendar_id": "team@example.invalid",
        "connection_id": "account:calendar-fixture",
        "range_end": "2026-09-17T00:00:00Z",
        "range_start": "2026-09-15T00:00:00Z",
        "timezone": "America/Chicago",
    }
    value.update(overrides)
    return GoogleCalendarSelection(**cast(dict[str, str], value))


def _event(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "attendees": [
            {"displayName": "Avery Example", "email": "private@example.invalid"},
            {"email": "discarded@example.invalid"},
        ],
        "description": "Synthetic planning notes.",
        "end": {"dateTime": "2026-09-15T11:00:00-05:00"},
        "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc",
        "htmlLink": "https://www.google.com/calendar/event?eid=synthetic-event-token",
        "id": "provider-event-1",
        "start": {"dateTime": "2026-09-15T10:00:00-05:00"},
        "status": "confirmed",
        "summary": "Synthetic planning event",
        "updated": "2026-09-14T12:00:00Z",
    }
    value.update(overrides)
    return value


def _client(transport: _Transport) -> GoogleCalendarClient:
    return GoogleCalendarClient(
        "account:calendar-fixture",
        lambda: "synthetic-access-token",
        transport=transport,
    )


def _query(request: Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.full_url).query)


def test_list_calendars_reads_all_bounded_pages_and_closes_responses() -> None:
    transport = _Transport(
        (
            {"items": [_metadata()], "nextPageToken": "page-2"},
            {
                "items": [
                    _metadata(
                        id="secondary@example.invalid",
                        summary="Secondary",
                        timeZone="UTC",
                        accessRole="owner",
                    )
                ]
            },
        )
    )

    calendars = _client(transport).list_calendars()

    assert [calendar.calendar_id for calendar in calendars] == [
        "team@example.invalid",
        "secondary@example.invalid",
    ]
    assert _query(transport.requests[0]) == {
        "maxResults": ["25"],
        "minAccessRole": ["reader"],
    }
    assert _query(transport.requests[1])["pageToken"] == ["page-2"]
    assert all(request.get_method() == "GET" for request in transport.requests)
    assert all(
        request.get_header("Authorization") == "Bearer synthetic-access-token"
        for request in transport.requests
    )
    assert transport.timeouts == [15, 15]
    assert all(response.closed for response in transport.returned)


def test_fetch_initial_page_uses_bounds_and_normalizes_events() -> None:
    transport = _Transport(
        (
            _metadata(),
            {
                "items": [_event()],
                "nextSyncToken": "sync-1",
                "timeZone": "America/Chicago",
            },
        )
    )
    selection = _selection()

    page = _client(transport).fetch_page(selection)

    assert page.next_page_token is None
    assert page.next_sync_token == "sync-1"
    assert len(page.changes) == 1
    change = page.changes[0]
    assert change.event_id == opaque_id("provider-event-1", "event")
    assert change.reason == "updated"
    assert change.record is not None
    assert change.record.calendar_id == selection.resource_id
    assert change.record.attendees == ("Avery Example", "1 unnamed attendee")
    assert change.record.meeting_link == "https://meet.google.com/aaa-bbbb-ccc"
    assert change.source_reference == (
        "https://www.google.com/calendar/event?eid=synthetic-event-token"
    )
    assert "private@example.invalid" not in repr(change.record)
    query = _query(transport.requests[1])
    assert query["timeMin"] == [selection.range_start]
    assert query["timeMax"] == [selection.range_end]
    assert query["singleEvents"] == ["true"]
    assert query["showDeleted"] == ["true"]
    assert query["maxAttendees"] == ["50"]
    assert transport.requests[1].full_url.startswith(
        "https://www.googleapis.com/calendar/v3/calendars/team%40example.invalid/events?"
    )


def test_fetch_incremental_page_omits_incompatible_bounds_and_emits_tombstones() -> None:
    out_of_range = _event(
        id="moved-event",
        summary="api_key = outside-range-value",
        description="client_secret = outside-range-value",
        start={"dateTime": "2026-09-20T10:00:00-05:00"},
        end={"dateTime": "2026-09-20T11:00:00-05:00"},
    )
    transport = _Transport(
        (
            _metadata(),
            {
                "items": [
                    {"id": "deleted-event", "status": "cancelled"},
                    out_of_range,
                ],
                "nextSyncToken": "sync-2",
                "timeZone": "America/Chicago",
            },
        )
    )

    page = _client(transport).fetch_page(
        _selection(), page_token="next-page", sync_token="sync-1"
    )

    assert [(change.reason, change.record) for change in page.changes] == [
        ("cancelled", None),
        ("out_of_range", None),
    ]
    assert all(change.source_reference is None for change in page.changes)
    query = _query(transport.requests[1])
    assert query["syncToken"] == ["sync-1"]
    assert query["pageToken"] == ["next-page"]
    assert "timeMin" not in query
    assert "timeMax" not in query
    assert "orderBy" not in query


def test_fetch_normalizes_all_day_event_in_selected_timezone() -> None:
    transport = _Transport(
        (
            _metadata(),
            {
                "items": [
                    _event(
                        id="all-day-event",
                        start={"date": "2026-09-15"},
                        end={"date": "2026-09-16"},
                    )
                ],
                "nextSyncToken": "sync-all-day",
            },
        )
    )

    page = _client(transport).fetch_page(_selection())

    record = page.changes[0].record
    assert record is not None
    assert record.start_time == "2026-09-15T00:00:00-05:00"
    assert record.end_time == "2026-09-16T00:00:00-05:00"


def test_revision_changes_only_when_normalized_provider_content_changes() -> None:
    unchanged = _event(updated="2026-09-15T01:00:00Z")
    changed = _event(description="Changed synthetic notes.")
    transport = _Transport(
        (
            _metadata(),
            {"items": [_event()], "nextSyncToken": "one"},
            _metadata(),
            {"items": [unchanged], "nextSyncToken": "two"},
            _metadata(),
            {"items": [changed], "nextSyncToken": "three"},
        )
    )
    client = _client(transport)

    first = client.fetch_page(_selection()).changes[0]
    second = client.fetch_page(_selection()).changes[0]
    third = client.fetch_page(_selection()).changes[0]

    assert first.revision_id == second.revision_id
    assert third.revision_id != first.revision_id


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "google_calendar_auth_required"),
        (403, "google_calendar_not_allowed"),
        (410, "google_calendar_sync_expired"),
        (429, "google_calendar_unavailable"),
        (500, "google_calendar_unavailable"),
    ],
)
def test_provider_maps_http_failures_to_closed_codes(status: int, code: str) -> None:
    transport = _Transport(((status, {}),))

    with pytest.raises(GoogleCalendarError, match=f"^{code}$"):
        _client(transport).list_calendars()


def test_provider_rejects_connection_and_timezone_mismatch_before_event_request() -> None:
    client = _client(_Transport(()))
    with pytest.raises(GoogleCalendarError, match="^google_calendar_connection_mismatch$"):
        client.fetch_page(_selection(connection_id="account:other"))

    transport = _Transport((_metadata(timeZone="UTC"),))
    with pytest.raises(GoogleCalendarError, match="^google_calendar_timezone_changed$"):
        _client(transport).fetch_page(_selection())
    assert len(transport.requests) == 1


def test_provider_rejects_partial_terminal_response_and_sensitive_in_range_content() -> None:
    partial_transport = _Transport((_metadata(), {"items": [_event()]}))
    with pytest.raises(GoogleCalendarError, match="^google_calendar_invalid_response$"):
        _client(partial_transport).fetch_page(_selection())

    sensitive_transport = _Transport(
        (
            _metadata(),
            {
                "items": [_event(description="client_secret = synthetic-value")],
                "nextSyncToken": "sync-sensitive",
            },
        )
    )
    with pytest.raises(GoogleCalendarError, match="^google_calendar_redaction_required$"):
        _client(sensitive_transport).fetch_page(_selection())

    unsafe_link_transport = _Transport(
        (
            _metadata(),
            {
                "items": [_event(htmlLink="https://evil.invalid/calendar/event?eid=token")],
                "nextSyncToken": "sync-unsafe-link",
            },
        )
    )
    with pytest.raises(GoogleCalendarError, match="^google_calendar_invalid_response$"):
        _client(unsafe_link_transport).fetch_page(_selection())

    credential_link_transport = _Transport(
        (
            _metadata(),
            {
                "items": [
                    _event(
                        htmlLink=(
                            "https://www.google.com/calendar/event?eid=synthetic"
                            "&access_token=forbidden"
                        )
                    )
                ],
                "nextSyncToken": "sync-credential-link",
            },
        )
    )
    with pytest.raises(GoogleCalendarError, match="^google_calendar_invalid_response$"):
        _client(credential_link_transport).fetch_page(_selection())


def test_provider_rejects_truncated_attendees_before_returning_event() -> None:
    transport = _Transport(
        (
            _metadata(),
            {
                "items": [_event(attendeesOmitted=True)],
                "nextSyncToken": "sync-truncated-attendees",
            },
        )
    )

    with pytest.raises(GoogleCalendarError, match="^google_calendar_incomplete_event$"):
        _client(transport).fetch_page(_selection())


def test_list_calendars_rejects_cursor_loop() -> None:
    transport = _Transport(
        (
            {"items": [], "nextPageToken": "loop"},
            {"items": [], "nextPageToken": "loop"},
        )
    )

    with pytest.raises(GoogleCalendarError, match="^google_calendar_invalid_response$"):
        _client(transport).list_calendars()
