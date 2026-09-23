"""Slack D4 adapter values for explicitly selected channels."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TypedDict

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
    D4_SLACK_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "SlackChannelCheckpoint",
    "SlackChannelCheckpointStore",
    "SlackChannelPage",
    "SlackChannelPageStatus",
    "SlackMessageRecord",
    "SlackSourceAdapter",
]

SlackContentType = Literal["message", "thread_reply"]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CHANNEL = re.compile(r"[A-Z0-9][A-Z0-9._:-]{0,120}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_HOST_URL = re.compile(r"https://[A-Za-z0-9.-]+(?:/[^\s\x00]*)?")
_TS = re.compile(r"[0-9]{10,}\.[0-9]{1,6}")
_MAX_PREVIEW_RECORDS = 25


class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class SlackChannelPageStatus(StrEnum):
    """Host-observed result for one bounded Slack channel page."""

    READY = "ready"
    RATE_LIMITED = "rate_limited"
    NEEDS_SIGN_IN = "needs_sign_in"
    NOT_ALLOWED = "not_allowed"
    RETENTION_EXPIRED = "retention_expired"


@dataclass(frozen=True, slots=True)
class SlackMessageRecord:
    """A bounded Slack message or thread reply from one selected channel."""

    channel_id: str
    message_ts: str
    revision_id: str
    permalink: str
    author_id: str
    text: str
    thread_ts: str | None = None

    @property
    def content_type(self) -> SlackContentType:
        if self.thread_ts is not None and self.thread_ts != self.message_ts:
            return "thread_reply"
        return "message"


@dataclass(frozen=True, slots=True)
class SlackChannelPage:
    status: SlackChannelPageStatus
    preview: SourcePreviewPage | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        try:
            status = SlackChannelPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid slack page") from error
        if status is SlackChannelPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage or self.retry_after_seconds is not None:
                raise ConnectorContractError("invalid slack page")
        elif self.preview is not None or (
            self.retry_after_seconds is not None
            and (
                type(self.retry_after_seconds) is not int
                or not 1 <= self.retry_after_seconds <= 86_400
            )
        ):
            raise ConnectorContractError("invalid slack page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class SlackChannelCheckpoint:
    """Durable selected-channel cursor committed after capture acknowledgements."""

    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.selection) is not SourceResourceSelection
            or self.selection.connector_name != D4_SLACK_SOURCE
            or self.selection.resource_type != "channel"
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str or not value.startswith("connector.slack.")
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
        ):
            raise ConnectorContractError("invalid slack checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> SlackChannelCheckpoint:
        return cls(1, selection, None, ())

    @classmethod
    def from_dict(cls, value: object) -> SlackChannelCheckpoint:
        return cls(**_checkpoint_parts(value))

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> SlackChannelCheckpoint:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError("invalid slack checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        revisions = tuple(committed_revision_identities)
        if tuple(committed_delivery_ids) != expected or len(revisions) != len(expected):
            raise ConnectorContractError("invalid slack checkpoint")
        existing_revisions = (
            dict(zip(self.committed_delivery_ids, self.committed_revision_identities, strict=True))
            if self.committed_revision_identities
            else {}
        )
        expected_set = set(expected)
        retained_delivery_ids = tuple(
            delivery_id
            for delivery_id in self.committed_delivery_ids
            if delivery_id not in expected_set
        )
        retained_revisions = tuple(
            existing_revisions[delivery_id]
            for delivery_id in retained_delivery_ids
            if delivery_id in existing_revisions
        )
        return SlackChannelCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=page.next_cursor,
            committed_delivery_ids=retained_delivery_ids + expected,
            committed_revision_identities=retained_revisions + revisions,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "committed_delivery_ids": list(self.committed_delivery_ids),
            "committed_revision_identities": list(self.committed_revision_identities),
            "connector_name": self.selection.connector_name,
            "connection_id": self.selection.connection_id,
            "next_cursor": self.next_cursor,
            "resource_id": self.selection.resource_id,
            "resource_type": self.selection.resource_type,
            "schema_version": self.schema_version,
        }


class SlackChannelCheckpointStore:
    """Goal-owned JSON checkpoint persistence for selected Slack channels."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid slack checkpoint store")
        self._root = root

    def _path(self, selection: SourceResourceSelection) -> Path:
        _require_selection(selection)
        digest = SourceRecordKey(
            connector_name=D4_SLACK_SOURCE,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id="channel",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"slack-channel-{digest}.json"

    def load(self, selection: SourceResourceSelection) -> SlackChannelCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return SlackChannelCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid slack checkpoint store") from error
        return SlackChannelCheckpoint.from_dict(decoded)

    def save(self, checkpoint: SlackChannelCheckpoint) -> Path:
        if type(checkpoint) is not SlackChannelCheckpoint:
            raise ConnectorContractError("invalid slack checkpoint store")
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
            raise ConnectorContractError("invalid slack checkpoint store") from error
        return path


class SlackSourceAdapter:
    """Convert host-mediated Slack Web API responses into D4 source values."""

    def channel_selection(
        self,
        *,
        connection_id: str,
        channel_id: str,
    ) -> SourceResourceSelection:
        if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
            raise ConnectorContractError("invalid slack source connection")
        if type(channel_id) is not str or _CHANNEL.fullmatch(channel_id) is None:
            raise ConnectorContractError("invalid slack channel selection")
        return SourceResourceSelection(
            connector_name=D4_SLACK_SOURCE,
            connection_id=connection_id,
            resource_id=f"channel:{channel_id}",
            resource_type="channel",
        )

    def page_from_rest(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SlackChannelPage:
        records = tuple(self.record_from_rest(value) for value in _require_values(values))
        return SlackChannelPage(
            status=SlackChannelPageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[SlackMessageRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection)
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid slack records")
        if len(records) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid slack records")
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(
                    self.intake(selection, record, privacy=privacy),
                    content_type=record.content_type,
                )
                for record in records
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: SlackChannelCheckpoint,
        page: SlackChannelPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[SlackChannelCheckpoint, ConnectorRunReceipt]:
        if page.status is SlackChannelPageStatus.RATE_LIMITED:
            return checkpoint, ConnectorRunReceipt.failed(
                D4_SLACK_SOURCE,
                ConnectorFailureCode.RUNTIME_FAILED,
            )
        if page.status in {
            SlackChannelPageStatus.NEEDS_SIGN_IN,
            SlackChannelPageStatus.NOT_ALLOWED,
            SlackChannelPageStatus.RETENTION_EXPIRED,
        }:
            return checkpoint, ConnectorRunReceipt.failed(
                D4_SLACK_SOURCE,
                ConnectorFailureCode.NOT_ALLOWED,
            )
        if (
            type(checkpoint) is not SlackChannelCheckpoint
            or type(page.preview) is not SourcePreviewPage
            or page.preview.selection != checkpoint.selection
            or not isinstance(intakes, Sequence)
            or isinstance(intakes, str)
            or type(capture_sink) is not ConnectorCaptureSink
        ):
            raise ConnectorContractError("invalid slack import")
        selected = tuple(record for record in page.preview.records if record.selected)
        if not selected:
            return checkpoint, ConnectorRunReceipt.empty(
                D4_SLACK_SOURCE,
                metadata_count=len(page.preview.records),
            )
        intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
        if (
            len(intake_by_delivery) != len(intakes)
            or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
            or any(
                intake.key.connector_name != D4_SLACK_SOURCE
                or intake.key.connection_id != page.preview.selection.connection_id
                or intake.key.resource_id != page.preview.selection.resource_id
                or intake.source_reference != record.source_reference
                for record, intake in zip(selected, intakes, strict=True)
            )
        ):
            raise ConnectorContractError("invalid slack import")
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
        if not changed_intakes:
            return checkpoint, ConnectorRunReceipt.empty(
                D4_SLACK_SOURCE,
                metadata_count=len(page.preview.records),
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
                next_cursor=page.preview.next_cursor,
            ),
            committed_delivery_ids=tuple(record.delivery_id for record in selected),
            committed_revision_identities=tuple(
                intake.key.revision_identity() for intake in intakes
            ),
        )
        return (
            advanced,
            ConnectorRunReceipt(
                connector_name=D4_SLACK_SOURCE,
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

    def rate_limited_page(self, *, retry_after_seconds: int) -> SlackChannelPage:
        return SlackChannelPage(
            status=SlackChannelPageStatus.RATE_LIMITED,
            retry_after_seconds=retry_after_seconds,
        )

    def needs_sign_in_page(self) -> SlackChannelPage:
        return SlackChannelPage(status=SlackChannelPageStatus.NEEDS_SIGN_IN)

    def not_allowed_page(self) -> SlackChannelPage:
        return SlackChannelPage(status=SlackChannelPageStatus.NOT_ALLOWED)

    def retention_expired_page(self) -> SlackChannelPage:
        return SlackChannelPage(status=SlackChannelPageStatus.RETENTION_EXPIRED)

    def intake(
        self,
        selection: SourceResourceSelection,
        record: SlackMessageRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not SlackMessageRecord:
            raise ConnectorContractError("invalid slack record")
        _require_record_matches_selection(selection, record)
        external_id = (
            f"reply:{record.thread_ts}:{record.message_ts}"
            if record.content_type == "thread_reply"
            else f"message:{record.message_ts}"
        )
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D4_SLACK_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=external_id,
                revision_id=f"edited:{record.revision_id}",
            ),
            url=record.permalink,
            title=f"Slack {record.channel_id} {record.message_ts}",
            text=record.text,
            privacy=privacy,
        )

    def record_from_rest(self, value: Mapping[str, object]) -> SlackMessageRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid slack record")
        channel_id = _required_str(value.get("channel_id"), "invalid slack record")
        message_ts = _required_ts(value.get("ts"), "invalid slack record")
        thread_value = value.get("thread_ts")
        thread_ts = (
            None if thread_value is None else _required_ts(thread_value, "invalid slack record")
        )
        revision_value = value.get("edited_ts", value.get("updated_at", message_ts))
        return SlackMessageRecord(
            channel_id=channel_id,
            message_ts=message_ts,
            revision_id=_required_str(revision_value, "invalid slack record"),
            permalink=_required_url(value.get("permalink"), "invalid slack record"),
            author_id=_required_str(value.get("user"), "invalid slack record"),
            text=_body_or_fallback(
                value.get("text"),
                f"Slack message {message_ts}",
                "invalid slack record",
            ),
            thread_ts=thread_ts,
        )


def _checkpoint_parts(value: object) -> _CheckpointParts:
    fields = {
        "committed_delivery_ids",
        "committed_revision_identities",
        "connector_name",
        "connection_id",
        "next_cursor",
        "resource_id",
        "resource_type",
        "schema_version",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ConnectorContractError("invalid slack checkpoint")
    committed = value["committed_delivery_ids"]
    committed_revisions = value["committed_revision_identities"]
    if not isinstance(committed, list) or not isinstance(committed_revisions, list):
        raise ConnectorContractError("invalid slack checkpoint")
    return {
        "schema_version": _typed_int(value["schema_version"]),
        "selection": SourceResourceSelection(
            connector_name=_typed_str(value["connector_name"]),
            connection_id=_typed_str(value["connection_id"]),
            resource_id=_typed_str(value["resource_id"]),
            resource_type=_typed_str(value["resource_type"]),
        ),
        "next_cursor": _optional_str(value["next_cursor"]),
        "committed_delivery_ids": tuple(_typed_str(item) for item in committed),
        "committed_revision_identities": tuple(_typed_str(item) for item in committed_revisions),
    }


def _require_values(values: Sequence[Mapping[str, object]]) -> Sequence[Mapping[str, object]]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid slack records")
    return values


def _require_selection(selection: SourceResourceSelection) -> None:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != D4_SLACK_SOURCE
        or selection.resource_type != "channel"
        or not selection.resource_id.startswith("channel:")
    ):
        raise ConnectorContractError("invalid slack channel selection")


def _require_record_matches_selection(
    selection: SourceResourceSelection,
    record: SlackMessageRecord,
) -> None:
    _require_selection(selection)
    if record.channel_id != selection.resource_id.removeprefix("channel:"):
        raise ConnectorContractError("invalid slack record")
    if "slack.com" not in record.permalink:
        raise ConnectorContractError("invalid slack record")


def _required_str(value: object, message: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ConnectorContractError(message)
    return value.strip()


def _required_ts(value: object, message: str) -> str:
    text = _required_str(value, message)
    if _TS.fullmatch(text) is None:
        raise ConnectorContractError(message)
    return text


def _required_url(value: object, message: str) -> str:
    text = _required_str(value, message)
    if _HOST_URL.fullmatch(text) is None:
        raise ConnectorContractError(message)
    return text


def _typed_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid slack checkpoint")
    return value


def _typed_str(value: object) -> str:
    if type(value) is not str:
        raise ConnectorContractError("invalid slack checkpoint")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _typed_str(value)


def _body_or_fallback(body: object, fallback: str, message: str) -> str:
    if body is None:
        return fallback
    if type(body) is not str or "\x00" in body:
        raise ConnectorContractError(message)
    return body.strip() or fallback
