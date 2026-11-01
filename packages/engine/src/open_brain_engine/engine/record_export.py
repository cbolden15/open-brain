"""Read-only export of current head captures for one external-provider authority.

The export is the bounded, consent-checked path for handing captured text to an external
consumer (a newsletter pipeline, for example) without giving it an MCP session. It reads
``logical_sources``, ``captures`` and ``source_revision_privacy`` inside ``read_snapshot`` and
never writes.

Two independent checks decide what leaves the Brain. The SQL selects active, available heads
whose stored effective tier is one the authority may read. Every candidate then goes through
``RecordProjector.source``, which re-applies tier, ``external_egress``, space, lifecycle,
visibility and sharing eligibility and re-verifies the source bytes. A record either check
refuses is skipped without a trace; denials are ``not_found`` by design, so the export cannot
be used as an existence oracle.

Output never includes the raw ``source_reference``. The source is described by its public
origin, payload family and source id only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .consent_contracts import EgressMode
from .normalization import _timestamp
from .owner_egress_policy import SHAREABLE_TIERS
from .paging import read_snapshot
from .records import RecordProjector
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine

EXPORT_CAPABILITY = "content-read"
LIMIT_MAX = 10_000
MAX_BYTES_PER_RECORD_MAX = 1_048_576
TRUNCATION_MARKER = " [truncated]"

# Malformed privacy JSON yields NULL here rather than aborting the whole statement; the
# projector refuses that record on its own.
_TIER_SQL = (
    "CASE WHEN json_valid(p.effective_privacy_json) "
    "THEN json_extract(p.effective_privacy_json, '$.tier') END"
)
# The time filter here is deliberately one day wider than the window on each side: stored
# timestamps may carry fractional seconds, which do not order correctly against a bound as
# text. The exact window and the final order are applied in Python after parsing.
_CANDIDATES_SQL = (
    f"SELECT s.source_id, s.head_capture_id, c.accepted_at, {_TIER_SQL} AS tier "
    "FROM logical_sources s "
    "JOIN captures c ON c.capture_id = s.head_capture_id "
    "JOIN source_revision_privacy p ON p.capture_id = s.head_capture_id "
    "WHERE s.lifecycle = 'active' AND s.historical_only = 0 AND s.availability = 'available' "
    "AND c.accepted_at >= ? AND c.accepted_at < ? "
    f"AND {_TIER_SQL} IN ({{tiers}}) "
    "ORDER BY c.capture_id COLLATE BINARY"
)


@dataclass(frozen=True, slots=True)
class ExportedRecord:
    capture_id: str
    captured_at: str
    privacy_tier: str
    origin: str
    payload_family: str
    source_id: str
    text: str
    truncated: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.capture_id,
            "captured_at": self.captured_at,
            "privacy_tier": self.privacy_tier,
            "source": {
                "origin": self.origin,
                "payload_family": self.payload_family,
                "source_id": self.source_id,
            },
            "text": self.text,
            "truncated": self.truncated,
        }


def _parse_accepted_at(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes on a character boundary, then mark it."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    end = max_bytes
    while end > 0 and encoded[end] & 0xC0 == 0x80:
        end -= 1
    return encoded[:end].decode("utf-8") + TRUNCATION_MARKER, True


def _validate(
    since: object, until: object, limit: object, max_bytes_per_record: object
) -> tuple[datetime, datetime]:
    if (
        not isinstance(since, datetime)
        or not isinstance(until, datetime)
        or since.tzinfo is None
        or until.tzinfo is None
        or not since < until
        or type(limit) is not int
        or not 1 <= limit <= LIMIT_MAX
        or type(max_bytes_per_record) is not int
        or not 1 <= max_bytes_per_record <= MAX_BYTES_PER_RECORD_MAX
    ):
        raise T03Error("invalid_arguments")
    return since, until


class RecordExportTasks:
    """One bounded, read-only export of head captures under one external-provider authority."""

    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def export(
        self,
        authority: EffectiveAuthority,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        max_bytes_per_record: int,
    ) -> tuple[ExportedRecord, ...]:
        """Records accepted in ``[since, until)``, oldest first.

        Raises ``response_too_large`` when more than ``limit`` records qualify, so a caller never
        mistakes a truncated export for a complete one.
        """
        if authority.owner or authority.egress_mode is not EgressMode.EXTERNAL_PROVIDER:
            raise T03Error("unsupported_capability")
        authority.require(EXPORT_CAPABILITY)
        since, until = _validate(since, until, limit, max_bytes_per_record)
        tiers = sorted(tier.value for tier in authority.allowed_read_tiers & SHAREABLE_TIERS)
        if not tiers or authority.space_ids == frozenset():
            return ()
        sql = _CANDIDATES_SQL.format(tiers=",".join("?" * len(tiers)))
        try:
            coarse = (
                _timestamp(since - timedelta(days=1)),
                _timestamp(until + timedelta(days=1)),
            )
        except OverflowError:
            raise T03Error("invalid_arguments") from None
        records: list[tuple[datetime, ExportedRecord]] = []
        with read_snapshot(self._engine) as connection:
            projector = RecordProjector(self._engine.profile, connection, authority)
            for row in connection.execute(sql, (*coarse, *tiers)):
                accepted = _parse_accepted_at(row["accepted_at"])
                if accepted is None or not since <= accepted < until:
                    continue
                head = row["head_capture_id"]
                try:
                    # ``read`` maps an unreadable or damaged record to ``not_found`` the same
                    # way an MCP read does, so one bad record cannot block the rest.
                    projected = projector.read(head)
                except T03Error:
                    continue
                if len(records) == limit:
                    raise T03Error("response_too_large")
                text, truncated = truncate_utf8(projected.text, max_bytes_per_record)
                summary = projected.summary
                records.append(
                    (
                        accepted,
                        ExportedRecord(
                            capture_id=head,
                            captured_at=_timestamp(accepted),
                            privacy_tier=row["tier"],
                            origin=summary["provenance"]["source_origin"],
                            payload_family=summary["payload_family"],
                            source_id=summary["source_id"],
                            text=text,
                            truncated=truncated,
                        ),
                    )
                )
        records.sort(key=lambda item: (item[0], item[1].capture_id))
        return tuple(record for _accepted, record in records)


__all__ = [
    "EXPORT_CAPABILITY",
    "LIMIT_MAX",
    "MAX_BYTES_PER_RECORD_MAX",
    "TRUNCATION_MARKER",
    "ExportedRecord",
    "RecordExportTasks",
    "truncate_utf8",
]
