from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_brain_connectors.runtime.calendar import (
    CalendarCheckpoint,
    CalendarCheckpointStore,
    CalendarEventRecord,
    CalendarPageStatus,
    CalendarSourceAdapter,
)
from open_brain_connectors.runtime.connectors import ConnectorContractError, ConnectorOutcome
from open_brain_connectors.runtime.source_intake import SourceRecordIntake

from .test_agent_session_source_adapter import _capture_sink
from .test_source_intake import _privacy

_CALENDAR_ID = "calendar:primary"
_EVENT_ID = "event:standup"


def test_calendar_adapter_builds_metadata_preview_without_body() -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="google_calendar",
    )

    page = adapter.page_from_events(
        selection,
        (_event(description="Synthetic event body must not appear in preview."),),
        privacy=_privacy(),
        selected_event_ids=(_EVENT_ID,),
        range_start="2026-09-15T00:00:00Z",
        range_end="2026-09-16T00:00:00Z",
        next_cursor="cursor:event2",
    )

    assert page.status is CalendarPageStatus.READY
    assert page.preview is not None
    assert page.preview.selection == selection
    assert page.preview.next_cursor == "cursor:event2"
    assert [record.content_type for record in page.preview.records] == ["calendar_event"]
    assert page.preview.records[0].title == "Daily Standup"
    assert "must not appear" not in repr(page.preview.to_dict())


def test_calendar_source_reference_encodes_event_path_token() -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="google_calendar",
    )

    page = adapter.page_from_events(
        selection,
        (_event(event_id="event:team/standup@fixture"),),
        privacy=_privacy(),
        selected_event_ids=("event:team/standup@fixture",),
        range_start="2026-09-15T00:00:00Z",
        range_end="2026-09-16T00:00:00Z",
    )

    assert page.preview is not None
    assert page.preview.records[0].source_reference.endswith(
        "/calendars/google/team%2Fstandup%40fixture"
    )


def test_calendar_import_updates_revision_checkpoint_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="outlook_calendar",
    )
    original = adapter.record_from_event(
        _event(
            provider="outlook_calendar",
            description="Synthetic original event notes.",
            revision_id="rev-1",
        )
    )
    original_page = adapter.preview(selection, (original,), privacy=_privacy())
    original_intake = adapter.intake(selection, original, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        CalendarCheckpoint.initial(selection),
        adapter.page_from_events(
            selection,
            (
                _event(
                    provider="outlook_calendar",
                    description="Synthetic original event notes.",
                    revision_id="rev-1",
                ),
            ),
            privacy=_privacy(),
            selected_event_ids=(_EVENT_ID,),
            range_start="2026-09-15T00:00:00Z",
            range_end="2026-09-16T00:00:00Z",
        ),
        (original_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert checkpoint.committed_revision_identities == (
        original_intake.key.revision_identity(),
    )

    replayed_checkpoint, replayed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_events(
            selection,
            (
                _event(
                    provider="outlook_calendar",
                    description="Synthetic original event notes.",
                    revision_id="rev-1",
                ),
            ),
            privacy=_privacy(),
            selected_event_ids=(_EVENT_ID,),
            range_start="2026-09-15T00:00:00Z",
            range_end="2026-09-16T00:00:00Z",
        ),
        (original_intake,),
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    changed = adapter.record_from_event(
        _event(
            provider="outlook_calendar",
            description="Synthetic changed event notes.",
            revision_id="rev-2",
        )
    )
    changed_page = adapter.preview(selection, (changed,), privacy=_privacy())
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())
    changed_checkpoint, changed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_events(
            selection,
            (
                _event(
                    provider="outlook_calendar",
                    description="Synthetic changed event notes.",
                    revision_id="rev-2",
                ),
            ),
            privacy=_privacy(),
            selected_event_ids=(_EVENT_ID,),
            range_start="2026-09-15T00:00:00Z",
            range_end="2026-09-16T00:00:00Z",
        ),
        (changed_intake,),
        sink,
    )

    assert original_page.records[0].delivery_id == changed_page.records[0].delivery_id
    assert changed_receipt.outcome is ConnectorOutcome.COMPLETED
    assert changed_receipt.created_count == 1
    assert changed_checkpoint.committed_delivery_ids == checkpoint.committed_delivery_ids
    assert changed_checkpoint.committed_revision_identities == (
        changed_intake.key.revision_identity(),
    )


def test_calendar_adapter_requires_selected_event_and_date_range() -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="google_calendar",
    )

    page = adapter.page_from_events(
        selection,
        (
            _event(
                event_id="event:outside",
                description="access_token = synthetic-secret",
                start_time="2026-09-17T10:00:00Z",
                end_time="2026-09-17T10:30:00Z",
            ),
            _event(title="Selected Event"),
        ),
        privacy=_privacy(),
        selected_event_ids=(_EVENT_ID,),
        range_start="2026-09-15T00:00:00Z",
        range_end="2026-09-16T00:00:00Z",
    )

    assert page.preview is not None
    assert len(page.preview.records) == 1
    assert page.preview.records[0].title == "Selected Event"

    with pytest.raises(ConnectorContractError, match="invalid calendar events"):
        adapter.page_from_events(
            selection,
            (_event(provider="outlook_calendar"),),
            privacy=_privacy(),
            selected_event_ids=(_EVENT_ID,),
            range_start="2026-09-15T00:00:00Z",
            range_end="2026-09-16T00:00:00Z",
        )
    with pytest.raises(ConnectorContractError, match="invalid calendar events"):
        adapter.page_from_events(
            selection,
            (_event(),),
            privacy=_privacy(),
            selected_event_ids=(),
            range_start="2026-09-15T00:00:00Z",
            range_end="2026-09-16T00:00:00Z",
        )


def test_calendar_adapter_normalizes_offset_instants_for_range_filtering() -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="google_calendar",
    )

    with pytest.raises(ConnectorContractError, match="invalid calendar events"):
        adapter.page_from_events(
            selection,
            (
                _event(
                    start_time="2026-09-15T04:00:00-05:00",
                    end_time="2026-09-15T04:30:00-05:00",
                ),
            ),
            privacy=_privacy(),
            selected_event_ids=(_EVENT_ID,),
            range_start="2026-09-15T00:00:00Z",
            range_end="2026-09-15T08:00:00Z",
        )


def test_calendar_adapter_rejects_invalid_calendar_dates_as_contract_errors() -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="google_calendar",
    )

    with pytest.raises(ConnectorContractError, match="invalid calendar event"):
        adapter.page_from_events(
            selection,
            (
                _event(
                    start_time="2026-99-99T10:00:00Z",
                    end_time="2026-99-99T10:30:00Z",
                ),
            ),
            privacy=_privacy(),
            selected_event_ids=(_EVENT_ID,),
            range_start="2026-09-15T00:00:00Z",
            range_end="2026-09-16T00:00:00Z",
        )


@pytest.mark.parametrize(
    ("start_time", "end_time", "included"),
    [
        ("2026-09-14T23:00:00Z", "2026-09-15T01:00:00Z", True),
        ("2026-09-14T23:00:00Z", "2026-09-16T01:00:00Z", True),
        ("2026-09-15T23:00:00Z", "2026-09-16T01:00:00Z", True),
        ("2026-09-14T23:00:00Z", "2026-09-15T00:00:00Z", False),
        ("2026-09-16T00:00:00Z", "2026-09-16T01:00:00Z", False),
        ("2026-09-15T01:00:00+02:00", "2026-09-15T03:00:00+02:00", True),
        ("2026-09-15T00:00:00Z", "2026-09-15T00:00:00Z", True),
        ("2026-09-16T00:00:00Z", "2026-09-16T00:00:00Z", False),
    ],
)
def test_calendar_adapter_selects_overlapping_event_intervals(
    start_time: str,
    end_time: str,
    included: bool,
) -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="google_calendar",
    )
    # Excluded event bodies must not be converted or secret-scanned.
    description = "Synthetic crossing event." if included else "client_" + "secret = excluded"
    page = adapter.page_from_events(
        selection,
        (
            _event(),
            _event(
                event_id="event:boundary",
                title="Boundary event",
                start_time=start_time,
                end_time=end_time,
                description=description,
            ),
        ),
        privacy=_privacy(),
        selected_event_ids=(_EVENT_ID, "event:boundary"),
        range_start="2026-09-15T00:00:00Z",
        range_end="2026-09-16T00:00:00Z",
    )

    assert page.preview is not None
    assert [record.title for record in page.preview.records] == (
        ["Daily Standup", "Boundary event"] if included else ["Daily Standup"]
    )


@pytest.mark.parametrize(
    "end_time",
    [
        "2026-09-15T09:00:00Z",
        "2026-09-15T11:00:00+02:00",
        "2026-09-15T08:00:00-01:00",
    ],
)
def test_calendar_record_rejects_reversed_event_intervals(end_time: str) -> None:
    with pytest.raises(ConnectorContractError, match="invalid calendar event"):
        CalendarEventRecord(
            calendar_id=_CALENDAR_ID,
            event_id=_EVENT_ID,
            revision_id="rev-1",
            provider="google_calendar",
            title="Daily Standup",
            start_time="2026-09-15T10:00:00Z",
            end_time=end_time,
            timezone="America/Chicago",
        )


@pytest.mark.parametrize("range_end", ["2026-09-14T00:00:00Z", "2026-09-15T00:00:00Z"])
def test_calendar_adapter_rejects_non_positive_selected_ranges(range_end: str) -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="google_calendar",
    )

    with pytest.raises(ConnectorContractError, match="invalid calendar events"):
        adapter.page_from_events(
            selection,
            (_event(start_time="2026-09-13T00:00:00Z", end_time="2026-09-17T00:00:00Z"),),
            privacy=_privacy(),
            selected_event_ids=(_EVENT_ID,),
            range_start="2026-09-15T00:00:00Z",
            range_end=range_end,
        )


def test_calendar_import_rejects_spoofed_intake_source_reference(tmp_path: Path) -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="google_calendar",
    )
    record = adapter.record_from_event(_event())
    preview = adapter.preview(selection, (record,), privacy=_privacy())
    page = adapter.page_from_events(
        selection,
        (_event(),),
        privacy=_privacy(),
        selected_event_ids=(_EVENT_ID,),
        range_start="2026-09-15T00:00:00Z",
        range_end="2026-09-16T00:00:00Z",
    )
    valid_intake = adapter.intake(selection, record, privacy=_privacy())
    spoofed_intake = SourceRecordIntake(
        key=valid_intake.key,
        url="https://example.invalid/spoofed",
        title=valid_intake.title,
        text=valid_intake.text,
        privacy=valid_intake.privacy,
    )

    with pytest.raises(ConnectorContractError, match="invalid calendar import"):
        adapter.import_page(
            CalendarCheckpoint.initial(selection),
            page,
            (spoofed_intake,),
            _capture_sink(tmp_path),
        )

    assert page.preview == preview


def test_calendar_adapter_rejects_secret_feedback_and_invalid_provider() -> None:
    adapter = CalendarSourceAdapter()

    with pytest.raises(ConnectorContractError, match="invalid calendar event"):
        adapter.record_from_event(_event(source_kind="open_brain_result"))
    with pytest.raises(ConnectorContractError, match="invalid calendar event"):
        adapter.record_from_event(_event(body_secret_scan="finding"))
    with pytest.raises(ConnectorContractError, match="invalid calendar event"):
        adapter.record_from_event(_event(description="client_secret = synthetic-secret"))
    with pytest.raises(ConnectorContractError, match="invalid calendar event"):
        CalendarEventRecord(
            calendar_id=_CALENDAR_ID,
            event_id=_EVENT_ID,
            revision_id="rev-1",
            provider="teams_calendar",
            title="Daily Standup",
            start_time="2026-09-15T10:00:00Z",
            end_time="2026-09-15T10:30:00Z",
            timezone="America/Chicago",
        )


def test_calendar_adapter_models_denied_invalidated_and_unsupported_statuses() -> None:
    adapter = CalendarSourceAdapter()

    assert adapter.not_allowed_page().status is CalendarPageStatus.NOT_ALLOWED
    assert adapter.token_invalidated_page().status is CalendarPageStatus.TOKEN_INVALIDATED
    assert adapter.unsupported_provider_page().status is CalendarPageStatus.UNSUPPORTED_PROVIDER


def test_calendar_checkpoint_store_is_metadata_only_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    adapter = CalendarSourceAdapter()
    selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id=_CALENDAR_ID,
        provider="google_calendar",
    )
    other_selection = adapter.calendar_selection(
        connection_id="account:calendar-fixture",
        calendar_id="calendar:secondary",
        provider="google_calendar",
    )
    record = adapter.record_from_event(_event(description="Synthetic notes excluded."))
    page = adapter.preview(selection, (record,), privacy=_privacy())
    intake = adapter.intake(selection, record, privacy=_privacy())
    checkpoint = CalendarCheckpoint.initial(selection).advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
        committed_revision_identities=(intake.key.revision_identity(),),
    )
    store = CalendarCheckpointStore(tmp_path / "checkpoints")

    path = store.save(checkpoint)

    assert store.load(selection) == checkpoint
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic notes" not in path.read_text(encoding="utf-8")

    path.write_text(
        json.dumps(CalendarCheckpoint.initial(other_selection).to_dict()),
        encoding="utf-8",
    )
    with pytest.raises(ConnectorContractError, match="invalid calendar checkpoint store"):
        store.load(selection)


def _event(
    *,
    calendar_id: str = _CALENDAR_ID,
    event_id: str = _EVENT_ID,
    revision_id: str = "rev-1",
    provider: str = "google_calendar",
    title: str = "Daily Standup",
    start_time: str = "2026-09-15T10:00:00Z",
    end_time: str = "2026-09-15T10:30:00Z",
    timezone: str = "America/Chicago",
    description: str | None = "Synthetic event description.",
    attendees: tuple[str, ...] = ("Ada Example",),
    meeting_link: str | None = "https://meet.example.invalid/standup",
    cancelled: bool = False,
    source_kind: object = "calendar_event",
    body_secret_scan: object = "clean",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "attendees": list(attendees),
        "body_secret_scan": body_secret_scan,
        "calendar_id": calendar_id,
        "cancelled": cancelled,
        "end_time": end_time,
        "event_id": event_id,
        "provider": provider,
        "revision_id": revision_id,
        "source_kind": source_kind,
        "start_time": start_time,
        "timezone": timezone,
        "title": title,
    }
    if description is not None:
        payload["description"] = description
    if meeting_link is not None:
        payload["meeting_link"] = meeting_link
    return payload
