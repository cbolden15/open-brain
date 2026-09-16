"""D4 adapter values for explicitly selected local agent sessions."""

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
    D4_AGENT_SESSION_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "AgentSessionCheckpoint",
    "AgentSessionCheckpointStore",
    "AgentSessionPage",
    "AgentSessionPageStatus",
    "AgentSessionRecord",
    "AgentSessionSourceAdapter",
]

AgentSessionContentType = Literal["session_summary", "session_transcript"]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_PROJECT_ID = re.compile(r"project:[0-9a-f]{16,64}")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_MAX_PREVIEW_RECORDS = 25


class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class AgentSessionPageStatus(StrEnum):
    """Host-observed result for one bounded local project session page."""

    READY = "ready"
    OPTED_OUT = "opted_out"
    NOT_ALLOWED = "not_allowed"
    LOOP_PREVENTED = "loop_prevented"
    UNSUPPORTED_FORMAT = "unsupported_format"


@dataclass(frozen=True, slots=True)
class AgentSessionRecord:
    """A selected local Claude Code or Codex session event."""

    project_id: str
    session_id: str
    revision_id: str
    client_name: str
    summary: str
    transcript: str | None = None
    transcript_selected: bool = False
    source_reference: str | None = None

    def __post_init__(self) -> None:
        project_id = _required_project_id(self.project_id)
        session_id = _required_session_id(self.session_id)
        revision_id = _required_session_id(self.revision_id)
        client_name = _required_client(self.client_name)
        summary = _body_or_fallback(
            self.summary,
            f"{client_name} session {session_id}",
            "invalid agent session record",
        )
        _require_redaction_clean(summary, "invalid agent session record")
        transcript = (
            None
            if self.transcript is None
            else _body_or_fallback(
                self.transcript,
                f"{client_name} transcript {session_id}",
                "invalid agent session record",
            )
        )
        if transcript is not None:
            _require_redaction_clean(transcript, "invalid agent session record")
        if type(self.transcript_selected) is not bool:
            raise ConnectorContractError("invalid agent session record")
        if self.source_reference is not None:
            raise ConnectorContractError("invalid agent session record")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "client_name", client_name)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "transcript", transcript)
        object.__setattr__(self, "source_reference", None)


@dataclass(frozen=True, slots=True)
class AgentSessionPage:
    status: AgentSessionPageStatus
    preview: SourcePreviewPage | None = None

    def __post_init__(self) -> None:
        try:
            status = AgentSessionPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid agent session page") from error
        if status is AgentSessionPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage:
                raise ConnectorContractError("invalid agent session page")
        elif self.preview is not None:
            raise ConnectorContractError("invalid agent session page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class AgentSessionCheckpoint:
    """Durable selected-project cursor committed after capture acknowledgements."""

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
            or self.selection.connector_name != D4_AGENT_SESSION_SOURCE
            or self.selection.resource_type != "local_project"
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str
                    or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str or not value.startswith("connector.agent_session.")
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
            raise ConnectorContractError("invalid agent session checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> AgentSessionCheckpoint:
        return cls(1, selection, None, ())

    @classmethod
    def from_dict(cls, value: object) -> AgentSessionCheckpoint:
        return cls(**_checkpoint_parts(value))

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> AgentSessionCheckpoint:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError("invalid agent session checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        revisions = tuple(committed_revision_identities)
        if tuple(committed_delivery_ids) != expected or len(revisions) != len(expected):
            raise ConnectorContractError("invalid agent session checkpoint")
        expected_set = set(expected)
        tracked_revisions = (
            dict(
                zip(
                    self.committed_delivery_ids,
                    self.committed_revision_identities,
                    strict=True,
                )
            )
            if len(self.committed_delivery_ids) == len(self.committed_revision_identities)
            else {}
        )
        retained = tuple(
            (delivery_id, tracked_revisions[delivery_id])
            for delivery_id in self.committed_delivery_ids
            if delivery_id not in expected_set and delivery_id in tracked_revisions
        )
        return AgentSessionCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=page.next_cursor,
            committed_delivery_ids=tuple(delivery_id for delivery_id, _revision in retained)
            + expected,
            committed_revision_identities=tuple(revision for _delivery_id, revision in retained)
            + revisions,
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


class AgentSessionCheckpointStore:
    """Goal-owned JSON checkpoint persistence for selected local projects."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid agent session checkpoint store")
        self._root = root

    def _path(self, selection: SourceResourceSelection) -> Path:
        _require_selection(selection)
        digest = SourceRecordKey(
            connector_name=D4_AGENT_SESSION_SOURCE,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id="project",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"agent-session-{digest}.json"

    def load(self, selection: SourceResourceSelection) -> AgentSessionCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return AgentSessionCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid agent session checkpoint store") from error
        checkpoint = AgentSessionCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid agent session checkpoint store")
        return checkpoint

    def save(self, checkpoint: AgentSessionCheckpoint) -> Path:
        if type(checkpoint) is not AgentSessionCheckpoint:
            raise ConnectorContractError("invalid agent session checkpoint store")
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
            raise ConnectorContractError("invalid agent session checkpoint store") from error
        return path


class AgentSessionSourceAdapter:
    """Convert explicit local agent-session payloads into D4 source values."""

    def project_selection(
        self,
        *,
        connection_id: str,
        project_id: str,
    ) -> SourceResourceSelection:
        if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
            raise ConnectorContractError("invalid agent session source connection")
        if type(project_id) is not str or _PROJECT_ID.fullmatch(project_id) is None:
            raise ConnectorContractError("invalid agent session project selection")
        return SourceResourceSelection(
            connector_name=D4_AGENT_SESSION_SOURCE,
            connection_id=connection_id,
            resource_id=project_id,
            resource_type="local_project",
        )

    def page_from_events(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        include_transcripts: bool,
        selected_session_ids: Sequence[str],
        next_cursor: str | None = None,
    ) -> AgentSessionPage:
        include = _required_bool(include_transcripts, "invalid agent session records")
        selected_sessions = _required_selected_sessions(selected_session_ids)
        selected_values = tuple(
            value
            for value in _require_values(values)
            if _matches_selected_session(selection, value, selected_sessions)
        )
        records = tuple(
            self.record_from_event(value, include_transcript=include)
            for value in selected_values
        )
        if not records:
            raise ConnectorContractError("invalid agent session records")
        return AgentSessionPage(
            status=AgentSessionPageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[AgentSessionRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection)
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid agent session records")
        expanded = tuple(
            intake
            for record in records
            for intake in self.intakes(selection, record, privacy=privacy)
        )
        if len(expanded) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid agent session records")
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(
                    intake,
                    content_type=(
                        "session_transcript"
                        if intake.key.external_id.startswith("transcript:")
                        else "session_summary"
                    ),
                )
                for intake in expanded
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: AgentSessionCheckpoint,
        page: AgentSessionPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[AgentSessionCheckpoint, ConnectorRunReceipt]:
        if page.status in {
            AgentSessionPageStatus.OPTED_OUT,
            AgentSessionPageStatus.NOT_ALLOWED,
            AgentSessionPageStatus.LOOP_PREVENTED,
            AgentSessionPageStatus.UNSUPPORTED_FORMAT,
        }:
            return checkpoint, ConnectorRunReceipt.failed(
                D4_AGENT_SESSION_SOURCE,
                ConnectorFailureCode.NOT_ALLOWED,
            )
        if (
            type(checkpoint) is not AgentSessionCheckpoint
            or type(page.preview) is not SourcePreviewPage
            or page.preview.selection != checkpoint.selection
            or not isinstance(intakes, Sequence)
            or isinstance(intakes, str)
            or type(capture_sink) is not ConnectorCaptureSink
        ):
            raise ConnectorContractError("invalid agent session import")
        selected = tuple(record for record in page.preview.records if record.selected)
        if not selected:
            return checkpoint, ConnectorRunReceipt.empty(
                D4_AGENT_SESSION_SOURCE,
                metadata_count=len(page.preview.records),
            )
        intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
        if (
            len(intake_by_delivery) != len(intakes)
            or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
            or any(
                intake.key.connector_name != D4_AGENT_SESSION_SOURCE
                or intake.key.connection_id != page.preview.selection.connection_id
                or intake.key.resource_id != page.preview.selection.resource_id
                or intake.source_reference != record.source_reference
                for record, intake in zip(selected, intakes, strict=True)
            )
        ):
            raise ConnectorContractError("invalid agent session import")
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
                D4_AGENT_SESSION_SOURCE,
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
                connector_name=D4_AGENT_SESSION_SOURCE,
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

    def opted_out_page(self) -> AgentSessionPage:
        return AgentSessionPage(status=AgentSessionPageStatus.OPTED_OUT)

    def not_allowed_page(self) -> AgentSessionPage:
        return AgentSessionPage(status=AgentSessionPageStatus.NOT_ALLOWED)

    def loop_prevented_page(self) -> AgentSessionPage:
        return AgentSessionPage(status=AgentSessionPageStatus.LOOP_PREVENTED)

    def unsupported_format_page(self) -> AgentSessionPage:
        return AgentSessionPage(status=AgentSessionPageStatus.UNSUPPORTED_FORMAT)

    def intakes(
        self,
        selection: SourceResourceSelection,
        record: AgentSessionRecord,
        *,
        privacy: PrivacyDecision,
    ) -> tuple[SourceRecordIntake, ...]:
        if type(record) is not AgentSessionRecord:
            raise ConnectorContractError("invalid agent session record")
        _require_record_matches_selection(selection, record)
        summary = SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D4_AGENT_SESSION_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=f"summary:{record.session_id}",
                revision_id=f"summary:{record.revision_id}",
            ),
            url=_source_reference(record),
            title=f"{record.client_name} session {record.session_id} summary",
            text=record.summary,
            privacy=privacy,
        )
        if not record.transcript_selected:
            return (summary,)
        transcript_text = _body_or_fallback(
            record.transcript,
            f"{record.client_name} transcript {record.session_id}",
            "invalid agent session record",
        )
        transcript = SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D4_AGENT_SESSION_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=f"transcript:{record.session_id}",
                revision_id=f"transcript:{record.revision_id}",
            ),
            url=_source_reference(record),
            title=f"{record.client_name} session {record.session_id} transcript",
            text=transcript_text,
            privacy=privacy,
        )
        return (summary, transcript)

    def record_from_event(
        self,
        value: Mapping[str, object],
        *,
        include_transcript: bool,
    ) -> AgentSessionRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid agent session record")
        include = _required_bool(include_transcript, "invalid agent session record")
        project_id = _required_project_id(value.get("project_id"))
        session_id = _required_session_id(value.get("session_id"))
        if "source_reference" in value and value["source_reference"] is not None:
            raise ConnectorContractError("invalid agent session record")
        client_name = _required_client(value.get("client_name"))
        if value.get("source_kind") != "agent_session":
            raise ConnectorContractError("invalid agent session record")
        if _required_bool(value.get("brain_result_reference"), "invalid agent session record"):
            raise ConnectorContractError("invalid agent session record")
        summary = _body_or_fallback(
            value.get("summary"),
            f"{client_name} session {session_id}",
            "invalid agent session record",
        )
        _require_clean_content_verdict(
            value.get("summary_secret_scan"),
            summary,
            "invalid agent session record",
        )
        transcript = None
        if include and value.get("transcript") is not None:
            transcript = _body_or_fallback(
                value.get("transcript"),
                f"{client_name} transcript {session_id}",
                "invalid agent session record",
            )
            _require_clean_content_verdict(
                value.get("transcript_secret_scan"),
                transcript,
                "invalid agent session record",
            )
        return AgentSessionRecord(
            project_id=project_id,
            session_id=session_id,
            revision_id=_required_session_id(value.get("revision_id")),
            client_name=client_name,
            summary=summary,
            transcript=transcript,
            transcript_selected=transcript is not None,
        )


def _checkpoint_parts(value: object) -> _CheckpointParts:
    legacy_fields = {
        "committed_delivery_ids",
        "connector_name",
        "connection_id",
        "next_cursor",
        "resource_id",
        "resource_type",
        "schema_version",
    }
    current_fields = legacy_fields | {"committed_revision_identities"}
    if not isinstance(value, dict) or set(value) not in {
        frozenset(legacy_fields),
        frozenset(current_fields),
    }:
        raise ConnectorContractError("invalid agent session checkpoint")
    committed = value["committed_delivery_ids"]
    committed_revisions = value.get("committed_revision_identities", [])
    if not isinstance(committed, list) or not isinstance(committed_revisions, list):
        raise ConnectorContractError("invalid agent session checkpoint")
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
        raise ConnectorContractError("invalid agent session records")
    return values


def _required_selected_sessions(values: Sequence[str]) -> set[str]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid agent session records")
    selected = tuple(_required_session_id(value) for value in values)
    if not selected:
        raise ConnectorContractError("invalid agent session records")
    return set(selected)


def _matches_selected_session(
    selection: SourceResourceSelection,
    value: Mapping[str, object],
    selected_session_ids: set[str],
) -> bool:
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid agent session record")
    project_id = _required_project_id(value.get("project_id"))
    if project_id != selection.resource_id:
        return False
    session_id = _required_session_id(value.get("session_id"))
    return session_id in selected_session_ids


def _require_selection(selection: SourceResourceSelection) -> None:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != D4_AGENT_SESSION_SOURCE
        or selection.resource_type != "local_project"
        or _PROJECT_ID.fullmatch(selection.resource_id) is None
    ):
        raise ConnectorContractError("invalid agent session project selection")


def _require_record_matches_selection(
    selection: SourceResourceSelection,
    record: AgentSessionRecord,
) -> None:
    _require_selection(selection)
    if record.project_id != selection.resource_id:
        raise ConnectorContractError("invalid agent session record")


def _source_reference(record: AgentSessionRecord) -> str:
    project_token = record.project_id.removeprefix("project:")
    session_token = quote(record.session_id, safe="")
    return f"https://local.openbrain.invalid/projects/{project_token}/sessions/{session_token}"


def _required_project_id(value: object) -> str:
    text = _required_str(value, "invalid agent session record")
    if _PROJECT_ID.fullmatch(text) is None:
        raise ConnectorContractError("invalid agent session record")
    return text


def _required_session_id(value: object) -> str:
    text = _required_str(value, "invalid agent session record")
    if _SESSION_ID.fullmatch(text) is None:
        raise ConnectorContractError("invalid agent session record")
    return text


def _required_client(value: object) -> str:
    text = _required_str(value, "invalid agent session record")
    if text not in {"claude_code", "codex"}:
        raise ConnectorContractError("invalid agent session record")
    return text


def _typed_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid agent session checkpoint")
    return value


def _typed_str(value: object) -> str:
    if type(value) is not str:
        raise ConnectorContractError("invalid agent session checkpoint")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _typed_str(value)


def _required_str(value: object, message: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ConnectorContractError(message)
    return value.strip()


def _required_bool(value: object, message: str) -> bool:
    if type(value) is not bool:
        raise ConnectorContractError(message)
    return value


def _require_clean_content_verdict(value: object, body: str, message: str) -> None:
    if value != "clean":
        raise ConnectorContractError(message)
    _require_redaction_clean(body, message)


def _require_redaction_clean(body: str, message: str) -> None:
    try:
        redaction_found = has_redaction_finding(body)
    except ValueError as error:
        raise ConnectorContractError(message) from error
    if redaction_found:
        raise ConnectorContractError(message)


def _body_or_fallback(body: object, fallback: str, message: str) -> str:
    if body is None:
        return fallback
    if type(body) is not str or "\x00" in body:
        raise ConnectorContractError(message)
    return body.strip() or fallback
