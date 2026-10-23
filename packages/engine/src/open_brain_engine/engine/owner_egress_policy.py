"""Owner egress policy: which valid privacy tiers external-provider sessions may read.

The policy is an owner-local overlay on effective privacy. It never rewrites
retained capture evidence, never grants ``cloud`` authority, and can only name
public, work or personal. Unknown, secret and invalid evidence never gain egress.
Every change appends one history row and bumps the monotone policy version in the
same transaction that refreshes every affected projection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ..core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from .consent_contracts import EgressMode
from .normalization import _timestamp
from .t03_contracts import EffectiveAuthority, SessionOrigin

if TYPE_CHECKING:
    from .local import BrainEngine

OWNER_EGRESS_POLICY_SCHEMA_VERSION = 22
OWNER_EGRESS_POLICY_STATUS_SCHEMA = "ob.owner-egress-policy-status.v1"
SHAREABLE_TIERS = frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL})
_MAX_OPERATION_ID = 128


class OwnerEgressPolicyError(ValueError):
    """Closed owner egress policy failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class OwnerEgressPolicy:
    version: int
    tiers: frozenset[PrivacyTier]
    recorded_at: str | None
    operation_id: str | None

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version < 0
            or not isinstance(self.tiers, frozenset)
            or not self.tiers <= SHAREABLE_TIERS
        ):
            raise OwnerEgressPolicyError("invalid_policy")

    @property
    def confirmation_ref(self) -> str:
        """The confirmation reference personal projections carry under this policy."""
        return f"owner-egress-policy:{self.version}"

    def status(self) -> dict[str, object]:
        return {
            "schema": OWNER_EGRESS_POLICY_STATUS_SCHEMA,
            "version": self.version,
            "tiers": sorted(tier.value for tier in self.tiers),
            "recorded_at": self.recorded_at,
            "operation_id": self.operation_id,
        }


_OVERLAY_REASON = {
    PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
    PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
    PrivacyTier.PERSONAL: PrivacyReason.PERSONAL_CONFIRMED,
}


def overlay_decision(tier: PrivacyTier, policy: OwnerEgressPolicy) -> PrivacyDecision | None:
    """The owner decision a shared tier projects under ``policy``, or ``None``.

    The decision always grants external egress and never cloud. Personal uses the
    confirmed reason bound to the policy version, as ``PrivacyDecision`` requires.
    """
    if tier not in policy.tiers or tier not in SHAREABLE_TIERS:
        return None
    return PrivacyDecision.create(
        tier=tier,
        reason=_OVERLAY_REASON[tier],
        policy_version=policy.confirmation_ref,
        authority=Authority(cloud=False, external_egress=True),
        confirmation_ref=policy.confirmation_ref if tier is PrivacyTier.PERSONAL else None,
    )


def validate_tiers(values: Iterable[object]) -> frozenset[PrivacyTier]:
    """Exact shareable tier values; anything else fails before mutation."""
    tiers: set[PrivacyTier] = set()
    for value in values:
        if type(value) is PrivacyTier:
            tier = value
        elif type(value) is str and value in {tier.value for tier in SHAREABLE_TIERS}:
            tier = PrivacyTier(value)
        else:
            raise OwnerEgressPolicyError("invalid_tier")
        if tier not in SHAREABLE_TIERS:
            raise OwnerEgressPolicyError("invalid_tier")
        tiers.add(tier)
    return frozenset(tiers)


def validate_operation_id(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= _MAX_OPERATION_ID:
        raise OwnerEgressPolicyError("invalid_operation_id")
    return value


def _tiers(row: sqlite3.Row | tuple[object, ...]) -> frozenset[PrivacyTier]:
    public, work, personal = row
    return frozenset(
        tier
        for tier, shared in (
            (PrivacyTier.PUBLIC, public),
            (PrivacyTier.WORK, work),
            (PrivacyTier.PERSONAL, personal),
        )
        if shared == 1
    )


def _policy_table_present(connection: sqlite3.Connection) -> bool:
    return bool(
        connection.execute("PRAGMA user_version").fetchone()[0]
        >= OWNER_EGRESS_POLICY_SCHEMA_VERSION
    )


def read_owner_egress_policy(connection: sqlite3.Connection) -> OwnerEgressPolicy:
    """Read the singleton policy; predecessor schemas share nothing."""
    if not _policy_table_present(connection):
        return OwnerEgressPolicy(0, frozenset(), None, None)
    rows = connection.execute(
        "SELECT version,share_public,share_work,share_personal,recorded_at,operation_id "
        "FROM owner_egress_policy"
    ).fetchall()
    if len(rows) != 1:
        raise OwnerEgressPolicyError("invalid_policy")
    version, public, work, personal, recorded_at, operation_id = tuple(rows[0])
    return OwnerEgressPolicy(version, _tiers((public, work, personal)), recorded_at, operation_id)


def active_egress_policy(connection: sqlite3.Connection) -> OwnerEgressPolicy | None:
    """The policy that overlays projections, or ``None`` while no tier is shareable."""
    policy = read_owner_egress_policy(connection)
    return policy if policy.tiers else None


def _require_owner(connection: sqlite3.Connection, authority: EffectiveAuthority) -> None:
    if (
        type(authority) is not EffectiveAuthority
        or not authority.owner
        or authority.egress_mode is not EgressMode.OWNER_LOCAL
        or authority.session_origin is not SessionOrigin.LOCAL_OWNER
    ):
        raise OwnerEgressPolicyError("owner_required")
    if authority.brain_id is not None:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        if identity is None or tuple(identity) != (authority.brain_id, authority.issuer_epoch):
            raise OwnerEgressPolicyError("owner_required")


def _history_policy(row: sqlite3.Row | tuple[object, ...]) -> OwnerEgressPolicy:
    version, public, work, personal, recorded_at, operation_id = tuple(row)
    assert type(version) is int
    return OwnerEgressPolicy(
        version,
        _tiers((public, work, personal)),
        None if recorded_at is None else str(recorded_at),
        None if operation_id is None else str(operation_id),
    )


def set_owner_egress_policy(
    connection: sqlite3.Connection,
    *,
    authority: EffectiveAuthority,
    tiers: Iterable[object],
    operation_id: str,
    recorded_at: str,
) -> OwnerEgressPolicy:
    """Owner-only replacement of the shareable tier set inside the caller's transaction.

    A repeated operation ID returns its original result when the tier set matches
    and conflicts otherwise; it never appends a second history row.
    """
    shared = validate_tiers(tiers)
    operation = validate_operation_id(operation_id)
    if not connection.in_transaction:
        raise OwnerEgressPolicyError("transaction_required")
    _require_owner(connection, authority)
    existing = connection.execute(
        "SELECT version,share_public,share_work,share_personal,recorded_at,operation_id "
        "FROM owner_egress_policy_history WHERE operation_id=?",
        (operation,),
    ).fetchone()
    if existing is not None:
        previous = _history_policy(existing)
        if previous.tiers != shared:
            raise OwnerEgressPolicyError("operation_conflict")
        return previous
    current = read_owner_egress_policy(connection)
    version = current.version + 1
    shares = tuple(
        int(tier in shared) for tier in (PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL)
    )
    connection.execute(
        "INSERT INTO owner_egress_policy_history VALUES(?,?,?,?,?,?)",
        (version, *shares, recorded_at, operation),
    )
    connection.execute(
        "UPDATE owner_egress_policy SET version=?,share_public=?,share_work=?,share_personal=?,"
        "recorded_at=?,operation_id=? WHERE singleton=1",
        (version, *shares, recorded_at, operation),
    )
    refresh_owner_egress_projections(connection)
    return read_owner_egress_policy(connection)


def refresh_owner_egress_projections(connection: sqlite3.Connection) -> None:
    """Re-project every stored effective-privacy row under the active policy.

    Runs in the caller's policy transaction, so content reads, search and cursors
    never observe a policy without its projections. Only changed rows are written;
    retained capture evidence and invalid-evidence markers are never touched.
    """
    from .privacy_projection import effective_privacy_json, project_retained_privacy_evidence
    from .privacy_repairs import resolve_search_privacy
    from .search_projection import write_search_privacy

    policy = active_egress_policy(connection)
    for row in list(
        connection.execute(
            "SELECT p.capture_id, p.effective_privacy_json, c.privacy_json "
            "FROM source_revision_privacy p LEFT JOIN captures c USING(capture_id) "
            "ORDER BY p.capture_id"
        )
    ):
        projected = effective_privacy_json(
            project_retained_privacy_evidence([row["privacy_json"]], egress_policy=policy)
        )
        if projected != row["effective_privacy_json"]:
            connection.execute(
                "UPDATE source_revision_privacy SET effective_privacy_json=? WHERE capture_id=?",
                (projected, row["capture_id"]),
            )
    for row in list(
        connection.execute(
            "SELECT revision_id, effective_privacy_json FROM canonical_revision_privacy "
            "ORDER BY revision_id"
        )
    ):
        values = tuple(
            member[0]
            for member in connection.execute(
                "SELECT c.privacy_json FROM canonical_revision_members m "
                "LEFT JOIN captures c USING(capture_id) WHERE m.revision_id=? ORDER BY m.ordinal",
                (row["revision_id"],),
            )
        )
        projected = effective_privacy_json(
            project_retained_privacy_evidence(values, egress_policy=policy)
        )
        if projected != row["effective_privacy_json"]:
            connection.execute(
                "UPDATE canonical_revision_privacy SET effective_privacy_json=? "
                "WHERE revision_id=?",
                (projected, row["revision_id"]),
            )
    for row in list(
        connection.execute(
            "SELECT result_id, capture_id, record_type, effective_cloud, "
            "effective_external_egress FROM search_documents ORDER BY result_id"
        )
    ):
        evidence, applied = resolve_search_privacy(
            connection,
            result_id=str(row["result_id"]),
            capture_id=str(row["capture_id"]),
            record_type=str(row["record_type"]),
        )
        if (int(evidence.authority.cloud), int(evidence.authority.external_egress)) != (
            row["effective_cloud"],
            row["effective_external_egress"],
        ):
            write_search_privacy(
                connection,
                result_id=str(row["result_id"]),
                evidence=evidence,
                applied_repair=applied,
            )
    connection.execute(
        "UPDATE engine_generations SET projection_policy_version=projection_policy_version+1 "
        "WHERE singleton=1"
    )


def history_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Retained transitions in version order, tiers as sorted wire values."""
    if not _policy_table_present(connection):
        return []
    return [
        {
            "operation_id": policy.operation_id,
            "recorded_at": policy.recorded_at,
            "tiers": sorted(tier.value for tier in policy.tiers),
            "version": policy.version,
        }
        for policy in (
            _history_policy(row)
            for row in connection.execute(
                "SELECT version,share_public,share_work,share_personal,recorded_at,operation_id "
                "FROM owner_egress_policy_history ORDER BY version"
            )
        )
    ]


def install_restored_policy(
    connection: sqlite3.Connection, rows: list[dict[str, object]]
) -> None:
    """Install validated Portable history and make its latest row the active policy."""
    if read_owner_egress_policy(connection).version != 0 or history_rows(connection):
        raise OwnerEgressPolicyError("invalid_policy")
    for row in rows:
        tiers = validate_tiers(cast(list[object], row["tiers"]))
        shares = tuple(
            int(tier in tiers)
            for tier in (PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL)
        )
        values = (row["version"], *shares, row["recorded_at"], row["operation_id"])
        connection.execute("INSERT INTO owner_egress_policy_history VALUES(?,?,?,?,?,?)", values)
        connection.execute(
            "UPDATE owner_egress_policy SET version=?,share_public=?,share_work=?,"
            "share_personal=?,recorded_at=?,operation_id=? WHERE singleton=1",
            values,
        )
    if rows:
        refresh_owner_egress_projections(connection)


class OwnerEgressPolicyTasks:
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

    def status(self, *, authority: EffectiveAuthority) -> OwnerEgressPolicy:
        self._engine._assert_root()
        with closing(self._engine._store.connect()) as connection:
            _require_owner(connection, authority)
            return read_owner_egress_policy(connection)

    def set(
        self,
        tiers: Iterable[object],
        *,
        operation_id: str,
        authority: EffectiveAuthority,
    ) -> OwnerEgressPolicy:
        shared = validate_tiers(tiers)
        operation = validate_operation_id(operation_id)
        with self._write() as connection:
            return set_owner_egress_policy(
                connection,
                authority=authority,
                tiers=shared,
                operation_id=operation,
                recorded_at=_timestamp(self._engine._clock()),
            )


__all__ = [
    "OWNER_EGRESS_POLICY_SCHEMA_VERSION",
    "OWNER_EGRESS_POLICY_STATUS_SCHEMA",
    "SHAREABLE_TIERS",
    "OwnerEgressPolicy",
    "OwnerEgressPolicyError",
    "OwnerEgressPolicyTasks",
    "active_egress_policy",
    "history_rows",
    "install_restored_policy",
    "overlay_decision",
    "read_owner_egress_policy",
    "refresh_owner_egress_projections",
    "set_owner_egress_policy",
    "validate_operation_id",
    "validate_tiers",
]
