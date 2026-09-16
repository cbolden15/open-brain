from __future__ import annotations

import json
import time
from pathlib import Path
from typing import cast

import pytest

from open_brain_connectors.runtime import google_sources_auth
from open_brain_connectors.runtime.google_sources_auth import GoogleSourcesAuth
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_http import LiveHttpResponse, LiveHttpTransport
from open_brain_connectors.runtime.live_oauth import OAuthCode


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


class _Http(LiveHttpTransport):
    def __init__(self, responses: list[LiveHttpResponse]) -> None:
        self.responses = responses
        self.forms: list[dict[str, str] | None] = []

    def request(self, method: str, url: str, **kwargs: object) -> LiveHttpResponse:
        del method, url
        self.forms.append(cast(dict[str, str] | None, kwargs.get("form")))
        return self.responses.pop(0)


def _json(status: int, value: dict[str, object]) -> LiveHttpResponse:
    return LiveHttpResponse(status, {}, json.dumps(value).encode())


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "client.json"
    path.write_text(
        json.dumps({"installed": {"client_id": "client-id.apps.googleusercontent.com"}})
    )
    return path


def _auth_responses(*, expires_in: int = 3_600) -> list[LiveHttpResponse]:
    return [
        _json(
            200,
            {
                "access_token": "access-token",
                "expires_in": expires_in,
                "refresh_token": "refresh-token",
                "scope": "openid https://www.googleapis.com/auth/gmail.readonly",
                "token_type": "Bearer",
            },
        ),
        _json(200, {"name": "Synthetic Account", "sub": "subject-123"}),
    ]


def test_disconnect_cannot_remove_same_accounts_credentials_from_another_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = _Credentials()
    monkeypatch.setattr(
        google_sources_auth,
        "authorize_loopback",
        lambda *_a, **_k: OAuthCode("code", "verifier", "http://127.0.0.1/callback"),
    )
    first = GoogleSourcesAuth(
        "gmail", tmp_path / "first", credentials=credentials, http=_Http(_auth_responses())
    )
    second = GoogleSourcesAuth(
        "gmail", tmp_path / "second", credentials=credentials, http=_Http(_auth_responses())
    )
    one, two = first.connect(_config(tmp_path)), second.connect(_config(tmp_path))
    assert one.connection_id == two.connection_id and len(credentials.values) == 2
    first.disconnect(one.connection_id)
    assert second.access_token(two.connection_id) == "access-token"
    assert len(credentials.values) == 1


def test_connect_stores_tokens_only_in_credentials_and_uses_exact_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = _Credentials()
    http = _Http(_auth_responses())
    auth = GoogleSourcesAuth("gmail", tmp_path, credentials=credentials, http=http)
    monkeypatch.setattr(
        google_sources_auth,
        "authorize_loopback",
        lambda _build_url, **_options: OAuthCode("code", "verifier", "http://127.0.0.1/callback"),
    )

    account = auth.connect(_config(tmp_path))

    assert account.provider == "gmail"
    assert account.scopes == ("openid", "https://www.googleapis.com/auth/gmail.readonly")
    assert auth.access_token(account.connection_id) == "access-token"
    metadata = (tmp_path / "google-gmail").glob("account-*.json")
    assert "access-token" not in next(metadata).read_text()
    assert len(credentials.values) == 1
    assert http.forms[0] == {
        "client_id": "client-id.apps.googleusercontent.com",
        "code": "code",
        "code_verifier": "verifier",
        "grant_type": "authorization_code",
        "redirect_uri": "http://127.0.0.1/callback",
    }

    auth.disconnect(account.connection_id)
    assert auth.accounts() == ()
    assert credentials.values == {}


def test_refresh_preserves_account_binding_and_rejects_extra_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = _Credentials()
    http = _Http(_auth_responses(expires_in=1))
    auth = GoogleSourcesAuth("gmail", tmp_path, credentials=credentials, http=http)
    monkeypatch.setattr(
        google_sources_auth,
        "authorize_loopback",
        lambda _build_url, **_options: OAuthCode("code", "verifier", "http://127.0.0.1/callback"),
    )
    account = auth.connect(_config(tmp_path))
    reference = next(iter(credentials.values))
    credentials.values[reference]["expires_at_epoch"] = int(time.time()) - 1
    http.responses.extend(
        [
            _json(
                200,
                {
                    "access_token": "fresh-token",
                    "expires_in": 3_600,
                    "scope": "openid https://www.googleapis.com/auth/gmail.readonly",
                    "token_type": "Bearer",
                },
            ),
            _json(200, {"name": "Synthetic Account", "sub": "subject-123"}),
        ]
    )

    assert auth.access_token(account.connection_id) == "fresh-token"

    http.responses.extend(
        [
            _json(
                200,
                {
                    "access_token": "bad-token",
                    "expires_in": 3_600,
                    "scope": "openid https://www.googleapis.com/auth/gmail.readonly extra",
                    "token_type": "Bearer",
                },
            )
        ]
    )
    credentials.values[reference]["expires_at_epoch"] = int(time.time()) - 1
    with pytest.raises(LiveSourceError, match="google_scope_mismatch"):
        auth.access_token(account.connection_id)
