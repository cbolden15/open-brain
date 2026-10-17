"""Owner-local embedding consent: one versioned policy row and append-only history.

Local embedding consent is separate from provider egress consent and from model
installation. Only owner-local authority changes it; restore never re-enables it.
Every transition bumps ``consent_version`` and ``policy_generation`` in one
transaction; the policy generation is the currentness fence for derived state.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .consent_contracts import EgressMode
from .normalization import _timestamp
from .t03_contracts import EffectiveAuthority, SessionOrigin

if TYPE_CHECKING:
    from .local import BrainEngine

EMBEDDING_POLICY_STATUS_SCHEMA = "ob.embedding-policy-status.v1"
_COMPONENT_ID = re.compile(r"[0-9a-f]{64}")


class EmbeddingPolicyError(ValueError):
    """Closed embedding policy failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EmbeddingPolicy:
    brain_id: str
    issuer_epoch: int
    enabled: bool
    component_id: str | None
    consent_version: int
    policy_generation: int
    changed_at: str | None

    def status(self) -> dict[str, object]:
        return {
            "schema": EMBEDDING_POLICY_STATUS_SCHEMA,
            "enabled": self.enabled,
            "component_id": self.component_id,
            "consent_version": self.consent_version,
        }


class ComponentVerifier(Protocol):
    """Typed hook for installed-resource verification (supplied by a later helper)."""

    def __call__(self, component_id: str) -> None: ...


def validate_component_id(value: object) -> str:
    if type(value) is not str or _COMPONENT_ID.fullmatch(value) is None:
        raise EmbeddingPolicyError("invalid_component_id")
    return value


def read_policy(connection: sqlite3.Connection) -> EmbeddingPolicy:
    rows = connection.execute(
        "SELECT brain_id,issuer_epoch,enabled,component_id,consent_version,policy_generation,"
        "changed_at FROM embedding_policy"
    ).fetchall()
    if len(rows) != 1:
        raise EmbeddingPolicyError("invalid_policy")
    brain_id, epoch, enabled, component, version, generation, changed = tuple(rows[0])
    identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    if identity is None or tuple(identity) != (brain_id, epoch):
        # Consent given under another issuer never authorizes this one. issuer_epoch is
        # immutable by trigger, so this only fires on damaged or foreign state; any future
        # in-place epoch rotation must reset the policy row (disabled, no component).
        enabled, component = 0, None
    return EmbeddingPolicy(brain_id, epoch, enabled == 1, component, version, generation, changed)


def _require_owner(connection: sqlite3.Connection, authority: EffectiveAuthority) -> None:
    if (
        type(authority) is not EffectiveAuthority
        or not authority.owner
        or authority.egress_mode is not EgressMode.OWNER_LOCAL
        or authority.session_origin is not SessionOrigin.LOCAL_OWNER
    ):
        raise EmbeddingPolicyError("owner_required")
    if authority.brain_id is not None:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        if identity is None or tuple(identity) != (authority.brain_id, authority.issuer_epoch):
            raise EmbeddingPolicyError("owner_required")


def _transition(
    connection: sqlite3.Connection,
    *,
    authority: EffectiveAuthority,
    component_id: str | None,
    changed_at: str,
) -> EmbeddingPolicy:
    if not connection.in_transaction:
        raise EmbeddingPolicyError("transaction_required")
    _require_owner(connection, authority)
    current = read_policy(connection)
    latest = connection.execute("SELECT max(version) FROM embedding_policy_history").fetchone()[0]
    version = max(current.consent_version, latest or 0) + 1
    connection.execute(
        "INSERT INTO embedding_policy_history VALUES(?,?,?,?)",
        (version, "disable" if component_id is None else "enable", component_id, changed_at),
    )
    connection.execute(
        "UPDATE embedding_policy SET enabled=?,component_id=?,consent_version=?,"
        "policy_generation=policy_generation+1,changed_at=? WHERE singleton=1",
        (0 if component_id is None else 1, component_id, version, changed_at),
    )
    return read_policy(connection)


def enable(
    connection: sqlite3.Connection,
    *,
    authority: EffectiveAuthority,
    component_id: str,
    changed_at: str,
) -> EmbeddingPolicy:
    """Owner-only enable inside the caller's write transaction."""
    return _transition(
        connection,
        authority=authority,
        component_id=validate_component_id(component_id),
        changed_at=changed_at,
    )


def disable(
    connection: sqlite3.Connection, *, authority: EffectiveAuthority, changed_at: str
) -> EmbeddingPolicy:
    """Owner-only disable; the generation bump fences every later currentness check."""
    return _transition(connection, authority=authority, component_id=None, changed_at=changed_at)


def history_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        {"action": action, "changed_at": changed, "component_id": component, "version": version}
        for version, action, component, changed in connection.execute(
            "SELECT version,action,component_id,changed_at FROM embedding_policy_history "
            "ORDER BY version"
        )
    ]


def install_restored_history(
    connection: sqlite3.Connection, rows: list[dict[str, object]]
) -> None:
    """Install validated history and leave the destination disabled at its own version."""
    for row in rows:
        connection.execute(
            "INSERT INTO embedding_policy_history VALUES(?,?,?,?)",
            (row["version"], row["action"], row["component_id"], row["changed_at"]),
        )
    if rows:
        connection.execute(
            "UPDATE embedding_policy SET enabled=0,component_id=NULL,consent_version=?,"
            "policy_generation=policy_generation+1,changed_at=NULL WHERE singleton=1",
            (max(int(row["version"]) for row in rows) + 1,),  # type: ignore[call-overload]
        )


def _unverified(_component_id: str) -> None:
    """Format-only placeholder until installed-resource verification exists."""


class EmbeddingPolicyTasks:
    """Owner-local policy operations; not exposed to MCP, plugin or desktop surfaces."""

    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._engine._assert_root()
        with (
            self._engine._writer_lease.acquire_shared_writer(),
            self._engine._store.transaction() as connection,
        ):
            yield connection

    def status(self, *, authority: EffectiveAuthority) -> EmbeddingPolicy:
        self._engine._assert_root()
        with closing(self._engine._store.connect()) as connection:
            _require_owner(connection, authority)
            return read_policy(connection)

    def enable(
        self,
        component_id: str,
        *,
        authority: EffectiveAuthority,
        verify_component: ComponentVerifier | Callable[[str], None] = _unverified,
    ) -> EmbeddingPolicy:
        component = validate_component_id(component_id)
        verify_component(component)
        with self._write() as connection:
            return enable(
                connection,
                authority=authority,
                component_id=component,
                changed_at=_timestamp(self._engine._clock()),
            )

    def disable(self, *, authority: EffectiveAuthority) -> EmbeddingPolicy:
        with self._write() as connection:
            return disable(
                connection, authority=authority, changed_at=_timestamp(self._engine._clock())
            )


__all__ = [
    "EMBEDDING_POLICY_STATUS_SCHEMA",
    "ComponentVerifier",
    "EmbeddingPolicy",
    "EmbeddingPolicyError",
    "EmbeddingPolicyTasks",
    "disable",
    "enable",
    "history_rows",
    "install_restored_history",
    "read_policy",
    "validate_component_id",
]
