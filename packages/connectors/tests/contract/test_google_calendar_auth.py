from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from email.message import Message
from http.client import HTTPConnection
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

import pytest

import open_brain_connectors.runtime.google_calendar_auth as auth_module
from open_brain_connectors.runtime.google_calendar_auth import (
    GoogleCalendarAccount,
    GoogleCalendarAuthStore,
    _default_urlopen,
)
from open_brain_connectors.runtime.google_calendar_contracts import (
    CALENDAR_SCOPE,
    GoogleCalendarError,
)

_CLIENT_ID = "123456789-test.apps.googleusercontent.com"
_CLIENT_SECRET = "synthetic-client-secret"
_SUBJECT = "synthetic-google-subject"
_SCOPES = f"openid {CALENDAR_SCOPE}"


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]


class _Network:
    def __init__(self, *, granted_scope: object = _SCOPES) -> None:
        self.granted_scope = granted_scope
        self.token_forms: list[dict[str, list[str]]] = []
        self.userinfo_tokens: list[str] = []
        self.refresh_subject = _SUBJECT

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        assert 0 < timeout <= 15
        if request.full_url == "https://oauth2.googleapis.com/token":
            assert request.data is not None
            assert isinstance(request.data, bytes)
            form = parse_qs(request.data.decode("ascii"), keep_blank_values=True)
            self.token_forms.append(form)
            if form["grant_type"] == ["authorization_code"]:
                return _Response(
                    {
                        "access_token": "synthetic-access-initial",
                        "expires_in": 120,
                        "refresh_token": "synthetic-refresh-token",
                        "scope": self.granted_scope,
                        "token_type": "Bearer",
                    }
                )
            return _Response(
                {
                    "access_token": "synthetic-access-refreshed",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            )
        assert request.full_url == "https://openidconnect.googleapis.com/v1/userinfo"
        authorization = request.get_header("Authorization")
        assert authorization is not None
        self.userinfo_tokens.append(authorization)
        subject = self.refresh_subject if "refreshed" in authorization else _SUBJECT
        return _Response({"sub": subject})


def _write_client_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "client_id": _CLIENT_ID,
                    "client_secret": _CLIENT_SECRET,
                    "project_id": "synthetic-project",
                    "redirect_uris": ["http://localhost"],
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        ),
        encoding="utf-8",
    )


def _callback_status(url: str) -> int:
    parsed = urlsplit(url)
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    # The synthetic loopback callback needs neither TLS setup nor ambient proxies.
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.request("GET", f"{parsed.path}?{parsed.query}")
        with connection.getresponse() as response:
            return response.status
    finally:
        connection.close()


def _browser_opener(captured: list[dict[str, list[str]]]) -> Callable[[str], object]:
    def open_browser(url: str) -> bool:
        parsed = urlsplit(url)
        assert (parsed.scheme, parsed.netloc, parsed.path) == (
            "https",
            "accounts.google.com",
            "/o/oauth2/v2/auth",
        )
        query = parse_qs(parsed.query)
        captured.append(query)
        callback_url = query["redirect_uri"][0] + "?" + urlencode(
            {"code": "synthetic-authorization-code", "state": query["state"][0]}
        )
        assert _callback_status(callback_url) == 200
        return True

    return open_browser


def _connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network: _Network,
) -> tuple[GoogleCalendarAuthStore, GoogleCalendarAccount, list[dict[str, list[str]]]]:
    config_path = tmp_path / "desktop-client.json"
    credential_root = tmp_path / "credentials"
    _write_client_config(config_path)
    monkeypatch.setattr(auth_module, "urlopen", network)
    captured: list[dict[str, list[str]]] = []
    store = GoogleCalendarAuthStore(credential_root)
    account = store.connect(
        config_path,
        timeout_seconds=3,
        browser_opener=_browser_opener(captured),
    )
    return store, account, captured


def test_connect_uses_pkce_loopback_and_persists_private_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ssl._create_default_https_context",
        lambda: pytest.fail("HTTP loopback fixture must not initialize TLS"),
    )
    monkeypatch.setattr(
        "open_brain_connectors.runtime.google_calendar_auth.time.time",
        lambda: 1_000_000.0,
    )
    network = _Network()
    store, account, captured = _connect(tmp_path, monkeypatch, network)

    expected_connection = "account:" + hashlib.sha256(_SUBJECT.encode("ascii")).hexdigest()
    assert account.connection_id == expected_connection
    assert account.expires_at_epoch == 1_000_120
    assert account.scopes == ("openid", CALENDAR_SCOPE)
    assert store.accounts() == (account,)
    assert store.access_token(expected_connection) == "synthetic-access-initial"
    assert "synthetic-access" not in repr(account)
    assert "synthetic-refresh" not in repr(account)

    auth_query = captured[0]
    assert auth_query["access_type"] == ["offline"]
    assert auth_query["code_challenge_method"] == ["S256"]
    assert set(auth_query["scope"][0].split()) == {"openid", CALENDAR_SCOPE}
    assert auth_query["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert auth_query["redirect_uri"][0].endswith("/oauth2/callback")
    assert len(auth_query["state"][0]) >= 32

    token_form = network.token_forms[0]
    verifier = token_form["code_verifier"][0]
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert 43 <= len(verifier) <= 128
    assert auth_query["code_challenge"] == [expected_challenge]
    assert token_form["redirect_uri"] == auth_query["redirect_uri"]
    assert token_form["client_secret"] == [_CLIENT_SECRET]
    assert network.userinfo_tokens == ["Bearer synthetic-access-initial"]

    credential_root = tmp_path / "credentials"
    credential_files = tuple(credential_root.iterdir())
    assert stat.S_IMODE(credential_root.stat().st_mode) == 0o700
    assert len(credential_files) == 1
    assert stat.S_IMODE(credential_files[0].stat().st_mode) == 0o600
    assert _SUBJECT not in credential_files[0].name


def test_access_token_refreshes_and_preserves_verified_account_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000_000.0]
    monkeypatch.setattr(
        "open_brain_connectors.runtime.google_calendar_auth.time.time",
        lambda: now[0],
    )
    network = _Network()
    store, account, _captured = _connect(tmp_path, monkeypatch, network)

    now[0] = 1_000_061.0
    assert store.access_token(account.connection_id) == "synthetic-access-refreshed"
    assert network.token_forms[1] == {
        "client_id": [_CLIENT_ID],
        "client_secret": [_CLIENT_SECRET],
        "grant_type": ["refresh_token"],
        "refresh_token": ["synthetic-refresh-token"],
    }
    assert network.userinfo_tokens[-1] == "Bearer synthetic-access-refreshed"
    assert store.accounts()[0].expires_at_epoch == 1_003_661


def test_refresh_rejects_a_different_userinfo_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000_000.0]
    monkeypatch.setattr(
        "open_brain_connectors.runtime.google_calendar_auth.time.time",
        lambda: now[0],
    )
    network = _Network()
    store, account, _captured = _connect(tmp_path, monkeypatch, network)
    now[0] = 1_000_061.0
    network.refresh_subject = "different-synthetic-subject"

    with pytest.raises(GoogleCalendarError, match="^google_calendar_auth_required$"):
        store.access_token(account.connection_id)
    assert store.accounts()[0] == account


@pytest.mark.parametrize("granted_scope", [CALENDAR_SCOPE, "openid", None])
def test_connect_rejects_missing_or_omitted_required_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    granted_scope: object,
) -> None:
    network = _Network(granted_scope=granted_scope)
    config_path = tmp_path / "desktop-client.json"
    _write_client_config(config_path)
    monkeypatch.setattr(auth_module, "urlopen", network)
    store = GoogleCalendarAuthStore(tmp_path / "credentials")

    with pytest.raises(GoogleCalendarError, match="^google_calendar_not_allowed$"):
        store.connect(
            config_path,
            timeout_seconds=3,
            browser_opener=_browser_opener([]),
        )
    assert store.accounts() == ()


def test_store_rejects_symlink_and_hardlink_credential_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network = _Network()
    store, _account, _captured = _connect(tmp_path, monkeypatch, network)
    credential_path = next((tmp_path / "credentials").iterdir())
    original_path = credential_path.with_suffix(".original")
    credential_path.rename(original_path)
    credential_path.symlink_to(original_path)

    with pytest.raises(GoogleCalendarError, match="^google_calendar_invalid_auth_store$"):
        store.accounts()

    credential_path.unlink()
    os.link(original_path, credential_path)
    with pytest.raises(GoogleCalendarError, match="^google_calendar_invalid_auth_store$"):
        store.accounts()


def test_connect_rejects_non_desktop_config_and_browser_failure(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "client.json"
    config_path.write_text(json.dumps({"web": {"client_id": _CLIENT_ID}}), encoding="utf-8")
    store = GoogleCalendarAuthStore(tmp_path / "credentials")
    with pytest.raises(GoogleCalendarError, match="^google_calendar_invalid_client_config$"):
        store.connect(config_path, browser_opener=lambda _url: True)

    _write_client_config(config_path)
    with pytest.raises(GoogleCalendarError, match="^google_calendar_browser_failed$"):
        store.connect(config_path, timeout_seconds=2, browser_opener=lambda _url: False)


def test_auth_paths_must_be_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(GoogleCalendarError, match="^google_calendar_invalid_auth_store$"):
        GoogleCalendarAuthStore(Path("credentials"))
    assert not (tmp_path / "credentials").exists()

    relative_config = Path("desktop-client.json")
    _write_client_config(tmp_path / relative_config)
    store = GoogleCalendarAuthStore(tmp_path / "credentials")
    with pytest.raises(GoogleCalendarError, match="^google_calendar_invalid_auth_request$"):
        store.connect(relative_config, browser_opener=lambda _url: True)
    assert not (tmp_path / "credentials").exists()


@pytest.mark.parametrize(
    ("mode", "expected_error"),
    [
        ("config", "google_calendar_invalid_client_config"),
        ("credential", "google_calendar_invalid_auth_store"),
    ],
)
def test_fifo_auth_files_fail_in_a_bounded_subprocess(
    tmp_path: Path,
    mode: str,
    expected_error: str,
) -> None:
    credential_root = tmp_path / "credentials"
    if mode == "config":
        fifo_path = tmp_path / "desktop-client.json"
    else:
        credential_root.mkdir(mode=0o700)
        fifo_path = credential_root / f"google-calendar-account-{'a' * 64}.json"
    os.mkfifo(fifo_path, mode=0o600)
    project_root = Path(__file__).resolve().parents[4]
    python_path = os.pathsep.join(
        (
            str(project_root / "packages/connectors/src"),
            str(project_root / "packages/engine/src"),
            os.environ.get("PYTHONPATH", ""),
        )
    )
    script = """
import sys
from pathlib import Path
from open_brain_connectors.runtime.google_calendar_auth import GoogleCalendarAuthStore
from open_brain_connectors.runtime.google_calendar_contracts import GoogleCalendarError

mode, root_value, fifo_value = sys.argv[1:]
try:
    store = GoogleCalendarAuthStore(Path(root_value))
    if mode == "config":
        store.connect(Path(fifo_value), timeout_seconds=1, browser_opener=lambda _url: False)
    else:
        store.accounts()
except GoogleCalendarError as error:
    print(str(error))
    raise SystemExit(0)
raise SystemExit(3)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, mode, str(credential_root), str(fifo_path)],
        cwd=project_root,
        env={**os.environ, "PYTHONPATH": python_path},
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected_error


def test_slow_callback_headers_cannot_extend_total_deadline(tmp_path: Path) -> None:
    config_path = tmp_path / "desktop-client.json"
    _write_client_config(config_path)

    def slow_opener(url: str) -> bool:
        redirect = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0])
        assert redirect.hostname == "127.0.0.1"
        assert redirect.port is not None
        request = b"GET /oauth2/callback?code=slow"
        with socket.create_connection((redirect.hostname, redirect.port), timeout=1) as connection:
            for byte in request:
                try:
                    connection.sendall(bytes((byte,)))
                except OSError:
                    break
                time.sleep(0.2)
        return True

    started = time.monotonic()
    with pytest.raises(GoogleCalendarError, match="^google_calendar_auth_timeout$"):
        GoogleCalendarAuthStore(tmp_path / "credentials").connect(
            config_path,
            timeout_seconds=1,
            browser_opener=slow_opener,
        )
    assert time.monotonic() - started < 1.5


def test_loopback_rejects_wrong_state_before_accepting_exact_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "desktop-client.json"
    _write_client_config(config_path)
    monkeypatch.setattr(auth_module, "urlopen", _Network())

    def opener(url: str) -> bool:
        query = parse_qs(urlsplit(url).query)
        redirect_uri = query["redirect_uri"][0]
        wrong_callback = redirect_uri + "?" + urlencode(
            {"code": "wrong-code", "state": query["state"][0] + "-wrong"}
        )
        assert _callback_status(wrong_callback) == 400
        exact_callback = redirect_uri + "?" + urlencode(
            {"code": "synthetic-authorization-code", "state": query["state"][0]}
        )
        assert _callback_status(exact_callback) == 200
        return True

    account = GoogleCalendarAuthStore(tmp_path / "credentials").connect(
        config_path,
        timeout_seconds=3,
        browser_opener=opener,
    )
    assert account.scopes == ("openid", CALENDAR_SCOPE)


def test_default_http_transport_delegates_to_bounded_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Request, float, int]] = []
    response = _Response({"ok": True})

    def bounded(request: Request, *, timeout: float, maximum: int) -> _Response:
        calls.append((request, timeout, maximum))
        return response

    monkeypatch.setattr(
        "open_brain_connectors.runtime.google_calendar_auth.bounded_urlopen",
        bounded,
    )
    request = Request("https://oauth2.googleapis.com/token", method="POST")
    assert json.loads(_default_urlopen(request, timeout=2.5).read()) == {"ok": True}
    assert calls == [(request, 2.5, 65_536)]
