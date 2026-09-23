"""D5.2 adapter values for selected calendar date ranges."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypedDict
from urllib.parse import quote

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.connectors import (
    ConnectorCaptureSink,
    ConnectorContractError,
    ConnectorFailureCode,
    ConnectorOutcome,
    ConnectorRunReceipt,
    capture_outcome_is_duplicate,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import (
    D5_CALENDAR_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "CalendarCheckpoint",
    "CalendarCheckpointStore",
    "CalendarEventRecord",
    "CalendarPage",
    "CalendarPageStatus",
    "CalendarSourceAdapter",
]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CALENDAR_ID = re.compile(r"calendar:[A-Za-z0-9][A-Za-z0-9._:@/-]{0,180}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_EVENT_ID = re.compile(r"event:[A-Za-z0-9][A-Za-z0-9._:@/-]{0,180}")
_ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})")
_RESOURCE_TYPE = re.compile(r"(google_calendar|outlook_calendar)")
_TIME_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+./-]{0,63}")
_MAX_PREVIEW_RECORDS = 25


class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    observed_page_cursors: tuple[str, ...]
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class CalendarPageStatus(StrEnum):
    """Provider-observed result for one bounded selected calendar page."""

    READY = "ready"
    NOT_ALLOWED = "not_allowed"
    OUT_OF_RANGE = "out_of_range"
    TOKEN_INVALIDATED = "token_invalidated"
    UNSUPPORTED_PROVIDER = "unsupported_provider"


@dataclass(frozen=True, slots=True)
class CalendarEventRecord:
    """A selected Google or Outlook calendar event in a selected date range."""

    calendar_id: str
    event_id: str
    revision_id: str
    provider: str
    title: str
    start_time: str
    end_time: str
    timezone: str
    description: str | None = None
    attendees: tuple[str, ...] = ()
    meeting_link: str | None = None
    cancelled: bool = False

    def __post_init__(self) -> None:
        calendar_id = _required_calendar_id(self.calendar_id)
        event_id = _required_event_id(self.event_id)
        revision_id = _required_token(self.revision_id, "invalid calendar event")
        provider = _required_provider(self.provider)
        title = _body_or_fallback(self.title, "Calendar event")
        start_time = _required_instant(self.start_time)
        end_time = _required_instant(self.end_time)
        if _instant_datetime(end_time) < _instant_datetime(start_time):
            raise ConnectorContractError("invalid calendar event")
        timezone = _required_timezone(self.timezone)
        description = (
            None
            if self.description is None
            else _body_or_fallback(
                self.description,
                title,
            )
        )
        attendees = _attendees(self.attendees)
        meeting_link = _optional_https_url(self.meeting_link)
        cancelled = _required_bool(self.cancelled)
        _require_redaction_clean(title)
        if description is not None:
            _require_redaction_clean(description)
        if meeting_link is not None:
            _require_redaction_clean(meeting_link)
        object.__setattr__(self, "calendar_id", calendar_id)
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "start_time", start_time)
        object.__setattr__(self, "end_time", end_time)
        object.__setattr__(self, "timezone", timezone)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "attendees", attendees)
        object.__setattr__(self, "meeting_link", meeting_link)
        object.__setattr__(self, "cancelled", cancelled)


@dataclass(frozen=True, slots=True)
class CalendarPage:
    status: CalendarPageStatus
    preview: SourcePreviewPage | None = None

    def __post_init__(self) -> None:
        try:
            status = CalendarPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid calendar page") from error
        if status is CalendarPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage:
                raise ConnectorContractError("invalid calendar page")
        elif self.preview is not None:
            raise ConnectorContractError("invalid calendar page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class CalendarCheckpoint:
    """Durable selected-calendar cursor committed after capture acknowledgements."""

    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...] = ()
    observed_page_cursors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.selection) is not SourceResourceSelection
            or self.selection.connector_name != D5_CALENDAR_SOURCE
            or self.selection.resource_type not in {"google_calendar", "outlook_calendar"}
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str or not value.startswith("connector.calendar.")
                for value in self.committed_delivery_ids
            )
            or len(self.committed_delivery_ids) != len(set(self.committed_delivery_ids))
            or not isinstance(self.committed_revision_identities, tuple)
            or any(
                type(value) is not str or _CURSOR.fullmatch(value) is None
                for value in self.committed_revision_identities
            )
            or (
                self.committed_revision_identities
                and len(self.committed_revision_identities) != len(self.committed_delivery_ids)
            )
            or not isinstance(self.observed_page_cursors, tuple)
            or any(
                type(value) is not str or _CURSOR.fullmatch(value) is None
                for value in self.observed_page_cursors
            )
            or len(self.observed_page_cursors) != len(set(self.observed_page_cursors))
        ):
            raise ConnectorContractError("invalid calendar checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> CalendarCheckpoint:
        return cls(1, selection, None, ())

    @classmethod
    def from_dict(cls, value: object) -> CalendarCheckpoint:
        return cls(**_checkpoint_parts(value))

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> CalendarCheckpoint:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError("invalid calendar checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        revisions = tuple(committed_revision_identities)
        if tuple(committed_delivery_ids) != expected or len(revisions) != len(expected):
            raise ConnectorContractError("invalid calendar checkpoint")
        committed_by_delivery = dict(
            zip(
                self.committed_delivery_ids,
                self.committed_revision_identities,
                strict=True,
            )
        )
        for delivery_id, revision_identity in zip(expected, revisions, strict=True):
            committed_by_delivery[delivery_id] = revision_identity
        return CalendarCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=page.next_cursor,
            committed_delivery_ids=tuple(committed_by_delivery),
            committed_revision_identities=tuple(committed_by_delivery.values()),
            observed_page_cursors=_append_cursor(self.observed_page_cursors, page.next_cursor),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "committed_delivery_ids": list(self.committed_delivery_ids),
            "committed_revision_identities": list(self.committed_revision_identities),
            "connector_name": self.selection.connector_name,
            "connection_id": self.selection.connection_id,
            "next_cursor": self.next_cursor,
            "observed_page_cursors": list(self.observed_page_cursors),
            "resource_id": self.selection.resource_id,
            "resource_type": self.selection.resource_type,
            "schema_version": self.schema_version,
        }


class CalendarCheckpointStore:
    """Goal-owned JSON checkpoint persistence for selected calendars."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid calendar checkpoint store")
        self._root = root

    def _path(self, selection: SourceResourceSelection) -> Path:
        _require_selection(selection)
        digest = SourceRecordKey(
            connector_name=D5_CALENDAR_SOURCE,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id=f"calendar:{selection.resource_type}",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"calendar-{digest}.json"

    def load(self, selection: SourceResourceSelection) -> CalendarCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return CalendarCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid calendar checkpoint store") from error
        checkpoint = CalendarCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid calendar checkpoint store")
        return checkpoint

    def save(self, checkpoint: CalendarCheckpoint) -> Path:
        if type(checkpoint) is not CalendarCheckpoint:
            raise ConnectorContractError("invalid calendar checkpoint store")
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(checkpoint.selection)
        payload = json.dumps(checkpoint.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        with NamedTemporaryFile(
            "w",
            delete=False,
            dir=self._root,
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            handle.write(payload)
            temp_name = handle.name
        try:
            os.replace(temp_name, path)
        except OSError as error:
            Path(temp_name).unlink(missing_ok=True)
            raise ConnectorContractError("invalid calendar checkpoint store") from error
        return path


class CalendarSourceAdapter:
    """Convert selected calendar date-range payloads into D5.2 source values."""

    def calendar_selection(
        self,
        *,
        connection_id: str,
        calendar_id: str,
        provider: str,
    ) -> SourceResourceSelection:
        if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
            raise ConnectorContractError("invalid calendar source connection")
        calendar_id = _required_calendar_id(calendar_id)
        provider = _required_provider(provider)
        return SourceResourceSelection(
            connector_name=D5_CALENDAR_SOURCE,
            connection_id=connection_id,
            resource_id=calendar_id,
            resource_type=provider,
        )

    def page_from_events(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        selected_event_ids: Sequence[str],
        range_start: str,
        range_end: str,
        next_cursor: str | None = None,
    ) -> CalendarPage:
        selected_ids = _required_selected_events(selected_event_ids)
        start = _required_instant(range_start)
        end = _required_instant(range_end)
        if _instant_datetime(end) <= _instant_datetime(start):
            raise ConnectorContractError("invalid calendar events")
        records = tuple(
            self.record_from_event(value)
            for value in _require_values(values)
            if _matches_selected_event(selection, value, selected_ids)
            and _overlaps_range(
                _typed_str(value.get("start_time")),
                _typed_str(value.get("end_time")),
                start,
                end,
            )
        )
        if not records:
            raise ConnectorContractError("invalid calendar events")
        return CalendarPage(
            status=CalendarPageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[CalendarEventRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection)
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid calendar events")
        intakes = tuple(self.intake(selection, record, privacy=privacy) for record in records)
        if len(intakes) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid calendar events")
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(intake, content_type="calendar_event")
                for intake in intakes
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: CalendarCheckpoint,
        page: CalendarPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[CalendarCheckpoint, ConnectorRunReceipt]:
        if page.status in {
            CalendarPageStatus.NOT_ALLOWED,
            CalendarPageStatus.OUT_OF_RANGE,
            CalendarPageStatus.TOKEN_INVALIDATED,
            CalendarPageStatus.UNSUPPORTED_PROVIDER,
        }:
            return checkpoint, ConnectorRunReceipt.failed(
                D5_CALENDAR_SOURCE,
                ConnectorFailureCode.NOT_ALLOWED,
            )
        if (
            type(checkpoint) is not CalendarCheckpoint
            or type(page.preview) is not SourcePreviewPage
            or page.preview.selection != checkpoint.selection
            or not isinstance(intakes, Sequence)
            or isinstance(intakes, str)
            or type(capture_sink) is not ConnectorCaptureSink
        ):
            raise ConnectorContractError("invalid calendar import")
        selected = tuple(record for record in page.preview.records if record.selected)
        if not selected:
            return checkpoint, ConnectorRunReceipt.empty(
                D5_CALENDAR_SOURCE,
                metadata_count=len(page.preview.records),
            )
        intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
        if (
            len(intake_by_delivery) != len(intakes)
            or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
            or any(
                intake.key.connector_name != D5_CALENDAR_SOURCE
                or intake.key.connection_id != page.preview.selection.connection_id
                or intake.key.resource_id != page.preview.selection.resource_id
                or intake.source_reference != record.source_reference
                for record, intake in zip(selected, intakes, strict=True)
            )
        ):
            raise ConnectorContractError("invalid calendar import")
        committed_revisions = (
            dict(
                zip(
                    checkpoint.committed_delivery_ids,
                    checkpoint.committed_revision_identities,
                    strict=True,
                )
            )
            if checkpoint.committed_revision_identities
            else {}
        )
        changed_intakes = tuple(
            intake
            for intake in intakes
            if committed_revisions.get(intake.key.delivery_id()) != intake.key.revision_identity()
        )
        selected_delivery_ids = tuple(record.delivery_id for record in selected)
        selected_revision_identities = tuple(intake.key.revision_identity() for intake in intakes)
        if not changed_intakes:
            duplicate_only_known_page = all(
                delivery_id in checkpoint.committed_delivery_ids
                for delivery_id in selected_delivery_ids
            )
            stale_duplicate_page = (
                page.preview.next_cursor is None
                or page.preview.next_cursor in checkpoint.observed_page_cursors
            )
            if selected_delivery_ids != checkpoint.committed_delivery_ids and (
                not duplicate_only_known_page or stale_duplicate_page
            ):
                return checkpoint, ConnectorRunReceipt.empty(
                    D5_CALENDAR_SOURCE,
                    metadata_count=len(page.preview.records),
                )
            advanced = checkpoint.advance(
                SourcePreviewPage(
                    selection=page.preview.selection,
                    records=selected,
                    next_cursor=page.preview.next_cursor,
                ),
                committed_delivery_ids=selected_delivery_ids,
                committed_revision_identities=selected_revision_identities,
            )
            if advanced == checkpoint:
                return checkpoint, ConnectorRunReceipt.empty(
                    D5_CALENDAR_SOURCE,
                    metadata_count=len(page.preview.records),
                )
            return (
                advanced,
                ConnectorRunReceipt(
                    connector_name=D5_CALENDAR_SOURCE,
                    outcome=ConnectorOutcome.COMPLETED,
                    failure_code=None,
                    discovered_count=len(page.preview.records),
                    fetched_count=len(selected),
                    extracted_count=len(selected),
                    submitted_count=0,
                    stubbed_count=0,
                    created_count=0,
                    duplicate_count=0,
                    checkpoint_committed=True,
                    metadata_count=len(page.preview.records),
                ),
            )
        receipts = tuple(
            capture_sink.submit(
                intake.payload(),
                delivery_id=intake.key.delivery_id(),
                source_origin="third_party",
                source_reference=intake.source_reference,
                provenance=intake.provenance(),
                privacy=intake.privacy,
                intent="reference",
                title=intake.title,
            )
            for intake in changed_intakes
        )
        advanced = checkpoint.advance(
            SourcePreviewPage(
                selection=page.preview.selection,
                records=selected,
                next_cursor=_advance_cursor(checkpoint, page.preview.next_cursor),
            ),
            committed_delivery_ids=selected_delivery_ids,
            committed_revision_identities=selected_revision_identities,
        )
        return (
            advanced,
            ConnectorRunReceipt(
                connector_name=D5_CALENDAR_SOURCE,
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=len(page.preview.records),
                fetched_count=len(selected),
                extracted_count=len(selected),
                submitted_count=len(receipts),
                stubbed_count=0,
                created_count=sum(
                    1 for receipt in receipts if not capture_outcome_is_duplicate(receipt)
                ),
                duplicate_count=sum(
                    1 for receipt in receipts if capture_outcome_is_duplicate(receipt)
                ),
                checkpoint_committed=True,
                metadata_count=len(page.preview.records),
            ),
        )

    def not_allowed_page(self) -> CalendarPage:
        return CalendarPage(status=CalendarPageStatus.NOT_ALLOWED)

    def token_invalidated_page(self) -> CalendarPage:
        return CalendarPage(status=CalendarPageStatus.TOKEN_INVALIDATED)

    def unsupported_provider_page(self) -> CalendarPage:
        return CalendarPage(status=CalendarPageStatus.UNSUPPORTED_PROVIDER)

    def intake(
        self,
        selection: SourceResourceSelection,
        record: CalendarEventRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not CalendarEventRecord:
            raise ConnectorContractError("invalid calendar event")
        _require_record_matches_selection(selection, record)
        text = _event_text(record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D5_CALENDAR_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=_provider_event_identity(record),
                revision_id=record.revision_id,
            ),
            url=_source_reference(record),
            title=record.title,
            text=text,
            privacy=privacy,
        )

    def record_from_event(self, value: Mapping[str, object]) -> CalendarEventRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid calendar event")
        if value.get("source_kind") != "calendar_event":
            raise ConnectorContractError("invalid calendar event")
        if value.get("body_secret_scan") != "clean":
            raise ConnectorContractError("invalid calendar event")
        return CalendarEventRecord(
            calendar_id=_required_calendar_id(value.get("calendar_id")),
            event_id=_required_event_id(value.get("event_id")),
            revision_id=_required_token(value.get("revision_id"), "invalid calendar event"),
            provider=_required_provider(value.get("provider")),
            title=_body_or_fallback(value.get("title"), "Calendar event"),
            start_time=_required_instant(value.get("start_time")),
            end_time=_required_instant(value.get("end_time")),
            timezone=_required_timezone(value.get("timezone")),
            description=_optional_body(value.get("description")),
            attendees=_attendees(value.get("attendees")),
            meeting_link=_optional_https_url(value.get("meeting_link")),
            cancelled=_required_bool(value.get("cancelled", False)),
        )


def _checkpoint_parts(value: object) -> _CheckpointParts:
    required_fields = {
        "committed_delivery_ids",
        "committed_revision_identities",
        "connector_name",
        "connection_id",
        "next_cursor",
        "resource_id",
        "resource_type",
        "schema_version",
    }
    optional_fields = {"observed_page_cursors"}
    if (
        not isinstance(value, dict)
        or not required_fields.issubset(value)
        or not set(value).issubset(required_fields | optional_fields)
    ):
        raise ConnectorContractError("invalid calendar checkpoint")
    committed = value["committed_delivery_ids"]
    committed_revisions = value["committed_revision_identities"]
    observed_cursors = value.get("observed_page_cursors", [])
    if not isinstance(committed, list) or not isinstance(committed_revisions, list):
        raise ConnectorContractError("invalid calendar checkpoint")
    if not isinstance(observed_cursors, list):
        raise ConnectorContractError("invalid calendar checkpoint")
    return {
        "schema_version": _typed_int(value["schema_version"]),
        "selection": SourceResourceSelection(
            connector_name=_typed_str(value["connector_name"]),
            connection_id=_typed_str(value["connection_id"]),
            resource_id=_typed_str(value["resource_id"]),
            resource_type=_typed_str(value["resource_type"]),
        ),
        "next_cursor": _optional_str(value["next_cursor"]),
        "observed_page_cursors": tuple(_typed_str(item) for item in observed_cursors),
        "committed_delivery_ids": tuple(_typed_str(item) for item in committed),
        "committed_revision_identities": tuple(_typed_str(item) for item in committed_revisions),
    }


def _append_cursor(cursors: tuple[str, ...], next_cursor: str | None) -> tuple[str, ...]:
    if next_cursor is None or next_cursor in cursors:
        return cursors
    return (*cursors, next_cursor)


def _require_values(values: Sequence[Mapping[str, object]]) -> Sequence[Mapping[str, object]]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid calendar events")
    return values


def _required_selected_events(values: Sequence[str]) -> set[str]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid calendar events")
    selected = tuple(_required_event_id(value) for value in values)
    if not selected:
        raise ConnectorContractError("invalid calendar events")
    return set(selected)


def _matches_selected_event(
    selection: SourceResourceSelection,
    value: Mapping[str, object],
    selected_event_ids: set[str],
) -> bool:
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid calendar event")
    event_id = _required_event_id(value.get("event_id"))
    if event_id not in selected_event_ids:
        return False
    return (
        _required_calendar_id(value.get("calendar_id")) == selection.resource_id
        and _required_provider(value.get("provider")) == selection.resource_type
    )


def _require_selection(selection: SourceResourceSelection) -> None:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != D5_CALENDAR_SOURCE
        or _CALENDAR_ID.fullmatch(selection.resource_id) is None
        or _RESOURCE_TYPE.fullmatch(selection.resource_type) is None
    ):
        raise ConnectorContractError("invalid calendar selection")


def _require_record_matches_selection(
    selection: SourceResourceSelection,
    record: CalendarEventRecord,
) -> None:
    _require_selection(selection)
    if record.calendar_id != selection.resource_id or record.provider != selection.resource_type:
        raise ConnectorContractError("invalid calendar event")


def _event_text(record: CalendarEventRecord) -> str:
    parts = [
        f"Title: {record.title}",
        f"When: {record.start_time} to {record.end_time} ({record.timezone})",
    ]
    if record.cancelled:
        parts.append("Status: cancelled")
    if record.description is not None:
        parts.append(f"Description: {record.description}")
    if record.attendees:
        parts.append(f"Attendees: {', '.join(record.attendees)}")
    if record.meeting_link is not None:
        parts.append(f"Meeting link: {record.meeting_link}")
    return "\n".join(parts)


def _source_reference(record: CalendarEventRecord) -> str:
    provider = "google" if record.provider == "google_calendar" else "outlook"
    token = quote(record.event_id.removeprefix("event:"), safe="")
    return f"https://local.openbrain.invalid/calendars/{provider}/{token}"


def _provider_event_identity(record: CalendarEventRecord) -> str:
    return f"{record.provider}:{record.event_id}"


def _advance_cursor(checkpoint: CalendarCheckpoint, next_cursor: str | None) -> str | None:
    if (
        next_cursor is not None
        and next_cursor in checkpoint.observed_page_cursors
        and next_cursor != checkpoint.next_cursor
    ):
        return checkpoint.next_cursor
    return next_cursor


def _overlaps_range(event_start: str, event_end: str, start: str, end: str) -> bool:
    first = _instant_datetime(_required_instant(event_start))
    last = _instant_datetime(_required_instant(event_end))
    if last < first:
        raise ConnectorContractError("invalid calendar event")
    if last == first:
        return _instant_datetime(start) <= first < _instant_datetime(end)
    return first < _instant_datetime(end) and last > _instant_datetime(start)


def _instant_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ConnectorContractError("invalid calendar event") from error


def _required_calendar_id(value: object) -> str:
    text = _required_token(value, "invalid calendar event")
    if _CALENDAR_ID.fullmatch(text) is None:
        raise ConnectorContractError("invalid calendar event")
    return text


def _required_event_id(value: object) -> str:
    text = _required_token(value, "invalid calendar event")
    if _EVENT_ID.fullmatch(text) is None:
        raise ConnectorContractError("invalid calendar event")
    return text


def _required_provider(value: object) -> str:
    text = _required_token(value, "invalid calendar event")
    if _RESOURCE_TYPE.fullmatch(text) is None:
        raise ConnectorContractError("invalid calendar event")
    return text


def _required_instant(value: object) -> str:
    text = _required_token(value, "invalid calendar event")
    if _ISO_INSTANT.fullmatch(text) is None:
        raise ConnectorContractError("invalid calendar event")
    return text


def _required_timezone(value: object) -> str:
    text = _required_token(value, "invalid calendar event")
    if _TIME_ZONE.fullmatch(text) is None:
        raise ConnectorContractError("invalid calendar event")
    return text


def _attendees(value: object) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ConnectorContractError("invalid calendar event")
    attendees = tuple(_body_or_fallback(item, "attendee") for item in value)
    for attendee in attendees:
        _require_redaction_clean(attendee)
    return attendees


def _optional_https_url(value: object) -> str | None:
    if value is None:
        return None
    text = _body_or_fallback(value, "meeting link")
    if not text.startswith("https://") or "\x00" in text or any(char.isspace() for char in text):
        raise ConnectorContractError("invalid calendar event")
    return text


def _optional_body(value: object) -> str | None:
    if value is None:
        return None
    return _body_or_fallback(value, "Calendar event")


def _typed_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid calendar checkpoint")
    return value


def _typed_str(value: object) -> str:
    if type(value) is not str:
        raise ConnectorContractError("invalid calendar checkpoint")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _typed_str(value)


def _required_token(value: object, message: str) -> str:
    if type(value) is not str or _CURSOR.fullmatch(value) is None:
        raise ConnectorContractError(message)
    return value


def _required_bool(value: object) -> bool:
    if type(value) is not bool:
        raise ConnectorContractError("invalid calendar event")
    return value


def _body_or_fallback(body: object, fallback: str) -> str:
    if body is None:
        return fallback
    if type(body) is not str or "\x00" in body:
        raise ConnectorContractError("invalid calendar event")
    return body.strip() or fallback


def _require_redaction_clean(body: str) -> None:
    try:
        redaction_found = has_redaction_finding(body)
    except ValueError as error:
        raise ConnectorContractError("invalid calendar event") from error
    if redaction_found:
        raise ConnectorContractError("invalid calendar event")
