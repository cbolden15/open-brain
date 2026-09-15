"""GitLab and Jira D4 adapter values for explicitly selected projects."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TypedDict, cast

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
    D4_GITLAB_SOURCE,
    D4_JIRA_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "ProjectPageStatus",
    "GitLabProjectCheckpoint",
    "GitLabProjectCheckpointStore",
    "GitLabProjectPage",
    "GitLabProjectRecord",
    "GitLabSourceAdapter",
    "JiraProjectCheckpoint",
    "JiraProjectCheckpointStore",
    "JiraProjectPage",
    "JiraProjectRecord",
    "JiraSourceAdapter",
]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}[A-Za-z0-9]")
_ISOISH = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z")
_GITLAB_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,19}")
_JIRA_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,19}")
_MAX_PREVIEW_RECORDS = 25

GitLabContentType = Literal["comment", "issue", "merge_request"]
JiraContentType = Literal["comment", "issue"]

class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class ProjectPageStatus(StrEnum):
    """Host-observed result for one bounded project page."""

    READY = "ready"
    RATE_LIMITED = "rate_limited"
    NEEDS_SIGN_IN = "needs_sign_in"
    NOT_ALLOWED = "not_allowed"


@dataclass(frozen=True, slots=True)
class GitLabProjectRecord:
    """A bounded GitLab issue, merge request, or note record."""

    content_type: GitLabContentType
    number: int
    external_id: str
    revision_id: str
    web_url: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class JiraProjectRecord:
    """A bounded Jira issue or comment record."""

    content_type: JiraContentType
    key: str
    external_id: str
    revision_id: str
    web_url: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class _ProjectPage:
    status: ProjectPageStatus
    preview: SourcePreviewPage | None = None
    retry_after_seconds: int | None = None
    connector_name: str = ""

    def __post_init__(self) -> None:
        try:
            status = ProjectPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError(f"invalid {self.connector_name} page") from error
        if status is ProjectPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage or self.retry_after_seconds is not None:
                raise ConnectorContractError(f"invalid {self.connector_name} page")
        elif (
            self.preview is not None
            or (
                self.retry_after_seconds is not None
                and (
                    type(self.retry_after_seconds) is not int
                    or not 1 <= self.retry_after_seconds <= 86_400
                )
            )
        ):
            raise ConnectorContractError(f"invalid {self.connector_name} page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class GitLabProjectPage(_ProjectPage):
    connector_name: str = D4_GITLAB_SOURCE


@dataclass(frozen=True, slots=True)
class JiraProjectPage(_ProjectPage):
    connector_name: str = D4_JIRA_SOURCE


@dataclass(frozen=True, slots=True)
class _ProjectCheckpoint:
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...] = ()

    @property
    def connector_name(self) -> str:
        return self.selection.connector_name

    def _validate_base(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.selection) is not SourceResourceSelection
            or self.selection.resource_type != "project"
            or self.connector_name not in {D4_GITLAB_SOURCE, D4_JIRA_SOURCE}
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str
                    or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str
                or not value.startswith(f"connector.{self.connector_name}.")
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
            raise ConnectorContractError(f"invalid {self.connector_name} checkpoint")

    def _advance_base(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str] | None = None,
    ) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError(f"invalid {self.connector_name} checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        if tuple(committed_delivery_ids) != expected:
            raise ConnectorContractError(f"invalid {self.connector_name} checkpoint")
        revision_identities = tuple(
            () if committed_revision_identities is None else committed_revision_identities
        )
        if revision_identities and len(revision_identities) != len(expected):
            raise ConnectorContractError(f"invalid {self.connector_name} checkpoint")
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
        return (
            page.next_cursor,
            retained_delivery_ids + expected,
            retained_revisions + revision_identities if revision_identities else (),
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

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str] | None = None,
    ) -> _ProjectCheckpoint:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class GitLabProjectCheckpoint(_ProjectCheckpoint):
    """Durable GitLab project cursor committed after capture acknowledgements."""

    def __post_init__(self) -> None:
        self._validate_base()
        if self.connector_name != D4_GITLAB_SOURCE:
            raise ConnectorContractError("invalid gitlab checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> GitLabProjectCheckpoint:
        return cls(
            schema_version=1,
            selection=selection,
            next_cursor=None,
            committed_delivery_ids=(),
        )

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str] | None = None,
    ) -> GitLabProjectCheckpoint:
        next_cursor, delivery_ids, revisions = self._advance_base(
            page,
            committed_delivery_ids=committed_delivery_ids,
            committed_revision_identities=committed_revision_identities,
        )
        return GitLabProjectCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=next_cursor,
            committed_delivery_ids=delivery_ids,
            committed_revision_identities=revisions,
        )

    @classmethod
    def from_dict(cls, value: object) -> GitLabProjectCheckpoint:
        parts = _checkpoint_parts(value, D4_GITLAB_SOURCE, "invalid gitlab checkpoint")
        return cls(
            schema_version=parts["schema_version"],
            selection=parts["selection"],
            next_cursor=parts["next_cursor"],
            committed_delivery_ids=parts["committed_delivery_ids"],
            committed_revision_identities=parts["committed_revision_identities"],
        )


@dataclass(frozen=True, slots=True)
class JiraProjectCheckpoint(_ProjectCheckpoint):
    """Durable Jira project cursor committed after capture acknowledgements."""

    def __post_init__(self) -> None:
        self._validate_base()
        if self.connector_name != D4_JIRA_SOURCE:
            raise ConnectorContractError("invalid jira checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> JiraProjectCheckpoint:
        return cls(
            schema_version=1,
            selection=selection,
            next_cursor=None,
            committed_delivery_ids=(),
        )

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str] | None = None,
    ) -> JiraProjectCheckpoint:
        next_cursor, delivery_ids, revisions = self._advance_base(
            page,
            committed_delivery_ids=committed_delivery_ids,
            committed_revision_identities=committed_revision_identities,
        )
        return JiraProjectCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=next_cursor,
            committed_delivery_ids=delivery_ids,
            committed_revision_identities=revisions,
        )

    @classmethod
    def from_dict(cls, value: object) -> JiraProjectCheckpoint:
        parts = _checkpoint_parts(value, D4_JIRA_SOURCE, "invalid jira checkpoint")
        return cls(
            schema_version=parts["schema_version"],
            selection=parts["selection"],
            next_cursor=parts["next_cursor"],
            committed_delivery_ids=parts["committed_delivery_ids"],
            committed_revision_identities=parts["committed_revision_identities"],
        )


class _ProjectCheckpointStore:
    def __init__(self, root: Path, connector_name: str) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError(f"invalid {connector_name} checkpoint store")
        self._root = root
        self._connector_name = connector_name

    def _path(self, selection: SourceResourceSelection) -> Path:
        if (
            type(selection) is not SourceResourceSelection
            or selection.connector_name != self._connector_name
            or selection.resource_type != "project"
        ):
            raise ConnectorContractError(f"invalid {self._connector_name} checkpoint store")
        digest = SourceRecordKey(
            connector_name=self._connector_name,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id="project",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"{self._connector_name}-project-{digest}.json"


class GitLabProjectCheckpointStore(_ProjectCheckpointStore):
    """Goal-owned JSON checkpoint persistence for selected GitLab projects."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, D4_GITLAB_SOURCE)

    def load(self, selection: SourceResourceSelection) -> GitLabProjectCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return GitLabProjectCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid gitlab checkpoint store") from error
        checkpoint = GitLabProjectCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid gitlab checkpoint store")
        return checkpoint

    def save(self, checkpoint: GitLabProjectCheckpoint) -> Path:
        return _save_checkpoint(self, checkpoint, "invalid gitlab checkpoint store")


class JiraProjectCheckpointStore(_ProjectCheckpointStore):
    """Goal-owned JSON checkpoint persistence for selected Jira projects."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, D4_JIRA_SOURCE)

    def load(self, selection: SourceResourceSelection) -> JiraProjectCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return JiraProjectCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid jira checkpoint store") from error
        checkpoint = JiraProjectCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid jira checkpoint store")
        return checkpoint

    def save(self, checkpoint: JiraProjectCheckpoint) -> Path:
        return _save_checkpoint(self, checkpoint, "invalid jira checkpoint store")


class GitLabSourceAdapter:
    """Convert host-mediated GitLab API responses into D4 source values."""

    def project_selection(
        self, *, connection_id: str, host: str, project_path: str
    ) -> SourceResourceSelection:
        _require_connection_id(connection_id)
        _require_host(host, "invalid gitlab project")
        _require_gitlab_path(project_path)
        return SourceResourceSelection(
            connector_name=D4_GITLAB_SOURCE,
            connection_id=connection_id,
            resource_id=f"project:{host}/{project_path}",
            resource_type="project",
        )

    def project_page_from_rest(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> GitLabProjectPage:
        records = tuple(
            self.note_from_rest(value)
            if _looks_like_gitlab_note(value)
            else self.issue_or_mr_from_rest(value)
            for value in _require_value_sequence(values, "invalid gitlab page")
        )
        return GitLabProjectPage(
            status=ProjectPageStatus.READY,
            preview=self.preview_project(
                selection,
                records,
                privacy=privacy,
                next_cursor=next_cursor,
            ),
        )

    def preview_project(
        self,
        selection: SourceResourceSelection,
        records: Sequence[GitLabProjectRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection, D4_GITLAB_SOURCE, "invalid gitlab selection")
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid gitlab records")
        if len(records) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid gitlab records")
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

    def import_project_page(
        self,
        checkpoint: GitLabProjectCheckpoint,
        page: GitLabProjectPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[GitLabProjectCheckpoint, ConnectorRunReceipt]:
        imported_checkpoint, receipt = _import_project_page(
            checkpoint,
            page,
            intakes,
            capture_sink,
            connector_name=D4_GITLAB_SOURCE,
            message="invalid gitlab import",
        )
        return imported_checkpoint, receipt

    def rate_limited_page(self, *, retry_after_seconds: int) -> GitLabProjectPage:
        return GitLabProjectPage(
            status=ProjectPageStatus.RATE_LIMITED,
            retry_after_seconds=retry_after_seconds,
        )

    def needs_sign_in_page(self) -> GitLabProjectPage:
        return GitLabProjectPage(status=ProjectPageStatus.NEEDS_SIGN_IN)

    def not_allowed_page(self) -> GitLabProjectPage:
        return GitLabProjectPage(status=ProjectPageStatus.NOT_ALLOWED)

    def intake(
        self,
        selection: SourceResourceSelection,
        record: GitLabProjectRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not GitLabProjectRecord:
            raise ConnectorContractError("invalid gitlab record")
        _require_gitlab_record_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D4_GITLAB_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=record.external_id,
                revision_id=record.revision_id,
            ),
            url=record.web_url,
            title=record.title,
            text=record.body,
            privacy=privacy,
        )

    def issue_or_mr_from_rest(self, value: Mapping[str, object]) -> GitLabProjectRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid gitlab record")
        iid = _positive_int(value.get("iid"), "invalid gitlab record")
        kind: GitLabContentType = (
            "merge_request" if "merge_status" in value or "source_branch" in value else "issue"
        )
        return GitLabProjectRecord(
            content_type=kind,
            number=iid,
            external_id=f"{'merge_request' if kind == 'merge_request' else 'issue'}:{iid}",
            revision_id="updated:"
            + _required_timestamp(value.get("updated_at"), "invalid gitlab record"),
            web_url=_required_str(value.get("web_url"), "invalid gitlab record"),
            title=_required_str(value.get("title"), "invalid gitlab record"),
            body=_body_or_title(
                value.get("description"),
                value.get("title"),
                "invalid gitlab record",
            ),
        )

    def note_from_rest(self, value: Mapping[str, object]) -> GitLabProjectRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid gitlab record")
        note_id = _positive_int(value.get("id"), "invalid gitlab record")
        noteable_iid = _positive_int(value.get("noteable_iid"), "invalid gitlab record")
        return GitLabProjectRecord(
            content_type="comment",
            number=noteable_iid,
            external_id=f"comment:{note_id}",
            revision_id="updated:"
            + _required_timestamp(value.get("updated_at"), "invalid gitlab record"),
            web_url=_required_str(value.get("web_url"), "invalid gitlab record"),
            title=f"Comment on {value.get('noteable_type', 'item')} {noteable_iid}",
            body=_body_or_title(
                value.get("body"),
                f"Comment on item {noteable_iid}",
                "invalid gitlab record",
            ),
        )


class JiraSourceAdapter:
    """Convert host-mediated Jira Cloud REST responses into D4 source values."""

    def project_selection(
        self, *, connection_id: str, site: str, project_key: str
    ) -> SourceResourceSelection:
        _require_connection_id(connection_id)
        _require_host(site, "invalid jira project")
        _require_jira_key(project_key)
        return SourceResourceSelection(
            connector_name=D4_JIRA_SOURCE,
            connection_id=connection_id,
            resource_id=f"project:{site}/{project_key}",
            resource_type="project",
        )

    def project_page_from_rest(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> JiraProjectPage:
        records = tuple(
            self.comment_from_rest(value)
            if _looks_like_jira_comment(value)
            else self.issue_from_rest(value)
            for value in _require_value_sequence(values, "invalid jira page")
        )
        return JiraProjectPage(
            status=ProjectPageStatus.READY,
            preview=self.preview_project(
                selection,
                records,
                privacy=privacy,
                next_cursor=next_cursor,
            ),
        )

    def preview_project(
        self,
        selection: SourceResourceSelection,
        records: Sequence[JiraProjectRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection, D4_JIRA_SOURCE, "invalid jira selection")
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid jira records")
        if len(records) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid jira records")
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

    def import_project_page(
        self,
        checkpoint: JiraProjectCheckpoint,
        page: JiraProjectPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[JiraProjectCheckpoint, ConnectorRunReceipt]:
        imported_checkpoint, receipt = _import_project_page(
            checkpoint,
            page,
            intakes,
            capture_sink,
            connector_name=D4_JIRA_SOURCE,
            message="invalid jira import",
        )
        return imported_checkpoint, receipt

    def rate_limited_page(self, *, retry_after_seconds: int) -> JiraProjectPage:
        return JiraProjectPage(
            status=ProjectPageStatus.RATE_LIMITED,
            retry_after_seconds=retry_after_seconds,
        )

    def needs_sign_in_page(self) -> JiraProjectPage:
        return JiraProjectPage(status=ProjectPageStatus.NEEDS_SIGN_IN)

    def not_allowed_page(self) -> JiraProjectPage:
        return JiraProjectPage(status=ProjectPageStatus.NOT_ALLOWED)

    def intake(
        self,
        selection: SourceResourceSelection,
        record: JiraProjectRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not JiraProjectRecord:
            raise ConnectorContractError("invalid jira record")
        _require_jira_record_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D4_JIRA_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=record.external_id,
                revision_id=record.revision_id,
            ),
            url=record.web_url,
            title=record.title,
            text=record.body,
            privacy=privacy,
        )

    def issue_from_rest(self, value: Mapping[str, object]) -> JiraProjectRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid jira record")
        key = _required_str(value.get("key"), "invalid jira record")
        fields = value.get("fields")
        if not isinstance(fields, Mapping):
            raise ConnectorContractError("invalid jira record")
        return JiraProjectRecord(
            content_type="issue",
            key=key,
            external_id=f"issue:{key}",
            revision_id="updated:"
            + _required_timestamp(fields.get("updated"), "invalid jira record"),
            web_url=_required_str(value.get("self_web_url"), "invalid jira record"),
            title=_required_str(fields.get("summary"), "invalid jira record"),
            body=_jira_text(fields.get("description"), fields.get("summary")),
        )

    def comment_from_rest(self, value: Mapping[str, object]) -> JiraProjectRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid jira record")
        issue_key = _required_str(value.get("issue_key"), "invalid jira record")
        comment_id = _required_str(value.get("id"), "invalid jira record")
        return JiraProjectRecord(
            content_type="comment",
            key=issue_key,
            external_id=f"comment:{comment_id}",
            revision_id="updated:"
            + _required_timestamp(value.get("updated"), "invalid jira record"),
            web_url=_required_str(value.get("self_web_url"), "invalid jira record"),
            title=f"Comment on {issue_key}",
            body=_jira_text(value.get("body"), f"Comment on {issue_key}"),
        )


def _import_project_page[CheckpointT: _ProjectCheckpoint](
    checkpoint: CheckpointT,
    page: _ProjectPage,
    intakes: Sequence[SourceRecordIntake],
    capture_sink: ConnectorCaptureSink,
    *,
    connector_name: str,
    message: str,
) -> tuple[CheckpointT, ConnectorRunReceipt]:
    if page.status is ProjectPageStatus.RATE_LIMITED:
        return checkpoint, ConnectorRunReceipt.failed(
            connector_name,
            ConnectorFailureCode.RUNTIME_FAILED,
        )
    if page.status in {ProjectPageStatus.NEEDS_SIGN_IN, ProjectPageStatus.NOT_ALLOWED}:
        return checkpoint, ConnectorRunReceipt.failed(
            connector_name,
            ConnectorFailureCode.NOT_ALLOWED,
        )
    if not isinstance(intakes, Sequence) or isinstance(intakes, str):
        raise ConnectorContractError(message)
    if type(capture_sink) is not ConnectorCaptureSink:
        raise ConnectorContractError(message)
    preview = page.preview
    if preview is None or preview.selection != checkpoint.selection:
        raise ConnectorContractError(message)
    selected = tuple(record for record in preview.records if record.selected)
    if not selected:
        return checkpoint, ConnectorRunReceipt.empty(
            connector_name,
            metadata_count=len(preview.records),
        )
    intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
    if (
        len(intake_by_delivery) != len(intakes)
        or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
        or any(
            intake.key.connector_name != preview.selection.connector_name
            or intake.key.connection_id != preview.selection.connection_id
            or intake.key.resource_id != preview.selection.resource_id
            or intake.source_reference != record.source_reference
            for record, intake in zip(selected, intakes, strict=True)
        )
    ):
        raise ConnectorContractError(message)
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
    committed_delivery_ids = set(checkpoint.committed_delivery_ids)
    if not committed_revisions:
        committed = tuple(
            record.delivery_id
            for record in selected
            if record.delivery_id in committed_delivery_ids
        )
        if committed and len(committed) != len(selected):
            raise ConnectorContractError(f"invalid {connector_name} checkpoint")
        changed_intakes = tuple(intakes)
    else:
        changed_intakes = tuple(
            intake
            for intake in intakes
            if committed_revisions.get(intake.key.delivery_id()) != intake.key.revision_identity()
        )
    if not changed_intakes:
        return checkpoint, ConnectorRunReceipt.empty(
            connector_name,
            metadata_count=len(preview.records),
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
            selection=preview.selection,
            records=selected,
            next_cursor=preview.next_cursor,
        ),
        committed_delivery_ids=tuple(record.delivery_id for record in selected),
        committed_revision_identities=tuple(intake.key.revision_identity() for intake in intakes),
    )
    return (
        cast(CheckpointT, advanced),
        ConnectorRunReceipt(
            connector_name=connector_name,
            outcome=ConnectorOutcome.COMPLETED,
            failure_code=None,
            discovered_count=len(preview.records),
            fetched_count=len(selected),
            extracted_count=len(selected),
            submitted_count=len(receipts),
            stubbed_count=0,
            created_count=sum(1 for receipt in receipts if not receipt.duplicate),
            duplicate_count=sum(1 for receipt in receipts if receipt.duplicate),
            checkpoint_committed=True,
            metadata_count=len(preview.records),
        ),
    )


def _checkpoint_parts(
    value: object, connector_name: str, message: str
) -> _CheckpointParts:
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
        raise ConnectorContractError(message)
    if value["connector_name"] != connector_name:
        raise ConnectorContractError(message)
    committed = value["committed_delivery_ids"]
    committed_revisions = value["committed_revision_identities"]
    if not isinstance(committed, list) or not isinstance(committed_revisions, list):
        raise ConnectorContractError(message)
    return {
        "schema_version": _typed_int(value["schema_version"], message),
        "selection": SourceResourceSelection(
            connector_name=_typed_str(value["connector_name"], message),
            connection_id=_typed_str(value["connection_id"], message),
            resource_id=_typed_str(value["resource_id"], message),
            resource_type=_typed_str(value["resource_type"], message),
        ),
        "next_cursor": _optional_str(value["next_cursor"], message),
        "committed_delivery_ids": tuple(_typed_str(item, message) for item in committed),
        "committed_revision_identities": tuple(
            _typed_str(item, message) for item in committed_revisions
        ),
    }


def _save_checkpoint(
    store: _ProjectCheckpointStore,
    checkpoint: _ProjectCheckpoint,
    message: str,
) -> Path:
    store._root.mkdir(parents=True, exist_ok=True)
    path = store._path(checkpoint.selection)
    payload = json.dumps(checkpoint.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    with NamedTemporaryFile(
        "w",
        delete=False,
        dir=store._root,
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
        raise ConnectorContractError(message) from error
    return path


def _require_selection(
    selection: SourceResourceSelection,
    connector_name: str,
    message: str,
) -> None:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != connector_name
        or selection.resource_type != "project"
    ):
        raise ConnectorContractError(message)


def _require_connection_id(connection_id: str) -> None:
    if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
        raise ConnectorContractError("invalid source connection")


def _require_host(value: str, message: str) -> None:
    if type(value) is not str or _HOST.fullmatch(value) is None or "." not in value:
        raise ConnectorContractError(message)


def _require_gitlab_path(value: str) -> None:
    if type(value) is not str or _GITLAB_PATH.fullmatch(value) is None:
        raise ConnectorContractError("invalid gitlab project")


def _require_jira_key(value: str) -> None:
    if type(value) is not str or _JIRA_KEY.fullmatch(value) is None:
        raise ConnectorContractError("invalid jira project")


def _require_value_sequence(
    values: Sequence[Mapping[str, object]], message: str
) -> Sequence[Mapping[str, object]]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError(message)
    return values


def _positive_int(value: object, message: str) -> int:
    if type(value) is not int or value < 1:
        raise ConnectorContractError(message)
    return value


def _required_str(value: object, message: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ConnectorContractError(message)
    return value.strip()


def _typed_int(value: object, message: str) -> int:
    if type(value) is not int:
        raise ConnectorContractError(message)
    return value


def _typed_str(value: object, message: str) -> str:
    if type(value) is not str:
        raise ConnectorContractError(message)
    return value


def _optional_str(value: object, message: str) -> str | None:
    if value is None:
        return None
    return _typed_str(value, message)


def _required_timestamp(value: object, message: str) -> str:
    timestamp = _required_str(value, message)
    if _ISOISH.fullmatch(timestamp) is None:
        raise ConnectorContractError(message)
    return timestamp


def _body_or_title(body: object, title: object, message: str) -> str:
    if body is None:
        return _required_str(title, message)
    if type(body) is not str or "\x00" in body:
        raise ConnectorContractError(message)
    return body.strip() or _required_str(title, message)


def _jira_text(value: object, fallback: object) -> str:
    if isinstance(value, str):
        return _body_or_title(value, fallback, "invalid jira record")
    if isinstance(value, Mapping):
        parts: list[str] = []
        for block in value.get("content", ()):
            if not isinstance(block, Mapping):
                continue
            for child in block.get("content", ()):
                if isinstance(child, Mapping) and child.get("type") == "text":
                    text = child.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        if parts:
            return "\n".join(parts)
    return _required_str(fallback, "invalid jira record")


def _looks_like_gitlab_note(value: object) -> bool:
    return isinstance(value, Mapping) and "noteable_iid" in value and "body" in value


def _looks_like_jira_comment(value: object) -> bool:
    return isinstance(value, Mapping) and "issue_key" in value and "body" in value


def _project_parts(
    selection: SourceResourceSelection,
    connector_name: str,
) -> tuple[str, str]:
    _require_selection(selection, connector_name, f"invalid {connector_name} selection")
    if not selection.resource_id.startswith("project:"):
        raise ConnectorContractError(f"invalid {connector_name} selection")
    host, _, project = selection.resource_id.removeprefix("project:").partition("/")
    if not host or not project:
        raise ConnectorContractError(f"invalid {connector_name} selection")
    return host, project


def _require_gitlab_record_matches_selection(
    selection: SourceResourceSelection, record: GitLabProjectRecord
) -> None:
    host, project = _project_parts(selection, D4_GITLAB_SOURCE)
    base = f"https://{host}/{project}/-"
    if record.content_type == "issue":
        expected = f"{base}/issues/{record.number}"
        if record.web_url != expected:
            raise ConnectorContractError("invalid gitlab record")
    elif record.content_type == "merge_request":
        expected = f"{base}/merge_requests/{record.number}"
        if record.web_url != expected:
            raise ConnectorContractError("invalid gitlab record")
    else:
        if not record.web_url.startswith(
            f"{base}/issues/{record.number}#note_"
        ) and not record.web_url.startswith(
            f"{base}/merge_requests/{record.number}#note_",
        ):
            raise ConnectorContractError("invalid gitlab record")


def _require_jira_record_matches_selection(
    selection: SourceResourceSelection, record: JiraProjectRecord
) -> None:
    site, project_key = _project_parts(selection, D4_JIRA_SOURCE)
    if not record.key.startswith(f"{project_key}-"):
        raise ConnectorContractError("invalid jira record")
    if not record.web_url.startswith(f"https://{site}/browse/{record.key}"):
        raise ConnectorContractError("invalid jira record")
