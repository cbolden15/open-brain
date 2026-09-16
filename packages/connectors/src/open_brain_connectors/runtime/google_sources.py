"""Bounded read-only Gmail and Google Drive live-source clients."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import cast
from urllib.parse import quote, urlencode

from open_brain_connectors.runtime.google_sources_auth import GoogleSourcesAuth
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveResource,
    LiveResourcePage,
    LiveSourceError,
    local_source_privacy,
    safe_text,
)
from open_brain_connectors.runtime.live_http import LiveHttpResponse, LiveHttpTransport
from open_brain_connectors.runtime.mail_drive import (
    DriveFileRecord,
    DriveSourceAdapter,
    GmailSourceAdapter,
    MailRecord,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = ["DriveSourceClient", "GmailSourceClient"]

_GMAIL_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
_DRIVE_ROOT = "https://www.googleapis.com/drive/v3"
_MAX_TEXT = 60_000
_MAX_PAGE = 25
_GMAIL_BATCH_MESSAGES = 5
_GMAIL_TRACKED_IDS = 200
_DRIVE_FIELDS = (
    "id,name,mimeType,webViewLink,trashed,capabilities/canDownload,modifiedTime,version,md5Checksum"
)
_WORKSPACE_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
_TEXT_MIME_TYPES = frozenset({"application/json", "application/xml"})


class GmailSourceClient:
    """Discover labels and fetch one selected label without persisting cursors."""

    def __init__(self, auth: GoogleSourcesAuth, *, http: LiveHttpTransport | None = None) -> None:
        if type(auth) is not GoogleSourcesAuth or auth.provider != "gmail":
            raise LiveSourceError("gmail_invalid_auth")
        self._auth = auth
        self._http = http or auth._http
        self._adapter = GmailSourceAdapter()

    def resources(self, connection_id: str, *, cursor: str | None = None) -> LiveResourcePage:
        if cursor is not None:
            raise LiveSourceError("gmail_invalid_cursor")
        payload = self._json(connection_id, "GET", f"{_GMAIL_ROOT}/labels")
        labels = _items(payload, "labels", maximum=100)
        resources: list[LiveResource] = []
        for label in labels:
            label_id = _text(label.get("id"), maximum=300)
            name = _text(label.get("name"), maximum=512)
            resources.append(LiveResource(f"mail_label:{label_id}", name, "mail_label"))
        return LiveResourcePage(tuple(resources))

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        label_id = _gmail_selection(selection)
        floor = _date_floor(options)
        state = _gmail_state(checkpoint, label_id, floor)
        if state["mode"] == "reconcile":
            return self._reconcile(selection, state)
        if state["mode"] == "history":
            return self._history(selection, label_id, floor, state)
        return self._full(selection, label_id, floor, state)

    def _full(
        self,
        selection: SourceResourceSelection,
        label_id: str,
        floor: date,
        state: dict[str, object],
        *,
        reset_notice: bool = False,
    ) -> LiveBatch:
        start_history = state.get("start_history_id")
        if start_history is None:
            profile = self._json(selection.connection_id, "GET", f"{_GMAIL_ROOT}/profile")
            start_history = _text(profile.get("historyId"), maximum=512)
        query = {
            "labelIds": label_id,
            "maxResults": str(_GMAIL_BATCH_MESSAGES),
            "q": f"after:{floor.isoformat()}",
        }
        page_token = state.get("page_token")
        if page_token is not None:
            query["pageToken"] = cast(str, page_token)
        payload = self._json(
            selection.connection_id, "GET", f"{_GMAIL_ROOT}/messages?{urlencode(query)}"
        )
        messages = _items(payload, "messages", maximum=_GMAIL_BATCH_MESSAGES)
        intakes, notices = self._message_intakes(selection, label_id, floor, messages)
        seen = _checkpoint_ids(state.get("seen_ids", [])) | {
            _text(item.get("id"), maximum=512) for item in messages
        }
        known = _checkpoint_ids(state.get("known_ids", []))
        next_token = _optional_text(payload.get("nextPageToken"), maximum=8_192)
        next_state: dict[str, object] = {
            "label_id": label_id,
            "mode": "full",
            "start_history_id": start_history,
            "date_floor": floor.isoformat(),
            "known_ids": sorted(known),
            "seen_ids": sorted(seen),
        }
        if next_token is not None:
            next_state["page_token"] = next_token
        else:
            missing = known - seen if state.get("recovery") is True else set()
            next_state = {
                "date_floor": floor.isoformat(),
                "history_id": start_history,
                "label_id": label_id,
                "mode": "history",
                "known_ids": sorted(seen),
            }
            if missing:
                next_state = {
                    "date_floor": floor.isoformat(),
                    "history_id": start_history,
                    "label_id": label_id,
                    "known_ids": sorted(known),
                    "mode": "reconcile",
                    "pending_removed": sorted(missing),
                }
        if reset_notice:
            notices.add("gmail_history_reset")
        return LiveBatch(
            tuple(intakes),
            next_state,
            has_more=next_token is not None or next_state["mode"] == "reconcile",
            notices=tuple(sorted(notices)),
        )

    def _history(
        self,
        selection: SourceResourceSelection,
        label_id: str,
        floor: date,
        state: dict[str, object],
    ) -> LiveBatch:
        if state.get("pending"):
            return self._pending_history(selection, label_id, floor, state)
        query = {
            "historyTypes": ["messageAdded", "labelAdded", "labelRemoved", "messageDeleted"],
            "labelId": label_id,
            "maxResults": str(_GMAIL_BATCH_MESSAGES),
            "startHistoryId": cast(str, state["history_id"]),
        }
        if state.get("page_token") is not None:
            query["pageToken"] = cast(str, state["page_token"])
        response = self._response(
            selection.connection_id,
            "GET",
            f"{_GMAIL_ROOT}/history?{urlencode(query, doseq=True)}",
        )
        if response.status == 404:
            return self._full(
                selection,
                label_id,
                floor,
                {
                    "mode": "full",
                    "label_id": label_id,
                    "date_floor": floor.isoformat(),
                    "known_ids": state.get("known_ids", []),
                    "recovery": True,
                    "seen_ids": [],
                },
                reset_notice=True,
            )
        payload = _response_json(response)
        history = _items(payload, "history", maximum=_GMAIL_BATCH_MESSAGES)
        changed, removed = _history_messages(history, label_id)
        pending = [
            {"id": message_id, "selected": message_id in changed}
            for message_id in sorted(changed | removed)
        ]
        history_id = _text(payload.get("historyId"), maximum=512)
        pending_state = dict(state)
        pending_state.update(
            {
                "page_history_id": history_id,
                "pending": pending,
            }
        )
        page_token = _optional_text(payload.get("nextPageToken"), maximum=8_192)
        if page_token is None:
            pending_state.pop("page_token", None)
        else:
            pending_state["page_token"] = page_token
        return self._pending_history(selection, label_id, floor, pending_state)

    def _pending_history(
        self,
        selection: SourceResourceSelection,
        label_id: str,
        floor: date,
        state: dict[str, object],
    ) -> LiveBatch:
        pending = _pending_ids(state.get("pending"))
        current, remaining = pending[:_GMAIL_BATCH_MESSAGES], pending[_GMAIL_BATCH_MESSAGES:]
        intakes: list[SourceRecordIntake] = []
        notices: set[str] = set()
        known = _checkpoint_ids(state.get("known_ids", []))
        for message_id, selected in current:
            if selected:
                intake, message_notices = self._message_intake(
                    selection, label_id, floor, message_id
                )
                notices.update(message_notices)
                if intake is not None:
                    intakes.append(intake)
                    if "gmail_message_removed" in message_notices:
                        known.discard(message_id)
                    else:
                        known.add(message_id)
            else:
                intakes.append(
                    _gmail_tombstone(
                        selection, message_id, _text(state["page_history_id"], maximum=512)
                    )
                )
                known.discard(message_id)
                notices.add("gmail_message_removed")
        _bounded_ids(known)
        if remaining:
            next_state = dict(state)
            next_state["known_ids"] = sorted(known)
            next_state["pending"] = [
                {"id": message_id, "selected": selected} for message_id, selected in remaining
            ]
            return LiveBatch(
                tuple(intakes), next_state, has_more=True, notices=tuple(sorted(notices))
            )
        next_token = state.get("page_token")
        committed_state: dict[str, object] = {
            "date_floor": floor.isoformat(),
            "history_id": state["page_history_id"] if next_token is None else state["history_id"],
            "label_id": label_id,
            "mode": "history",
            "known_ids": sorted(known),
        }
        if next_token is not None:
            committed_state["page_token"] = next_token
        return LiveBatch(
            tuple(intakes),
            committed_state,
            has_more=next_token is not None,
            notices=tuple(sorted(notices)),
        )

    def _reconcile(self, selection: SourceResourceSelection, state: dict[str, object]) -> LiveBatch:
        pending = sorted(_checkpoint_ids(state["pending_removed"]))
        current, remaining = pending[:_GMAIL_BATCH_MESSAGES], pending[_GMAIL_BATCH_MESSAGES:]
        known = _checkpoint_ids(state["known_ids"])
        intakes = tuple(
            _gmail_tombstone(selection, message_id, _text(state["history_id"], maximum=512))
            for message_id in current
        )
        known.difference_update(current)
        _bounded_ids(known)
        if remaining:
            next_state = dict(state)
            next_state["known_ids"] = sorted(known)
            next_state["pending_removed"] = sorted(remaining)
            return LiveBatch(intakes, next_state, has_more=True, notices=("gmail_message_removed",))
        return LiveBatch(
            intakes,
            {
                "date_floor": state["date_floor"],
                "history_id": state["history_id"],
                "label_id": state["label_id"],
                "known_ids": sorted(known),
                "mode": "history",
            },
            notices=("gmail_message_removed",),
        )

    def _message_intakes(
        self,
        selection: SourceResourceSelection,
        label_id: str,
        floor: date,
        messages: tuple[dict[str, object], ...],
    ) -> tuple[list[SourceRecordIntake], set[str]]:
        result: list[SourceRecordIntake] = []
        notices: set[str] = set()
        for item in messages:
            message_id = _text(item.get("id"), maximum=512)
            intake, message_notices = self._message_intake(selection, label_id, floor, message_id)
            notices.update(message_notices)
            if intake is not None:
                result.append(intake)
        return result, notices

    def _message_intake(
        self,
        selection: SourceResourceSelection,
        label_id: str,
        floor: date,
        message_id: str,
    ) -> tuple[SourceRecordIntake | None, set[str]]:
        response = self._response(
            selection.connection_id,
            "GET",
            f"{_GMAIL_ROOT}/messages/{quote(message_id, safe='')}?format=full",
        )
        if response.status == 404:
            return _gmail_tombstone(selection, message_id, "gone"), {"gmail_message_removed"}
        payload = _response_json(response)
        if _text(payload.get("id"), maximum=512) != message_id:
            raise LiveSourceError("gmail_invalid_response")
        label_ids = _string_list(payload.get("labelIds"), maximum=100)
        if label_id not in label_ids:
            return _gmail_tombstone(
                selection, message_id, _text(payload.get("historyId"), maximum=512)
            ), {"gmail_message_removed"}
        if _gmail_date(payload.get("internalDate")) < floor:
            return None, set()
        subject = _gmail_subject(payload.get("payload"))
        text, attachment_seen = _mime_text(payload.get("payload"))
        notices: set[str] = set()
        if attachment_seen:
            notices.add("gmail_attachments_omitted")
        if text is None:
            notices.add("gmail_plain_text_unavailable")
            return None, notices
        revision = _gmail_revision(payload, label_ids)
        record = MailRecord(
            connector_name="gmail",
            resource_id=label_id,
            message_id=message_id,
            revision_id=revision,
            web_url=_gmail_url(message_id),
            subject=subject,
            body_text=text,
        )
        try:
            return self._adapter.intake(selection, record, privacy=local_source_privacy()), notices
        except Exception as error:
            raise LiveSourceError("gmail_invalid_response") from error

    def _response(self, connection_id: str, method: str, url: str) -> LiveHttpResponse:
        return self._http.request(
            method,
            url,
            headers={
                "authorization": f"Bearer {self._auth.access_token(connection_id)}",
                "accept": "application/json",
            },
            max_bytes=1_048_576,
        )

    def _json(self, connection_id: str, method: str, url: str) -> dict[str, object]:
        return _response_json(self._response(connection_id, method, url))


class DriveSourceClient:
    """Discover files and fetch exactly one selected text-like Drive file."""

    def __init__(self, auth: GoogleSourcesAuth, *, http: LiveHttpTransport | None = None) -> None:
        if type(auth) is not GoogleSourcesAuth or auth.provider != "google_drive":
            raise LiveSourceError("drive_invalid_auth")
        self._auth = auth
        self._http = http or auth._http
        self._adapter = DriveSourceAdapter()

    def resources(self, connection_id: str, *, cursor: str | None = None) -> LiveResourcePage:
        query = {
            "fields": f"nextPageToken,files({_DRIVE_FIELDS})",
            "includeItemsFromAllDrives": "true",
            "pageSize": "100",
            "q": "trashed = false",
            "supportsAllDrives": "true",
        }
        if cursor is not None:
            query["pageToken"] = _text(cursor, maximum=8_192)
        payload = self._json(connection_id, "GET", f"{_DRIVE_ROOT}/files?{urlencode(query)}")
        files = _items(payload, "files", maximum=100)
        resources = tuple(
            LiveResource(
                f"drive_file:{_text(file.get('id'), maximum=512)}",
                _text(file.get("name"), maximum=512),
                "drive_file",
            )
            for file in files
        )
        return LiveResourcePage(
            resources, _optional_text(payload.get("nextPageToken"), maximum=8_192)
        )

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        file_id = _drive_selection(selection)
        if type(options) is not dict or options:
            raise LiveSourceError("drive_invalid_options")
        _drive_state(checkpoint, file_id)
        fields = _DRIVE_FIELDS
        metadata_query = urlencode({"fields": fields, "supportsAllDrives": "true"})
        response = self._response(
            selection.connection_id,
            "GET",
            f"{_DRIVE_ROOT}/files/{quote(file_id, safe='')}?{metadata_query}",
        )
        if response.status == 404:
            return _drive_loss(selection, file_id, "missing", "drive_file_missing")
        if response.status == 401:
            raise LiveSourceError("google_auth_required")
        if response.status == 403:
            error = _drive_forbidden(response)
            if error.code == "drive_access_lost":
                return _drive_loss(selection, file_id, "access_lost", error.code)
            raise error
        metadata = _response_json(response)
        if _text(metadata.get("id"), maximum=512) != file_id:
            raise LiveSourceError("drive_invalid_response")
        revision = _drive_revision(metadata)
        base_checkpoint: dict[str, object] = {
            "file_id": file_id,
            "revision": revision,
            "status": "available",
        }
        if metadata.get("trashed") is True:
            return _drive_loss(
                selection, file_id, "trashed", "drive_file_trashed", revision=revision
            )
        capability = metadata.get("capabilities")
        if not isinstance(capability, Mapping) or capability.get("canDownload") is not True:
            return _drive_loss(
                selection, file_id, "access_lost", "drive_download_not_allowed", revision=revision
            )
        mime_type = _text(metadata.get("mimeType"), maximum=256)
        try:
            content = self._content(selection.connection_id, file_id, mime_type)
        except LiveSourceError as error:
            if error.code == "drive_file_missing":
                return _drive_loss(selection, file_id, "missing", error.code, revision=revision)
            if error.code == "drive_access_lost":
                return _drive_loss(selection, file_id, "access_lost", error.code, revision=revision)
            raise
        if content is None:
            return LiveBatch((), base_checkpoint, notices=("drive_unsupported_type",))
        name = _text(metadata.get("name"), maximum=200)
        url = _drive_url(metadata, file_id)
        record = DriveFileRecord(
            file_id=file_id,
            revision_id=revision,
            web_url=url,
            name=name,
            text=content,
            mime_type=mime_type,
        )
        try:
            intake = self._adapter.intake(selection, record, privacy=local_source_privacy())
        except Exception as error:
            raise LiveSourceError("drive_invalid_response") from error
        return LiveBatch((intake,), base_checkpoint)

    def _content(self, connection_id: str, file_id: str, mime_type: str) -> str | None:
        if mime_type.startswith("text/") or mime_type in _TEXT_MIME_TYPES:
            url = f"{_DRIVE_ROOT}/files/{quote(file_id, safe='')}?alt=media&supportsAllDrives=true"
        elif mime_type in _WORKSPACE_EXPORTS:
            export_query = urlencode(
                {"mimeType": _WORKSPACE_EXPORTS[mime_type], "supportsAllDrives": "true"}
            )
            url = f"{_DRIVE_ROOT}/files/{quote(file_id, safe='')}/export?{export_query}"
        else:
            return None
        response = self._response(connection_id, "GET", url, accept="text/plain, text/csv")
        if response.status == 404:
            raise LiveSourceError("drive_file_missing")
        if response.status == 401:
            raise LiveSourceError("google_auth_required")
        if response.status == 403:
            raise _drive_forbidden(response)
        if response.status >= 400:
            raise _provider_error(response)
        if len(response.body) > _MAX_TEXT * 4:
            raise LiveSourceError("drive_content_too_large")
        try:
            text = response.body.decode("utf-8")
        except UnicodeDecodeError:
            raise LiveSourceError("drive_unsupported_type") from None
        if not text or len(text) > _MAX_TEXT or "\x00" in text:
            raise LiveSourceError("drive_content_too_large")
        return text

    def _response(
        self, connection_id: str, method: str, url: str, *, accept: str = "application/json"
    ) -> LiveHttpResponse:
        return self._http.request(
            method,
            url,
            headers={
                "authorization": f"Bearer {self._auth.access_token(connection_id)}",
                "accept": accept,
            },
            max_bytes=1_048_576,
        )

    def _json(self, connection_id: str, method: str, url: str) -> dict[str, object]:
        return _response_json(self._response(connection_id, method, url))


def _response_json(response: LiveHttpResponse) -> dict[str, object]:
    if response.status >= 400:
        raise _provider_error(response)
    return response.json()


def _provider_error(response: LiveHttpResponse) -> LiveSourceError:
    if response.status == 401:
        return LiveSourceError("google_auth_required")
    if response.status == 403:
        return LiveSourceError("google_not_allowed")
    if response.status == 429:
        return LiveSourceError(
            "google_rate_limited", retry_after_seconds=response.retry_after_seconds()
        )
    return LiveSourceError("google_fetch_failed")


def _drive_forbidden(response: LiveHttpResponse) -> LiveSourceError:
    try:
        error = response.json().get("error")
    except LiveSourceError:
        return LiveSourceError("drive_forbidden")
    if not isinstance(error, Mapping):
        return LiveSourceError("drive_forbidden")
    errors = error.get("errors")
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], Mapping):
        return LiveSourceError("drive_forbidden")
    reason = errors[0].get("reason")
    if reason in {"userRateLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded"}:
        return LiveSourceError(
            "google_rate_limited", retry_after_seconds=response.retry_after_seconds()
        )
    if reason in {"insufficientFilePermissions", "fileNotDownloadable"}:
        return LiveSourceError("drive_access_lost")
    return LiveSourceError("drive_forbidden")


def _gmail_selection(selection: SourceResourceSelection) -> str:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != "gmail"
        or selection.resource_type != "mail_label"
    ):
        raise LiveSourceError("gmail_invalid_selection")
    return _prefixed_id(selection.resource_id, "mail_label:", "gmail_invalid_selection")


def _drive_selection(selection: SourceResourceSelection) -> str:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != "google_drive"
        or selection.resource_type != "drive_file"
    ):
        raise LiveSourceError("drive_invalid_selection")
    return _prefixed_id(selection.resource_id, "drive_file:", "drive_invalid_selection")


def _prefixed_id(value: str, prefix: str, code: str) -> str:
    if type(value) is not str or not value.startswith(prefix):
        raise LiveSourceError(code)
    try:
        return safe_text(value.removeprefix(prefix), maximum=512)
    except LiveSourceError:
        raise LiveSourceError(code) from None


def _date_floor(options: dict[str, object]) -> date:
    if (
        type(options) is not dict
        or set(options) != {"date_floor"}
        or type(options["date_floor"]) is not str
    ):
        raise LiveSourceError("gmail_invalid_options")
    try:
        return date.fromisoformat(options["date_floor"])
    except ValueError:
        raise LiveSourceError("gmail_invalid_options") from None


def _gmail_state(
    checkpoint: dict[str, object] | None, label_id: str, floor: date
) -> dict[str, object]:
    if checkpoint is None:
        return {
            "date_floor": floor.isoformat(),
            "known_ids": [],
            "label_id": label_id,
            "mode": "full",
            "seen_ids": [],
        }
    if (
        type(checkpoint) is not dict
        or checkpoint.get("label_id") != label_id
        or checkpoint.get("date_floor") != floor.isoformat()
    ):
        raise LiveSourceError("gmail_invalid_checkpoint")
    mode = checkpoint.get("mode")
    if mode == "full":
        allowed = {
            "mode",
            "label_id",
            "date_floor",
            "start_history_id",
            "page_token",
            "known_ids",
            "seen_ids",
            "recovery",
        }
        if not set(checkpoint).issubset(allowed):
            raise LiveSourceError("gmail_invalid_checkpoint")
        for name in ("start_history_id", "page_token"):
            if name in checkpoint:
                _text(checkpoint[name], maximum=8_192)
        _bounded_ids(_checkpoint_ids(checkpoint.get("known_ids", [])))
        _bounded_ids(_checkpoint_ids(checkpoint.get("seen_ids", [])))
        if "recovery" in checkpoint and checkpoint["recovery"] is not True:
            raise LiveSourceError("gmail_invalid_checkpoint")
        return dict(checkpoint)
    if mode == "history":
        allowed = {
            "mode",
            "label_id",
            "date_floor",
            "history_id",
            "page_token",
            "known_ids",
            "pending",
            "page_history_id",
        }
        if not set(checkpoint).issubset(allowed) or "history_id" not in checkpoint:
            raise LiveSourceError("gmail_invalid_checkpoint")
        _text(checkpoint["history_id"], maximum=512)
        if "page_token" in checkpoint:
            _text(checkpoint["page_token"], maximum=8_192)
        _bounded_ids(_checkpoint_ids(checkpoint.get("known_ids", [])))
        if "pending" in checkpoint:
            _pending_ids(checkpoint["pending"])
            _text(checkpoint.get("page_history_id"), maximum=512)
        return dict(checkpoint)
    if mode == "reconcile":
        allowed = {"mode", "label_id", "date_floor", "history_id", "known_ids", "pending_removed"}
        if not set(checkpoint).issubset(allowed) or "pending_removed" not in checkpoint:
            raise LiveSourceError("gmail_invalid_checkpoint")
        _text(checkpoint.get("history_id"), maximum=512)
        _bounded_ids(_checkpoint_ids(checkpoint.get("known_ids", [])))
        _bounded_ids(_checkpoint_ids(checkpoint["pending_removed"]))
        return dict(checkpoint)
    raise LiveSourceError("gmail_invalid_checkpoint")


def _drive_state(checkpoint: dict[str, object] | None, file_id: str) -> None:
    if checkpoint is None:
        return
    if (
        type(checkpoint) is not dict
        or set(checkpoint) != {"file_id", "revision", "status"}
        or checkpoint.get("file_id") != file_id
    ):
        raise LiveSourceError("drive_invalid_checkpoint")
    _text(checkpoint.get("revision"), maximum=512)
    if checkpoint.get("status") not in {"available", "missing", "trashed", "access_lost"}:
        raise LiveSourceError("drive_invalid_checkpoint")


def _items(
    payload: Mapping[str, object], key: str, *, maximum: int
) -> tuple[dict[str, object], ...]:
    value = payload.get(key, [])
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(item, dict) for item in value)
    ):
        raise LiveSourceError("google_invalid_response")
    return tuple(cast(dict[str, object], item) for item in value)


def _text(value: object, *, maximum: int) -> str:
    try:
        return safe_text(cast(str, value), maximum=maximum)
    except LiveSourceError:
        raise LiveSourceError("google_invalid_response") from None


def _optional_text(value: object, *, maximum: int) -> str | None:
    return None if value is None else _text(value, maximum=maximum)


def _string_list(value: object, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise LiveSourceError("google_invalid_response")
    return tuple(_text(item, maximum=512) for item in value)


def _checkpoint_ids(value: object) -> set[str]:
    if not isinstance(value, list):
        raise LiveSourceError("gmail_invalid_checkpoint")
    result = {_text(item, maximum=512) for item in value}
    if len(result) != len(value):
        raise LiveSourceError("gmail_invalid_checkpoint")
    _bounded_ids(result)
    return result


def _bounded_ids(value: set[str]) -> None:
    if len(value) > _GMAIL_TRACKED_IDS:
        raise LiveSourceError("gmail_reconciliation_too_large")


def _pending_ids(value: object) -> list[tuple[str, bool]]:
    if not isinstance(value, list) or len(value) > _GMAIL_TRACKED_IDS:
        raise LiveSourceError("gmail_invalid_checkpoint")
    result: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"id", "selected"}:
            raise LiveSourceError("gmail_invalid_checkpoint")
        message_id = _text(item.get("id"), maximum=512)
        if type(item.get("selected")) is not bool or message_id in seen:
            raise LiveSourceError("gmail_invalid_checkpoint")
        seen.add(message_id)
        result.append((message_id, cast(bool, item["selected"])))
    return result


def _gmail_date(value: object) -> date:
    try:
        milliseconds = int(_text(value, maximum=32))
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC).date()
    except OverflowError, ValueError, OSError:
        raise LiveSourceError("gmail_invalid_response") from None


def _gmail_subject(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise LiveSourceError("gmail_invalid_response")
    headers = payload.get("headers", [])
    if not isinstance(headers, list):
        raise LiveSourceError("gmail_invalid_response")
    for header in headers:
        if isinstance(header, Mapping) and header.get("name") == "Subject":
            try:
                return _text(header.get("value"), maximum=200)
            except LiveSourceError:
                break
    return "Gmail message"


def _mime_text(payload: object) -> tuple[str | None, bool]:
    if not isinstance(payload, Mapping):
        raise LiveSourceError("gmail_invalid_response")
    parts: list[Mapping[str, object]] = [payload]
    fragments: list[str] = []
    attachment_seen = False
    while parts:
        part = parts.pop()
        children = part.get("parts", [])
        if children is not None:
            if not isinstance(children, list) or len(children) > 100:
                raise LiveSourceError("gmail_invalid_response")
            if any(not isinstance(child, Mapping) for child in children):
                raise LiveSourceError("gmail_invalid_response")
            parts.extend(cast(Mapping[str, object], child) for child in children)
        body = part.get("body", {})
        if not isinstance(body, Mapping):
            raise LiveSourceError("gmail_invalid_response")
        if body.get("attachmentId") is not None:
            attachment_seen = True
            continue
        if part.get("mimeType") != "text/plain" or body.get("data") is None:
            continue
        data = _text(body.get("data"), maximum=_MAX_TEXT * 4)
        try:
            decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8")
        except UnicodeDecodeError, ValueError:
            raise LiveSourceError("gmail_invalid_response") from None
        if "\x00" in decoded:
            raise LiveSourceError("gmail_invalid_response")
        fragments.append(decoded)
        if sum(len(value) for value in fragments) > _MAX_TEXT:
            raise LiveSourceError("gmail_content_too_large")
    text = "\n\n".join(fragment for fragment in fragments if fragment.strip()).strip()
    return (text or None), attachment_seen


def _gmail_revision(payload: Mapping[str, object], label_ids: tuple[str, ...]) -> str:
    history_id = _text(payload.get("historyId"), maximum=512)
    digest = hashlib.sha256("\x1f".join(label_ids).encode()).hexdigest()[:16]
    return f"history:{history_id}:{digest}"


def _gmail_url(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{quote(message_id, safe='')}"


def _history_messages(
    history: tuple[dict[str, object], ...], label_id: str
) -> tuple[set[str], set[str]]:
    states: dict[str, bool] = {}
    for item in history:
        for event in _items(item, "messagesAdded", maximum=100):
            _set_history_state(states, event, selected=True)
        for event in _items(item, "labelsAdded", maximum=100):
            if label_id in _string_list(event.get("labelIds"), maximum=100):
                _set_history_state(states, event, selected=True)
        for event in _items(item, "messagesDeleted", maximum=100):
            _set_history_state(states, event, selected=False)
        for event in _items(item, "labelsRemoved", maximum=100):
            if label_id in _string_list(event.get("labelIds"), maximum=100):
                _set_history_state(states, event, selected=False)
    return (
        {message_id for message_id, selected in states.items() if selected},
        {message_id for message_id, selected in states.items() if not selected},
    )


def _set_history_state(
    states: dict[str, bool], event: Mapping[str, object], *, selected: bool
) -> None:
    message = event.get("message")
    if not isinstance(message, Mapping):
        raise LiveSourceError("gmail_invalid_response")
    states[_text(message.get("id"), maximum=512)] = selected


def _gmail_tombstone(
    selection: SourceResourceSelection, message_id: str, revision: str
) -> SourceRecordIntake:
    return SourceRecordIntake(
        key=SourceRecordKey(
            "gmail",
            selection.connection_id,
            selection.resource_id,
            f"message:{message_id}",
            f"unavailable:{revision}",
        ),
        url=_gmail_url(message_id),
        title="Gmail message unavailable",
        text="Message is no longer in the selected Gmail label.",
        privacy=local_source_privacy(),
    )


def _drive_loss(
    selection: SourceResourceSelection,
    file_id: str,
    status: str,
    notice: str,
    *,
    revision: str | None = None,
) -> LiveBatch:
    current_revision = revision or f"unavailable:{status}"
    intake = SourceRecordIntake(
        key=SourceRecordKey(
            "google_drive",
            selection.connection_id,
            selection.resource_id,
            f"file:{file_id}",
            current_revision,
        ),
        url=f"https://drive.google.com/open?id={quote(file_id, safe='')}",
        title="Google Drive file unavailable",
        text="Source is no longer available from the selected Google Drive file.",
        privacy=local_source_privacy(),
    )
    return LiveBatch(
        (intake,),
        {"file_id": file_id, "revision": current_revision, "status": status},
        notices=(notice,),
    )


def _drive_revision(metadata: Mapping[str, object]) -> str:
    parts = [
        _text(metadata.get("modifiedTime"), maximum=128),
        _optional_text(metadata.get("version"), maximum=128) or "",
        _optional_text(metadata.get("md5Checksum"), maximum=128) or "",
        _text(metadata.get("mimeType"), maximum=256),
        _text(metadata.get("name"), maximum=512),
        _optional_text(metadata.get("webViewLink"), maximum=8_192) or "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _drive_url(metadata: Mapping[str, object], file_id: str) -> str:
    link = metadata.get("webViewLink")
    if isinstance(link, str) and link.startswith("https://drive.google.com/"):
        return link
    return f"https://drive.google.com/open?id={quote(file_id, safe='')}"
