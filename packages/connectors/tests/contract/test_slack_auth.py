from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_http import LiveHttpTransport
from open_brain_connectors.runtime.live_oauth import OAuthCode
from open_brain_connectors.runtime.slack_auth import SLACK_USER_SCOPES, SlackAuth


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
    def __init__(self, payload: dict[str, object]) -> None:
        self.status = 200
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload

    def retry_after_seconds(self) -> int | None:
        return None


class _Transport:
    def __init__(self) -> None:
        self.forms: list[dict[str, str] | None] = []

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
        del method, url, headers, max_bytes, timeout_seconds
        self.forms.append(form)
        return _Response(_token_payload())


def test_slack_connect_uses_public_pkce_and_keeps_tokens_out_of_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "slack-client.json"
    config.write_text(
        json.dumps(
            {
                "client_id": "123456789.123456789",
                "redirect_uri": "http://localhost:8765/oauth2/callback",
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_loopback(build_url: Callable[[str, str, str], str], **options: object) -> OAuthCode:
        calls.append(options)
        url = build_url("http://localhost:8765/oauth2/callback", "state", "challenge")
        assert (
            "user_scope=channels%3Ahistory%2Cchannels%3Aread%2Cgroups%3Ahistory%2Cgroups%3Aread"
            in url
        )
        assert "client_secret" not in url
        return OAuthCode(
            "synthetic-code", "synthetic-verifier", "http://localhost:8765/oauth2/callback"
        )

    monkeypatch.setattr(
        "open_brain_connectors.runtime.slack_auth.authorize_loopback", fake_loopback
    )
    credentials = _Credentials()
    transport = _Transport()
    auth = SlackAuth(
        tmp_path / "state", credentials=credentials, http=cast(LiveHttpTransport, transport)
    )

    account = auth.connect(config, timeout_seconds=5, browser_opener=lambda _: True)

    assert account.provider == "slack"
    assert account.scopes == SLACK_USER_SCOPES
    assert len(calls) == 1
    assert calls[0]["callback_path"] == "/oauth2/callback"
    assert calls[0]["hostname"] == "localhost"
    assert calls[0]["port"] == 8765
    assert calls[0]["timeout_seconds"] == 5
    assert callable(calls[0]["browser_opener"])
    assert transport.forms == [
        {
            "client_id": "123456789.123456789",
            "code": "synthetic-code",
            "code_verifier": "synthetic-verifier",
            "grant_type": "authorization_code",
            "redirect_uri": "http://localhost:8765/oauth2/callback",
        }
    ]
    metadata = next((tmp_path / "state").glob("slack-account-*.json")).read_text(encoding="utf-8")
    assert "synthetic-access-token" not in metadata
    assert "synthetic-refresh-token" not in metadata
    assert auth.access_token(account.connection_id) == "synthetic-access-token"


def test_slack_auth_rejects_secret_or_unregistered_redirect(tmp_path: Path) -> None:
    config = tmp_path / "slack-client.json"
    config.write_text(
        json.dumps(
            {
                "client_id": "123456789.123456789",
                "client_secret": "must-not-be-accepted",
                "redirect_uri": "http://127.0.0.1:8765/oauth2/callback",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LiveSourceError, match="invalid_client_config"):
        SlackAuth(tmp_path / "state", credentials=_Credentials()).connect(config)


def test_slack_auth_uses_the_bounded_live_transport_by_default(tmp_path: Path) -> None:
    auth = SlackAuth(tmp_path / "state", credentials=_Credentials())

    assert isinstance(auth._http, LiveHttpTransport)


def _token_payload() -> dict[str, object]:
    return {
        "authed_user": {
            "access_token": "synthetic-access-token",
            "expires_in": 43_200,
            "id": "UOPENBRAIN",
            "refresh_token": "synthetic-refresh-token",
            "scope": ",".join(SLACK_USER_SCOPES),
        },
        "ok": True,
        "team": {"id": "TOPENBRAIN", "name": "Synthetic Slack"},
    }


def test_disconnect_keeps_another_brains_same_slack_account_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "client.json"
    config.write_text(
        json.dumps(
            {
                "client_id": "123456789.123456789",
                "redirect_uri": "http://localhost:8765/oauth2/callback",
            }
        )
    )
    monkeypatch.setattr(
        "open_brain_connectors.runtime.slack_auth.authorize_loopback",
        lambda *_a, **_k: OAuthCode("code", "verifier", "http://localhost:8765/oauth2/callback"),
    )
    credentials = _Credentials()
    first = SlackAuth(
        tmp_path / "first", credentials=credentials, http=cast(LiveHttpTransport, _Transport())
    )
    second = SlackAuth(
        tmp_path / "second", credentials=credentials, http=cast(LiveHttpTransport, _Transport())
    )
    one, two = first.connect(config), second.connect(config)
    assert one.connection_id == two.connection_id and len(credentials.values) == 2
    first.disconnect(one.connection_id)
    assert second.access_token(two.connection_id) == "synthetic-access-token"
    assert len(credentials.values) == 1
