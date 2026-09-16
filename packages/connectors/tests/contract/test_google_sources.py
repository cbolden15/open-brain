from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import cast

import pytest

from open_brain_connectors.runtime.google_sources import DriveSourceClient, GmailSourceClient
from open_brain_connectors.runtime.google_sources_auth import GoogleSourcesAuth
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_http import LiveHttpResponse, LiveHttpTransport
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


class _Credentials:
    def get(self, reference: str) -> dict[str, object] | None:
        del reference
        return None

    def set(self, reference: str, value: dict[str, object]) -> None:
        del reference, value

    def delete(self, reference: str) -> None:
        del reference

    def status(self, reference: str) -> str:
        del reference
        return "missing"


class _Http(LiveHttpTransport):
    def __init__(self, responses: list[LiveHttpResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def request(self, method: str, url: str, **kwargs: object) -> LiveHttpResponse:
        del method, kwargs
        self.urls.append(url)
        return self.responses.pop(0)


def _json(status: int, value: dict[str, object]) -> LiveHttpResponse:
    return LiveHttpResponse(status, {}, json.dumps(value).encode())


def _auth(tmp_path: Path, provider: str, http: _Http) -> GoogleSourcesAuth:
    auth = GoogleSourcesAuth(provider, tmp_path, credentials=_Credentials(), http=http)
    auth.access_token = lambda connection_id: "synthetic-token"  # type: ignore[method-assign]
    return auth


def _gmail_selection() -> SourceResourceSelection:
    return SourceResourceSelection("gmail", "account:abc", "mail_label:Label_1", "mail_label")


def _drive_selection() -> SourceResourceSelection:
    return SourceResourceSelection("google_drive", "account:abc", "drive_file:file-1", "drive_file")


def _message(message_id: str = "m1") -> dict[str, object]:
    return {
        "historyId": "11",
        "id": message_id,
        "internalDate": "1767312000000",
        "labelIds": ["Label_1"],
        "payload": {
            "body": {},
            "headers": [{"name": "Subject", "value": "Synthetic subject"}],
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "body": {"data": base64.urlsafe_b64encode(b"Plain body").decode()},
                    "mimeType": "text/plain",
                }
            ],
        },
    }


def test_gmail_full_fetch_returns_proposed_history_checkpoint_without_persisting(
    tmp_path: Path,
) -> None:
    http = _Http(
        [
            _json(200, {"historyId": "10"}),
            _json(200, {"messages": [{"id": "m1"}]}),
            _json(200, _message()),
        ]
    )
    client = GmailSourceClient(_auth(tmp_path, "gmail", http), http=http)

    batch = client.fetch(_gmail_selection(), {"date_floor": "2026-01-01"}, None)

    assert len(batch.intakes) == 1
    assert batch.intakes[0].text == "Plain body"
    assert batch.checkpoint == {
        "date_floor": "2026-01-01",
        "history_id": "10",
        "known_ids": ["m1"],
        "label_id": "Label_1",
        "mode": "history",
    }
    assert tuple((tmp_path / "google-gmail").glob("*.json")) == ()


def test_gmail_history_404_falls_back_to_bounded_full_recovery(tmp_path: Path) -> None:
    http = _Http(
        [
            LiveHttpResponse(404, {}, b"{}"),
            _json(200, {"historyId": "20"}),
            _json(200, {"messages": []}),
        ]
    )
    client = GmailSourceClient(_auth(tmp_path, "gmail", http), http=http)

    batch = client.fetch(
        _gmail_selection(),
        {"date_floor": "2026-01-01"},
        {"date_floor": "2026-01-01", "history_id": "10", "label_id": "Label_1", "mode": "history"},
    )

    assert batch.intakes == ()
    assert batch.notices == ("gmail_history_reset",)
    assert batch.checkpoint["history_id"] == "20"


def test_gmail_label_removal_is_a_same_identity_tombstone(tmp_path: Path) -> None:
    http = _Http(
        [
            _json(
                200,
                {
                    "history": [
                        {"labelsRemoved": [{"labelIds": ["Label_1"], "message": {"id": "m1"}}]}
                    ],
                    "historyId": "12",
                },
            )
        ]
    )
    client = GmailSourceClient(_auth(tmp_path, "gmail", http), http=http)
    batch = client.fetch(
        _gmail_selection(),
        {"date_floor": "2026-01-01"},
        {"date_floor": "2026-01-01", "history_id": "10", "label_id": "Label_1", "mode": "history"},
    )

    assert batch.notices == ("gmail_message_removed",)
    assert batch.intakes[0].key.delivery_id().startswith("connector.gmail.")
    assert batch.intakes[0].text == "Message is no longer in the selected Gmail label."


def test_drive_export_and_missing_file_use_one_selected_file(tmp_path: Path) -> None:
    http = _Http(
        [
            _json(
                200,
                {
                    "capabilities": {"canDownload": True},
                    "id": "file-1",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-01-02T00:00:00Z",
                    "name": "Synthetic doc",
                    "trashed": False,
                    "version": "4",
                    "webViewLink": "https://drive.google.com/file/d/file-1/view",
                },
            ),
            LiveHttpResponse(200, {}, b"Document text"),
        ]
    )
    client = DriveSourceClient(_auth(tmp_path, "google_drive", http), http=http)
    batch = client.fetch(_drive_selection(), {}, None)

    assert batch.intakes[0].text == "Document text"
    assert "/export?" in http.urls[1]

    missing = _Http([LiveHttpResponse(404, {}, b"{}")])
    missing_client = DriveSourceClient(
        _auth(tmp_path / "missing", "google_drive", missing), http=missing
    )
    loss = missing_client.fetch(_drive_selection(), {}, None)
    assert loss.notices == ("drive_file_missing",)
    assert (
        loss.intakes[0].text == "Source is no longer available from the selected Google Drive file."
    )


def test_gmail_busy_history_and_expired_history_reconcile_in_bounded_pages(tmp_path: Path) -> None:
    history: dict[str, object] = {
        "history": [{"messagesAdded": [{"message": {"id": f"m{index}"}} for index in range(6)]}],
        "historyId": "20",
    }
    http = _Http([_json(200, history), *[_json(200, _message(f"m{index}")) for index in range(5)]])
    client = GmailSourceClient(_auth(tmp_path, "gmail", http), http=http)
    state: dict[str, object] = {
        "date_floor": "2026-01-01",
        "history_id": "10",
        "known_ids": [],
        "label_id": "Label_1",
        "mode": "history",
    }
    first = client.fetch(_gmail_selection(), {"date_floor": "2026-01-01"}, state)
    assert first.has_more and len(first.intakes) == 5
    assert len(cast(list[object], first.checkpoint["pending"])) == 1
    http.responses.append(_json(200, _message("m5")))
    terminal = client.fetch(_gmail_selection(), {"date_floor": "2026-01-01"}, first.checkpoint)
    assert not terminal.has_more and terminal.checkpoint["history_id"] == "20"

    recovery_http = _Http(
        [
            LiveHttpResponse(404, {}, b"{}"),
            _json(200, {"historyId": "30"}),
            _json(200, {"messages": []}),
        ]
    )
    recovery = GmailSourceClient(
        _auth(tmp_path / "recovery", "gmail", recovery_http), http=recovery_http
    )
    reset = recovery.fetch(
        _gmail_selection(),
        {"date_floor": "2026-01-01"},
        {
            "date_floor": "2026-01-01",
            "history_id": "10",
            "known_ids": ["m1"],
            "label_id": "Label_1",
            "mode": "history",
        },
    )
    assert reset.has_more and reset.checkpoint["mode"] == "reconcile"
    reconciled = recovery.fetch(_gmail_selection(), {"date_floor": "2026-01-01"}, reset.checkpoint)
    assert reconciled.intakes[0].text == "Message is no longer in the selected Gmail label."


def test_drive_auth_quota_and_shared_drive_are_not_false_removals(tmp_path: Path) -> None:
    auth_http = _Http([LiveHttpResponse(401, {}, b"{}")])
    with pytest.raises(LiveSourceError, match="google_auth_required"):
        DriveSourceClient(
            _auth(tmp_path / "auth", "google_drive", auth_http), http=auth_http
        ).fetch(_drive_selection(), {}, None)
    quota = _Http([_json(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})])
    with pytest.raises(LiveSourceError, match="google_rate_limited"):
        DriveSourceClient(_auth(tmp_path / "quota", "google_drive", quota), http=quota).fetch(
            _drive_selection(), {}, None
        )
    shared = _Http([_json(200, {"files": []})])
    DriveSourceClient(_auth(tmp_path / "shared", "google_drive", shared), http=shared).resources(
        "account:abc"
    )
    assert (
        "includeItemsFromAllDrives=true" in shared.urls[0]
        and "supportsAllDrives=true" in shared.urls[0]
    )
