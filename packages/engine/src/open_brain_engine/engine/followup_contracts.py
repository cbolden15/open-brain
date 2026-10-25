"""Pure follow-up contracts: kinds, states, requests and the closed error codes.

No I/O. Every request validates its own invariants on construction so the task
layer only sees well-formed values; a refused construction writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import date
from enum import StrEnum
from typing import Any, Literal

MAX_SUMMARY_CHARS = 280
MAX_ITEMS = 10_000
MAX_EVIDENCE_PER_ITEM = 50
MAX_OPERATION_ID_CHARS = 128
MAX_LIST_LIMIT = 200

_ERROR_CODES = frozenset(
    {
        "evidence_mismatch",
        "not_found",
        "tier_mismatch",
        "version_conflict",
        "operation_conflict",
        "duplicate_item",
        "invalid_request",
        "capacity_reached",
    }
)
_SHA256_ALPHABET = frozenset("0123456789abcdef")
CORRECTABLE_FIELDS = frozenset(
    {"summary", "owner", "owner_name", "project", "due", "due_evidence_id"}
)


class FollowupError(Exception):
    """Closed follow-up failure code; refusals never write."""

    def __init__(
        self,
        code: str,
        *,
        current_version: int | None = None,
        existing_item_id: str | None = None,
    ) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("invalid error code")
        super().__init__(code)
        self.code = code
        self.current_version = current_version
        self.existing_item_id = existing_item_id


class FollowupKind(StrEnum):
    COMMITMENT = "commitment"
    QUESTION = "question"


class FollowupState(StrEnum):
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    SNOOZED = "snoozed"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class OwnerKind(StrEnum):
    SELF = "self"
    OTHER = "other"
    UNKNOWN = "unknown"


class EvidenceRole(StrEnum):
    ORIGIN = "origin"
    SUPPORTING = "supporting"
    CORRECTION = "correction"
    RESOLUTION = "resolution"


class DecisionAction(StrEnum):
    CONFIRM = "confirm"
    DISMISS = "dismiss"
    SNOOZE = "snooze"
    CORRECT = "correct"
    LINK = "link"
    RESOLVE = "resolve"
    REOPEN = "reopen"
    SUPERSEDE = "supersede"


OPEN_STATES = frozenset({FollowupState.SUGGESTED, FollowupState.CONFIRMED, FollowupState.SNOOZED})
CLOSED_STATES = frozenset(
    {FollowupState.DISMISSED, FollowupState.RESOLVED, FollowupState.SUPERSEDED}
)


def _invalid() -> FollowupError:
    return FollowupError("invalid_request")


def _require_text(value: object, *, limit: int, allow_none: bool = False) -> None:
    if value is None:
        if allow_none:
            return
        raise _invalid()
    if (
        type(value) is not str
        or not 1 <= len(value) <= limit
        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise _invalid()


def _require_operation_id(value: object) -> None:
    _require_text(value, limit=MAX_OPERATION_ID_CHARS)


def _require_item_id(value: object) -> None:
    if type(value) is not str or not value.startswith("followup_") or len(value) > 128:
        raise _invalid()


def _require_owner(owner: object, owner_name: object) -> None:
    if type(owner) is not OwnerKind:
        raise _invalid()
    if owner is OwnerKind.OTHER:
        _require_text(owner_name, limit=128)
    elif owner_name is not None:
        raise _invalid()


def _require_date(value: object, *, allow_none: bool) -> None:
    if value is None and allow_none:
        return
    if type(value) is not date:
        raise _invalid()


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    capture_id: str
    revision_id: str
    quote_start: int
    quote_end: int
    quote_sha256: str
    role: EvidenceRole

    def __post_init__(self) -> None:
        # v1 cites source captures only; a source record's revision identity is its
        # capture identity (records.py source projection).
        if (
            type(self.capture_id) is not str
            or not self.capture_id.startswith("capture_")
            or self.revision_id != self.capture_id
            or type(self.quote_start) is not int
            or type(self.quote_end) is not int
            or not 0 <= self.quote_start < self.quote_end
            or type(self.quote_sha256) is not str
            or len(self.quote_sha256) != 64
            or not set(self.quote_sha256) <= _SHA256_ALPHABET
            or type(self.role) is not EvidenceRole
        ):
            raise _invalid()

    def position(self) -> tuple[str, int, int]:
        return (self.revision_id, self.quote_start, self.quote_end)


@dataclass(frozen=True, slots=True)
class ProposeRequest:
    operation_id: str
    kind: FollowupKind
    summary: str
    project: str | None
    owner: OwnerKind
    owner_name: str | None
    due: date | None
    due_evidence_index: int | None
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if type(self.kind) is not FollowupKind:
            raise _invalid()
        _require_text(self.summary, limit=MAX_SUMMARY_CHARS)
        _require_text(self.project, limit=128, allow_none=True)
        _require_owner(self.owner, self.owner_name)
        _require_date(self.due, allow_none=True)
        if (
            type(self.evidence) is not tuple
            or not 1 <= len(self.evidence) <= MAX_EVIDENCE_PER_ITEM
            or any(type(ref) is not EvidenceRef for ref in self.evidence)
            or self.evidence[0].role is not EvidenceRole.ORIGIN
            or any(ref.role is EvidenceRole.ORIGIN for ref in self.evidence[1:])
            or len({ref.position() for ref in self.evidence}) != len(self.evidence)
        ):
            raise _invalid()
        if self.due is None:
            if self.due_evidence_index is not None:
                raise _invalid()
        elif type(self.due_evidence_index) is not int or not 0 <= self.due_evidence_index < len(
            self.evidence
        ):
            raise _invalid()


@dataclass(frozen=True, slots=True)
class AddEvidenceRequest:
    operation_id: str
    item_id: str
    evidence: EvidenceRef

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        _require_item_id(self.item_id)
        if type(self.evidence) is not EvidenceRef or self.evidence.role is EvidenceRole.ORIGIN:
            raise _invalid()


def _validate_corrections(corrections: Mapping[str, object]) -> None:
    if not corrections or not set(corrections) <= CORRECTABLE_FIELDS:
        raise _invalid()
    if "summary" in corrections:
        _require_text(corrections["summary"], limit=MAX_SUMMARY_CHARS)
    if "project" in corrections:
        _require_text(corrections["project"], limit=128, allow_none=True)
    if "owner" in corrections or "owner_name" in corrections:
        try:
            owner = OwnerKind(str(corrections.get("owner")))
        except ValueError:
            raise _invalid() from None
        _require_owner(owner, corrections.get("owner_name"))
    if "due_evidence_id" in corrections and "due" not in corrections:
        raise _invalid()
    if "due" in corrections:
        due = corrections["due"]
        if due is None:
            if "due_evidence_id" in corrections:
                raise _invalid()
        else:
            try:
                date.fromisoformat(str(due))
            except ValueError:
                raise _invalid() from None
            _require_text(corrections.get("due_evidence_id"), limit=128)


@dataclass(frozen=True, slots=True)
class DecideRequest:
    operation_id: str
    item_id: str
    expected_version: int
    action: DecisionAction
    snoozed_until: date | None = None
    corrections: Mapping[str, object] | None = None
    ticket_key: str | None = None
    ticket_url: str | None = None
    superseded_by: str | None = None
    resolution: Literal["evidence", "owner_statement"] | None = None

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        _require_item_id(self.item_id)
        if type(self.expected_version) is not int or self.expected_version < 1:
            raise _invalid()
        if type(self.action) is not DecisionAction:
            raise _invalid()
        expected = {
            DecisionAction.SNOOZE: {"snoozed_until"},
            DecisionAction.CORRECT: {"corrections"},
            DecisionAction.LINK: {"ticket_key"},
            DecisionAction.RESOLVE: {"resolution"},
            DecisionAction.SUPERSEDE: {"superseded_by"},
        }.get(self.action, set())
        optional = {
            "snoozed_until",
            "corrections",
            "ticket_key",
            "ticket_url",
            "superseded_by",
            "resolution",
        }
        present = {name for name in optional if getattr(self, name) is not None}
        if present - {"ticket_url"} != expected or (
            self.ticket_url is not None and self.action is not DecisionAction.LINK
        ):
            raise _invalid()
        if self.action is DecisionAction.SNOOZE:
            _require_date(self.snoozed_until, allow_none=False)
        elif self.action is DecisionAction.CORRECT:
            assert self.corrections is not None
            _validate_corrections(self.corrections)
        elif self.action is DecisionAction.LINK:
            _require_text(self.ticket_key, limit=200)
            _require_text(self.ticket_url, limit=2048, allow_none=True)
            if self.ticket_url is not None and not self.ticket_url.startswith(
                ("https://", "http://")
            ):
                raise _invalid()
        elif self.action is DecisionAction.RESOLVE:
            if self.resolution not in {"evidence", "owner_statement"}:
                raise _invalid()
        elif self.action is DecisionAction.SUPERSEDE:
            _require_item_id(self.superseded_by)
            if self.superseded_by == self.item_id:
                raise _invalid()


@dataclass(frozen=True, slots=True)
class FollowupItem:
    item_id: str
    kind: FollowupKind
    project: str | None
    summary: str | None
    owner: OwnerKind
    owner_name: str | None
    owner_confirmed: bool
    due: date | None
    due_evidence_id: str | None
    state: FollowupState
    snoozed_until: date | None
    snooze_return_state: FollowupState | None
    ticket_key: str | None
    ticket_url: str | None
    superseded_by: str | None
    resolution: str | None
    privacy_tier: str
    version: int
    created_at: str
    updated_at: str
    evidence_retracted: bool = False
    new_evidence_since_decision: bool = False


@dataclass(frozen=True, slots=True)
class FollowupEvidenceView:
    """One evidence row as a session sees it now; ``status`` and head positions are read-time."""

    evidence_id: str
    role: EvidenceRole
    capture_id: str
    revision_id: str
    quote: str | None
    # "current": cited revision is the head and the quote still hashes.
    # "moved": the source gained a revision; the exact quote appears once in the head, and
    #          head_quote_start/end give its position there.
    # "source_changed": the source gained a revision and the quote is absent or ambiguous in the
    #          head; quote is None for every session, the owner included.
    # "retracted": owner retraction withheld the cited capture; quote is None for everyone.
    status: Literal["current", "moved", "source_changed", "retracted"] = "current"
    head_revision_id: str | None = None
    head_quote_start: int | None = None
    head_quote_end: int | None = None


@dataclass(frozen=True, slots=True)
class FollowupDecisionRow:
    decision_id: str
    item_id: str
    sequence: int
    operation_id: str
    principal_id: str
    action: str
    prior_state: FollowupState | None
    new_state: FollowupState
    prior_version: int
    new_version: int
    evidence_ids: tuple[str, ...]
    recorded_at: str


@dataclass(frozen=True, slots=True)
class ListFilter:
    kinds: frozenset[FollowupKind] = field(default_factory=frozenset)
    states: frozenset[FollowupState] = field(default_factory=frozenset)
    owner: OwnerKind | None = None
    project: str | None = None
    due_before: date | None = None
    include_closed: bool = False
    limit: int = 50
    cursor: str | None = None
    # None keeps every item; True keeps only items with evidence added after their last
    # decision (``new_evidence_since_decision``), False only those without.
    new_evidence: bool | None = None

    def __post_init__(self) -> None:
        if (
            type(self.limit) is not int
            or not 1 <= self.limit <= MAX_LIST_LIMIT
            or any(type(k) is not FollowupKind for k in self.kinds)
            or any(type(s) is not FollowupState for s in self.states)
            or (self.owner is not None and type(self.owner) is not OwnerKind)
            or (self.due_before is not None and type(self.due_before) is not date)
            or type(self.include_closed) is not bool
            or (self.new_evidence is not None and type(self.new_evidence) is not bool)
        ):
            raise _invalid()
        _require_text(self.project, limit=128, allow_none=True)
        _require_text(self.cursor, limit=512, allow_none=True)


def _wire_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_wire_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _wire_value(value[key]) for key in sorted(value)}
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _wire_value(getattr(value, f.name)) for f in fields(value)}
    return value


def request_wire(request: ProposeRequest | AddEvidenceRequest | DecideRequest) -> dict[str, Any]:
    """Canonical JSON-ready form of one write request; digested for replay."""
    wire = _wire_value(request)
    assert isinstance(wire, dict)
    return wire


__all__ = [
    "CLOSED_STATES",
    "CORRECTABLE_FIELDS",
    "MAX_EVIDENCE_PER_ITEM",
    "MAX_ITEMS",
    "MAX_LIST_LIMIT",
    "MAX_OPERATION_ID_CHARS",
    "MAX_SUMMARY_CHARS",
    "OPEN_STATES",
    "AddEvidenceRequest",
    "DecideRequest",
    "DecisionAction",
    "EvidenceRef",
    "EvidenceRole",
    "FollowupDecisionRow",
    "FollowupError",
    "FollowupEvidenceView",
    "FollowupItem",
    "FollowupKind",
    "FollowupState",
    "ListFilter",
    "OwnerKind",
    "ProposeRequest",
    "request_wire",
]
