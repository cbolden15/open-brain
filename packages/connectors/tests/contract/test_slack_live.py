from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

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


def test_moving_floor_does_not_prove_older_message_deleted(tmp_path: Path) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "ok": True,
                    "messages": [{"ts": "1760000000.000001", "user": "UONE", "text": "old"}],
                },
            ),
            _Response(200, {"ok": True, "messages": []}),
        ]
    )
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))
    initial = client.fetch(selection, {"date_floor": "2025-01-01T00:00:00Z"}, None)
    later = client.fetch(selection, {"date_floor": "2026-01-01T00:00:00Z"}, initial.checkpoint)
    assert later.intakes == ()
    assert later.notices == ()


def test_tracked_old_thread_refresh_freezes_replies_and_reports_partial(tmp_path: Path) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "ok": True,
                    "messages": [
                        {"ts": "1760000000.000001", "user": "UONE", "text": "root"},
                        {
                            "ts": "1760000001.000001",
                            "thread_ts": "1760000000.000001",
                            "user": "UTWO",
                            "text": "new reply",
                        },
                    ],
                },
            )
        ]
    )
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))
    batch = client.refresh_thread(selection, "1760000000.000001")
    assert "latest=" in transport.urls[0]
    assert "partial_thread_coverage" in batch.notices
    assert batch.intakes[1].key.external_id == "reply:1760000000.000001:1760000001.000001"


def test_optional_discovery_disabled_or_unverified_makes_no_requests(tmp_path: Path) -> None:
    from open_brain_connectors.runtime.slack_discovery import SlackMessageDiscoveryClient

    _, auth = _selection_and_auth(tmp_path)
    transport = _Transport([])
    client = SlackMessageDiscoveryClient(auth, http=cast(LiveHttpTransport, transport))
    common: dict[str, Any] = dict(
        owner_id="UOWNER", excluded_conversations=(), oldest="1760000000.000001"
    )
    disabled = client.discover(
        "account:synthetic", capabilities=(), ai_exclusions_verified=True, **common
    )
    excluded = client.discover(
        "account:synthetic", capabilities=("dm",), ai_exclusions_verified=False, **common
    )
    assert disabled.notices == ("discovery_disabled",)
    assert excluded.notices == ("unsupported_ai_exclusions",)
    assert transport.urls == []


def test_search_discovery_hints_overlap_identity_and_exclude_before_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_connectors.runtime.live_common import LiveAccount
    from open_brain_connectors.runtime.slack_auth import slack_user_scopes
    from open_brain_connectors.runtime.slack_discovery import SlackMessageDiscoveryClient

    selection, auth = _selection_and_auth(tmp_path)
    monkeypatch.setattr(
        auth,
        "accounts",
        lambda: (
            LiveAccount(
                "slack", selection.connection_id, "Slack", slack_user_scopes(("mentions",)), None
            ),
        ),
    )
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "ok": True,
                    "messages": {
                        "matches": [
                            {
                                "channel": {"id": "CONE"},
                                "ts": "1760000001.000001",
                                "thread_ts": "1760000000.000001",
                            },
                            {"channel": {"id": "CEXCLUDED"}, "ts": "1760000002.000001"},
                            {"channel": {"id": "DONE", "is_im": True}, "ts": "1760000003.000001"},
                        ],
                        "paging": {"pages": 1},
                    },
                },
            )
        ]
    )
    batch = SlackMessageDiscoveryClient(auth, http=cast(LiveHttpTransport, transport)).discover(
        selection.connection_id,
        owner_id="UOWNER",
        capabilities=("mentions",),
        excluded_conversations=("CEXCLUDED",),
        ai_exclusions_verified=True,
        oldest="1760000000.000001",
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.message_ts == "1760000001.000001"
    assert candidate.thread_ts == "1760000000.000001"
    assert not hasattr(batch, "intakes")
    assert "incomplete_search_coverage" in batch.notices
    assert "conversations.history" not in transport.urls[0]


def test_optional_dm_requires_granted_scopes_before_any_request(tmp_path: Path) -> None:
    from open_brain_connectors.runtime.slack_discovery import SlackMessageDiscoveryClient

    selection, auth = _selection_and_auth(tmp_path)
    transport = _Transport([])
    with pytest.raises(LiveSourceError, match="not_allowed"):
        SlackMessageDiscoveryClient(auth, http=cast(LiveHttpTransport, transport)).discover(
            selection.connection_id,
            owner_id="UOWNER",
            capabilities=("dm",),
            excluded_conversations=(),
            ai_exclusions_verified=True,
            oldest="1760000000.000001",
        )
    assert transport.urls == []


def test_dm_discovery_is_bounded_and_revocation_is_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_connectors.runtime.live_common import LiveAccount
    from open_brain_connectors.runtime.slack_auth import slack_user_scopes
    from open_brain_connectors.runtime.slack_discovery import SlackMessageDiscoveryClient

    selection, auth = _selection_and_auth(tmp_path)
    monkeypatch.setattr(
        auth,
        "accounts",
        lambda: (
            LiveAccount(
                "slack", selection.connection_id, "Slack", slack_user_scopes(("dm",)), None
            ),
        ),
    )
    transport = _Transport(
        [
            _Response(200, {"ok": True, "channels": [{"id": "DONE"}, {"id": "DEXCLUDED"}]}),
            _Response(
                200, {"ok": True, "messages": [{"ts": "1760000001.000001", "text": "synthetic"}]}
            ),
            _Response(200, {"ok": False, "error": "token_revoked"}),
        ]
    )
    client = SlackMessageDiscoveryClient(auth, http=cast(LiveHttpTransport, transport))
    options: dict[str, Any] = dict(
        owner_id="UOWNER",
        capabilities=("dm",),
        excluded_conversations=("DEXCLUDED",),
        ai_exclusions_verified=True,
        oldest="1760000000.000001",
    )
    listing = client.discover(selection.connection_id, **options)
    assert listing.candidates == ()
    batch = client.discover(selection.connection_id, checkpoint=listing.checkpoint, **options)
    assert batch.candidates[0].conversation_id == "DONE"
    assert not batch.has_more
    assert "types=im" in transport.urls[0]
    assert "limit=15" in transport.urls[1]
    with pytest.raises(LiveSourceError, match="auth_required"):
        client.discover(selection.connection_id, **options)


def test_candidate_resolution_uses_exact_reply_identity(tmp_path: Path) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    channel = selection.resource_id.removeprefix("channel:")
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "ok": True,
                    "messages": [
                        {"ts": "1760000001.000001", "user": "UONE", "text": "synthetic reply"},
                    ],
                },
            )
        ]
    )
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))
    record = client.resolve_candidate(
        selection.connection_id, channel, "1760000001.000001", thread_ts="1760000000.000001"
    )
    assert record.thread_ts == "1760000000.000001"
    assert client._intake(selection, record).key.external_id == (
        "reply:1760000000.000001:1760000001.000001"
    )
    assert "conversations.replies" in transport.urls[0]
    assert "inclusive=true" in transport.urls[0]
    assert "oldest=1760000001.000001" in transport.urls[0]


@pytest.mark.parametrize(
    "messages", [[], [{"ts": "1760000002.000001", "user": "UONE", "text": "other"}]]
)
def test_unresolvable_hint_never_synthesizes_root(tmp_path: Path, messages: list[object]) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    transport = _Transport([_Response(200, {"ok": True, "messages": messages})])
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))
    with pytest.raises(LiveSourceError, match="source_candidate_identity_unsupported"):
        client.resolve_candidate(
            selection.connection_id,
            selection.resource_id.removeprefix("channel:"),
            "1760000001.000001",
        )


def test_candidate_resolution_rejects_oversized_provider_page(tmp_path: Path) -> None:
    selection, auth = _selection_and_auth(tmp_path)
    transport = _Transport([_Response(200, {"ok": True, "messages": [{}, {}]})])
    client = SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))
    with pytest.raises(LiveSourceError, match="invalid_provider_response"):
        client.resolve_candidate(
            selection.connection_id,
            selection.resource_id.removeprefix("channel:"),
            "1760000001.000001",
        )


def test_optional_search_freezes_upper_bound_and_refuses_changed_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_connectors.runtime.live_common import LiveAccount
    from open_brain_connectors.runtime.slack_auth import slack_user_scopes
    from open_brain_connectors.runtime.slack_discovery import SlackMessageDiscoveryClient

    selection, auth = _selection_and_auth(tmp_path)
    monkeypatch.setattr(
        auth,
        "accounts",
        lambda: (
            LiveAccount(
                "slack",
                selection.connection_id,
                "Synthetic",
                slack_user_scopes(("mentions",)),
                None,
            ),
        ),
    )
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "ok": True,
                    "messages": {
                        "matches": [
                            {"channel": {"id": "CONE"}, "ts": "1760000001.000001"},
                            {"channel": {"id": "CONE"}, "ts": "1760000003.000001"},
                        ],
                        "paging": {"pages": 2},
                    },
                },
            )
        ]
    )
    client = SlackMessageDiscoveryClient(auth, http=cast(LiveHttpTransport, transport))
    options: dict[str, Any] = {
        "owner_id": "UOWNER",
        "capabilities": ("mentions",),
        "excluded_conversations": (),
        "ai_exclusions_verified": True,
        "oldest": "1760000000.000001",
        "latest": "1760000002.000001",
    }
    batch = client.discover(selection.connection_id, **options)
    assert [item.message_ts for item in batch.candidates] == ["1760000001.000001"]
    assert "before%3A" in transport.urls[0]
    options["latest"] = "1760000004.000001"
    with pytest.raises(LiveSourceError, match="invalid_checkpoint"):
        client.discover(selection.connection_id, checkpoint=batch.checkpoint, **options)
    assert len(transport.urls) == 1
