"""Private query-owned historical inputs, with fresh validation before output.

The owner holds the reader lease and supplies one nonescaping read-only BEGIN.
This view never caches an authorization across queries or retained-body checks.
Application consent and provider policy remain the caller's responsibility.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import get_ident

from open_brain_engine.storage.filesystem import StorageError

from .contracts import LocalEngineContext
from .historical_dispatch import Transition
from .historical_registry import HistoricalClaimRegistry
from .sharing_contracts import SharingError


@dataclass(frozen=True, slots=True)
class HistoricalAuthorityBundle:
    registry: HistoricalClaimRegistry
    records: tuple[Transition, ...]


type Grant = tuple[str, bool, str | None, str | None, int | None]
_active: ContextVar[_HistoricalReadSnapshot | None] = ContextVar(
    "historical_read_snapshot", default=None
)
_ownership: ContextVar[object | None] = ContextVar("historical_snapshot_owner", default=None)
_DENIALS = (SharingError, StorageError, KeyError, TypeError, ValueError, sqlite3.Error)


class _HistoricalReadSnapshot:
    def __init__(self, connection: sqlite3.Connection, profile: LocalEngineContext) -> None:
        self.connection = connection
        self.profile = profile
        self.thread = get_ident()
        self.total_changes = connection.total_changes
        self.alive = True
        self.bundle: HistoricalAuthorityBundle | None = None
        self.denied = False
        self.invalid = False
        self.grants: set[Grant] = set()
        self.marker = object()
        self.owner_token: Token[object | None] = _ownership.set(self.marker)

    def owns_context(self) -> bool:
        if not self.alive or self.thread != get_ident() or _active.get() is not self:
            return False
        # Tokens can only be reset in their originating Context. This rejects a
        # copied Context even on the same thread while the original remains live.
        try:
            _ownership.reset(self.owner_token)
        except ValueError:
            return False
        self.owner_token = _ownership.set(self.marker)
        return True

    def assert_snapshot(self) -> None:
        if (
            not self.owns_context()
            or not self.connection.in_transaction
            or self.connection.total_changes != self.total_changes
            or self.connection.execute("PRAGMA query_only").fetchone()[0] != 1
        ):
            raise SharingError("operation_pending")

    def authorize_sql(
        self,
        action: int,
        first: str | None,
        second: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        # This owned connection has no prior authorizer. Protect the exact BEGIN,
        # including rollback()/commit(), not just plausible transaction flags.
        if action in (
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_SAVEPOINT,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
        ) or (
            action == sqlite3.SQLITE_PRAGMA
            and first is not None
            and first.casefold() == "query_only"
            and second is not None
        ):
            self.invalid = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def visibility(
        self,
        capture_id: str,
        *,
        local_history: bool,
        provider_id: str | None,
        brain_id: str | None,
        issuer_epoch: int | None,
    ) -> bool | None:
        from .historical_visibility import (
            evaluate_historical_visibility,
            load_historical_authority,
            read_historical_registry,
        )

        if self.denied or self.invalid:
            return False
        key = (capture_id, local_history, provider_id, brain_id, issuer_epoch)
        try:
            self.assert_snapshot()
            if self.bundle is None:
                self.bundle = load_historical_authority(self.connection, self.profile)
            else:
                if read_historical_registry(self.connection, self.profile) != self.bundle.registry:
                    raise SharingError("binding_mismatch")
        except _DENIALS:
            if self.bundle is None:
                self.denied = True
            else:
                self.invalid = True
            return False
        try:
            result = evaluate_historical_visibility(
                self.connection,
                self.profile,
                self.bundle,
                capture_id,
                local_history=local_history,
                provider_id=provider_id,
                brain_id=brain_id,
                issuer_epoch=issuer_epoch,
            )
        except _DENIALS:
            result = False
        if result is True:
            self.grants.add(key)
        elif key in self.grants:
            self.invalid = True
        return result

    def finish(self) -> None:
        from .historical_visibility import evaluate_historical_visibility, load_historical_authority

        if self.invalid:
            raise SharingError("operation_pending")
        if self.bundle is None:
            return
        self.assert_snapshot()
        terminal = load_historical_authority(self.connection, self.profile)
        if terminal != self.bundle:
            raise SharingError("operation_pending")
        for capture_id, local_history, provider_id, brain_id, issuer_epoch in self.grants:
            if (
                evaluate_historical_visibility(
                    self.connection,
                    self.profile,
                    terminal,
                    capture_id,
                    local_history=local_history,
                    provider_id=provider_id,
                    brain_id=brain_id,
                    issuer_epoch=issuer_epoch,
                )
                is not True
            ):
                raise SharingError("operation_pending")


def current_historical_snapshot(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
) -> _HistoricalReadSnapshot | None:
    scope = _active.get()
    if (
        scope is None
        or scope.connection is not connection
        or scope.profile.root != profile.root
        or scope.profile.root_identity != profile.root_identity
        or not scope.owns_context()
    ):
        return None
    return scope


@contextmanager
def historical_read_snapshot(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
) -> Iterator[None]:
    current = _active.get()
    if current is not None and current.alive and current.connection is connection:
        raise SharingError("operation_pending")
    scope = _HistoricalReadSnapshot(connection, profile)
    token = _active.set(scope)
    try:
        connection.set_authorizer(scope.authorize_sql)
        yield
        try:
            scope.finish()
        except _DENIALS:
            raise SharingError("operation_pending") from None
    finally:
        scope.alive = False
        _active.reset(token)
        _ownership.reset(scope.owner_token)
        connection.set_authorizer(None)
