from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_http import LiveHttpTransport
from open_brain_connectors.runtime.slack_auth import SlackAuth, _credential_reference
from open_brain_connectors.runtime.slack_live import SlackSourceClient
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


class _Credentials:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, object]] = {}

    def get(self, reference: str) -> dict[str, object] | None:
        return self.values.get(reference)

    def set(self, reference: str, value: dict[str, object]) -> None:
        self.values[reference] = value

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)

    def status(self, reference: str) -> str:
        return "available" if reference in self.values else "missing"


class _Response:
    def __init__(
        self, status: int, payload: dict[str, object], retry_after: int | None = None
    ) -> None:
        self.status = status
        self._payload = payload
        self._retry_after = retry_after

    def json(self) -> dict[str, object]:
        return self._payload

    def retry_after_seconds(self) -> int | None:
        return self._retry_after


class _Transport:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        max_bytes: int = 1_048_576,
        timeout_seconds: float = 30,
    ) -> _Response:
        del method, headers, form, max_bytes, timeout_seconds
        self.urls.append(url)
        return self.responses.pop(0)


def test_selected_channel_fetch_resumes_thread_pagination_without_duplicate_parent(
    tmp_path: Path,
) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "messages": [
                        {
                            "ts": "1760000000.000001",
                            "user": "UONE",
                            "text": "root",
                            "reply_count": 2,
                        }
                    ],
                    "ok": True,
                    "response_metadata": {"next_cursor": "history-next"},
                },
            ),
            _Response(
                200,
                {
                    "messages": [
                        {
                            "ts": "1760000000.000001",
                            "user": "UONE",
                            "text": "root",
                            "reply_count": 2,
                        },
                        {
                            "ts": "1760000001.000001",
                            "thread_ts": "1760000000.000001",
                            "user": "UTWO",
                            "text": "reply",
                        },
                    ],
                    "ok": True,
                    "response_metadata": {"next_cursor": "replies-next"},
                },
            ),
            _Response(
                200,
                {
                    "messages": [
                        {
                            "ts": "1760000002.000001",
                            "thread_ts": "1760000000.000001",
                            "user": "UTHREE",
                            "text": "reply two",
                        },
                    ],
                    "ok": True,
                    "response_metadata": {"next_cursor": ""},
                },
            ),
        ]
    )
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))

    first = client.fetch(selection, {"date_floor": "2025-01-01T00:00:00Z"}, None)
    second = client.fetch(selection, {"date_floor": "2025-01-01T00:00:00Z"}, first.checkpoint)

    assert [intake.text for intake in first.intakes] == ["root", "reply"]
    assert [intake.key.external_id for intake in first.intakes] == [
        "message:1760000000.000001",
        "reply:1760000000.000001:1760000001.000001",
    ]
    assert [intake.text for intake in second.intakes] == ["reply two"]
    assert second.checkpoint["history_cursor"] == "history-next"
    assert second.checkpoint["thread"] is None
    assert "conversations.history" in transport.urls[0]
    assert "conversations.replies" in transport.urls[1]
    assert "cursor=replies-next" in transport.urls[2]


def test_channel_discovery_and_rate_limit_are_bounded(tmp_path: Path) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "channels": [
                        {"id": "COPENBRAIN", "name": "open-brain", "is_archived": False},
                        {"id": "CARCHIVED", "name": "old", "is_archived": True},
                    ],
                    "ok": True,
                    "response_metadata": {"next_cursor": "channels-next"},
                },
            ),
            _Response(429, {}, retry_after=42),
        ]
    )
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))

    page = client.resources(selection.connection_id)
    assert [resource.resource_id for resource in page.resources] == ["channel:COPENBRAIN"]
    assert page.next_cursor == "channels-next"
    with pytest.raises(LiveSourceError, match="rate_limited") as error:
        client.fetch(selection, {"date_floor": "2025-01-01T00:00:00Z"}, None)
    assert error.value.retry_after_seconds == 42


def test_candidate_discovery_scores_metadata_resumes_history_and_never_creates_intakes(
    tmp_path: Path,
) -> None:
    _, auth = _selection_and_auth(tmp_path)
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "channels": [
                        {
                            "id": "COPENBRAIN",
                            "name": "open-brain",
                            "topic": {"value": "priority project"},
                        }
                    ],
                    "ok": True,
                    "response_metadata": {"next_cursor": ""},
                },
            ),
            _Response(
                200,
                {
                    "messages": [{"ts": "1760000000.000001"}],
                    "ok": True,
                    "response_metadata": {"next_cursor": "history-next"},
                },
            ),
            _Response(
                200,
                {
                    "messages": [{"ts": "1760000001.000001"}],
                    "ok": True,
                    "response_metadata": {"next_cursor": ""},
                },
            ),
        ]
    )
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))
    policy = {
        "activity_weight": 1,
        "allowlist": [],
        "keyword_weight": 20,
        "keywords": ["priority"],
        "lookback_seconds": 86_400,
        "threshold": 20,
    }

    listed = client.discover(auth.accounts()[0].connection_id, policy, None)
    counted = client.discover(auth.accounts()[0].connection_id, policy, listed.checkpoint)
    completed = client.discover(auth.accounts()[0].connection_id, policy, counted.checkpoint)

    assert listed.fetch_candidates == () and listed.suggestions == ()
    assert counted.has_more and counted.suggestions == ()
    assert completed.checkpoint is None and completed.fetch_candidates == ()
    assert completed.suggestions == (
        {
            "channel_id": "COPENBRAIN",
            "keyword_hits": 1,
            "message_count": 2,
            "name": "open-brain",
            "score": 22,
            "topic": "priority project",
        },
    )
    assert "cursor=history-next" in transport.urls[2]


def test_candidate_discovery_returns_allowlisted_channel_without_suggestion(tmp_path: Path) -> None:
    _, auth = _selection_and_auth(tmp_path)
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "channels": [{"id": "COPENBRAIN", "name": "open-brain"}],
                    "ok": True,
                    "response_metadata": {"next_cursor": ""},
                },
            ),
            _Response(
                200,
                {"messages": [], "ok": True, "response_metadata": {"next_cursor": ""}},
            ),
        ]
    )
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))
    policy = {
        "activity_weight": 1,
        "allowlist": ["COPENBRAIN"],
        "keyword_weight": 20,
        "keywords": [],
        "lookback_seconds": 86_400,
        "threshold": 20,
    }

    listed = client.discover(auth.accounts()[0].connection_id, policy, None)
    completed = client.discover(auth.accounts()[0].connection_id, policy, listed.checkpoint)

    assert completed.suggestions == ()
    assert completed.fetch_candidates[0]["channel_id"] == "COPENBRAIN"


def test_fetch_rejects_any_channel_or_option_outside_the_selected_source(tmp_path: Path) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, _Transport([])))
    wrong = SourceResourceSelection("slack", selection.connection_id, "not-a-channel", "channel")

    with pytest.raises(LiveSourceError, match="invalid_options"):
        client.fetch(selection, {"date_floor": "2025-01-01T00:00:00Z", "limit": 2}, None)
    with pytest.raises(LiveSourceError, match="invalid_selection"):
        client.fetch(wrong, {"date_floor": "2025-01-01T00:00:00Z"}, None)


def test_complete_frozen_scan_reconciles_missing_message_and_reply(tmp_path: Path) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    initial_transport = _Transport(
        [
            _Response(
                200,
                {
                    "messages": [
                        {
                            "ts": "1760000000.000001",
                            "user": "UONE",
                            "text": "root",
                            "reply_count": 1,
                        }
                    ],
                    "ok": True,
                    "response_metadata": {"next_cursor": ""},
                },
            ),
            _Response(
                200,
                {
                    "messages": [
                        {
                            "ts": "1760000000.000001",
                            "user": "UONE",
                            "text": "root",
                            "reply_count": 1,
                        },
                        {
                            "ts": "1760000001.000001",
                            "thread_ts": "1760000000.000001",
                            "user": "UTWO",
                            "text": "reply",
                        },
                    ],
                    "ok": True,
                    "response_metadata": {"next_cursor": ""},
                },
            ),
        ]
    )
    initial = SlackSourceClient(auth, http=cast(LiveHttpTransport, initial_transport)).fetch(
        selection, {"date_floor": "2025-01-01T00:00:00Z"}, None
    )
    assert len(initial.intakes) == 2
    initial_scan = cast(dict[str, object], initial.checkpoint["scan"])
    assert initial_scan["cutoff"] is None

    omission = SlackSourceClient(
        auth,
        http=cast(
            LiveHttpTransport,
            _Transport(
                [
                    _Response(
                        200,
                        {"messages": [], "ok": True, "response_metadata": {"next_cursor": ""}},
                    )
                ]
            ),
        ),
    ).fetch(selection, {"date_floor": "2025-01-01T00:00:00Z"}, initial.checkpoint)

    assert [intake.key.external_id for intake in omission.intakes] == [
        "message:1760000000.000001",
        "reply:1760000000.000001:1760000001.000001",
    ]
    assert all("no longer available" in intake.text for intake in omission.intakes)
    assert omission.notices == ("message_deleted",)
    omission_scan = cast(dict[str, object], omission.checkpoint["scan"])
    assert omission_scan["known"] == []


def test_failed_or_partial_scan_cannot_reconcile_absence_and_keeps_checkpoint(
    tmp_path: Path,
) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    baseline = SlackSourceClient(
        auth,
        http=cast(
            LiveHttpTransport,
            _Transport(
                [
                    _Response(
                        200,
                        {
                            "messages": [
                                {"ts": "1760000000.000001", "user": "UONE", "text": "root"}
                            ],
                            "ok": True,
                            "response_metadata": {"next_cursor": ""},
                        },
                    )
                ]
            ),
        ),
    ).fetch(selection, {"date_floor": "2025-01-01T00:00:00Z"}, None)
    failing = SlackSourceClient(
        auth,
        http=cast(LiveHttpTransport, _Transport([_Response(429, {}, retry_after=17)])),
    )

    with pytest.raises(LiveSourceError, match="rate_limited") as error:
        failing.fetch(selection, {"date_floor": "2025-01-01T00:00:00Z"}, baseline.checkpoint)

    assert error.value.retry_after_seconds == 17
    baseline_scan = cast(dict[str, object], baseline.checkpoint["scan"])
    assert baseline_scan["known"]


def test_scan_cutoff_stays_frozen_across_history_cursor_pages(tmp_path: Path) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "messages": [{"ts": "1760000002.000001", "user": "UONE", "text": "new"}],
                    "ok": True,
                    "response_metadata": {"next_cursor": "older"},
                },
            ),
            _Response(
                200,
                {
                    "messages": [{"ts": "1760000001.000001", "user": "UONE", "text": "old"}],
                    "ok": True,
                    "response_metadata": {"next_cursor": ""},
                },
            ),
        ]
    )
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))
    first = client.fetch(selection, {"date_floor": "2025-01-01T00:00:00Z"}, None)
    client.fetch(selection, {"date_floor": "2025-01-01T00:00:00Z"}, first.checkpoint)

    first_latest = transport.urls[0].split("latest=", maxsplit=1)[1].split("&", maxsplit=1)[0]
    second_latest = transport.urls[1].split("latest=", maxsplit=1)[1].split("&", maxsplit=1)[0]
    assert first_latest == second_latest
    assert "cursor=older" in transport.urls[1]


def test_source_client_uses_the_bounded_live_transport_by_default(tmp_path: Path) -> None:
    _, auth = _selection_and_auth(tmp_path)
    client = SlackSourceClient(auth)

    assert isinstance(client._http, LiveHttpTransport)


def _selection_and_auth(tmp_path: Path) -> tuple[SourceResourceSelection, SlackAuth]:
    credentials = _Credentials()
    connection_id = "account:" + "a" * 64
    root = tmp_path / "state"
    reference = _credential_reference(connection_id, root)
    credentials.set(
        reference,
        {
            "access_token": "synthetic-access-token",
            "client_id": "123456789.123456789",
            "expires_at_epoch": 4_000_000_000,
            "refresh_token": "synthetic-refresh-token",
        },
    )
    root.mkdir(mode=0o700)
    metadata_path = root / ("slack-account-" + "a" * 64 + ".json")
    metadata_path.write_text(
        json.dumps(
            {
                "account": {
                    "connection_id": connection_id,
                    "display_name": "Slack",
                    "expires_at_epoch": 4_000_000_000,
                    "provider": "slack",
                    "scopes": [
                        "channels:history",
                        "channels:read",
                        "groups:history",
                        "groups:read",
                    ],
                },
                "credential_ref": reference,
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(metadata_path, 0o600)
    selection = SourceResourceSelection("slack", connection_id, "channel:COPENBRAIN", "channel")
    return selection, SlackAuth(root, credentials=credentials)
