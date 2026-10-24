"""Read-only capture-family freshness for the daily brief.

A family is one capture namespace within one space (``source_namespaces``: an agent
``consumer_chat`` namespace or a connector connection, labelled ``connector_name/connection_id``) or
the Markdown import of one space. Manual captures form no family. Scope is the current-read rule of
``RecordProjector.source`` minus its availability check, because unavailable captures are what the
brief must count. The task reads ``logical_sources``, ``source_revisions``, ``source_namespaces``
and ``markdown_import_revisions`` only, never writes, never parses capture bodies and never leaves
``read_snapshot``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from .consent_contracts import EgressMode
from .mcp_eligibility import capture_visible
from .normalization import _timestamp
from .paging import read_snapshot
from .records import RecordProjector
from .sharing import _managed_copy_identity, require_capture_eligibility
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine

BRIEF_READ_CAPABILITY = "brief-read"
STALE_DAYS_MIN = 1
STALE_DAYS_MAX = 365
UNASSIGNED_SPACE = "unassigned"
UNAVAILABLE = frozenset({"missing", "inaccessible"})

FamilyKind = Literal["consumer_namespace", "markdown_import"]

# Active, current heads only; historical_only and retired sources never enter a current read
# (records.py:126-129). Availability is selected, not filtered.
_HEADS_SQL = (
    "SELECT s.source_id, s.head_capture_id, s.space_id, s.availability, r.recorded_at, "
    "n.namespace_json, (m.capture_id IS NOT NULL) AS markdown_import "
    "FROM logical_sources s "
    "JOIN source_revisions r ON r.capture_id = s.head_capture_id "
    "LEFT JOIN source_namespaces n ON n.source_id = s.source_id "
    "LEFT JOIN markdown_import_revisions m ON m.capture_id = s.head_capture_id "
    "WHERE s.lifecycle = 'active' AND s.historical_only = 0 "
    "ORDER BY s.source_id COLLATE BINARY"
)


@dataclass(frozen=True)
class FamilyFreshness:
    family_id: str
    kind: FamilyKind
    space_id: str
    newest_recorded_at: str
    capture_count: int
    unavailable_count: int
    stale: bool


@dataclass(frozen=True)
class FreshnessReport:
    families: tuple[FamilyFreshness, ...]
    stale_days: int
    generated_at: str


def family_id(kind: FamilyKind, namespace_label: str | None, space_id: str | None) -> str:
    """``consumer:<connector>/<connection>:<space>`` or ``markdown:<space>``; compared whole."""
    space = UNASSIGNED_SPACE if space_id is None else space_id
    if kind == "markdown_import":
        return f"markdown:{space}"
    if namespace_label is None:
        raise ValueError("a namespace family needs a namespace label")
    return f"consumer:{namespace_label}:{space}"


@dataclass
class _Family:
    kind: FamilyKind
    label: str | None
    space_id: str | None
    newest: datetime
    newest_text: str
    capture_count: int = 0
    unavailable_count: int = 0


def _namespace_label(namespace_json: object) -> str | None:
    if type(namespace_json) is not str:
        return None
    try:
        namespace = json.loads(namespace_json)
    except ValueError:
        return None
    if not isinstance(namespace, dict):
        return None
    connector, connection = namespace.get("connector_name"), namespace.get("connection_id")
    if type(connector) is not str or not connector or type(connection) is not str or not connection:
        return None
    return f"{connector}/{connection}"


def _classify(row: sqlite3.Row) -> tuple[FamilyKind, str | None] | None:
    if row["markdown_import"]:
        return "markdown_import", None
    label = _namespace_label(row["namespace_json"])
    if label is None:
        return None  # manual capture: no family
    return "consumer_namespace", label


def _recorded_at(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _unavailable_head_admitted(
    connection: sqlite3.Connection,
    head: str,
    authority: EffectiveAuthority,
    engine: BrainEngine,
    *,
    external: bool,
) -> bool:
    """``sharing_eligible`` for a non-owner without its availability test, failing closed."""
    from .historical_visibility import historical_capture_visibility

    historical = historical_capture_visibility(
        connection,
        engine.profile,
        head,
        local_history=not external,
        provider_id=authority.provider_id,
        brain_id=authority.brain_id,
        issuer_epoch=authority.issuer_epoch,
    )
    if historical is not None:
        return historical
    revision = connection.execute(
        "SELECT r.source_id,r.source_bytes,c.source_reference,c.delivery_id,c.privacy_json,"
        "c.provenance_json FROM source_revisions r LEFT JOIN captures c USING(capture_id) "
        "WHERE r.capture_id=?",
        (head,),
    ).fetchone()
    if revision is None or _managed_copy_identity(connection, head, revision):
        return False
    managed_original = connection.execute(
        "SELECT 1 FROM managed_source_deliveries WHERE source_id=? "
        "AND receipt_json IS NOT NULL "
        "AND json_extract(receipt_json,'$.source_receipt.capture_id')=? LIMIT 1",
        (revision["source_id"], head),
    ).fetchone()
    return managed_original is None


def _readable(
    connection: sqlite3.Connection,
    projector: RecordProjector,
    authority: EffectiveAuthority,
    head: str,
    engine: BrainEngine,
    *,
    available: bool,
) -> bool:
    """The current-read rules of ``RecordProjector.source`` (records.py:131-155) for one head.

    Sharing eligibility itself requires ``availability = 'available'`` (sharing.py, the
    ``OWNER_CURRENT`` and ``LOCAL_CURRENT`` modes), so it applies to available heads only. An
    unavailable head is counted for the local owner once it passes the other rules; for any other
    session it must also pass the non-owner historical fence, and a sharing-managed copy or managed
    original fails closed.
    """
    if not capture_visible(connection, head, authority):
        return False
    if not projector.source_revision_readable(head):
        return False
    if not available:
        external = authority.egress_mode is EgressMode.EXTERNAL_PROVIDER
        if authority.owner and not external:
            return True
        return _unavailable_head_admitted(connection, head, authority, engine, external=external)
    try:
        require_capture_eligibility(
            connection, head, authority, history=False, profile=engine.profile
        )
    except T03Error:
        return False
    return True


class SourceFreshnessTasks:
    """One read-only report of capture-family freshness under one authority."""

    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def freshness(
        self,
        authority: EffectiveAuthority,
        *,
        space_ids: frozenset[str] | None,
        stale_days: int,
    ) -> FreshnessReport:
        authority.require(BRIEF_READ_CAPABILITY)
        if type(stale_days) is not int or not STALE_DAYS_MIN <= stale_days <= STALE_DAYS_MAX:
            raise T03Error("invalid_arguments")
        if space_ids is not None and (
            type(space_ids) is not frozenset
            or any(type(space_id) is not str or not space_id for space_id in space_ids)
        ):
            raise T03Error("invalid_arguments")
        now = self._engine._clock()
        cutoff = now - timedelta(days=stale_days)
        families: dict[tuple[FamilyKind, str | None, str | None], _Family] = {}
        with read_snapshot(self._engine) as connection:
            projector = RecordProjector(self._engine.profile, connection, authority)
            for row in connection.execute(_HEADS_SQL):
                classified = _classify(row)
                if classified is None:
                    continue
                kind, label = classified
                space_id = row["space_id"]
                if not authority.permits_space(space_id):
                    continue
                if space_ids is not None and space_id not in space_ids:
                    continue
                if not _readable(
                    connection,
                    projector,
                    authority,
                    row["head_capture_id"],
                    self._engine,
                    available=row["availability"] == "available",
                ):
                    continue
                recorded = _recorded_at(row["recorded_at"])
                if recorded is None:
                    continue
                key = (kind, label, space_id)
                family = families.get(key)
                if family is None:
                    family = families[key] = _Family(
                        kind, label, space_id, recorded, row["recorded_at"]
                    )
                family.capture_count += 1
                if row["availability"] in UNAVAILABLE:
                    family.unavailable_count += 1
                if recorded > family.newest:
                    family.newest, family.newest_text = recorded, row["recorded_at"]
        report = tuple(
            sorted(
                (
                    FamilyFreshness(
                        family_id=family_id(family.kind, family.label, family.space_id),
                        kind=family.kind,
                        space_id=UNASSIGNED_SPACE if family.space_id is None else family.space_id,
                        newest_recorded_at=family.newest_text,
                        capture_count=family.capture_count,
                        unavailable_count=family.unavailable_count,
                        stale=family.newest < cutoff,
                    )
                    for family in families.values()
                ),
                key=lambda family: family.family_id,
            )
        )
        return FreshnessReport(families=report, stale_days=stale_days, generated_at=_timestamp(now))


__all__ = [
    "BRIEF_READ_CAPABILITY",
    "STALE_DAYS_MAX",
    "STALE_DAYS_MIN",
    "UNASSIGNED_SPACE",
    "UNAVAILABLE",
    "FamilyFreshness",
    "FreshnessReport",
    "SourceFreshnessTasks",
    "family_id",
]
