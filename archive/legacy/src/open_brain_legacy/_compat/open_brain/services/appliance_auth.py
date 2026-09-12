# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Purpose-bound appliance credentials and bounded browser session state."""

from __future__ import annotations

import base64
import hmac
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.cookies import SimpleCookie

APPLIANCE_SESSION_COOKIE = "open-brain-session"
SECURE_APPLIANCE_SESSION_COOKIE = "__Host-open-brain-session"
_DEFAULT_SESSION_TTL = timedelta(hours=8)
_DEFAULT_MAXIMUM_SESSIONS = 16


@dataclass(frozen=True, slots=True)
class ApplianceBrowserSession:
    session_id: str
    csrf_token: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not _valid_token(self.session_id) or not _valid_token(self.csrf_token):
            raise ValueError("invalid appliance browser session")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("invalid appliance browser session")
        object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class _StoredBrowserSession:
    session_id: bytes
    csrf_token: bytes
    expires_at: datetime


class ApplianceBrowserSessionStore:
    """Issue and validate host-only browser sessions without persisting secrets."""

    def __init__(
        self,
        *,
        expected_bootstrap_credential: str,
        now: Callable[[], datetime] | None = None,
        session_ttl: timedelta = _DEFAULT_SESSION_TTL,
        maximum_sessions: int = _DEFAULT_MAXIMUM_SESSIONS,
        secure_cookie: bool = False,
    ) -> None:
        if not _valid_token(expected_bootstrap_credential):
            raise ValueError("invalid browser bootstrap credential")
        if (
            now is not None
            and not callable(now)
            or not isinstance(session_ttl, timedelta)
            or session_ttl <= timedelta()
            or type(maximum_sessions) is not int
            or not 1 <= maximum_sessions <= 128
            or type(secure_cookie) is not bool
        ):
            raise ValueError("invalid appliance browser session store")
        self._expected_bootstrap_credential = expected_bootstrap_credential.encode("ascii")
        self._now = _utc_now if now is None else now
        self._session_ttl = session_ttl
        self._maximum_sessions = maximum_sessions
        self._cookie_name = (
            SECURE_APPLIANCE_SESSION_COOKIE if secure_cookie else APPLIANCE_SESSION_COOKIE
        )
        self._secure_cookie = secure_cookie
        self._sessions: OrderedDict[str, _StoredBrowserSession] = OrderedDict()
        self._lock = threading.Lock()

    def create_session(self, credential: str) -> ApplianceBrowserSession:
        if not _compare_ascii_token(credential, self._expected_bootstrap_credential):
            raise ValueError("invalid browser bootstrap credential")
        current = self._current_time()
        with self._lock:
            self._prune(current)
            session = ApplianceBrowserSession(
                session_id=_issued_token(),
                csrf_token=_issued_token(),
                expires_at=current + self._session_ttl,
            )
            self._sessions[session.session_id] = _StoredBrowserSession(
                session_id=session.session_id.encode("ascii"),
                csrf_token=session.csrf_token.encode("ascii"),
                expires_at=session.expires_at,
            )
            while len(self._sessions) > self._maximum_sessions:
                self._sessions.popitem(last=False)
            return session

    def authenticate(self, *, cookie_header: str | None, csrf_token: str | None) -> bool:
        current = self._current_time()
        with self._lock:
            self._prune(current)
            stored = self._stored_session(cookie_header, current=current)
            if stored is None:
                return False
            if not isinstance(csrf_token, str) or not _valid_token(csrf_token):
                return False
            return _compare_ascii_token(csrf_token, stored.csrf_token)

    def authenticate_session(self, *, cookie_header: str | None) -> bool:
        current = self._current_time()
        with self._lock:
            self._prune(current)
            return self._stored_session(cookie_header, current=current) is not None

    def logout(self, *, cookie_header: str | None) -> bool:
        current = self._current_time()
        with self._lock:
            self._prune(current)
            session_id = _session_cookie(cookie_header, self._cookie_name)
            if session_id is None:
                return False
            return self._sessions.pop(session_id, None) is not None

    def set_cookie_header(self, session: ApplianceBrowserSession) -> str:
        secure = "; Secure" if self._secure_cookie else ""
        return (
            f"{self._cookie_name}={session.session_id}; Path=/; HttpOnly; SameSite=Strict{secure}"
        )

    def clear_cookie_header(self) -> str:
        secure = "; Secure" if self._secure_cookie else ""
        return f"{self._cookie_name}=deleted; Max-Age=0; Path=/; HttpOnly; SameSite=Strict{secure}"

    def _prune(self, current: datetime) -> None:
        expired = [
            session_id
            for session_id, stored in self._sessions.items()
            if stored.expires_at <= current
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)

    def _current_time(self) -> datetime:
        current = self._now()
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            raise ValueError("invalid appliance browser session store")
        return current.astimezone(UTC)

    def _stored_session(
        self,
        cookie_header: str | None,
        *,
        current: datetime,
    ) -> _StoredBrowserSession | None:
        session_id = _session_cookie(cookie_header, self._cookie_name)
        if session_id is None:
            return None
        stored = self._sessions.get(session_id)
        if stored is None or stored.expires_at <= current:
            self._sessions.pop(session_id, None)
            return None
        if not _compare_ascii_token(session_id, stored.session_id):
            return None
        return stored


def derive_appliance_credential(seed: str, *, purpose: str) -> str:
    if not _valid_seed(seed) or not _valid_purpose(purpose):
        raise ValueError("invalid appliance credential derivation")
    digest = hmac.new(
        key=seed.encode("utf-8"),
        msg=purpose.encode("ascii"),
        digestmod=sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def allowed_origin_for_host(host: str, port: int) -> str:
    if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65_535:
        raise ValueError("invalid appliance origin")
    normalized = f"[{host}]" if ":" in host else host
    return f"http://{normalized}:{port}"


def _compare_ascii_token(candidate: str, expected: bytes) -> bool:
    return _valid_token(candidate) and hmac.compare_digest(candidate.encode("ascii"), expected)


def _session_cookie(cookie_header: str | None, cookie_name: str) -> str | None:
    if not isinstance(cookie_header, str) or not cookie_header:
        return None
    try:
        cookie = SimpleCookie()
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(cookie_name)
    if morsel is None:
        return None
    value = morsel.value
    return value if _valid_token(value) else None


def _issued_token() -> str:
    return secrets.token_urlsafe(24)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _valid_purpose(value: str) -> bool:
    return _valid_token(value) and len(value) <= 64


def _valid_seed(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= 4_096


def _valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isascii()
        and value == value.strip()
        and all(not character.isspace() for character in value)
    )


__all__ = [
    "APPLIANCE_SESSION_COOKIE",
    "ApplianceBrowserSession",
    "ApplianceBrowserSessionStore",
    "SECURE_APPLIANCE_SESSION_COOKIE",
    "allowed_origin_for_host",
    "derive_appliance_credential",
]
