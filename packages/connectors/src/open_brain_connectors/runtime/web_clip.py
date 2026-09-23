"""D5.1 adapter values for explicit browser-to-local web clips."""

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
    D5_WEB_CLIP_SOURCE,
    SourcePreviewPage,
    SourcePreviewRecord,
    SourceResourceSelection,
)

__all__ = [
    "SafariWebClipDeliveryHarness",
    "WebClipDeliveryResult",
    "WebClipCheckpoint",
    "WebClipCheckpointStore",
    "WebClipPage",
    "WebClipPageStatus",
    "WebClipRecord",
    "WebClipSourceAdapter",
]

_ACCOUNT = re.compile(r"account:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_BROWSER_ID = re.compile(r"browser:[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
_CLIP_ID = re.compile(r"clip:[0-9a-f]{16,64}")
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_RESOURCE_TYPE = re.compile(r"(current_page|selected_passage)")
_URL = re.compile(r"https://[A-Za-z0-9.-]+(?:[/:?#][^\s\x00]*)?")
_MAX_PREVIEW_RECORDS = 25


class _CheckpointParts(TypedDict):
    schema_version: int
    selection: SourceResourceSelection
    next_cursor: str | None
    committed_delivery_ids: tuple[str, ...]
    committed_revision_identities: tuple[str, ...]


class WebClipPageStatus(StrEnum):
    """Host-observed result for one bounded explicit browser clip page."""

    READY = "ready"
    NOT_ALLOWED = "not_allowed"
    UNSUPPORTED_BROWSER = "unsupported_browser"
    UNSUPPORTED_FORMAT = "unsupported_format"


@dataclass(frozen=True, slots=True)
class WebClipDeliveryResult:
    """Result of one explicit browser-to-local clip activation."""

    status: WebClipPageStatus
    clip: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        try:
            status = WebClipPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid web clip delivery") from error
        if status is WebClipPageStatus.READY:
            if not isinstance(self.clip, Mapping):
                raise ConnectorContractError("invalid web clip delivery")
        elif self.clip is not None:
            raise ConnectorContractError("invalid web clip delivery")
        object.__setattr__(self, "status", status)


class SafariWebClipDeliveryHarness:
    """Build D5.1 Safari clip payloads from explicit current-page activations."""

    browser_id = "browser:safari"

    def deliver(
        self,
        selection: SourceResourceSelection,
        value: Mapping[str, object],
    ) -> WebClipDeliveryResult:
        _require_selection(selection)
        if selection.resource_id != self.browser_id:
            return WebClipDeliveryResult(status=WebClipPageStatus.UNSUPPORTED_BROWSER)
        if not isinstance(value, Mapping):
            return WebClipDeliveryResult(status=WebClipPageStatus.UNSUPPORTED_FORMAT)
        if value.get("permission_granted") is False:
            return WebClipDeliveryResult(status=WebClipPageStatus.NOT_ALLOWED)
        if (
            value.get("delivery_kind") != "explicit_browser_clip"
            or value.get("browser") != "safari"
            or value.get("clip_type") != selection.resource_type
            or value.get("selection_confirmed") is not True
            or value.get("history_scan") is not False
            or value.get("cookie_capture") is not False
        ):
            return WebClipDeliveryResult(status=WebClipPageStatus.UNSUPPORTED_FORMAT)
        page_url = _required_url(value.get("page_url"))
        title = _body_or_fallback(value.get("title"), "Selected web clip")
        text = _selected_text(selection.resource_type, value)
        clip_id = _clip_id_from_activation(selection.resource_type, page_url, value)
        revision_id = _revision_id(selection.resource_type, page_url, title, text)
        return WebClipDeliveryResult(
            status=WebClipPageStatus.READY,
            clip={
                "browser_id": self.browser_id,
                "clip_id": clip_id,
                "clip_type": selection.resource_type,
                "cookie_capture": False,
                "history_scan": False,
                "page_url": page_url,
                "revision_id": revision_id,
                "source_kind": "web_clip",
                "text": text,
                "text_secret_scan": value.get("text_secret_scan"),
                "title": title,
            },
        )


@dataclass(frozen=True, slots=True)
class WebClipRecord:
    """A selected current page or selected passage delivered by a local browser."""

    clip_id: str
    revision_id: str
    clip_type: str
    browser_id: str
    page_url: str
    title: str
    text: str

    def __post_init__(self) -> None:
        clip_id = _required_clip_id(self.clip_id)
        revision_id = _required_token(self.revision_id, "invalid web clip record")
        clip_type = _required_clip_type(self.clip_type)
        browser_id = _required_browser_id(self.browser_id)
        page_url = _required_url(self.page_url)
        title = _body_or_fallback(self.title, "Selected web clip")
        text = _body_or_fallback(self.text, title)
        _require_redaction_clean(page_url)
        _require_redaction_clean(title)
        _require_redaction_clean(text)
        object.__setattr__(self, "clip_id", clip_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "clip_type", clip_type)
        object.__setattr__(self, "browser_id", browser_id)
        object.__setattr__(self, "page_url", page_url)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "text", text)


@dataclass(frozen=True, slots=True)
class WebClipPage:
    status: WebClipPageStatus
    preview: SourcePreviewPage | None = None

    def __post_init__(self) -> None:
        try:
            status = WebClipPageStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid web clip page") from error
        if status is WebClipPageStatus.READY:
            if type(self.preview) is not SourcePreviewPage:
                raise ConnectorContractError("invalid web clip page")
        elif self.preview is not None:
            raise ConnectorContractError("invalid web clip page")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class WebClipCheckpoint:
    """Durable selected web-clip cursor committed after capture acknowledgements."""

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
            or self.selection.connector_name != D5_WEB_CLIP_SOURCE
            or self.selection.resource_type not in {"current_page", "selected_passage"}
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not str or _CURSOR.fullmatch(self.next_cursor) is None
                )
            )
            or not isinstance(self.committed_delivery_ids, tuple)
            or any(
                type(value) is not str or not value.startswith("connector.web_clip.")
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
            raise ConnectorContractError("invalid web clip checkpoint")

    @classmethod
    def initial(cls, selection: SourceResourceSelection) -> WebClipCheckpoint:
        return cls(1, selection, None, ())

    @classmethod
    def from_dict(cls, value: object) -> WebClipCheckpoint:
        return cls(**_checkpoint_parts(value))

    def advance(
        self,
        page: SourcePreviewPage,
        *,
        committed_delivery_ids: Sequence[str],
        committed_revision_identities: Sequence[str],
    ) -> WebClipCheckpoint:
        if type(page) is not SourcePreviewPage or page.selection != self.selection:
            raise ConnectorContractError("invalid web clip checkpoint")
        expected = tuple(record.delivery_id for record in page.records)
        revisions = tuple(committed_revision_identities)
        if tuple(committed_delivery_ids) != expected or len(revisions) != len(expected):
            raise ConnectorContractError("invalid web clip checkpoint")
        return WebClipCheckpoint(
            schema_version=1,
            selection=self.selection,
            next_cursor=page.next_cursor,
            committed_delivery_ids=expected,
            committed_revision_identities=revisions,
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


class WebClipCheckpointStore:
    """Goal-owned JSON checkpoint persistence for explicitly selected web clips."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise ConnectorContractError("invalid web clip checkpoint store")
        self._root = root

    def _path(self, selection: SourceResourceSelection) -> Path:
        _require_selection(selection)
        digest = SourceRecordKey(
            connector_name=D5_WEB_CLIP_SOURCE,
            connection_id=selection.connection_id,
            resource_id=selection.resource_id,
            external_id="clip",
            revision_id="checkpoint",
        ).revision_identity()
        return self._root / f"web-clip-{digest}.json"

    def load(self, selection: SourceResourceSelection) -> WebClipCheckpoint:
        path = self._path(selection)
        if not path.exists():
            return WebClipCheckpoint.initial(selection)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConnectorContractError("invalid web clip checkpoint store") from error
        checkpoint = WebClipCheckpoint.from_dict(decoded)
        if checkpoint.selection != selection:
            raise ConnectorContractError("invalid web clip checkpoint store")
        return checkpoint

    def save(self, checkpoint: WebClipCheckpoint) -> Path:
        if type(checkpoint) is not WebClipCheckpoint:
            raise ConnectorContractError("invalid web clip checkpoint store")
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
            raise ConnectorContractError("invalid web clip checkpoint store") from error
        return path


class WebClipSourceAdapter:
    """Convert explicit current-page or selected-passage payloads into D5.1 values."""

    def clip_selection(
        self,
        *,
        connection_id: str,
        browser_id: str,
        clip_type: str,
    ) -> SourceResourceSelection:
        if type(connection_id) is not str or _ACCOUNT.fullmatch(connection_id) is None:
            raise ConnectorContractError("invalid web clip source connection")
        browser_id = _required_browser_id(browser_id)
        clip_type = _required_clip_type(clip_type)
        return SourceResourceSelection(
            connector_name=D5_WEB_CLIP_SOURCE,
            connection_id=connection_id,
            resource_id=browser_id,
            resource_type=clip_type,
        )

    def page_from_clips(
        self,
        selection: SourceResourceSelection,
        values: Sequence[Mapping[str, object]],
        *,
        privacy: PrivacyDecision,
        selected_clip_ids: Sequence[str],
        next_cursor: str | None = None,
    ) -> WebClipPage:
        selected_ids = _required_selected_clips(selected_clip_ids)
        selected_values = tuple(
            value
            for value in _require_values(values)
            if _matches_selected_clip(selection, value, selected_ids)
        )
        records = tuple(self.record_from_clip(value) for value in selected_values)
        if not records:
            raise ConnectorContractError("invalid web clip records")
        return WebClipPage(
            status=WebClipPageStatus.READY,
            preview=self.preview(selection, records, privacy=privacy, next_cursor=next_cursor),
        )

    def preview(
        self,
        selection: SourceResourceSelection,
        records: Sequence[WebClipRecord],
        *,
        privacy: PrivacyDecision,
        next_cursor: str | None = None,
    ) -> SourcePreviewPage:
        _require_selection(selection)
        if not isinstance(records, Sequence) or isinstance(records, str):
            raise ConnectorContractError("invalid web clip records")
        intakes = tuple(self.intake(selection, record, privacy=privacy) for record in records)
        if len(intakes) > _MAX_PREVIEW_RECORDS:
            raise ConnectorContractError("invalid web clip records")
        return SourcePreviewPage(
            selection=selection,
            records=tuple(
                SourcePreviewRecord.from_intake(intake, content_type=_content_type(selection))
                for intake in intakes
            ),
            next_cursor=next_cursor,
        )

    def import_page(
        self,
        checkpoint: WebClipCheckpoint,
        page: WebClipPage,
        intakes: Sequence[SourceRecordIntake],
        capture_sink: ConnectorCaptureSink,
    ) -> tuple[WebClipCheckpoint, ConnectorRunReceipt]:
        if page.status in {
            WebClipPageStatus.NOT_ALLOWED,
            WebClipPageStatus.UNSUPPORTED_BROWSER,
            WebClipPageStatus.UNSUPPORTED_FORMAT,
        }:
            return checkpoint, ConnectorRunReceipt.failed(
                D5_WEB_CLIP_SOURCE,
                ConnectorFailureCode.NOT_ALLOWED,
            )
        if (
            type(checkpoint) is not WebClipCheckpoint
            or type(page.preview) is not SourcePreviewPage
            or page.preview.selection != checkpoint.selection
            or not isinstance(intakes, Sequence)
            or isinstance(intakes, str)
            or type(capture_sink) is not ConnectorCaptureSink
        ):
            raise ConnectorContractError("invalid web clip import")
        selected = tuple(record for record in page.preview.records if record.selected)
        if not selected:
            return checkpoint, ConnectorRunReceipt.empty(
                D5_WEB_CLIP_SOURCE,
                metadata_count=len(page.preview.records),
            )
        intake_by_delivery = {intake.key.delivery_id(): intake for intake in intakes}
        if (
            len(intake_by_delivery) != len(intakes)
            or tuple(intake_by_delivery) != tuple(record.delivery_id for record in selected)
            or any(
                intake.key.connector_name != D5_WEB_CLIP_SOURCE
                or intake.key.connection_id != page.preview.selection.connection_id
                or intake.key.resource_id != page.preview.selection.resource_id
                or intake.source_reference != record.source_reference
                for record, intake in zip(selected, intakes, strict=True)
            )
        ):
            raise ConnectorContractError("invalid web clip import")
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
                D5_WEB_CLIP_SOURCE,
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
                connector_name=D5_WEB_CLIP_SOURCE,
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

    def not_allowed_page(self) -> WebClipPage:
        return WebClipPage(status=WebClipPageStatus.NOT_ALLOWED)

    def unsupported_browser_page(self) -> WebClipPage:
        return WebClipPage(status=WebClipPageStatus.UNSUPPORTED_BROWSER)

    def unsupported_format_page(self) -> WebClipPage:
        return WebClipPage(status=WebClipPageStatus.UNSUPPORTED_FORMAT)

    def intake(
        self,
        selection: SourceResourceSelection,
        record: WebClipRecord,
        *,
        privacy: PrivacyDecision,
    ) -> SourceRecordIntake:
        if type(record) is not WebClipRecord:
            raise ConnectorContractError("invalid web clip record")
        _require_record_matches_selection(selection, record)
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=D5_WEB_CLIP_SOURCE,
                connection_id=selection.connection_id,
                resource_id=selection.resource_id,
                external_id=f"{record.clip_type}:{record.clip_id}",
                revision_id=record.revision_id,
            ),
            url=record.page_url,
            title=record.title,
            text=record.text,
            privacy=privacy,
        )

    def record_from_clip(self, value: Mapping[str, object]) -> WebClipRecord:
        if not isinstance(value, Mapping):
            raise ConnectorContractError("invalid web clip record")
        if value.get("source_kind") != "web_clip":
            raise ConnectorContractError("invalid web clip record")
        if value.get("history_scan") is True or value.get("cookie_capture") is True:
            raise ConnectorContractError("invalid web clip record")
        text = _body_or_fallback(value.get("text"), "Selected web clip")
        title = _body_or_fallback(value.get("title"), "Selected web clip")
        page_url = _required_url(value.get("page_url"))
        if value.get("text_secret_scan") != "clean":
            raise ConnectorContractError("invalid web clip record")
        _require_redaction_clean(page_url)
        _require_redaction_clean(title)
        _require_redaction_clean(text)
        return WebClipRecord(
            clip_id=_required_clip_id(value.get("clip_id")),
            revision_id=_required_token(value.get("revision_id"), "invalid web clip record"),
            clip_type=_required_clip_type(value.get("clip_type")),
            browser_id=_required_browser_id(value.get("browser_id")),
            page_url=page_url,
            title=title,
            text=text,
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
        raise ConnectorContractError("invalid web clip checkpoint")
    committed = value["committed_delivery_ids"]
    committed_revisions = value["committed_revision_identities"]
    if not isinstance(committed, list) or not isinstance(committed_revisions, list):
        raise ConnectorContractError("invalid web clip checkpoint")
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
        raise ConnectorContractError("invalid web clip records")
    return values


def _required_selected_clips(values: Sequence[str]) -> set[str]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ConnectorContractError("invalid web clip records")
    selected = tuple(_required_clip_id(value) for value in values)
    if not selected:
        raise ConnectorContractError("invalid web clip records")
    return set(selected)


def _matches_selected_clip(
    selection: SourceResourceSelection,
    value: Mapping[str, object],
    selected_clip_ids: set[str],
) -> bool:
    if not isinstance(value, Mapping):
        raise ConnectorContractError("invalid web clip record")
    clip_id = _required_clip_id(value.get("clip_id"))
    if clip_id not in selected_clip_ids:
        return False
    return (
        _required_browser_id(value.get("browser_id")) == selection.resource_id
        and _required_clip_type(value.get("clip_type")) == selection.resource_type
    )


def _require_selection(selection: SourceResourceSelection) -> None:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != D5_WEB_CLIP_SOURCE
        or selection.resource_type not in {"current_page", "selected_passage"}
        or _BROWSER_ID.fullmatch(selection.resource_id) is None
    ):
        raise ConnectorContractError("invalid web clip selection")


def _require_record_matches_selection(
    selection: SourceResourceSelection,
    record: WebClipRecord,
) -> None:
    _require_selection(selection)
    if record.browser_id != selection.resource_id or record.clip_type != selection.resource_type:
        raise ConnectorContractError("invalid web clip record")


def _content_type(selection: SourceResourceSelection) -> str:
    _require_selection(selection)
    return "page" if selection.resource_type == "current_page" else "selected_passage"


def _selected_text(clip_type: str, value: Mapping[str, object]) -> str:
    if clip_type == "current_page":
        return _body_or_fallback(value.get("page_text"), "Selected web clip")
    if "selected_text" not in value:
        raise ConnectorContractError("invalid web clip record")
    return _body_or_fallback(value.get("selected_text"), "Selected web clip")


def _clip_id_from_activation(
    clip_type: str,
    page_url: str,
    value: Mapping[str, object],
) -> str:
    nonce = value.get("activation_id")
    if nonce is not None:
        token = _required_token(nonce, "invalid web clip record")
        return f"clip:{sha256(token.encode('utf-8')).hexdigest()[:32]}"
    digest = sha256(
        "\x1f".join(
            (
                "safari",
                clip_type,
                page_url,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"clip:{digest[:32]}"


def _revision_id(clip_type: str, page_url: str, title: str, text: str) -> str:
    return sha256(
        "\x1f".join(("safari", clip_type, page_url, title, text)).encode("utf-8")
    ).hexdigest()[:32]


def _required_clip_id(value: object) -> str:
    text = _required_token(value, "invalid web clip record")
    if _CLIP_ID.fullmatch(text) is None:
        raise ConnectorContractError("invalid web clip record")
    return text


def _required_browser_id(value: object) -> str:
    text = _required_token(value, "invalid web clip record")
    if _BROWSER_ID.fullmatch(text) is None:
        raise ConnectorContractError("invalid web clip record")
    return text


def _required_clip_type(value: object) -> str:
    text = _required_token(value, "invalid web clip record")
    if _RESOURCE_TYPE.fullmatch(text) is None:
        raise ConnectorContractError("invalid web clip record")
    return text


def _required_url(value: object) -> str:
    if type(value) is not str or _URL.fullmatch(value) is None:
        raise ConnectorContractError("invalid web clip record")
    return value


def _typed_int(value: object) -> int:
    if type(value) is not int:
        raise ConnectorContractError("invalid web clip checkpoint")
    return value


def _typed_str(value: object) -> str:
    if type(value) is not str:
        raise ConnectorContractError("invalid web clip checkpoint")
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
        raise ConnectorContractError("invalid web clip record")
    return body.strip() or fallback


def _require_redaction_clean(body: str) -> None:
    try:
        redaction_found = has_redaction_finding(body)
    except ValueError as error:
        raise ConnectorContractError("invalid web clip record") from error
    if redaction_found:
        raise ConnectorContractError("invalid web clip record")
