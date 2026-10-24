"""Pure daily-brief contracts: closed enums, frozen wire shapes, limits, ref parsing.

Nothing here reads the engine, the log or the clock. ``to_wire()`` is the only producer of the
brief wire shapes; every brief surface (CLI, MCP, log entries) serializes through it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

BRIEF_MAX_ITEMS = 5
BRIEF_MAX_FAMILY_ITEMS = 2
BRIEF_FOLLOWUP_SCAN_LIMIT = 500
BRIEF_RESPONSE_MAX_BYTES = 32 * 1024
BRIEF_LOG_MAX_BRIEFS = 30
BRIEF_LOG_MAX_FILE_BYTES = 64 * 1024
BRIEF_LOG_MAX_KEYS = 16
BRIEF_STALE_DAYS_DEFAULT = 7
BRIEF_STALE_DAYS_MIN = 1
BRIEF_STALE_DAYS_MAX = 365
BRIEF_GRANT_READ = "brief-read"
BRIEF_GRANT_ACK = "brief-ack"
BRIEF_GRANTS = frozenset({BRIEF_GRANT_READ, BRIEF_GRANT_ACK})
BRIEF_REASON_TEXT_MAX_CHARS = 200

BriefErrorCode = Literal[
    "invalid_request",
    "unsupported_capability",
    "operation_pending",
    "version_conflict",
    "not_found",
]
BRIEF_ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "unsupported_capability",
        "operation_pending",
        "version_conflict",
        "not_found",
    }
)

_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_FOLLOWUP_ID = re.compile(rf"^followup_{_UUID4}$")
# "consumer:<connector>/<connection>:<space|unassigned>" or "markdown:<space|unassigned>";
# compared whole, never parsed into components. No control characters, bounded length.
_FAMILY_ID = re.compile(r"^(consumer|markdown):[^\x00-\x1f\x7f]{1,2200}$")


class BriefError(Exception):
    """Closed brief failure code; refusals never write."""

    def __init__(self, code: str) -> None:
        if code not in BRIEF_ERROR_CODES:
            raise ValueError("invalid brief error code")
        super().__init__(code)
        self.code: BriefErrorCode = cast(BriefErrorCode, code)


class BriefItemKind(StrEnum):
    FOLLOWUP_DUE = "followup_due"
    FOLLOWUP_NEW_EVIDENCE = "followup_new_evidence"
    FOLLOWUP_OPEN = "followup_open"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_STALE = "source_stale"


class BriefChange(StrEnum):
    NEW = "new"
    STILL_OPEN = "still_open"
    CHANGED = "changed"


class BriefReasonCode(StrEnum):
    OVERDUE = "overdue"
    DUE_TODAY = "due_today"
    NEW_EVIDENCE = "new_evidence"
    OPEN_OWNED = "open_owned"
    CAPTURES_UNAVAILABLE = "captures_unavailable"
    NO_CAPTURE_SINCE = "no_capture_since"


FAMILY_KINDS = frozenset({BriefItemKind.SOURCE_UNAVAILABLE, BriefItemKind.SOURCE_STALE})
FOLLOWUP_KINDS = frozenset(BriefItemKind) - FAMILY_KINDS

_TIERS = frozenset({"public", "work", "personal"})
_FOLLOWUP_STATUS = frozenset({"read", "not_granted", "failed"})
_FRESHNESS_STATUS = frozenset({"read", "failed"})


def parse_family_ref(ref: object) -> str:
    """``family:<family_id>`` -> ``<family_id>``; anything else is ``invalid_request``."""
    if type(ref) is not str or not ref.startswith("family:"):
        raise BriefError("invalid_request")
    family_id = ref[len("family:") :]
    if _FAMILY_ID.fullmatch(family_id) is None:
        raise BriefError("invalid_request")
    return family_id


def parse_followup_ref(ref: object) -> str:
    """``followup:<item_id>`` -> ``<item_id>``; anything else is ``invalid_request``."""
    if type(ref) is not str or not ref.startswith("followup:"):
        raise BriefError("invalid_request")
    item_id = ref[len("followup:") :]
    if _FOLLOWUP_ID.fullmatch(item_id) is None:
        raise BriefError("invalid_request")
    return item_id


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


@dataclass(frozen=True)
class BriefItem:
    kind: BriefItemKind
    ref: str
    reasons: tuple[BriefReasonCode, ...]
    reason_text: str
    evidence: tuple[str, ...]
    changed: BriefChange
    rank: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, BriefItemKind)
            or not isinstance(self.changed, BriefChange)
            or type(self.rank) is not int
            or not 1 <= self.rank <= BRIEF_MAX_ITEMS
            or type(self.reasons) is not tuple
            or not self.reasons
            or any(not isinstance(reason, BriefReasonCode) for reason in self.reasons)
            or len(set(self.reasons)) != len(self.reasons)
            or type(self.reason_text) is not str
            or not 1 <= len(self.reason_text) <= BRIEF_REASON_TEXT_MAX_CHARS
            or any(ord(c) < 32 or ord(c) == 0x7F for c in self.reason_text)
            or type(self.evidence) is not tuple
            or any(type(e) is not str or not e for e in self.evidence)
        ):
            raise ValueError("invalid brief item")
        try:
            if self.kind in FAMILY_KINDS:
                parse_family_ref(self.ref)
                if self.evidence:
                    raise ValueError("invalid brief item")
            else:
                parse_followup_ref(self.ref)
        except BriefError:
            raise ValueError("invalid brief item") from None

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "ref": self.ref,
            "reasons": [reason.value for reason in self.reasons],
            "reason_text": self.reason_text,
            "evidence": list(self.evidence),
            "changed": self.changed.value,
            "rank": self.rank,
        }


@dataclass(frozen=True)
class BriefLogCoverage:
    entries: int
    bytes: int
    unavailable: bool

    def __post_init__(self) -> None:
        if not _count(self.entries) or not _count(self.bytes) or type(self.unavailable) is not bool:
            raise ValueError("invalid brief log coverage")

    def to_wire(self) -> dict[str, object]:
        return {"entries": self.entries, "bytes": self.bytes, "unavailable": self.unavailable}


@dataclass(frozen=True)
class BriefCoverage:
    families_checked: int
    families_stale: int
    captures_unavailable: int
    followups_scanned: int
    followups_truncated: bool
    followups: Literal["read", "not_granted", "failed"]
    freshness: Literal["read", "failed"]
    collector: Literal["not_read"]
    hidden_dropped: int
    log: BriefLogCoverage

    def __post_init__(self) -> None:
        if (
            not all(
                _count(value)
                for value in (
                    self.families_checked,
                    self.families_stale,
                    self.captures_unavailable,
                    self.followups_scanned,
                    self.hidden_dropped,
                )
            )
            or type(self.followups_truncated) is not bool
            or self.followups not in _FOLLOWUP_STATUS
            or self.freshness not in _FRESHNESS_STATUS
            or self.collector != "not_read"
            or not isinstance(self.log, BriefLogCoverage)
        ):
            raise ValueError("invalid brief coverage")

    def to_wire(self) -> dict[str, object]:
        return {
            "families_checked": self.families_checked,
            "families_stale": self.families_stale,
            "captures_unavailable": self.captures_unavailable,
            "followups_scanned": self.followups_scanned,
            "followups_truncated": self.followups_truncated,
            "followups": self.followups,
            "freshness": self.freshness,
            "collector": self.collector,
            "hidden_dropped": self.hidden_dropped,
            "log": self.log.to_wire(),
        }


@dataclass(frozen=True)
class BriefScope:
    space_ids: tuple[str, ...] | None  # None == every space the authority may read
    tiers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.space_ids is not None and (
            type(self.space_ids) is not tuple
            or not self.space_ids
            or any(type(s) is not str or not s for s in self.space_ids)
            or len(set(self.space_ids)) != len(self.space_ids)
        ):
            raise ValueError("invalid brief scope")
        if (
            type(self.tiers) is not tuple
            or any(tier not in _TIERS for tier in self.tiers)
            or len(set(self.tiers)) != len(self.tiers)
        ):
            raise ValueError("invalid brief scope")

    def to_wire(self) -> dict[str, object]:
        return {
            "space_ids": "all" if self.space_ids is None else sorted(self.space_ids),
            "tiers": sorted(self.tiers),
        }


@dataclass(frozen=True)
class Brief:
    generated_at: str
    scope: BriefScope
    items: tuple[BriefItem, ...]
    nothing_needs_attention: bool
    since: str | None
    coverage: BriefCoverage
    model: Literal["none"]
    saved: bool

    def __post_init__(self) -> None:
        items = tuple(self.items) if isinstance(self.items, list | tuple) else None
        if (
            items is None
            or type(self.generated_at) is not str
            or not self.generated_at
            or not isinstance(self.scope, BriefScope)
            or any(not isinstance(item, BriefItem) for item in items)
            or len(items) > BRIEF_MAX_ITEMS
            or [item.rank for item in items] != list(range(1, len(items) + 1))
            or len({item.ref for item in items}) != len(items)
            or type(self.nothing_needs_attention) is not bool
            or self.nothing_needs_attention != (not items)
            or (self.since is not None and (type(self.since) is not str or not self.since))
            or not isinstance(self.coverage, BriefCoverage)
            or self.model != "none"
            or type(self.saved) is not bool
        ):
            raise ValueError("invalid brief")
        object.__setattr__(self, "items", items)

    def to_wire(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "scope": self.scope.to_wire(),
            "items": [item.to_wire() for item in self.items],
            "nothing_needs_attention": self.nothing_needs_attention,
            "since": self.since,
            "coverage": self.coverage.to_wire(),
            "model": self.model,
            "saved": self.saved,
        }


__all__ = [
    "BRIEF_ERROR_CODES",
    "BRIEF_FOLLOWUP_SCAN_LIMIT",
    "BRIEF_GRANT_ACK",
    "BRIEF_GRANT_READ",
    "BRIEF_GRANTS",
    "BRIEF_LOG_MAX_BRIEFS",
    "BRIEF_LOG_MAX_FILE_BYTES",
    "BRIEF_LOG_MAX_KEYS",
    "BRIEF_MAX_FAMILY_ITEMS",
    "BRIEF_MAX_ITEMS",
    "BRIEF_REASON_TEXT_MAX_CHARS",
    "BRIEF_RESPONSE_MAX_BYTES",
    "BRIEF_STALE_DAYS_DEFAULT",
    "BRIEF_STALE_DAYS_MAX",
    "BRIEF_STALE_DAYS_MIN",
    "FAMILY_KINDS",
    "FOLLOWUP_KINDS",
    "Brief",
    "BriefChange",
    "BriefCoverage",
    "BriefError",
    "BriefErrorCode",
    "BriefItem",
    "BriefItemKind",
    "BriefLogCoverage",
    "BriefReasonCode",
    "BriefScope",
    "parse_family_ref",
    "parse_followup_ref",
]
