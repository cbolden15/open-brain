"""Follow-up scope: a session sees an item only when it can read every cited source's head now.

This is the only place follow-up scope rules live. Tier, space, lifecycle and
external-egress rules are not restated here; they are applied by reading each cited
source through ``RecordProjector.source`` exactly as record reads do. When a cited source
has gained a new revision, visibility and quotes are judged against the current head; the
cited older revision is read internally only and its text never reaches a client session.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from hashlib import sha256
from typing import Literal

from open_brain_engine.core.models import PrivacyTier

from .capture_lifecycle_policy import lifecycle_permits_capture
from .followup_contracts import EvidenceRef, EvidenceRole, FollowupItem
from .privacy_projection import parse_effective_privacy_json
from .records import ProjectedRecord, RecordProjector
from .t03_contracts import EffectiveAuthority, T03Error

EvidenceStatus = Literal["current", "moved", "source_changed", "retracted"]


def rebuild_quote(revision_text: str, ref: EvidenceRef) -> str | None:
    """Return the cited slice while its position and hash still match the revision text."""
    if not 0 <= ref.quote_start < ref.quote_end <= len(revision_text):
        return None
    quote = revision_text[ref.quote_start : ref.quote_end]
    if sha256(quote.encode("utf-8")).hexdigest() != ref.quote_sha256:
        return None
    return quote


def locate_quote(head_text: str, quote: str) -> tuple[int, int] | None:
    """Code-point positions of ``quote`` in ``head_text`` when it occurs exactly once."""
    if not quote:
        return None
    first = head_text.find(quote)
    if first < 0 or head_text.find(quote, first + 1) >= 0:
        return None
    return first, first + len(quote)


def head_record(projector: RecordProjector, ref: EvidenceRef) -> ProjectedRecord | None:
    """The cited source's current head through the session's own read path; None if unreadable."""
    try:
        return projector.source(ref.capture_id, history=False)
    except T03Error:
        return None


def head_tier(connection: sqlite3.Connection, record: ProjectedRecord) -> PrivacyTier | None:
    """Effective privacy tier of the head revision ``record`` describes."""
    row = connection.execute(
        "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id=?",
        (record.summary["revision_id"],),
    ).fetchone()
    if row is None:
        return None
    try:
        return parse_effective_privacy_json(row["effective_privacy_json"]).tier
    except ValueError:
        return None


def revision_text(projector: RecordProjector, ref: EvidenceRef) -> str | None:
    """Read the cited revision itself (head or retained history); None when unreadable."""
    try:
        return projector.source(ref.capture_id, expected=ref.revision_id, history=True).text
    except T03Error:
        return None


def internal_projector(projector: RecordProjector) -> RecordProjector:
    """Owner-scoped reader for the engine's own comparison of an older revision.

    Text read through it is compared and sliced inside this module and is never returned
    to a non-owner session.
    """
    return RecordProjector(
        projector.profile, projector.connection, replace(projector.authority, owner=True)
    )


def evidence_ref(row: sqlite3.Row) -> EvidenceRef:
    """One stored ``followup_evidence`` row as the value type the scope rules consume."""
    return EvidenceRef(
        row["capture_id"],
        row["revision_id"],
        row["quote_start"],
        row["quote_end"],
        row["quote_sha256"],
        EvidenceRole(row["role"]),
    )


def evidence_retracted(
    connection: sqlite3.Connection, evidence_rows: Sequence[EvidenceRef]
) -> bool:
    """True when owner retraction withholds any cited capture from current reads."""
    return any(not lifecycle_permits_capture(connection, ref.capture_id) for ref in evidence_rows)


def visible_to(
    item: FollowupItem,
    evidence_rows: Sequence[EvidenceRef],
    *,
    authority: EffectiveAuthority,
    projector: RecordProjector,
) -> bool:
    """The owner sees every item; a client sees one only if every cited source's head reads now
    under the session's tier, space and egress rules and that head keeps the item's tier."""
    if not evidence_rows:
        return False
    if authority.owner:
        return True
    try:
        tier = PrivacyTier(item.privacy_tier)
    except ValueError:
        return False
    if not authority.permits_read_tier(tier):
        return False
    for ref in evidence_rows:
        head = head_record(projector, ref)
        if head is None or head_tier(projector.connection, head) is not tier:
            return False
    return True


__all__ = [
    "EvidenceStatus",
    "evidence_ref",
    "evidence_retracted",
    "head_record",
    "head_tier",
    "internal_projector",
    "locate_quote",
    "rebuild_quote",
    "revision_text",
    "visible_to",
]
