from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from open_brain_engine.core.locks import LockScope
from open_brain_engine.storage.filesystem import assert_root_identity
from open_brain_engine.storage.locks import FileLease

from .contracts import LocalEngineContext
from .normalization import _utc_now

Clock = Callable[[], datetime]


class DaemonAuthorityError(RuntimeError):
    """The daemon lifetime authority capability is missing or invalid."""


class DaemonAuthorityStaleError(DaemonAuthorityError):
    """The daemon lifetime authority capability is no longer active."""


class DaemonAuthorityRootMismatchError(DaemonAuthorityError):
    """The daemon lifetime authority capability is bound to another root."""


class DaemonAuthorityCapability:
    _token: object
    __slots__ = ("_token",)

    def __init__(self) -> None:
        raise TypeError("daemon authority capabilities are issuer-created")

    @classmethod
    def _issued(cls, token: object) -> DaemonAuthorityCapability:
        instance = object.__new__(cls)
        instance._token = token
        return instance

    def _authority_token(self) -> object:
        return self._token


@dataclass(frozen=True, slots=True)
class _AuthorityRecord:
    root: Path
    root_identity: tuple[int, int]


_ACTIVE_AUTHORITIES: dict[object, _AuthorityRecord] = {}


@contextmanager
def acquire_daemon_authority(
    profile: LocalEngineContext,
    *,
    clock: Clock | None = None,
) -> Iterator[DaemonAuthorityCapability]:
    if not isinstance(profile, LocalEngineContext):
        raise ValueError("invalid local profile")
    assert_root_identity(profile.root, profile.root_identity)
    lease = FileLease(
        profile.root / ".open-brain",
        "daemon-" + sha256(profile.owner_actor_id.encode("utf-8")).hexdigest()[:32],
        clock=_utc_now if clock is None else clock,
        parent_root_identity=profile.root_identity,
    )
    token = object()
    with lease.acquire(LockScope.DAEMON_AUTHORITY):
        _ACTIVE_AUTHORITIES[token] = _AuthorityRecord(
            root=profile.root,
            root_identity=profile.root_identity,
        )
        try:
            yield DaemonAuthorityCapability._issued(token)
        finally:
            _ACTIVE_AUTHORITIES.pop(token, None)


def require_daemon_authority(
    profile: LocalEngineContext,
    authority: object | None,
) -> None:
    if not isinstance(profile, LocalEngineContext):
        raise ValueError("invalid local profile")
    if not isinstance(authority, DaemonAuthorityCapability):
        raise DaemonAuthorityError("daemon authority capability is missing")
    record = _ACTIVE_AUTHORITIES.get(authority._authority_token())
    if record is None:
        raise DaemonAuthorityStaleError("daemon authority capability is stale")
    assert_root_identity(profile.root, profile.root_identity)
    if record.root != profile.root or record.root_identity != profile.root_identity:
        raise DaemonAuthorityRootMismatchError("daemon authority root mismatch")


__all__ = [
    "DaemonAuthorityCapability",
    "DaemonAuthorityError",
    "DaemonAuthorityRootMismatchError",
    "DaemonAuthorityStaleError",
    "acquire_daemon_authority",
    "require_daemon_authority",
]
