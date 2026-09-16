"""Slack public-client PKCE authorization for selected-channel capture."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast
from urllib.parse import urlencode, urlsplit

from open_brain_connectors.runtime.live_common import (
    LiveAccount,
    LiveCredentialStore,
    LiveSourceError,
)
from open_brain_connectors.runtime.live_http import LiveHttpTransport
from open_brain_connectors.runtime.live_oauth import authorize_loopback
from open_brain_connectors.runtime.live_storage import OsCredentialStore, PrivateJsonStore

__all__ = ["SLACK_USER_SCOPES", "SlackAuth"]

_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
_ACCOUNT_NAME = re.compile(r"slack-account-([0-9a-f]{64})\.json")
_CLIENT_ID = re.compile(r"[0-9]+\.[0-9]+")
_REDIRECT_PATH = "/oauth2/callback"
_REFRESH_SKEW_SECONDS = 60

SLACK_USER_SCOPES = (
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
)


class SlackAuth:
    """Store Slack tokens in the OS credential store and metadata privately on disk."""

    def __init__(
        self,
        root: Path,
        *,
        credentials: LiveCredentialStore | None = None,
        http: LiveHttpTransport | None = None,
    ) -> None:
        if not isinstance(root, Path):
            raise LiveSourceError("invalid_auth_store")
        self._store = PrivateJsonStore(root)
        self._credentials = credentials or OsCredentialStore()
        self._http = http or LiveHttpTransport()

    def connect(self, client_config: Path, **loopback_options: object) -> LiveAccount:
        config = _load_client_config(client_config)
        callback_port = _redirect_port(config.redirect_uri)
        options = dict(loopback_options)
        if set(options) - {"timeout_seconds", "browser_opener"}:
            raise LiveSourceError("invalid_auth_request")
        timeout_seconds = options.get("timeout_seconds", 180)
        browser_opener = options.get("browser_opener")
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 600
            or browser_opener is not None
            and not callable(browser_opener)
        ):
            raise LiveSourceError("invalid_auth_request")

        def build_url(redirect_uri: str, state: str, challenge: str) -> str:
            if redirect_uri != config.redirect_uri:
                raise LiveSourceError("invalid_auth_request")
            return (
                _AUTHORIZE_URL
                + "?"
                + urlencode(
                    {
                        "client_id": config.client_id,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "redirect_uri": redirect_uri,
                        "response_type": "code",
                        "state": state,
                        "user_scope": ",".join(SLACK_USER_SCOPES),
                    }
                )
            )

        try:
            code = authorize_loopback(
                build_url,
                hostname="localhost",
                port=callback_port,
                callback_path=_REDIRECT_PATH,
                timeout_seconds=timeout_seconds,
                browser_opener=cast(Callable[[str], object] | None, browser_opener),
            )
        except LiveSourceError:
            raise
        except Exception as error:  # pragma: no cover - loopback boundary is shared.
            raise LiveSourceError("authorization_failed") from error
        payload = self._request_token(
            {
                "client_id": config.client_id,
                "code": code.code,
                "code_verifier": code.verifier,
                "grant_type": "authorization_code",
                "redirect_uri": code.redirect_uri,
            }
        )
        token = _token_from_payload(payload)
        account = _account_from_payload(payload)
        credential_ref = _credential_reference(account.connection_id, self._store.root)
        name = _account_name(account.connection_id)
        with self._store.lock(name):
            self._credentials.set(
                credential_ref,
                {
                    "access_token": token.access_token,
                    "client_id": config.client_id,
                    "expires_at_epoch": account.expires_at_epoch,
                    "refresh_token": token.refresh_token,
                },
            )
            self._store.write(
                name,
                {
                    "account": account.to_dict(),
                    "credential_ref": credential_ref,
                    "schema_version": 1,
                },
            )
        return account

    def accounts(self) -> tuple[LiveAccount, ...]:
        accounts: list[LiveAccount] = []
        for name in self._store.names("slack-account-"):
            match = _ACCOUNT_NAME.fullmatch(name)
            if match is None:
                continue
            accounts.append(_metadata_account(self._read_metadata(name), name))
        return tuple(sorted(accounts, key=lambda account: account.connection_id))

    def access_token(self, connection_id: str) -> str:
        name = _account_name(connection_id)
        with self._store.lock(name):
            metadata = self._read_metadata(name)
            account = _metadata_account(metadata, name)
            reference = cast(str, metadata["credential_ref"])
            credential = self._credential(reference)
            access_token = _required_secret(credential.get("access_token"))
            expires_at = account.expires_at_epoch
            if expires_at is None or expires_at > int(time.time()) + _REFRESH_SKEW_SECONDS:
                return access_token
            refreshed = self._request_token(
                {
                    "client_id": _required_secret(credential.get("client_id")),
                    "grant_type": "refresh_token",
                    "refresh_token": _required_secret(credential.get("refresh_token")),
                }
            )
            refreshed_token = _token_from_payload(refreshed)
            refreshed_account = _account_from_payload(refreshed, fallback=account)
            if refreshed_account.connection_id != account.connection_id:
                raise LiveSourceError("auth_required")
            self._credentials.set(
                reference,
                {
                    "access_token": refreshed_token.access_token,
                    "client_id": _required_secret(credential.get("client_id")),
                    "expires_at_epoch": refreshed_account.expires_at_epoch,
                    "refresh_token": refreshed_token.refresh_token
                    or _required_secret(credential.get("refresh_token")),
                },
            )
            self._store.write(
                name,
                {
                    "account": refreshed_account.to_dict(),
                    "credential_ref": reference,
                    "schema_version": 1,
                },
            )
            return refreshed_token.access_token

    def disconnect(self, connection_id: str) -> None:
        name = _account_name(connection_id)
        with self._store.lock(name):
            metadata = self._read_metadata(name)
            self._credentials.delete(cast(str, metadata["credential_ref"]))
            self._store.delete(name)

    def _read_metadata(self, name: str) -> dict[str, object]:
        metadata = _metadata(self._store.read(name), name)
        account = _metadata_account(metadata, name)
        if metadata["credential_ref"] != _credential_reference(
            account.connection_id, self._store.root
        ):
            raise LiveSourceError("invalid_auth_store")
        return metadata

    def _credential(self, reference: str) -> dict[str, object]:
        status = self._credentials.status(reference)
        if status == "locked":
            raise LiveSourceError("credential_locked")
        if status != "available":
            raise LiveSourceError("auth_required")
        value = self._credentials.get(reference)
        if value is None:
            raise LiveSourceError("auth_required")
        return value

    def _request_token(self, form: dict[str, str]) -> dict[str, object]:
        response = self._http.request("POST", _TOKEN_URL, form=form)
        if response.status == 429:
            raise LiveSourceError(
                "rate_limited", retry_after_seconds=response.retry_after_seconds()
            )
        if response.status >= 500:
            raise LiveSourceError("provider_unavailable")
        if response.status != 200:
            raise LiveSourceError("auth_required")
        payload = response.json()
        if payload.get("ok") is not True:
            raise LiveSourceError(_oauth_error(payload.get("error")))
        return payload


class _ClientConfig:
    def __init__(self, client_id: str, redirect_uri: str) -> None:
        self.client_id = client_id
        self.redirect_uri = redirect_uri


class _Token:
    def __init__(self, access_token: str, refresh_token: str | None) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token


def _load_client_config(path: Path) -> _ClientConfig:
    if not isinstance(path, Path) or not path.is_absolute():
        raise LiveSourceError("invalid_client_config")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LiveSourceError("invalid_client_config") from error
    if not isinstance(value, dict) or set(value) != {"client_id", "redirect_uri"}:
        raise LiveSourceError("invalid_client_config")
    client_id = value.get("client_id")
    redirect_uri = value.get("redirect_uri")
    if type(client_id) is not str or _CLIENT_ID.fullmatch(client_id) is None:
        raise LiveSourceError("invalid_client_config")
    if type(redirect_uri) is not str or _redirect_port(redirect_uri) is None:
        raise LiveSourceError("invalid_client_config")
    return _ClientConfig(client_id, redirect_uri)


def _redirect_port(redirect_uri: str) -> int:
    parsed = urlsplit(redirect_uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "localhost"
        or parsed.path != _REDIRECT_PATH
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
    ):
        raise LiveSourceError("invalid_client_config")
    return parsed.port


def _token_from_payload(payload: Mapping[str, object]) -> _Token:
    user = payload.get("authed_user")
    if not isinstance(user, Mapping):
        raise LiveSourceError("invalid_token_response")
    access_token = _required_secret(user.get("access_token"))
    refresh_value = user.get("refresh_token")
    refresh_token = None if refresh_value is None else _required_secret(refresh_value)
    return _Token(access_token, refresh_token)


def _account_from_payload(
    payload: Mapping[str, object], *, fallback: LiveAccount | None = None
) -> LiveAccount:
    user = payload.get("authed_user")
    team = payload.get("team")
    if isinstance(user, Mapping):
        scopes = _scopes(user.get("scope"))
        expires_at = _expires_at(user.get("expires_in"))
    else:
        raise LiveSourceError("invalid_token_response")
    if isinstance(team, Mapping):
        user_id = _identifier(user.get("id"))
        team_id = _identifier(team.get("id"))
        connection_id = (
            "account:" + hashlib.sha256(f"slack:{team_id}:{user_id}".encode()).hexdigest()
        )
        return LiveAccount("slack", connection_id, "Slack", scopes, expires_at)
    if fallback is not None:
        return LiveAccount(
            fallback.provider,
            fallback.connection_id,
            fallback.display_name,
            scopes,
            expires_at,
        )
    raise LiveSourceError("invalid_token_response")


def _metadata(value: object, name: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != {"account", "credential_ref", "schema_version"}
        or value.get("schema_version") != 1
        or type(value.get("credential_ref")) is not str
    ):
        raise LiveSourceError("invalid_auth_store")
    return value


def _metadata_account(value: object, name: str) -> LiveAccount:
    metadata = _metadata(value, name)
    try:
        account = LiveAccount.from_dict(metadata["account"])
    except (TypeError, ValueError, KeyError) as error:
        raise LiveSourceError("invalid_auth_store") from error
    if account.provider != "slack" or name != _account_name(account.connection_id):
        raise LiveSourceError("invalid_auth_store")
    return account


def _account_name(connection_id: str) -> str:
    if type(connection_id) is not str or not connection_id.startswith("account:"):
        raise LiveSourceError("invalid_connection")
    digest = connection_id.removeprefix("account:")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise LiveSourceError("invalid_connection")
    return f"slack-account-{digest}.json"


def _credential_reference(connection_id: str, root: Path) -> str:
    namespace = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]
    return "slack:" + namespace + ":" + connection_id.removeprefix("account:")


def _scopes(value: object) -> tuple[str, ...]:
    if type(value) is not str:
        raise LiveSourceError("invalid_token_response")
    granted = tuple(sorted(part for part in re.split(r"[ ,]+", value) if part))
    if granted != SLACK_USER_SCOPES:
        raise LiveSourceError("invalid_scope")
    return granted


def _expires_at(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 2_592_000:
        raise LiveSourceError("invalid_token_response")
    return int(time.time()) + value


def _identifier(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Z0-9]{2,128}", value) is None:
        raise LiveSourceError("invalid_token_response")
    return value


def _required_secret(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value or len(value) > 8192:
        raise LiveSourceError("invalid_token_response")
    return value


def _oauth_error(value: object) -> str:
    if value in {"access_denied", "missing_scope"}:
        return "not_allowed"
    if value in {"invalid_auth", "token_revoked", "invalid_code"}:
        return "auth_required"
    return "authorization_failed"
