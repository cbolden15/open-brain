"""D5.5 adapter values for selected Zoom and Google Meet transcripts."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
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
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import (
    D5_MEETING_TRANSCRIPT_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "MeetingTranscriptCheckpoint",
    "MeetingTranscriptCheckpointStore",
    "MeetingTranscriptPage",
    "MeetingTranscriptPageStatus",
    "MeetingTranscriptRecord",
    "MeetingTranscriptSourceAdapter",
]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_MEETING_ID = re.compile(r"meeting:[A-Za-z0-9][A-Za-z0-9._:@/-]{0,180}")
_PROVIDER = re.compile(r"(zoom|google_meet)")
_TRANSCRIPT_ID = re.compile(r"transcript:[A-Za-z0-9][A-Za-z0-9._:@/-]{0,180}")
_ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})")
_MAX_PREVIEW_RECORDS = 25


class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    observed_page_cursors: tuple[str, ...]
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class MeetingTranscriptPageStatus(StrEnum):
    """Provider-observed result for one bounded selected meeting transcript page."""

    READY = "ready"
    NOT_ALLOWED = "not_allowed"
    TRANSCRIPT_UNAVAILABLE = "transcript_unavailable"
    TOKEN_INVALIDATED = "token_invalidated"
    UNSUPPORTED_PROVIDER = "unsupported_provider"


@dataclass(frozen=True, slots=True)
class MeetingTranscriptRecord:
    """An existing transcript from a selected Zoom or Google Meet meeting."""

    meeting_id: str
    transcript_id: str
    revision_id: str
    provider: str
    title: str
    transcript: str
    started_at: str
    speaker_segments: tuple[str, ...] = ()
    source_link: str | None = None

    def __post_init__(self) -> None:
        meeting_id = _required_meeting_id(self.meeting_id)
        transcript_id = _required_transcript_id(self.transcript_id)
        revision_id = _required_token(self.revision_id, "invalid meeting transcript")
        provider = _required_provider(self.provider)
        title = _body_or_fallback(self.title, "Meeting transcript")
        transcript = _required_body(self.transcript, "invalid meeting transcript")
        started_at = _required_instant(self.started_at)
        speaker_segments = _speaker_segments(self.speaker_segments)
        source_link = _optional_https_url(self.source_link)
        _require_redaction_clean(title)
        _require_redaction_clean(transcript)
        for segment in speaker_segments:
            _require_redaction_clean(segment)
        if source_link is not None:
            _require_redaction_clean(source_link)
        object.__setattr__(self, "meeting_id", meeting_id)
        object.__setattr__(self, "transcript_id", transcript_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "transcript", transcript)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "speaker_segments", speaker_segments)
        object.__setattr__(self, "source_link", source_link)


@dataclass(frozen=True, slots=True)
class MeetingTranscriptPage:
    status: MeetingTranscriptPageStatus
    preview: SourcePreviewPage | None = None

    def __post_init__(self) -> None:
        try:
            status = MeetingTranscriptPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid meeting transcript page") from error
        if status is MeetingTranscriptPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage:
                raise ConnectorContractError("invalid meeting transcript page")
        elif self.preview is not None:
            raise ConnectorContractError("invalid meeting transcript page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class MeetingTranscriptCheckpoint:
    """Durable selected-meeting cursor committed after capture acknowledgements."""

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
            or self.selection.connector_name != D5_MEETING_TRANSCRIPT_SOURCE
            or self.selection.resource_type not in {"zoom", "google_meet"}
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str or not value.startswith("connector.meeting_transcript.")
                for value in self.committed_delivery_ids
            )
            or len(self.committed_delivery_ids) != len(set(self.committed_delivery_ids))
            or not isinstance(self.committed_revision_identities, tuple)
            or any(
                type(value) is not str or _CURSOR.fullmatch(value) is None
                for value in self.committed_revision_identities
            )
            or not isinstance(self.observed_page_cursors, tuple)
            or any(
                type(value) is not str or _CURSOR.fullmatch(value) is None
                for value in self.observed_page_cursors
            )
            or len(self.observed_page_cursors) != len(set(self.observed_page_cursors))
            or (
                self.committed_revision_identities
                and len(self.committed_revision_identities) != len(self.committed_delivery_ids)
            )
        ):
            raise ConnectorContractError("invalid meeting transcript checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> MeetingTranscriptCheckpoint:
        return cls(1, selection, None, ())

    @classmethod
    def from_dict(cls, value: object) -> MeetingTranscriptCheckpoint:
        return cls(**_checkpoint_parts(value))

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> MeetingTranscriptCheckpoint:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError("invalid meeting transcript checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        revisions = tuple(committed_revision_identities)
        if tuple(committed_delivery_ids) != expected or len(revisions) != len(expected):
            raise ConnectorContractError("invalid meeting transcript checkpoint")
        committed_by_delivery = dict(
            zip(
                self.committed_delivery_ids,
                self.committed_revision_identities,
                strict=True,
            )
        )
        for delivery_id, revision_identity in zip(expected, revisions, strict=True):
            committed_by_delivery[delivery_id] = revision_identity
        return MeetingTranscriptCheckpoint(
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


class MeetingTranscriptCheckpointStore:
    """Goal-owned JSON checkpoint persistence for selected meeting transcripts."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid meeting transcript checkpoint store")
        self._root = root

    def _path(self, selection: SourceResourceSelection) -> Path:
        _require_selection(selection)
        digest = SourceRecordKey(
            connector_name=D5_MEETING_TRANSCRIPT_SOURCE,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id=f"meeting:{selection.resource_type}",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"meeting-transcript-{digest}.json"

    def load(self, selection: SourceResourceSelection) -> MeetingTranscriptCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return MeetingTranscriptCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid meeting transcript checkpoint store") from error
        checkpoint = MeetingTranscriptCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid meeting transcript checkpoint store")
        return checkpoint

    def save(self, checkpoint: MeetingTranscriptCheckpoint) -> Path:
        if type(checkpoint) is not MeetingTranscriptCheckpoint:
            raise ConnectorContractError("invalid meeting transcript checkpoint store")
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
            raise ConnectorContractError("invalid meeting transcript checkpoint store") from error
        return path


class MeetingTranscriptSourceAdapter:
    """Convert existing selected meeting transcripts into D5.5 source values."""

    def meeting_selection(
        self,
        *,
        connection_id: str,
        meeting_id: str,
        provider: str,
    ) -> SourceResourceSelection:
        if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
            raise ConnectorContractError("invalid meeting transcript source connection")
        meeting_id = _required_meeting_id(meeting_id)
        provider = _required_provider(provider)
        return SourceResourceSelection(
            connector_name=D5_MEETING_TRANSCRIPT_SOURCE,
            connection_id=connection_id,
            resource_id=meeting_id,
            resource_type=provider,
        )

    def page_from_transcripts(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        selected_meeting_ids: Sequence[str],
        next_cursor: str | None = None,
    ) -> MeetingTranscriptPage:
        selected_ids = _required_selected_meetings(selection, selected_meeting_ids)
        records = tuple(
            self.record_from_transcript(value)
            for value in _require_values(values)
            if _matches_selected_meeting(selection, value, selected_ids)
        )
        if not records:
            raise ConnectorContractError("invalid meeting transcripts")
        return MeetingTranscriptPage(
            status=MeetingTranscriptPageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[MeetingTranscriptRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection)
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid meeting transcripts")
        intakes = _coalesce_artifact_intakes(
            tuple(self.intake(selection, record, privacy=privacy) for record in records)
        )
        if len(intakes) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid meeting transcripts")
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(intake, content_type="meeting_transcript")
                for intake in intakes
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: MeetingTranscriptCheckpoint,
        page: MeetingTranscriptPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[MeetingTranscriptCheckpoint, ConnectorRunReceipt]:
        if page.status is MeetingTranscriptPageStatus.UNSUPPORTED_PROVIDER:
            return checkpoint, ConnectorRunReceipt.failed(
                D5_MEETING_TRANSCRIPT_SOURCE,
                ConnectorFailureCode.UNSUPPORTED_CAPABILITY,
            )
        if page.status in {
            MeetingTranscriptPageStatus.NOT_ALLOWED,
            MeetingTranscriptPageStatus.TOKEN_INVALIDATED,
            MeetingTranscriptPageStatus.TRANSCRIPT_UNAVAILABLE,
        }:
            return checkpoint, ConnectorRunReceipt.failed(
                D5_MEETING_TRANSCRIPT_SOURCE,
                ConnectorFailureCode.NOT_ALLOWED,
            )
        if (
            type(checkpoint) is not MeetingTranscriptCheckpoint
            or type(page.preview) is not SourcePreviewPage
            or page.preview.selection != checkpoint.selection
            or not isinstance(intakes, Sequence)
            or isinstance(intakes, str)
            or type(capture_sink) is not ConnectorCaptureSink
        ):
            raise ConnectorContractError("invalid meeting transcript import")
        selected = tuple(record for record in page.preview.records if record.selected)
        if not selected:
            return checkpoint, ConnectorRunReceipt.empty(
                D5_MEETING_TRANSCRIPT_SOURCE,
                metadata_count=len(page.preview.records),
            )
        intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
        if (
            len(intake_by_delivery) != len(intakes)
            or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
            or any(
                intake.key.connector_name != D5_MEETING_TRANSCRIPT_SOURCE
                or intake.key.connection_id != page.preview.selection.connection_id
                or intake.key.resource_id != page.preview.selection.resource_id
                or intake.source_reference != record.source_reference
                for record, intake in zip(selected, intakes, strict=True)
            )
        ):
            raise ConnectorContractError("invalid meeting transcript import")
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
        selected_delivery_ids = tuple(record.delivery_id for record in selected)
        selected_revision_identities = tuple(intake.key.revision_identity() for intake in intakes)
        changed_intakes = tuple(
            intake
            for intake in intakes
            if committed_revisions.get(intake.key.delivery_id()) != intake.key.revision_identity()
        )
        if not changed_intakes:
            duplicate_only_known_page = all(
                delivery_id in checkpoint.committed_delivery_ids
                for delivery_id in selected_delivery_ids
            )
            stale_duplicate_page = (
                page.preview.next_cursor is None
                or page.preview.next_cursor in checkpoint.observed_page_cursors
            )
            if (
                selected_delivery_ids != checkpoint.committed_delivery_ids
                and (not duplicate_only_known_page or stale_duplicate_page)
            ):
                return checkpoint, ConnectorRunReceipt.empty(
                    D5_MEETING_TRANSCRIPT_SOURCE,
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
                    D5_MEETING_TRANSCRIPT_SOURCE,
                    metadata_count=len(page.preview.records),
                )
            return (
                advanced,
                ConnectorRunReceipt(
                    connector_name=D5_MEETING_TRANSCRIPT_SOURCE,
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
                connector_name=D5_MEETING_TRANSCRIPT_SOURCE,
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=len(page.preview.records),
                fetched_count=len(selected),
                extracted_count=len(selected),
                submitted_count=len(receipts),
                stubbed_count=0,
                created_count=sum(1 for receipt in receipts if not receipt.duplicate),
                duplicate_count=sum(1 for receipt in receipts if receipt.duplicate),
                checkpoint_committed=True,
                metadata_count=len(page.preview.records),
            ),
        )

    def not_allowed_page(self) -> MeetingTranscriptPage:
        return MeetingTranscriptPage(status=MeetingTranscriptPageStatus.NOT_ALLOWED)

    def transcript_unavailable_page(self) -> MeetingTranscriptPage:
        return MeetingTranscriptPage(status=MeetingTranscriptPageStatus.TRANSCRIPT_UNAVAILABLE)

    def token_invalidated_page(self) -> MeetingTranscriptPage:
        return MeetingTranscriptPage(status=MeetingTranscriptPageStatus.TOKEN_INVALIDATED)

    def unsupported_provider_page(self) -> MeetingTranscriptPage:
        return MeetingTranscriptPage(status=MeetingTranscriptPageStatus.UNSUPPORTED_PROVIDER)

    def intake(
        self,
        selection: SourceResourceSelection,
        record: MeetingTranscriptRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not MeetingTranscriptRecord:
            raise ConnectorContractError("invalid meeting transcript")
        _require_record_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D5_MEETING_TRANSCRIPT_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=_provider_artifact_identity(record),
                revision_id=_revision_identity(record),
            ),
            url=_source_reference(record),
            title=record.title,
            text=_transcript_text(record),
            privacy=privacy,
        )

    def record_from_transcript(self, value: Mapping[str, object]) -> MeetingTranscriptRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid meeting transcript")
        if value.get("source_kind") != "meeting_transcript":
            raise ConnectorContractError("invalid meeting transcript")
        if value.get("transcript_secret_scan") != "clean":
            raise ConnectorContractError("invalid meeting transcript")
        return MeetingTranscriptRecord(
            meeting_id=_required_meeting_id(value.get("meeting_id")),
            transcript_id=_required_transcript_id(value.get("transcript_id")),
            revision_id=_required_token(value.get("revision_id"), "invalid meeting transcript"),
            provider=_required_provider(value.get("provider")),
            title=_body_or_fallback(value.get("title"), "Meeting transcript"),
            transcript=_required_body(value.get("transcript"), "invalid meeting transcript"),
            started_at=_required_instant(value.get("started_at")),
            speaker_segments=_speaker_segments(value.get("speaker_segments")),
            source_link=_optional_https_url(value.get("source_link")),
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
        raise ConnectorContractError("invalid meeting transcript checkpoint")
    committed = value["committed_delivery_ids"]
    committed_revisions = value["committed_revision_identities"]
    observed_cursors = value.get("observed_page_cursors", [])
    if not isinstance(committed, list) or not isinstance(committed_revisions, list):
        raise ConnectorContractError("invalid meeting transcript checkpoint")
    if not isinstance(observed_cursors, list):
        raise ConnectorContractError("invalid meeting transcript checkpoint")
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
        raise ConnectorContractError("invalid meeting transcripts")
    return values


def _coalesce_artifact_intakes(
    intakes: Sequence[SourceRecordIntake],
) -> tuple[SourceRecordIntake, ...]:
    by_delivery: dict[str, SourceRecordIntake] = {}
    order: list[str] = []
    for intake in intakes:
        delivery_id = intake.key.delivery_id()
        if delivery_id not in by_delivery:
            order.append(delivery_id)
        by_delivery[delivery_id] = intake
    return tuple(by_delivery[delivery_id] for delivery_id in order)


def _required_selected_meetings(
    selection: SourceResourceSelection,
    values: Sequence[str],
) -> set[str]:
    _require_selection(selection)
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid meeting transcripts")
    selected = tuple(_required_meeting_id(value) for value in values)
    if not selected or set(selected) != {selection.resource_id}:
        raise ConnectorContractError("invalid meeting transcripts")
    return set(selected)


def _matches_selected_meeting(
    selection: SourceResourceSelection,
    value: Mapping[str, object],
    selected_meeting_ids: set[str],
) -> bool:
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid meeting transcript")
    meeting_id = _required_meeting_id(value.get("meeting_id"))
    if meeting_id not in selected_meeting_ids:
        return False
    return (
        meeting_id == selection.resource_id
        and _required_provider(value.get("provider")) == selection.resource_type
    )


def _require_selection(selection: SourceResourceSelection) -> None:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != D5_MEETING_TRANSCRIPT_SOURCE
        or _MEETING_ID.fullmatch(selection.resource_id) is None
        or _PROVIDER.fullmatch(selection.resource_type) is None
    ):
        raise ConnectorContractError("invalid meeting transcript selection")


def _require_record_matches_selection(
    selection: SourceResourceSelection,
    record: MeetingTranscriptRecord,
) -> None:
    _require_selection(selection)
    if record.meeting_id != selection.resource_id or record.provider != selection.resource_type:
        raise ConnectorContractError("invalid meeting transcript")


def _transcript_text(record: MeetingTranscriptRecord) -> str:
    parts = [
        f"Title: {record.title}",
        f"Started: {record.started_at}",
        f"Provider: {record.provider}",
        "Transcript:",
        record.transcript,
    ]
    if record.speaker_segments:
        parts.extend(("Speaker segments:", *record.speaker_segments))
    return "\n".join(parts)


def _source_reference(record: MeetingTranscriptRecord) -> str:
    if record.source_link is not None:
        artifact = quote(_artifact_identity(record), safe="")
        return f"https://local.openbrain.invalid/meeting-transcripts/{record.provider}/artifacts/{artifact}"
    token = quote(record.meeting_id.removeprefix("meeting:"), safe="")
    transcript = quote(record.transcript_id.removeprefix("transcript:"), safe="")
    return f"https://local.openbrain.invalid/meeting-transcripts/{record.provider}/{token}/{transcript}"


def _artifact_identity(record: MeetingTranscriptRecord) -> str:
    if record.source_link is None:
        return record.transcript_id
    digest = sha256(record.source_link.encode("utf-8")).hexdigest()
    return f"transcript_artifact:{digest}"


def _provider_artifact_identity(record: MeetingTranscriptRecord) -> str:
    return f"{record.provider}:{_artifact_identity(record)}"


def _revision_identity(record: MeetingTranscriptRecord) -> str:
    if record.source_link is None:
        return record.revision_id
    digest = sha256(_transcript_text(record).encode("utf-8")).hexdigest()
    return f"transcript_revision:{digest}"


def _advance_cursor(checkpoint: MeetingTranscriptCheckpoint, next_cursor: str | None) -> str | None:
    if (
        next_cursor is not None
        and next_cursor in checkpoint.observed_page_cursors
        and next_cursor != checkpoint.next_cursor
    ):
        return checkpoint.next_cursor
    return next_cursor


def _required_meeting_id(value: object) -> str:
    text = _required_token(value, "invalid meeting transcript")
    if _MEETING_ID.fullmatch(text) is None:
        raise ConnectorContractError("invalid meeting transcript")
    return text


def _required_transcript_id(value: object) -> str:
    text = _required_token(value, "invalid meeting transcript")
    if _TRANSCRIPT_ID.fullmatch(text) is None:
        raise ConnectorContractError("invalid meeting transcript")
    return text


def _required_provider(value: object) -> str:
    text = _required_token(value, "invalid meeting transcript")
    if _PROVIDER.fullmatch(text) is None:
        raise ConnectorContractError("invalid meeting transcript")
    return text


def _required_instant(value: object) -> str:
    text = _required_token(value, "invalid meeting transcript")
    if _ISO_INSTANT.fullmatch(text) is None:
        raise ConnectorContractError("invalid meeting transcript")
    return text


def _speaker_segments(value: object) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ConnectorContractError("invalid meeting transcript")
    return tuple(_body_or_fallback(item, "speaker segment") for item in value)


def _optional_https_url(value: object) -> str | None:
    if value is None:
        return None
    text = _body_or_fallback(value, "meeting transcript link")
    if not text.startswith("https://") or "\x00" in text or any(char.isspace() for char in text):
        raise ConnectorContractError("invalid meeting transcript")
    return text


def _typed_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid meeting transcript checkpoint")
    return value


def _typed_str(value: object) -> str:
    if type(value) is not str:
        raise ConnectorContractError("invalid meeting transcript checkpoint")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _typed_str(value)


def _required_token(value: object, message: str) -> str:
    if type(value) is not str or _CURSOR.fullmatch(value) is None:
        raise ConnectorContractError(message)
    return value


def _body_or_fallback(body: object, fallback: str) -> str:
    if body is None:
        return fallback
    if type(body) is not str or "\x00" in body:
        raise ConnectorContractError("invalid meeting transcript")
    return body.strip() or fallback


def _required_body(body: object, message: str) -> str:
    if type(body) is not str or "\x00" in body:
        raise ConnectorContractError(message)
    text = body.strip()
    if not text:
        raise ConnectorContractError(message)
    return text


def _require_redaction_clean(body: str) -> None:
    try:
        redaction_found = has_redaction_finding(body)
    except ValueError as error:
        raise ConnectorContractError("invalid meeting transcript") from error
    if redaction_found:
        raise ConnectorContractError("invalid meeting transcript")
