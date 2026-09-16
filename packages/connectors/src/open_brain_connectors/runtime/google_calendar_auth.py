"""Explicit desktop OAuth for the optional Google Calendar connector."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import ClassVar, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from open_brain_connectors.runtime.google_calendar_contracts import (
    CALENDAR_SCOPE,
    GoogleCalendarError,
)
from open_brain_connectors.runtime.google_calendar_http import BoundedResponse, bounded_urlopen

__all__ = ["GoogleCalendarAccount", "GoogleCalendarAuthStore"]

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
_CALLBACK_PATH = "/oauth2/callback"
_SCOPES = ("openid", CALENDAR_SCOPE)
_CONNECTION_ID = re.compile(r"account:[0-9a-f]{64}")
_ACCOUNT_FILE = re.compile(r"google-calendar-account-([0-9a-f]{64})\.json")
_MAX_JSON_BYTES = 65_536
_MAX_TOKEN_LENGTH = 8_192


@dataclass(frozen=True, slots=True)
class GoogleCalendarAccount:
    """Safe account metadata; token material never appears in this value."""

    connection_id: str
    expires_at_epoch: int
    scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.connection_id) is not str
            or _CONNECTION_ID.fullmatch(self.connection_id) is None
            or type(self.expires_at_epoch) is not int
            or self.expires_at_epoch <= 0
            or type(self.scopes) is not tuple
            or self.scopes != _SCOPES
        ):
            raise GoogleCalendarError("google_calendar_invalid_auth_store")


@dataclass(frozen=True, slots=True, repr=False)
class _Credential:
    account: GoogleCalendarAccount
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    client_id: str
    client_secret: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class _ClientConfig:
    client_id: str
    client_secret: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Callback:
    code: str | None = field(default=None, repr=False)
    error: str | None = None
    redirect_uri: str = ""


class _CallbackServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    expected_state: str
    expected_host: str
    callback: _Callback | None
    request_timeout: float
    active_requests: set[socket.socket]
    active_lock: threading.Lock

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, cast(tuple[str, int], address)

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        del request, client_address

    def process_request(  # type: ignore[override]
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        with self.active_lock:
            self.active_requests.add(request)
        super().process_request(request, client_address)

    def shutdown_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
    ) -> None:
        if isinstance(request, socket.socket):
            with self.active_lock:
                self.active_requests.discard(request)
        super().shutdown_request(request)

    def close_active_requests(self) -> None:
        with self.active_lock:
            requests = tuple(self.active_requests)
        for request in requests:
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)
            request.close()


def _default_urlopen(request: Request, *, timeout: float) -> BoundedResponse:
    return bounded_urlopen(
        request,
        timeout=timeout,
        maximum=_MAX_JSON_BYTES,
    )


# Kept as a module boundary so focused tests can supply synthetic responses.
urlopen: Callable[..., BoundedResponse] = _default_urlopen


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "OpenBrainOAuth/1"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        server = cast(_CallbackServer, self.server)
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        state_values = query.get("state", [])
        code_values = query.get("code", [])
        error_values = query.get("error", [])
        valid = (
            parsed.scheme == ""
            and parsed.netloc == ""
            and parsed.path == _CALLBACK_PATH
            and parsed.fragment == ""
            and self.headers.get("Host") == server.expected_host
            and state_values == [server.expected_state]
            and len(code_values) + len(error_values) == 1
            and all(len(values) == 1 for values in (code_values, error_values) if values)
        )
        if not valid:
            self._respond(400, b"Invalid OAuth callback. You can close this tab.")
            return
        callback: _Callback
        if code_values:
            code = code_values[0]
            if not _valid_secret(code):
                self._respond(400, b"Invalid OAuth callback. You can close this tab.")
                return
            callback = _Callback(code=code)
        else:
            error = error_values[0]
            if not _valid_protocol_value(error, maximum=128):
                self._respond(400, b"Invalid OAuth callback. You can close this tab.")
                return
            callback = _Callback(error=error)
        self._respond(200, b"Google Calendar connection received. You can close this tab.")
        server.callback = callback

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        self._respond(405, b"Method not allowed.")

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class GoogleCalendarAuthStore:
    """Private credential store and explicit Google desktop authorization flow."""

    _refresh_skew_seconds: ClassVar[int] = 60

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise GoogleCalendarError("google_calendar_invalid_auth_store")
        self._root = root

    def connect(
        self,
        client_config: Path,
        *,
        timeout_seconds: int = 180,
        browser_opener: Callable[[str], object] | None = None,
    ) -> GoogleCalendarAccount:
        if (
            not isinstance(client_config, Path)
            or not client_config.is_absolute()
            or type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 600
            or (browser_opener is not None and not callable(browser_opener))
        ):
            raise GoogleCalendarError("google_calendar_invalid_auth_request")
        config = _load_client_config(client_config)
        deadline = time.monotonic() + timeout_seconds
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        state_value = secrets.token_urlsafe(32)
        callback = self._authorize(
            config=config,
            deadline=deadline,
            challenge=challenge,
            state_value=state_value,
            browser_opener=browser_opener,
        )
        if callback.error is not None:
            if callback.error == "access_denied":
                raise GoogleCalendarError("google_calendar_not_allowed")
            raise GoogleCalendarError("google_calendar_auth_required")
        if callback.code is None:
            raise GoogleCalendarError("google_calendar_auth_failed")
        if not callback.redirect_uri:
            raise GoogleCalendarError("google_calendar_auth_failed")
        decoded = _post_token(
            {
                "client_id": config.client_id,
                **({"client_secret": config.client_secret} if config.client_secret else {}),
                "code": callback.code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": callback.redirect_uri,
            },
            deadline=deadline,
            refreshing=False,
        )
        access_token = _token(decoded.get("access_token"))
        refresh_token = _token(decoded.get("refresh_token"))
        _require_bearer(decoded.get("token_type"))
        expires_at = _expires_at(decoded.get("expires_in"))
        scopes = _granted_scopes(decoded.get("scope"), fallback=None)
        subject = _userinfo_subject(access_token, deadline=deadline)
        connection_id = "account:" + hashlib.sha256(subject.encode("ascii")).hexdigest()
        credential = _Credential(
            account=GoogleCalendarAccount(connection_id, expires_at, scopes),
            access_token=access_token,
            refresh_token=refresh_token,
            client_id=config.client_id,
            client_secret=config.client_secret,
        )
        self._save(credential)
        return credential.account

    def accounts(self) -> tuple[GoogleCalendarAccount, ...]:
        directory_fd = self._directory_fd(create=True)
        try:
            accounts = [
                self._read(name, directory_fd).account
                for name in os.listdir(directory_fd)
                if _ACCOUNT_FILE.fullmatch(name) is not None
            ]
        except OSError as error:
            raise GoogleCalendarError("google_calendar_invalid_auth_store") from error
        finally:
            os.close(directory_fd)
        return tuple(sorted(accounts, key=lambda account: account.connection_id))

    def access_token(self, connection_id: str) -> str:
        name = _account_filename(connection_id)
        directory_fd = self._directory_fd(create=True)
        try:
            credential = self._read(name, directory_fd)
        finally:
            os.close(directory_fd)
        if credential.account.connection_id != connection_id:
            raise GoogleCalendarError("google_calendar_invalid_auth_store")
        if credential.account.expires_at_epoch > int(time.time()) + self._refresh_skew_seconds:
            return credential.access_token
        decoded = _post_token(
            {
                "client_id": credential.client_id,
                **(
                    {"client_secret": credential.client_secret}
                    if credential.client_secret
                    else {}
                ),
                "grant_type": "refresh_token",
                "refresh_token": credential.refresh_token,
            },
            deadline=time.monotonic() + 15,
            refreshing=True,
        )
        access_token = _token(decoded.get("access_token"))
        _require_bearer(decoded.get("token_type"))
        scopes = _granted_scopes(decoded.get("scope"), fallback=credential.account.scopes)
        subject = _userinfo_subject(access_token, deadline=time.monotonic() + 15)
        refreshed_connection = "account:" + hashlib.sha256(subject.encode("ascii")).hexdigest()
        if refreshed_connection != connection_id:
            raise GoogleCalendarError("google_calendar_auth_required")
        refreshed = _Credential(
            account=GoogleCalendarAccount(
                connection_id=connection_id,
                expires_at_epoch=_expires_at(decoded.get("expires_in")),
                scopes=scopes,
            ),
            access_token=access_token,
            refresh_token=(
                _token(decoded.get("refresh_token"))
                if decoded.get("refresh_token") is not None
                else credential.refresh_token
            ),
            client_id=credential.client_id,
            client_secret=credential.client_secret,
        )
        self._save(refreshed)
        return access_token

    def _authorize(
        self,
        *,
        config: _ClientConfig,
        deadline: float,
        challenge: str,
        state_value: str,
        browser_opener: Callable[[str], object] | None,
    ) -> _Callback:
        try:
            server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
        except OSError as error:
            raise GoogleCalendarError("google_calendar_auth_failed") from error
        with server:
            port = cast(tuple[str, int], server.server_address)[1]
            redirect_uri = f"http://127.0.0.1:{port}{_CALLBACK_PATH}"
            server.expected_state = state_value
            server.expected_host = f"127.0.0.1:{port}"
            server.callback = None
            server.request_timeout = 1.0
            server.active_requests = set()
            server.active_lock = threading.Lock()
            auth_url = _AUTH_ENDPOINT + "?" + urlencode(
                {
                    "access_type": "offline",
                    "client_id": config.client_id,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "prompt": "consent",
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": " ".join(_SCOPES),
                    "state": state_value,
                }
            )
            opener = browser_opener or _open_browser
            opener_result: list[object] = []
            opener_error: list[BaseException] = []

            def open_browser() -> None:
                try:
                    opener_result.append(opener(auth_url))
                except BaseException as error:  # pragma: no cover - defensive thread boundary.
                    opener_error.append(error)

            thread = threading.Thread(target=open_browser, daemon=True)
            thread.start()
            try:
                while server.callback is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GoogleCalendarError("google_calendar_auth_timeout")
                    if opener_error or (opener_result and opener_result[0] is False):
                        raise GoogleCalendarError("google_calendar_browser_failed")
                    server.timeout = min(0.05, remaining)
                    server.request_timeout = min(1.0, remaining)
                    server.handle_request()
                callback = server.callback
            finally:
                server.close_active_requests()
            return _Callback(
                code=callback.code,
                error=callback.error,
                redirect_uri=redirect_uri,
            )

    def _directory_fd(self, *, create: bool) -> int:
        try:
            if create:
                self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root_stat = self._root.lstat()
            if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
                raise GoogleCalendarError("google_calendar_invalid_auth_store")
            directory_fd = os.open(
                self._root,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            os.fchmod(directory_fd, 0o700)
            opened = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o700:
                os.close(directory_fd)
                raise GoogleCalendarError("google_calendar_invalid_auth_store")
            return directory_fd
        except GoogleCalendarError:
            raise
        except OSError as error:
            raise GoogleCalendarError("google_calendar_invalid_auth_store") from error

    def _save(self, credential: _Credential) -> None:
        name = _account_filename(credential.account.connection_id)
        payload = (
            json.dumps(
                {
                    "access_token": credential.access_token,
                    "client_id": credential.client_id,
                    "client_secret": credential.client_secret,
                    "connection_id": credential.account.connection_id,
                    "expires_at_epoch": credential.account.expires_at_epoch,
                    "refresh_token": credential.refresh_token,
                    "schema_version": 1,
                    "scopes": list(credential.account.scopes),
                    "token_type": "Bearer",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > _MAX_JSON_BYTES:
            raise GoogleCalendarError("google_calendar_invalid_auth_store")
        directory_fd = self._directory_fd(create=True)
        temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
        try:
            _validate_existing_target(name, directory_fd)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(file_fd, "wb", closefd=False) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                os.close(file_fd)
            os.replace(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        except GoogleCalendarError:
            raise
        except OSError as error:
            raise GoogleCalendarError("google_calendar_invalid_auth_store") from error
        finally:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory_fd)

    def _read(self, name: str, directory_fd: int) -> _Credential:
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                file_stat = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_nlink != 1
                    or stat.S_IMODE(file_stat.st_mode) != 0o600
                    or file_stat.st_size > _MAX_JSON_BYTES
                ):
                    raise GoogleCalendarError("google_calendar_invalid_auth_store")
                raw = os.read(file_fd, _MAX_JSON_BYTES + 1)
            finally:
                os.close(file_fd)
            decoded = json.loads(raw.decode("utf-8"))
        except FileNotFoundError as error:
            raise GoogleCalendarError("google_calendar_auth_required") from error
        except GoogleCalendarError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GoogleCalendarError("google_calendar_invalid_auth_store") from error
        if not isinstance(decoded, Mapping) or decoded.get("schema_version") != 1:
            raise GoogleCalendarError("google_calendar_invalid_auth_store")
        connection_id = _stored_str(decoded.get("connection_id"), maximum=72)
        account = GoogleCalendarAccount(
            connection_id=connection_id,
            expires_at_epoch=_stored_int(decoded.get("expires_at_epoch")),
            scopes=_stored_scopes(decoded.get("scopes")),
        )
        if name != _account_filename(connection_id) or decoded.get("token_type") != "Bearer":
            raise GoogleCalendarError("google_calendar_invalid_auth_store")
        client_secret_value = decoded.get("client_secret")
        client_secret = (
            None
            if client_secret_value is None
            else _stored_str(client_secret_value, maximum=1_024)
        )
        return _Credential(
            account=account,
            access_token=_stored_secret(decoded.get("access_token")),
            refresh_token=_stored_secret(decoded.get("refresh_token")),
            client_id=_stored_client_id(decoded.get("client_id")),
            client_secret=client_secret,
        )


def _open_browser(url: str) -> bool:
    return webbrowser.open(url, new=1, autoraise=True)


def _load_client_config(path: Path) -> _ClientConfig:
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(path, flags)
        try:
            file_stat = os.fstat(file_fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_nlink != 1
                or file_stat.st_size > _MAX_JSON_BYTES
            ):
                raise GoogleCalendarError("google_calendar_invalid_client_config")
            raw = os.read(file_fd, _MAX_JSON_BYTES + 1)
        finally:
            os.close(file_fd)
        decoded = json.loads(raw.decode("utf-8"))
    except GoogleCalendarError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GoogleCalendarError("google_calendar_invalid_client_config") from error
    if not isinstance(decoded, Mapping) or set(decoded) != {"installed"}:
        raise GoogleCalendarError("google_calendar_invalid_client_config")
    installed = decoded.get("installed")
    if not isinstance(installed, Mapping):
        raise GoogleCalendarError("google_calendar_invalid_client_config")
    allowed = {
        "auth_provider_x509_cert_url",
        "auth_uri",
        "client_id",
        "client_secret",
        "project_id",
        "redirect_uris",
        "token_uri",
    }
    if not set(installed).issubset(allowed):
        raise GoogleCalendarError("google_calendar_invalid_client_config")
    if installed.get("token_uri") != _TOKEN_ENDPOINT or installed.get("auth_uri") not in {
        _AUTH_ENDPOINT,
        "https://accounts.google.com/o/oauth2/auth",
    }:
        raise GoogleCalendarError("google_calendar_invalid_client_config")
    redirect_uris = installed.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or not all(
        value in {"http://localhost", "http://127.0.0.1"} for value in redirect_uris
    ):
        raise GoogleCalendarError("google_calendar_invalid_client_config")
    secret_value = installed.get("client_secret")
    client_secret = (
        None if secret_value is None else _stored_str(secret_value, maximum=1_024)
    )
    return _ClientConfig(
        client_id=_client_id(installed.get("client_id")),
        client_secret=client_secret,
    )


def _post_token(
    parameters: Mapping[str, str],
    *,
    deadline: float,
    refreshing: bool,
) -> Mapping[str, object]:
    request = Request(
        _TOKEN_ENDPOINT,
        data=urlencode(parameters).encode("ascii"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        return _request_json(request, deadline=deadline)
    except HTTPError as error:
        if refreshing and error.code in {400, 401}:
            raise GoogleCalendarError("google_calendar_auth_required") from error
        raise GoogleCalendarError("google_calendar_auth_failed") from error


def _userinfo_subject(access_token: str, *, deadline: float) -> str:
    request = Request(
        _USERINFO_ENDPOINT,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        decoded = _request_json(request, deadline=deadline)
    except HTTPError as error:
        if error.code in {401, 403}:
            raise GoogleCalendarError("google_calendar_auth_required") from error
        raise GoogleCalendarError("google_calendar_auth_failed") from error
    subject = decoded.get("sub")
    if (
        type(subject) is not str
        or not 1 <= len(subject) <= 255
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in subject)
    ):
        raise GoogleCalendarError("google_calendar_auth_failed")
    return subject


def _request_json(request: Request, *, deadline: float) -> Mapping[str, object]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GoogleCalendarError("google_calendar_auth_timeout")
    try:
        response = urlopen(request, timeout=min(15.0, remaining))  # noqa: S310 - fixed endpoints.
        with response:
            content_type = response.headers.get_content_type()
            raw = response.read(_MAX_JSON_BYTES + 1)
    except HTTPError:
        raise
    except TimeoutError as error:
        raise GoogleCalendarError("google_calendar_auth_timeout") from error
    except (OSError, URLError) as error:
        raise GoogleCalendarError("google_calendar_auth_failed") from error
    if content_type != "application/json" or len(raw) > _MAX_JSON_BYTES:
        raise GoogleCalendarError("google_calendar_auth_failed")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GoogleCalendarError("google_calendar_auth_failed") from error
    if not isinstance(decoded, Mapping):
        raise GoogleCalendarError("google_calendar_auth_failed")
    return cast(Mapping[str, object], decoded)


def _granted_scopes(value: object, *, fallback: tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        if fallback is None:
            raise GoogleCalendarError("google_calendar_not_allowed")
        return fallback
    if type(value) is not str:
        raise GoogleCalendarError("google_calendar_not_allowed")
    if set(value.split()) != set(_SCOPES):
        raise GoogleCalendarError("google_calendar_not_allowed")
    return _SCOPES


def _expires_at(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 86_400:
        raise GoogleCalendarError("google_calendar_auth_failed")
    return int(time.time()) + value


def _token(value: object) -> str:
    if type(value) is not str or not _valid_secret(value):
        raise GoogleCalendarError("google_calendar_auth_failed")
    return value


def _require_bearer(value: object) -> None:
    if type(value) is not str or value.lower() != "bearer":
        raise GoogleCalendarError("google_calendar_auth_failed")


def _valid_secret(value: str) -> bool:
    return 1 <= len(value) <= _MAX_TOKEN_LENGTH and all(
        0x21 <= ord(char) <= 0x7E for char in value
    )


def _valid_protocol_value(value: str, *, maximum: int) -> bool:
    return 1 <= len(value) <= maximum and all(0x21 <= ord(char) <= 0x7E for char in value)


def _client_id(value: object) -> str:
    if (
        type(value) is not str
        or not value.endswith(".apps.googleusercontent.com")
        or not _valid_protocol_value(value, maximum=512)
    ):
        raise GoogleCalendarError("google_calendar_invalid_client_config")
    return value


def _account_filename(connection_id: str) -> str:
    if type(connection_id) is not str or _CONNECTION_ID.fullmatch(connection_id) is None:
        raise GoogleCalendarError("google_calendar_auth_required")
    return f"google-calendar-account-{connection_id.removeprefix('account:')}.json"


def _validate_existing_target(name: str, directory_fd: int) -> None:
    try:
        target = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(target.st_mode)
        or target.st_nlink != 1
        or stat.S_IMODE(target.st_mode) != 0o600
    ):
        raise GoogleCalendarError("google_calendar_invalid_auth_store")


def _stored_str(value: object, *, maximum: int) -> str:
    if type(value) is not str or not _valid_protocol_value(value, maximum=maximum):
        raise GoogleCalendarError("google_calendar_invalid_auth_store")
    return value


def _stored_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise GoogleCalendarError("google_calendar_invalid_auth_store")
    return value


def _stored_scopes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(type(item) is str for item in value):
        raise GoogleCalendarError("google_calendar_invalid_auth_store")
    scopes = tuple(cast(list[str], value))
    if scopes != _SCOPES:
        raise GoogleCalendarError("google_calendar_invalid_auth_store")
    return scopes


def _stored_secret(value: object) -> str:
    if type(value) is not str or not _valid_secret(value):
        raise GoogleCalendarError("google_calendar_invalid_auth_store")
    return value


def _stored_client_id(value: object) -> str:
    if (
        type(value) is not str
        or not value.endswith(".apps.googleusercontent.com")
        or not _valid_protocol_value(value, maximum=512)
    ):
        raise GoogleCalendarError("google_calendar_invalid_auth_store")
    return value
