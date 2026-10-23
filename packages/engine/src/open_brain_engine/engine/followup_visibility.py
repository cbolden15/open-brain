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
from .followup_contracts import EvidenceRef, EvidenceRole, FollowupEvidenceView, FollowupItem
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


def project_evidence(
    evidence_id: str,
    ref: EvidenceRef,
    *,
    connection: sqlite3.Connection,
    authority: EffectiveAuthority,
    projector: RecordProjector,
) -> FollowupEvidenceView:
    """One evidence row as this session sees it now (status, quote, head positions)."""
    if not lifecycle_permits_capture(connection, ref.capture_id):
        return FollowupEvidenceView(
            evidence_id, ref.role, ref.capture_id, ref.revision_id, None, "retracted"
        )
    head = head_record(projector, ref)
    if head is not None and head.summary["revision_id"] == ref.revision_id:
        quote = rebuild_quote(head.text, ref)
        if quote is None:
            return FollowupEvidenceView(
                evidence_id, ref.role, ref.capture_id, ref.revision_id, None, "source_changed"
            )
        return FollowupEvidenceView(
            evidence_id,
            ref.role,
            ref.capture_id,
            ref.revision_id,
            quote,
            "current",
            ref.revision_id,
            ref.quote_start,
            ref.quote_end,
        )
    # The source moved on. Compare against the cited revision internally; never hand its text
    # to a client session.
    cited = revision_text(internal_projector(projector), ref)
    original = None if cited is None else rebuild_quote(cited, ref)
    located = None if head is None or original is None else locate_quote(head.text, original)
    if head is not None and located is not None:
        return FollowupEvidenceView(
            evidence_id,
            ref.role,
            ref.capture_id,
            ref.revision_id,
            original,
            "moved",
            head.summary["revision_id"],
            located[0],
            located[1],
        )
    return FollowupEvidenceView(
        evidence_id,
        ref.role,
        ref.capture_id,
        ref.revision_id,
        original if authority.owner else None,
        "source_changed",
    )


def project_item(
    item: FollowupItem,
    evidence_ids: Sequence[str],
    evidence_rows: Sequence[EvidenceRef],
    *,
    connection: sqlite3.Connection,
    authority: EffectiveAuthority,
    projector: RecordProjector,
) -> tuple[FollowupItem, tuple[FollowupEvidenceView, ...]] | None:
    """One item for one session: None when hidden, else recomputed flags and per-read quotes."""
    if len(evidence_ids) != len(evidence_rows):
        raise ValueError("evidence ids and rows differ in length")
    if not visible_to(item, evidence_rows, authority=authority, projector=projector):
        return None
    views = tuple(
        project_evidence(
            evidence_id, ref, connection=connection, authority=authority, projector=projector
        )
        for evidence_id, ref in zip(evidence_ids, evidence_rows, strict=True)
    )
    retracted = any(view.status == "retracted" for view in views)
    if retracted and not authority.owner:
        # Retracted evidence hides the item from every client session, whatever the head says.
        return None
    projected = replace(
        item,
        evidence_retracted=retracted,
        summary=None if retracted else item.summary,
    )
    return projected, views


__all__ = [
    "EvidenceStatus",
    "evidence_ref",
    "evidence_retracted",
    "head_record",
    "head_tier",
    "internal_projector",
    "locate_quote",
    "project_evidence",
    "project_item",
    "rebuild_quote",
    "revision_text",
    "visible_to",
]
