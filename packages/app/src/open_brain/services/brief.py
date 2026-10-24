"""Daily work brief: rule-based composition over follow-ups and capture-family freshness.

No model call, no network, no subprocess, no background work. Items are pointers (refs,
reason codes, evidence IDs); text is fetched through the follow-up tools, where scope is
checked again.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from open_brain_engine.engine.contracts import LocalEngineContext
from open_brain_engine.engine.followup_contracts import (
    MAX_EVIDENCE_PER_ITEM,
    FollowupError,
    FollowupItem,
    FollowupState,
    ListFilter,
    OwnerKind,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.services.brief_authority import BriefSessionAuthority, visibility_key
from open_brain.services.brief_contracts import (
    BRIEF_FOLLOWUP_SCAN_LIMIT,
    BRIEF_GRANTS,
    BRIEF_MAX_FAMILY_ITEMS,
    BRIEF_MAX_ITEMS,
    BRIEF_RESPONSE_MAX_BYTES,
    BRIEF_STALE_DAYS_DEFAULT,
    BRIEF_STALE_DAYS_MAX,
    BRIEF_STALE_DAYS_MIN,
    Brief,
    BriefChange,
    BriefCoverage,
    BriefError,
    BriefItem,
    BriefItemKind,
    BriefLogCoverage,
    BriefReasonCode,
    BriefScope,
    parse_family_ref,
)
from open_brain.services.brief_log import (
    LOG_ITEM_KEYS,
    BriefLogEntry,
    BriefLogStore,
    BriefLogWriteError,
    FamilyFingerprint,
)
from open_brain.services.local_operations import database_is_busy

BRIEF_MAX_SPACE_SELECTION = 32
BRIEF_SCAN_PAGE = 100
BRIEF_SPACE_CAPTURE_LIMIT = 10_000

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
_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_SPACE_ID = re.compile(r"^space_" + _UUID4 + "$")
# The contract's family-ID bound (brief_contracts._FAMILY_ID): 1..2200 non-control characters,
# here also refusing a leading space. Connector names and connection IDs may be non-ASCII.
_FAMILY_REF = re.compile(r"^family:(consumer|markdown):[^\x00-\x1f\x7f]{1,2200}$")


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

    The open budget is ``BRIEF_FOLLOWUP_SCAN_LIMIT`` rows. The engine lists in creation order, so
    items due today or earlier are scanned first (``due_before`` is exclusive, hence tomorrow)
    and the rest of the budget is filled from the general open scan, deduplicated by item ID; a
    newly created due item is never pushed out by older open items. The decided scan (dismissed
    or resolved, ``include_closed``) is a separate ``BRIEF_FOLLOWUP_SCAN_LIMIT`` rows in creation
    order: ``ListFilter`` cannot select ``new_evidence_since_decision``, so past that bound newer
    decided items are not examined and ``truncated`` reports it. ``capture_filter`` is the
    owner's ``--space`` selection as a set of capture IDs; an item stays when any of its evidence
    rows cites one of them. Clients never pass it: their authority's ``space_ids`` already
    governs every read.
    """
    try:
        due, due_more = _scan(
            followups,
            authority,
            states=_OPEN_STATES,
            include_closed=False,
            due_before=today + timedelta(days=1),
        )
        rest, rest_more = _scan(
            followups,
            authority,
            states=_OPEN_STATES,
            include_closed=False,
            limit=BRIEF_FOLLOWUP_SCAN_LIMIT - len(due),
        )
        seen = {item.item_id for item in due}
        open_items = [*due, *(item for item in rest if item.item_id not in seen)]
        open_more = due_more or rest_more
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
    limit: int = BRIEF_FOLLOWUP_SCAN_LIMIT,
    due_before: date | None = None,
) -> tuple[list[FollowupItem], bool]:
    """At most ``limit`` rows; the flag is true when rows may remain (always for a zero budget)."""
    items: list[FollowupItem] = []
    cursor: str | None = None
    while len(items) < limit:
        page, cursor = followups.list(
            ListFilter(
                states=states,
                include_closed=include_closed,
                due_before=due_before,
                limit=min(BRIEF_SCAN_PAGE, limit - len(items)),
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


# -- operations ---------------------------------------------------------------------


class BriefServiceError(ValueError):
    """One bounded argument-shape failure; never carries item or family content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_REQUIRED: dict[str, set[str]] = {"brief": set(), "ack": {"family_ref"}}
_OPTIONAL: dict[str, set[str]] = {"brief": {"space_ids", "stale_days"}, "ack": set()}


def validate_brief_arguments(operation: str, arguments: Mapping[str, object]) -> None:
    required = _REQUIRED.get(operation)
    optional = _OPTIONAL.get(operation)
    if required is None or optional is None or not isinstance(arguments, Mapping):
        raise BriefServiceError("invalid_arguments")
    keys = set(arguments)
    if required - keys or keys - (required | optional):
        raise BriefServiceError("invalid_arguments")
    if "space_ids" in arguments:
        value = arguments["space_ids"]
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= BRIEF_MAX_SPACE_SELECTION
            or len(set(cast(list[object], value))) != len(value)
            or any(not isinstance(item, str) or _SPACE_ID.fullmatch(item) is None for item in value)
        ):
            raise BriefServiceError("invalid_arguments")
    if "stale_days" in arguments:
        days = arguments["stale_days"]
        if type(days) is not int or not BRIEF_STALE_DAYS_MIN <= days <= BRIEF_STALE_DAYS_MAX:
            raise BriefServiceError("invalid_arguments")
    if "family_ref" in arguments:
        ref = arguments["family_ref"]
        if not isinstance(ref, str) or _FAMILY_REF.fullmatch(ref) is None:
            raise BriefServiceError("invalid_arguments")


def brief_generate(
    tasks: Any,
    session: BriefSessionAuthority,
    *,
    log: BriefLogStore,
    clock: Callable[[], datetime] = default_brief_clock,
    space_ids: Sequence[str] | None = None,
    stale_days: int = BRIEF_STALE_DAYS_DEFAULT,
) -> Brief:
    """Compose, cap and log one brief for ``session``.

    Raises ``BriefError``: ``invalid_request`` (bad selection or ``stale_days``),
    ``unsupported_capability`` (the freshness task refused the authority) or
    ``operation_pending`` (the engine could not be read). Nothing is served from the log.
    """
    selected = _validated_selection(space_ids)
    if (
        type(stale_days) is not int
        or not BRIEF_STALE_DAYS_MIN <= stale_days <= BRIEF_STALE_DAYS_MAX
    ):
        raise BriefError("invalid_request")
    _require_known_spaces(tasks, session.authority, selected)
    authority = _scoped_authority(session.authority, selected)
    now = clock()
    generated_at = _timestamp(now)
    today = now.astimezone(UTC).date()
    key = visibility_key(session, selected_space_ids=selected)
    log_read = log.read(key)
    previous = (
        log_read.file.briefs[-1] if log_read.file is not None and log_read.file.briefs else None
    )
    # Acks are independent of the selection: they live under the unselected key.
    ack_read = (
        log_read if selected is None else log.read(visibility_key(session, selected_space_ids=None))
    )
    acks = ack_read.file.acks if ack_read.file is not None else {}
    capture_filter, filter_truncated = _owner_capture_filter(tasks, authority, selected)
    if authority.owner or "followup-read" in session.followup_grants:
        followup_result = followup_candidates(
            tasks.followups, authority, today=today, capture_filter=capture_filter
        )
    else:
        followup_result = FollowupProviderResult((), "not_granted", 0, False)
    family_result = family_candidates(
        tasks.source_freshness,
        authority,
        space_ids=None if selected is None else frozenset(selected),
        stale_days=stale_days,
        acks=acks,
    )
    previous_items = _previous_items(previous)
    ordered = rank_candidates(
        followup_result.candidates, family_result.candidates, previous_refs=previous_items
    )
    items, hidden = select_items(
        ordered,
        previous=previous_items,
        reread=lambda candidate: _reread(tasks.followups, authority, candidate),
    )
    coverage = BriefCoverage(
        families_checked=family_result.families_checked,
        families_stale=family_result.families_stale,
        captures_unavailable=family_result.captures_unavailable,
        followups_scanned=followup_result.scanned,
        followups_truncated=followup_result.truncated or filter_truncated,
        followups=followup_result.status,
        freshness=family_result.status,
        collector="not_read",
        hidden_dropped=hidden,
        log=BriefLogCoverage(
            entries=log_read.entries, bytes=log_read.bytes, unavailable=log_read.unavailable
        ),
    )
    brief = _fit_response(
        Brief(
            generated_at=generated_at,
            scope=BriefScope(space_ids=selected, tiers=_tiers(authority)),
            items=items,
            nothing_needs_attention=not items,
            since=None if previous is None else previous.generated_at,
            coverage=coverage,
            model="none",
            saved=False,
        )
    )
    saved = _append_with_retry(
        log, key, brief, expected_sha256=None if log_read.file is None else log_read.file.sha256
    )
    return replace(brief, saved=saved)


def brief_ack(
    tasks: Any, session: BriefSessionAuthority, *, log: BriefLogStore, family_ref: str
) -> dict[str, object]:
    """Record the family's current fingerprint for this visibility; CAS conflict is
    ``version_conflict`` and the caller retries. Needs both ``brief-read`` and ``brief-ack``."""
    if not session.brief_grants >= BRIEF_GRANTS:
        # The grant flags are independent, but an ack takes effect only under the visibility key
        # of a session that reads briefs, and its fingerprint is brief-read data: ack needs both.
        raise BriefError("unsupported_capability")
    family_id = parse_family_ref(family_ref)
    authority = session.authority
    try:
        report = tasks.source_freshness.freshness(
            authority, space_ids=None, stale_days=BRIEF_STALE_DAYS_DEFAULT
        )
    except T03Error as error:
        if error.code == "unsupported_capability":
            raise BriefError("unsupported_capability") from None
        _raise_if_unreadable(error)
        raise BriefError("operation_pending") from None
    except Exception as error:  # noqa: BLE001 - classified by _raise_if_unreadable
        _raise_if_unreadable(error)
        raise BriefError("operation_pending") from None
    family = next((item for item in report.families if item.family_id == family_id), None)
    if family is None:
        raise BriefError("not_found")
    fingerprint = FamilyFingerprint(family.newest_recorded_at, family.unavailable_count)
    key = visibility_key(session, selected_space_ids=None)
    current = log.read(key)
    try:
        written = log.ack(
            key,
            family_ref,
            fingerprint,
            expected_sha256=None if current.file is None else current.file.sha256,
        )
    except BriefLogWriteError:
        raise BriefError("operation_pending") from None
    if not written:
        raise BriefError("version_conflict")
    return {"family_ref": family_ref, "fingerprint": fingerprint.to_wire()}


def brief_last(
    tasks: Any,
    session: BriefSessionAuthority,
    *,
    log: BriefLogStore,
    space_ids: Sequence[str] | None = None,
) -> dict[str, object] | None:
    """Owner CLI only: the most recent saved brief for this visibility, re-read so a follow-up
    whose evidence was retracted (or that no longer reads) shows ``withheld``."""
    if not session.authority.owner:
        raise BriefError("unsupported_capability")
    selected = _validated_selection(space_ids)
    _require_known_spaces(tasks, session.authority, selected)
    current = log.read(visibility_key(session, selected_space_ids=selected))
    if current.file is None or not current.file.briefs:
        return None
    briefs = current.file.briefs
    entry = briefs[-1]
    items: list[dict[str, object]] = []
    for logged in entry.items:
        view: dict[str, object] = dict(logged)
        ref = cast(str, logged["ref"])
        if ref.startswith("followup:"):
            try:
                item = tasks.followups.show(
                    ref.removeprefix("followup:"), authority=session.authority
                )[0]
                readable = not item.evidence_retracted
            except Exception:  # noqa: BLE001 - the dated brief stays readable
                readable = False
            view["status"] = "current" if readable else "withheld"
        else:
            view["status"] = "logged"
        items.append(view)
    return {
        "generated_at": entry.generated_at,
        "since": briefs[-2].generated_at if len(briefs) > 1 else None,
        "items": items,
        "coverage": entry.coverage,
        "model": "none",
        "log": {
            "entries": current.entries,
            "bytes": current.bytes,
            "unavailable": current.unavailable,
        },
    }


def bind_brief_operations(
    tasks: Any,
    session: BriefSessionAuthority,
    *,
    log: BriefLogStore,
    clock: Callable[[], datetime] = default_brief_clock,
) -> dict[str, BriefOperation]:
    """Close the two MCP operations over one task set, one session authority and one log."""

    def brief(arguments: Mapping[str, object]) -> dict[str, object]:
        validate_brief_arguments("brief", arguments)
        try:
            result = brief_generate(
                tasks,
                session,
                log=log,
                clock=clock,
                space_ids=cast(list[str] | None, arguments.get("space_ids")),
                stale_days=cast(int, arguments.get("stale_days", BRIEF_STALE_DAYS_DEFAULT)),
            )
        except BriefError as error:
            return _refusal(error)
        return _ok(result.to_wire())

    def ack(arguments: Mapping[str, object]) -> dict[str, object]:
        validate_brief_arguments("ack", arguments)
        try:
            data = brief_ack(tasks, session, log=log, family_ref=cast(str, arguments["family_ref"]))
        except BriefError as error:
            return _refusal(error)
        return _ok(data)

    return {"brief": brief, "ack": ack}


# -- operation helpers --------------------------------------------------------------


def _validated_selection(space_ids: Sequence[str] | None) -> tuple[str, ...] | None:
    if space_ids is None:
        return None
    values = tuple(space_ids)
    if not values:
        return None
    if (
        len(values) > BRIEF_MAX_SPACE_SELECTION
        or any(not isinstance(value, str) or not value for value in values)
        or len(set(values)) != len(values)
    ):
        raise BriefError("invalid_request")
    return values


def _require_known_spaces(
    tasks: Any, authority: EffectiveAuthority, selected: tuple[str, ...] | None
) -> None:
    """Refuse a selection naming a space that does not exist or that the session cannot see.

    Runs before any visibility key is derived, so unknown IDs can never mint key files (and
    evict other sessions' logs, the owner's included)."""
    if selected is None:
        return
    if authority.space_ids is not None and not frozenset(selected) <= authority.space_ids:
        raise BriefError("invalid_request")
    try:
        known = {space.space_id for space in tasks.spaces.spaces()}
    except Exception as error:  # noqa: BLE001 - classified by _raise_if_unreadable
        _raise_if_unreadable(error)
        raise BriefError("operation_pending") from None
    if not set(selected) <= known:
        raise BriefError("invalid_request")


def _scoped_authority(
    authority: EffectiveAuthority, selected: tuple[str, ...] | None
) -> EffectiveAuthority:
    """Owner authority is never narrowed (the owner filters explicitly); a client's selection
    must lie within its authority and then replaces ``space_ids`` for every provider read."""
    if selected is None or authority.owner:
        return authority
    chosen = frozenset(selected)
    if authority.space_ids is not None and not chosen <= authority.space_ids:
        raise BriefError("invalid_request")
    return replace(authority, space_ids=chosen)


def _owner_capture_filter(
    tasks: Any, authority: EffectiveAuthority, selected: tuple[str, ...] | None
) -> tuple[frozenset[str] | None, bool]:
    if selected is None or not authority.owner:
        return None, False
    captures: set[str] = set()
    truncated = False
    for space_id in selected:
        try:
            ids, more = tasks.spaces.space_capture_ids(space_id, limit=BRIEF_SPACE_CAPTURE_LIMIT)
        except T03Error as error:
            if error.code == "not_found":
                raise BriefError("invalid_request") from None
            _raise_if_unreadable(error)
            raise BriefError("operation_pending") from None
        except Exception as error:  # noqa: BLE001 - classified by _raise_if_unreadable
            _raise_if_unreadable(error)
            raise BriefError("operation_pending") from None
        captures.update(ids)
        truncated = truncated or bool(more)
    return frozenset(captures), truncated


def _reread(
    followups: Any, authority: EffectiveAuthority, candidate: FollowupCandidate
) -> tuple[str, ...] | None:
    try:
        shown = followups.show(candidate.item_id, authority=authority)
    except FollowupError:
        return None
    except T03Error as error:
        if error.code == "operation_pending":
            raise BriefError("operation_pending") from None
        return None
    except Exception as error:  # noqa: BLE001 - classified by _raise_if_unreadable
        _raise_if_unreadable(error)
        return None
    return tuple(view.evidence_id for view in shown[1])[:MAX_EVIDENCE_PER_ITEM]


def _append_with_retry(
    log: BriefLogStore, key: str, brief: Brief, *, expected_sha256: str | None
) -> bool:
    entry = BriefLogEntry(
        generated_at=brief.generated_at,
        items=tuple(_log_item(item) for item in brief.items),
        coverage=brief.coverage.to_wire(),
    )
    expected = expected_sha256
    for _attempt in range(2):
        try:
            if log.append(key, entry, expected_sha256=expected):
                return True
        except BriefLogWriteError:
            pass
        current = log.read(key)
        expected = None if current.file is None else current.file.sha256
    return False


def _log_item(item: BriefItem) -> dict[str, object]:
    logged: dict[str, object] = {
        "kind": item.kind.value,
        "ref": item.ref,
        "reasons": [reason.value for reason in item.reasons],
        "evidence": list(item.evidence),
        "changed": item.changed.value,
        "rank": item.rank,
    }
    assert set(logged) == LOG_ITEM_KEYS
    return logged


def _tiers(authority: EffectiveAuthority) -> tuple[str, ...]:
    return tuple(sorted(tier.value for tier in authority.allowed_read_tiers))


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("brief clock must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _ok(data: object) -> dict[str, object]:
    return {"outcome": "ok", "data": data, "code": None}


def _refusal(error: BriefError) -> dict[str, object]:
    return {"outcome": "refused", "data": None, "code": error.code}


__all__ = [
    "BRIEF_GRANTS",
    "BRIEF_MAX_SPACE_SELECTION",
    "BRIEF_SCAN_PAGE",
    "BRIEF_SPACE_CAPTURE_LIMIT",
    "BriefOperation",
    "BriefServiceError",
    "Candidate",
    "FamilyCandidate",
    "FamilyProviderResult",
    "FollowupCandidate",
    "FollowupProviderResult",
    "bind_brief_operations",
    "brief_ack",
    "brief_generate",
    "brief_last",
    "brief_state_dir",
    "default_brief_clock",
    "family_candidates",
    "followup_candidates",
    "rank_candidates",
    "select_items",
    "validate_brief_arguments",
]
