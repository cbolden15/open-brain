"""Explicit PKCE authorization through a bounded, temporary loopback callback."""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

from open_brain_connectors.runtime.live_common import LiveSourceError


@dataclass(frozen=True, slots=True)
class OAuthCode:
    code: str = field(repr=False)
    verifier: str = field(repr=False)
    redirect_uri: str


class _Server(HTTPServer):
    expected_state: str
    expected_host: str
    callback_path: str
    result: str | None = None
    refused: bool = False

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, address = super().get_request()
        connection.settimeout(0.5)
        return connection, cast(tuple[str, int], address)

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]
    ) -> None:
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "OpenBrainOAuth"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        server = cast(_Server, self.server)
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        terminal = ("code" in query) != ("error" in query)
        valid = (
            len(self.path) <= 16_384
            and parsed.scheme == ""
            and parsed.netloc == ""
            and not parsed.fragment
            and parsed.path == server.callback_path
            and self.headers.get_all("Host") == [server.expected_host]
            and query.get("state") == [server.expected_state]
            and terminal
            and all(len(values) == 1 for values in query.values())
            and server.result is None
            and not server.refused
        )
        result = query.get("code", query.get("error", [""]))[0]
        if not result or len(result) > 8192 or any(ord(c) < 33 for c in result):
            valid = False
        if valid:
            server.refused = "error" in query
            server.result = None if server.refused else result
        body = (
            b"Open Brain received the sign-in result. You can close this tab."
            if valid
            else b"Invalid sign-in callback."
        )
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def authorize_loopback(
    build_url: Callable[[str, str, str], str],
    *,
    timeout_seconds: int = 180,
    browser_opener: Callable[[str], object] | None = None,
    port: int = 0,
    hostname: str = "127.0.0.1",
    callback_path: str = "/oauth2/callback",
) -> OAuthCode:
    if (
        type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= 600
        or type(port) is not int
        or not 0 <= port <= 65535
        or hostname not in {"127.0.0.1", "localhost"}
        or callback_path != "/oauth2/callback"
    ):
        raise LiveSourceError("oauth_invalid_configuration")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    try:
        server = _Server(("127.0.0.1", port), _Handler)
    except OSError:
        raise LiveSourceError("oauth_callback_unavailable") from None
    with server:
        server.timeout = 0.25
        server.expected_state = state
        server.expected_host = f"{hostname}:{server.server_port}"
        server.callback_path = callback_path
        redirect = f"http://{server.expected_host}{callback_path}"
        url = build_url(redirect, state, challenge.rstrip(b"=").decode("ascii"))
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc not in {"accounts.google.com", "slack.com"}
            or parsed.path not in {"/o/oauth2/v2/auth", "/oauth/v2/authorize"}
            or parsed.fragment
        ):
            raise LiveSourceError("oauth_invalid_authorization_url")
        deadline = time.monotonic() + timeout_seconds
        try:
            opened = (browser_opener or webbrowser.open)(url)
        except Exception:
            raise LiveSourceError("oauth_browser_unavailable") from None
        if opened is False:
            raise LiveSourceError("oauth_browser_unavailable")
        while server.result is None and not server.refused:
            if time.monotonic() >= deadline:
                raise LiveSourceError("oauth_callback_timeout")
            server.handle_request()
        if server.refused:
            raise LiveSourceError("oauth_consent_declined")
        return OAuthCode(cast(str, server.result), verifier, redirect)
