"""Read-only OAuth accounts shared by the Gmail and Google Drive live sources."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlencode

from open_brain_connectors.runtime.live_common import (
    LiveAccount,
    LiveCredentialStore,
    LiveSourceError,
    safe_text,
)
from open_brain_connectors.runtime.live_http import LiveHttpTransport
from open_brain_connectors.runtime.live_oauth import OAuthCode, authorize_loopback
from open_brain_connectors.runtime.live_storage import OsCredentialStore, PrivateJsonStore

__all__ = ["DRIVE_SCOPE", "GMAIL_SCOPE", "GoogleSourcesAuth"]

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GMAIL_SCOPE = _GMAIL_SCOPE
DRIVE_SCOPE = _DRIVE_SCOPE
_MAX_CONFIG_BYTES = 65_536
_REFRESH_SKEW_SECONDS = 60


class _LoopbackOptions(TypedDict, total=False):
    browser_opener: Callable[[str], object] | None
    callback_path: str
    hostname: str
    port: int
    timeout_seconds: int


class GoogleSourcesAuth:
    """Own opaque Google account metadata while keeping token material in credentials."""

    def __init__(
        self,
        provider: str,
        root: Path,
        *,
        credentials: LiveCredentialStore | None = None,
        http: LiveHttpTransport | None = None,
    ) -> None:
        if provider not in {"gmail", "google_drive"}:
            raise LiveSourceError("google_invalid_provider")
        if not isinstance(root, Path) or not root.is_absolute():
            raise LiveSourceError("google_invalid_storage")
        self.provider = provider
        self._store = PrivateJsonStore(root / f"google-{provider}")
        self._credentials = credentials or OsCredentialStore()
        self._http = http or LiveHttpTransport()

    @property
    def scopes(self) -> tuple[str, str]:
        return ("openid", _GMAIL_SCOPE if self.provider == "gmail" else _DRIVE_SCOPE)

    def connect(self, client_config: Path, **loopback_options: object) -> LiveAccount:
        client = _load_client_config(client_config)

        def build_url(redirect_uri: str, state: str, challenge: str) -> str:
            return (
                _AUTH_URL
                + "?"
                + urlencode(
                    {
                        "access_type": "offline",
                        "client_id": client["client_id"],
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "prompt": "consent",
                        "redirect_uri": redirect_uri,
                        "response_type": "code",
                        "scope": " ".join(self.scopes),
                        "state": state,
                    }
                )
            )

        code = authorize_loopback(build_url, **cast(_LoopbackOptions, loopback_options))
        if type(code) is not OAuthCode:
            raise LiveSourceError("google_auth_failed")
        token = self._token_response(
            {
                **client,
                "code": code.code,
                "code_verifier": code.verifier,
                "grant_type": "authorization_code",
                "redirect_uri": code.redirect_uri,
            },
            refreshing=False,
        )
        access_token, refresh_token, expires_at, scopes = _token_fields(
            token, self.scopes, allow_omitted_scope=False
        )
        subject, display_name = self._account_identity(access_token)
        connection_id = (
            "account:" + hashlib.sha256(f"{self.provider}:{subject}".encode()).hexdigest()
        )
        account = LiveAccount(
            provider=self.provider,
            connection_id=connection_id,
            display_name=display_name,
            scopes=scopes,
            expires_at_epoch=expires_at,
        )
        name = _account_name(connection_id)
        reference = _credential_reference(self.provider, connection_id, self._store.root)
        with self._store.lock(name):
            self._credentials.set(
                reference,
                {
                    "access_token": access_token,
                    **client,
                    "expires_at_epoch": expires_at,
                    "refresh_token": refresh_token,
                    "scopes": list(scopes),
                },
            )
            self._store.write(
                name,
                {"account": account.to_dict(), "credential_reference": reference, "version": 1},
            )
        return account

    def accounts(self) -> tuple[LiveAccount, ...]:
        accounts: list[LiveAccount] = []
        for name in self._store.names("account-"):
            value = self._store.read(name)
            if not isinstance(value, dict) or value.get("version") != 1:
                raise LiveSourceError("google_invalid_account")
            account = LiveAccount.from_dict(value.get("account"))
            if account.provider != self.provider or account.scopes != self.scopes:
                raise LiveSourceError("google_invalid_account")
            accounts.append(account)
        return tuple(sorted(accounts, key=lambda account: account.connection_id))

    def access_token(self, connection_id: str) -> str:
        name = _account_name(connection_id)
        with self._store.lock(name):
            account, reference = self._metadata(connection_id)
            credential = self._credentials.get(reference)
            if credential is None:
                raise LiveSourceError("google_auth_required")
            access_token = _secret(credential.get("access_token"))
            expires_at = _expires_at_epoch(credential.get("expires_at_epoch"))
            if expires_at > int(time.time()) + _REFRESH_SKEW_SECONDS:
                return access_token
            client = _client_credentials(credential)
            refreshed = self._token_response(
                {
                    **client,
                    "grant_type": "refresh_token",
                    "refresh_token": _secret(credential.get("refresh_token")),
                },
                refreshing=True,
            )
            refreshed_access, refresh_token, refreshed_expiry, scopes = _token_fields(
                refreshed,
                account.scopes,
                fallback_refresh=_secret(credential.get("refresh_token")),
                allow_omitted_scope=True,
            )
            subject, _display_name = self._account_identity(refreshed_access)
            refreshed_connection_id = (
                "account:" + hashlib.sha256(f"{self.provider}:{subject}".encode()).hexdigest()
            )
            if refreshed_connection_id != connection_id:
                raise LiveSourceError("google_auth_required")
            refreshed_account = LiveAccount(
                provider=self.provider,
                connection_id=connection_id,
                display_name=account.display_name,
                scopes=scopes,
                expires_at_epoch=refreshed_expiry,
            )
            self._credentials.set(
                reference,
                {
                    "access_token": refreshed_access,
                    **client,
                    "expires_at_epoch": refreshed_expiry,
                    "refresh_token": refresh_token,
                    "scopes": list(scopes),
                },
            )
            self._store.write(
                name,
                {
                    "account": refreshed_account.to_dict(),
                    "credential_reference": reference,
                    "version": 1,
                },
            )
            return refreshed_access

    def disconnect(self, connection_id: str) -> None:
        name = _account_name(connection_id)
        with self._store.lock(name):
            _account, reference = self._metadata(connection_id)
            self._credentials.delete(reference)
            self._store.delete(name)

    def _metadata(self, connection_id: str) -> tuple[LiveAccount, str]:
        value = self._store.read(_account_name(connection_id))
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "account",
                "credential_reference",
                "version",
            }
            or value.get("version") != 1
        ):
            raise LiveSourceError("google_auth_required")
        account = LiveAccount.from_dict(value["account"])
        reference = value["credential_reference"]
        if (
            account.provider != self.provider
            or account.connection_id != connection_id
            or account.scopes != self.scopes
            or type(reference) is not str
            or reference != _credential_reference(self.provider, connection_id, self._store.root)
        ):
            raise LiveSourceError("google_auth_required")
        return account, reference

    def _token_response(self, form: dict[str, str], *, refreshing: bool) -> dict[str, object]:
        response = self._http.request(
            "POST",
            _TOKEN_URL,
            headers={"accept": "application/json"},
            form=form,
            max_bytes=_MAX_CONFIG_BYTES,
        )
        if response.status >= 400:
            if refreshing and response.status in {400, 401}:
                raise LiveSourceError("google_auth_required")
            raise _http_error(response.status, response.retry_after_seconds())
        return response.json()

    def _account_identity(self, access_token: str) -> tuple[str, str]:
        response = self._http.request(
            "GET",
            _USERINFO_URL,
            headers={"authorization": f"Bearer {access_token}", "accept": "application/json"},
            max_bytes=_MAX_CONFIG_BYTES,
        )
        if response.status >= 400:
            raise _http_error(response.status, response.retry_after_seconds())
        value = response.json()
        subject = _bounded_text(value.get("sub"), maximum=255)
        display_name = value.get("name", value.get("email", "Google account"))
        return subject, _bounded_text(display_name, maximum=200)


def _load_client_config(path: Path) -> dict[str, str]:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise LiveSourceError("google_invalid_client_config")
    try:
        raw = path.read_bytes()
    except OSError:
        raise LiveSourceError("google_invalid_client_config") from None
    if len(raw) > _MAX_CONFIG_BYTES:
        raise LiveSourceError("google_invalid_client_config")
    try:
        decoded = json.loads(raw)
    except ValueError, UnicodeDecodeError:
        raise LiveSourceError("google_invalid_client_config") from None
    if not isinstance(decoded, Mapping):
        raise LiveSourceError("google_invalid_client_config")
    installed = decoded.get("installed")
    if not isinstance(installed, Mapping):
        raise LiveSourceError("google_invalid_client_config")
    return _client_credentials(installed)


def _client_credentials(value: Mapping[str, object]) -> dict[str, str]:
    client = {"client_id": _client_id(value.get("client_id"))}
    if "client_secret" in value:
        client["client_secret"] = _secret(value["client_secret"])
    return client


def _account_name(connection_id: str) -> str:
    if type(connection_id) is not str or not connection_id.startswith("account:"):
        raise LiveSourceError("google_invalid_connection")
    digest = connection_id.removeprefix("account:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise LiveSourceError("google_invalid_connection")
    return f"account-{digest}.json"


def _credential_reference(provider: str, connection_id: str, root: Path) -> str:
    namespace = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]
    return f"google.{provider}.{namespace}.{connection_id.removeprefix('account:')}"


def _token_fields(
    value: Mapping[str, object],
    expected_scopes: tuple[str, ...],
    *,
    fallback_refresh: str | None = None,
    allow_omitted_scope: bool,
) -> tuple[str, str, int, tuple[str, ...]]:
    access_token = _secret(value.get("access_token"))
    refresh_token = (
        _secret(value.get("refresh_token")) if value.get("refresh_token") else fallback_refresh
    )
    if refresh_token is None or value.get("token_type") != "Bearer":
        raise LiveSourceError("google_auth_failed")
    expires_in = _expires_in(value.get("expires_in"))
    scope_value = value.get("scope")
    if (
        scope_value is None
        and allow_omitted_scope
        or type(scope_value) is str
        and tuple(sorted(scope_value.split())) == tuple(sorted(expected_scopes))
    ):
        scopes = expected_scopes
    else:
        raise LiveSourceError("google_scope_mismatch")
    return access_token, refresh_token, int(time.time()) + expires_in, scopes


def _http_error(status: int, retry_after_seconds: int | None) -> LiveSourceError:
    if status == 401:
        return LiveSourceError("google_auth_required")
    if status == 403:
        return LiveSourceError("google_not_allowed")
    if status == 429:
        return LiveSourceError("google_rate_limited", retry_after_seconds=retry_after_seconds)
    return LiveSourceError("google_auth_failed")


def _client_id(value: object) -> str:
    return _bounded_text(value, maximum=512)


def _secret(value: object) -> str:
    return _bounded_text(value, maximum=8_192)


def _expires_in(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 86_400:
        raise LiveSourceError("google_auth_failed")
    return value


def _expires_at_epoch(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 4_102_444_800:
        raise LiveSourceError("google_auth_failed")
    return value


def _bounded_text(value: object, *, maximum: int) -> str:
    try:
        return safe_text(cast(str, value), maximum=maximum)
    except LiveSourceError:
        raise LiveSourceError("google_auth_failed") from None
