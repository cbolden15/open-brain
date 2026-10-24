"""Daily work brief: rule-based composition over follow-ups and capture-family freshness.

No model call, no network, no subprocess, no background work. Items are pointers (refs,
reason codes, evidence IDs); text is fetched through the follow-up tools, where scope is
checked again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, cast

from open_brain_engine.engine.contracts import LocalEngineContext
from open_brain_engine.engine.followup_contracts import (
    FollowupError,
    FollowupItem,
    FollowupState,
    ListFilter,
    OwnerKind,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.services.brief_contracts import (
    BRIEF_FOLLOWUP_SCAN_LIMIT,
    BRIEF_GRANTS,
    BRIEF_MAX_FAMILY_ITEMS,
    BRIEF_MAX_ITEMS,
    BRIEF_RESPONSE_MAX_BYTES,
    Brief,
    BriefChange,
    BriefError,
    BriefItem,
    BriefItemKind,
    BriefReasonCode,
)
from open_brain.services.brief_log import BriefLogEntry, FamilyFingerprint
from open_brain.services.local_operations import database_is_busy

BRIEF_SCAN_PAGE = 100

BriefOperation = Callable[[Mapping[str, object]], dict[str, object]]

_LEVELS = (
    BriefItemKind.FOLLOWUP_DUE,
    BriefItemKind.SOURCE_UNAVAILABLE,
    BriefItemKind.FOLLOWUP_NEW_EVIDENCE,
    BriefItemKind.FOLLOWUP_OPEN,
    BriefItemKind.SOURCE_STALE,
)
_OPEN_STATES = frozenset({FollowupState.SUGGESTED, FollowupState.CONFIRMED})
_DECIDED_STATES = frozenset({FollowupState.DISMISSED, FollowupState.RESOLVED})
_DECIDING_ACTIONS = frozenset({"dismiss", "resolve"})


def default_brief_clock() -> datetime:
    """The same clock the engine uses by default (``datetime.now(UTC)``)."""
    return datetime.now(UTC)


def brief_state_dir(profile: LocalEngineContext) -> Path:
    """The current Brain's state directory, where graph state also lives."""
    return profile.root / ".open-brain" / "state"


# -- candidates ---------------------------------------------------------------------


@dataclass(frozen=True)
class FollowupCandidate:
    kind: BriefItemKind
    item_id: str
    reasons: tuple[BriefReasonCode, ...]
    reason_text: str
    sort_key: tuple[object, ...]

    @property
    def ref(self) -> str:
        return "followup:" + self.item_id


@dataclass(frozen=True)
class FamilyCandidate:
    kind: BriefItemKind
    family_id: str
    reasons: tuple[BriefReasonCode, ...]
    reason_text: str
    sort_key: tuple[object, ...]
    fingerprint: FamilyFingerprint

    @property
    def ref(self) -> str:
        return "family:" + self.family_id


Candidate = FollowupCandidate | FamilyCandidate


@dataclass(frozen=True)
class FollowupProviderResult:
    candidates: tuple[FollowupCandidate, ...]
    status: Literal["read", "not_granted", "failed"]
    scanned: int
    truncated: bool


@dataclass(frozen=True)
class FamilyProviderResult:
    candidates: tuple[FamilyCandidate, ...]
    status: Literal["read", "failed"]
    families_checked: int
    families_stale: int
    captures_unavailable: int


# -- providers ----------------------------------------------------------------------


def followup_candidates(
    followups: Any,
    authority: EffectiveAuthority,
    *,
    today: date,
    capture_filter: frozenset[str] | None = None,
) -> FollowupProviderResult:
    """Scan open items and decided items with new evidence under ``authority``.

    Two bounded scans (open: suggested or confirmed; decided: dismissed or resolved with
    ``include_closed``), each stopping at ``BRIEF_FOLLOWUP_SCAN_LIMIT`` rows. ``capture_filter``
    is the owner's ``--space`` selection as a set of capture IDs; an item stays when any of its
    evidence rows cites one of them. Clients never pass it: their authority's ``space_ids``
    already governs every read.
    """
    try:
        open_items, open_more = _scan(
            followups, authority, states=_OPEN_STATES, include_closed=False
        )
        decided, decided_more = _scan(
            followups, authority, states=_DECIDED_STATES, include_closed=True
        )
        candidates = _classify(
            followups, authority, open_items, decided, today=today, capture_filter=capture_filter
        )
    except BriefError:
        raise
    except Exception as error:  # noqa: BLE001 - classified by _raise_if_unreadable
        _raise_if_unreadable(error)
        return FollowupProviderResult((), "failed", 0, False)
    return FollowupProviderResult(
        tuple(candidates), "read", len(open_items) + len(decided), open_more or decided_more
    )


def _scan(
    followups: Any,
    authority: EffectiveAuthority,
    *,
    states: frozenset[FollowupState],
    include_closed: bool,
) -> tuple[list[FollowupItem], bool]:
    items: list[FollowupItem] = []
    cursor: str | None = None
    while len(items) < BRIEF_FOLLOWUP_SCAN_LIMIT:
        page, cursor = followups.list(
            ListFilter(
                states=states,
                include_closed=include_closed,
                limit=min(BRIEF_SCAN_PAGE, BRIEF_FOLLOWUP_SCAN_LIMIT - len(items)),
                cursor=cursor,
            ),
            authority=authority,
        )
        items.extend(page)
        if cursor is None or not page:
            return items, cursor is not None
    return items, True


def _classify(
    followups: Any,
    authority: EffectiveAuthority,
    open_items: Sequence[FollowupItem],
    decided: Sequence[FollowupItem],
    *,
    today: date,
    capture_filter: frozenset[str] | None,
) -> list[FollowupCandidate]:
    candidates: list[FollowupCandidate] = []
    for item in open_items:
        if not _in_selection(followups, authority, item.item_id, capture_filter):
            continue
        if item.due is not None and item.due <= today:
            overdue = (today - item.due).days
            if overdue > 0:
                reasons = (BriefReasonCode.OVERDUE,)
                plural = "s" if overdue != 1 else ""
                text = f"Due {item.due.isoformat()}; {overdue} day{plural} overdue."
            else:
                reasons = (BriefReasonCode.DUE_TODAY,)
                text = f"Due today ({item.due.isoformat()})."
            candidates.append(
                FollowupCandidate(
                    BriefItemKind.FOLLOWUP_DUE,
                    item.item_id,
                    reasons,
                    text,
                    (item.due.isoformat(), item.created_at, item.item_id),
                )
            )
        elif item.owner is OwnerKind.SELF:
            candidates.append(
                FollowupCandidate(
                    BriefItemKind.FOLLOWUP_OPEN,
                    item.item_id,
                    (BriefReasonCode.OPEN_OWNED,),
                    f"Open since {item.created_at[:10]} and owned by you.",
                    (item.created_at, item.item_id),
                )
            )
    for item in decided:
        if not item.new_evidence_since_decision:
            continue
        shown = _show_or_none(followups, authority, item.item_id)
        if shown is None:
            continue
        _shown_item, views, history = shown
        if capture_filter is not None and not any(
            view.capture_id in capture_filter for view in views
        ):
            continue
        decided_at = max(
            (row.sequence for row in history if row.action in _DECIDING_ACTIONS), default=0
        )
        later = [
            row.recorded_at
            for row in history
            if row.sequence > decided_at and row.action == "add_evidence"
        ]
        newest = max(later) if later else item.updated_at
        candidates.append(
            FollowupCandidate(
                BriefItemKind.FOLLOWUP_NEW_EVIDENCE,
                item.item_id,
                (BriefReasonCode.NEW_EVIDENCE,),
                f"New evidence recorded {newest} after the last decision.",
                (-_epoch(newest), item.item_id),
            )
        )
    return candidates


def _in_selection(
    followups: Any,
    authority: EffectiveAuthority,
    item_id: str,
    capture_filter: frozenset[str] | None,
) -> bool:
    if capture_filter is None:
        return True
    shown = _show_or_none(followups, authority, item_id)
    return shown is not None and any(view.capture_id in capture_filter for view in shown[1])


def _show_or_none(followups: Any, authority: EffectiveAuthority, item_id: str) -> Any:
    """``show`` under the session authority; a hidden or unknown item is ``None``."""
    try:
        return followups.show(item_id, authority=authority)
    except FollowupError:
        return None
    except T03Error as error:
        if error.code == "operation_pending":
            raise
        return None


def _epoch(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())


def family_candidates(
    source_freshness: Any,
    authority: EffectiveAuthority,
    *,
    space_ids: frozenset[str] | None,
    stale_days: int,
    acks: Mapping[str, FamilyFingerprint],
) -> FamilyProviderResult:
    """Capture-family freshness under ``authority``; an acked family stays out until its
    fingerprint (newest ``recorded_at``, unavailable count) changes."""
    try:
        report = source_freshness.freshness(authority, space_ids=space_ids, stale_days=stale_days)
    except T03Error as error:
        if error.code == "unsupported_capability":
            raise BriefError("unsupported_capability") from None
        if error.code == "invalid_arguments":
            raise BriefError("invalid_request") from None
        _raise_if_unreadable(error)
        return FamilyProviderResult((), "failed", 0, 0, 0)
    except Exception as error:  # noqa: BLE001 - classified by _raise_if_unreadable
        _raise_if_unreadable(error)
        return FamilyProviderResult((), "failed", 0, 0, 0)
    candidates: list[FamilyCandidate] = []
    stale_total = 0
    unavailable_total = 0
    for family in report.families:
        stale_total += int(family.stale)
        unavailable_total += family.unavailable_count
        fingerprint = FamilyFingerprint(family.newest_recorded_at, family.unavailable_count)
        if acks.get("family:" + family.family_id) == fingerprint:
            continue
        if family.unavailable_count > 0:
            count = family.unavailable_count
            text = f"{count} capture{'s' if count != 1 else ''} missing or inaccessible"
            reasons: tuple[BriefReasonCode, ...] = (BriefReasonCode.CAPTURES_UNAVAILABLE,)
            if family.stale:
                reasons += (BriefReasonCode.NO_CAPTURE_SINCE,)
                text += f"; no capture since {family.newest_recorded_at}."
            else:
                text += "."
            candidates.append(
                FamilyCandidate(
                    BriefItemKind.SOURCE_UNAVAILABLE,
                    family.family_id,
                    reasons,
                    text,
                    (-count, family.family_id),
                    fingerprint,
                )
            )
        elif family.stale:
            candidates.append(
                FamilyCandidate(
                    BriefItemKind.SOURCE_STALE,
                    family.family_id,
                    (BriefReasonCode.NO_CAPTURE_SINCE,),
                    f"No capture since {family.newest_recorded_at}.",
                    (family.newest_recorded_at, family.family_id),
                    fingerprint,
                )
            )
    return FamilyProviderResult(
        tuple(candidates), "read", len(report.families), stale_total, unavailable_total
    )


def _raise_if_unreadable(error: BaseException) -> None:
    if (
        database_is_busy(error)
        or isinstance(error, sqlite3.OperationalError)
        or (isinstance(error, T03Error) and error.code == "operation_pending")
    ):
        raise BriefError("operation_pending") from None


# -- ranking and selection ----------------------------------------------------------


def rank_candidates(
    followups: Sequence[FollowupCandidate],
    families: Sequence[FamilyCandidate],
    *,
    previous_refs: Collection[str],
) -> tuple[Candidate, ...]:
    """Fixed level order; within a level refs new since the last brief sort first."""
    by_kind: dict[BriefItemKind, list[Candidate]] = {kind: [] for kind in _LEVELS}
    candidates: tuple[Candidate, ...] = (*followups, *families)
    for candidate in candidates:
        by_kind[candidate.kind].append(candidate)
    ordered: list[Candidate] = []
    for kind in _LEVELS:
        ordered.extend(
            sorted(by_kind[kind], key=lambda item: (item.ref in previous_refs, item.sort_key))
        )
    return tuple(ordered)


def select_items(
    ordered: Sequence[Candidate],
    *,
    previous: Mapping[str, Mapping[str, object]],
    reread: Callable[[FollowupCandidate], tuple[str, ...] | None],
) -> tuple[tuple[BriefItem, ...], int]:
    """Walk the ranked list: at most five items, at most two families; a follow-up that no
    longer re-reads is dropped and counted (its identity is never reported)."""
    items: list[BriefItem] = []
    families = 0
    hidden = 0
    for candidate in ordered:
        if len(items) == BRIEF_MAX_ITEMS:
            break
        if isinstance(candidate, FamilyCandidate):
            if families == BRIEF_MAX_FAMILY_ITEMS:
                continue
            families += 1
            evidence: tuple[str, ...] = ()
        else:
            found = reread(candidate)
            if found is None:
                hidden += 1
                continue
            evidence = found
        items.append(
            BriefItem(
                kind=candidate.kind,
                ref=candidate.ref,
                reasons=candidate.reasons,
                reason_text=candidate.reason_text,
                evidence=evidence,
                changed=_changed(previous.get(candidate.ref), candidate, evidence),
                rank=len(items) + 1,
            )
        )
    return tuple(items), hidden


def _changed(
    previous: Mapping[str, object] | None, candidate: Candidate, evidence: tuple[str, ...]
) -> BriefChange:
    if previous is None:
        return BriefChange.NEW
    same = (
        previous.get("kind") == candidate.kind.value
        and sorted(cast(list[str], previous.get("reasons", [])))
        == sorted(reason.value for reason in candidate.reasons)
        and sorted(cast(list[str], previous.get("evidence", []))) == sorted(evidence)
    )
    return BriefChange.STILL_OPEN if same else BriefChange.CHANGED


def _previous_items(entry: BriefLogEntry | None) -> dict[str, Mapping[str, object]]:
    if entry is None:
        return {}
    return {cast(str, item["ref"]): item for item in entry.items}


def _fit_response(brief: Brief) -> Brief:
    """Drop evidence IDs (most-cited item first) until the wire form fits 32 KB; items stay."""
    items = list(brief.items)
    while _wire_size(replace(brief, items=tuple(items))) > BRIEF_RESPONSE_MAX_BYTES:
        widest = max(range(len(items)), key=lambda index: len(items[index].evidence), default=None)
        if widest is None or not items[widest].evidence:
            break
        items[widest] = replace(items[widest], evidence=items[widest].evidence[:-1])
    return replace(brief, items=tuple(items))


def _wire_size(brief: Brief) -> int:
    return len(
        json.dumps(brief.to_wire(), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    )


__all__ = [
    "BRIEF_GRANTS",
    "BRIEF_SCAN_PAGE",
    "BriefOperation",
    "Candidate",
    "FamilyCandidate",
    "FamilyProviderResult",
    "FollowupCandidate",
    "FollowupProviderResult",
    "brief_state_dir",
    "default_brief_clock",
    "family_candidates",
    "followup_candidates",
    "rank_candidates",
    "select_items",
]
