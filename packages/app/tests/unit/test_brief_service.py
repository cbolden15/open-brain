from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.followup_contracts import (
    FollowupError,
    FollowupKind,
    FollowupState,
    ListFilter,
    OwnerKind,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.services.brief import (
    BRIEF_GRANTS,
    BRIEF_SCAN_PAGE,
    FamilyCandidate,
    FollowupCandidate,
    family_candidates,
    followup_candidates,
    rank_candidates,
    select_items,
)
from open_brain.services.brief_contracts import (
    BRIEF_FOLLOWUP_SCAN_LIMIT,
    BRIEF_MAX_FAMILY_ITEMS,
    BRIEF_MAX_ITEMS,
    BriefChange,
    BriefError,
    BriefItemKind,
    BriefReasonCode,
)
from open_brain.services.brief_log import FamilyFingerprint

TODAY = date(2026, 10, 24)
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
OWNER = EffectiveAuthority("synthetic-owner", "synthetic-session", frozenset(), None, owner=True)
WORK = EffectiveAuthority(
    "open-brain-mcp-local",
    "mcp-synthetic",
    frozenset({"brief-read", "followup-read"}),
    None,
    allowed_read_tiers=frozenset({PrivacyTier.WORK}),
)


def _uuid(index: int) -> str:
    return f"3e6e8e2c-e638-47c6-8195-4bd6f3{index:06d}"


def _item_id(index: int) -> str:
    return "followup_" + _uuid(index)


def _evidence_id(index: int) -> str:
    return "evidence_" + _uuid(index)


@dataclass(frozen=True)
class _Item:
    """The FollowupItem fields the composer reads."""

    item_id: str
    state: FollowupState
    owner: OwnerKind = OwnerKind.SELF
    due: date | None = None
    created_at: str = "2026-10-01T09:00:00Z"
    updated_at: str = "2026-10-01T09:00:00Z"
    kind: FollowupKind = FollowupKind.COMMITMENT
    privacy_tier: str = "work"
    evidence_retracted: bool = False
    new_evidence_since_decision: bool = False


@dataclass(frozen=True)
class _View:
    evidence_id: str
    capture_id: str
    status: str = "current"


@dataclass(frozen=True)
class _Decision:
    sequence: int
    action: str
    recorded_at: str


@dataclass
class _FakeFollowups:
    items: tuple[_Item, ...] = ()
    views: dict[str, tuple[_View, ...]] = field(default_factory=dict)
    history: dict[str, tuple[_Decision, ...]] = field(default_factory=dict)
    hidden: frozenset[str] = frozenset()
    list_error: Exception | None = None
    calls: list[tuple[str, Any, EffectiveAuthority]] = field(default_factory=list)

    def list(self, filter_: ListFilter, *, authority: EffectiveAuthority) -> Any:
        self.calls.append(("list", filter_, authority))
        if self.list_error is not None:
            raise self.list_error
        rows = sorted(
            (item for item in self.items if item.state in filter_.states),
            key=lambda item: (item.created_at, item.item_id),
        )
        if filter_.cursor is not None:
            anchor = next(item for item in rows if item.item_id == filter_.cursor)
            rows = [
                row
                for row in rows
                if (row.created_at, row.item_id) > (anchor.created_at, anchor.item_id)
            ]
        page = rows[: filter_.limit]
        cursor = page[-1].item_id if len(rows) > filter_.limit and page else None
        return tuple(page), cursor

    def show(self, item_id: str, *, authority: EffectiveAuthority) -> Any:
        self.calls.append(("show", item_id, authority))
        if item_id in self.hidden:
            raise FollowupError("not_found")
        item = next(item for item in self.items if item.item_id == item_id)
        return (
            item,
            self.views.get(item_id, (_View(_evidence_id(1), "capture_a"),)),
            self.history.get(item_id, ()),
        )


@dataclass(frozen=True)
class _Family:
    family_id: str
    space_id: str
    newest_recorded_at: str
    unavailable_count: int
    stale: bool
    kind: str = "consumer_namespace"
    capture_count: int = 3


@dataclass(frozen=True)
class _Report:
    families: tuple[_Family, ...]
    stale_days: int
    generated_at: str


@dataclass
class _FakeFreshness:
    families: tuple[_Family, ...] = ()
    error: Exception | None = None
    calls: list[tuple[EffectiveAuthority, frozenset[str] | None, int]] = field(default_factory=list)

    def freshness(
        self, authority: EffectiveAuthority, *, space_ids: frozenset[str] | None, stale_days: int
    ) -> _Report:
        self.calls.append((authority, space_ids, stale_days))
        if self.error is not None:
            raise self.error
        selected = tuple(f for f in self.families if space_ids is None or f.space_id in space_ids)
        return _Report(selected, stale_days, "2026-10-24T09:00:00Z")


def test_grants_constant_is_the_two_brief_grants() -> None:
    assert frozenset({"brief-read", "brief-ack"}) == BRIEF_GRANTS


def test_followup_scan_pages_and_stops_at_the_cap_with_truncation() -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    many = tuple(
        _Item(
            _item_id(index),
            FollowupState.SUGGESTED,
            created_at=(start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
        )
        for index in range(BRIEF_FOLLOWUP_SCAN_LIMIT + 150)
    )
    fake = _FakeFollowups(items=many)
    result = followup_candidates(fake, WORK, today=TODAY)
    assert result.status == "read"
    assert result.scanned == BRIEF_FOLLOWUP_SCAN_LIMIT and result.truncated is True
    list_calls = [call for call in result_calls(fake) if call[0] == "list"]
    open_pages = [
        call
        for call in list_calls
        if call[1].states == frozenset({FollowupState.SUGGESTED, FollowupState.CONFIRMED})
    ]
    assert len(open_pages) == BRIEF_FOLLOWUP_SCAN_LIMIT // BRIEF_SCAN_PAGE
    assert all(
        call[1].limit == BRIEF_SCAN_PAGE and call[1].include_closed is False for call in open_pages
    )
    assert open_pages[0][1].cursor is None
    assert open_pages[1][1].cursor == many[BRIEF_SCAN_PAGE - 1].item_id
    decided_pages = [
        call
        for call in list_calls
        if call[1].states == frozenset({FollowupState.DISMISSED, FollowupState.RESOLVED})
    ]
    assert len(decided_pages) == 1 and decided_pages[0][1].include_closed is True
    assert all(call[2] is WORK for call in list_calls)
    # Open items owned by self and not due are followup_open candidates; the cap bounds them too.
    assert len(result.candidates) == BRIEF_FOLLOWUP_SCAN_LIMIT
    assert {candidate.kind for candidate in result.candidates} == {BriefItemKind.FOLLOWUP_OPEN}


def result_calls(fake: _FakeFollowups) -> list[tuple[str, Any, EffectiveAuthority]]:
    return fake.calls


def test_followup_classification_reasons_and_keys() -> None:
    items = (
        _Item(
            _item_id(1),
            FollowupState.CONFIRMED,
            due=date(2026, 10, 20),
            created_at="2026-10-02T09:00:00Z",
        ),
        _Item(_item_id(2), FollowupState.SUGGESTED, due=TODAY, created_at="2026-10-03T09:00:00Z"),
        _Item(
            _item_id(3),
            FollowupState.SUGGESTED,
            owner=OwnerKind.OTHER,
            created_at="2026-09-01T09:00:00Z",
        ),
        _Item(_item_id(4), FollowupState.CONFIRMED, created_at="2026-09-15T09:00:00Z"),
        _Item(
            _item_id(5),
            FollowupState.CONFIRMED,
            due=date(2026, 11, 1),
            created_at="2026-09-16T09:00:00Z",
        ),
        _Item(
            _item_id(6),
            FollowupState.DISMISSED,
            new_evidence_since_decision=True,
            updated_at="2026-10-23T12:00:00Z",
        ),
        _Item(_item_id(7), FollowupState.RESOLVED, new_evidence_since_decision=False),
        _Item(
            _item_id(8),
            FollowupState.RESOLVED,
            new_evidence_since_decision=True,
            updated_at="2026-10-22T12:00:00Z",
        ),
    )
    history: dict[str, tuple[_Decision, ...]] = {
        _item_id(6): (
            _Decision(1, "propose", "2026-10-01T09:00:00Z"),
            _Decision(2, "dismiss", "2026-10-10T09:00:00Z"),
            _Decision(3, "add_evidence", "2026-10-21T09:00:00Z"),
            _Decision(4, "add_evidence", "2026-10-23T11:00:00Z"),
            _Decision(5, "link", "2026-10-23T12:00:00Z"),
        ),
    }
    result = followup_candidates(_FakeFollowups(items=items, history=history), WORK, today=TODAY)
    by_id = {candidate.item_id: candidate for candidate in result.candidates}
    assert set(by_id) == {
        _item_id(1),
        _item_id(2),
        _item_id(4),
        _item_id(5),
        _item_id(6),
        _item_id(8),
    }
    overdue = by_id[_item_id(1)]
    assert overdue.kind is BriefItemKind.FOLLOWUP_DUE and overdue.reasons == (
        BriefReasonCode.OVERDUE,
    )
    assert overdue.reason_text == "Due 2026-10-20; 4 days overdue."
    assert overdue.sort_key == ("2026-10-20", "2026-10-02T09:00:00Z", _item_id(1))
    due_today = by_id[_item_id(2)]
    assert (
        due_today.reasons == (BriefReasonCode.DUE_TODAY,)
        and due_today.reason_text == "Due today (2026-10-24)."
    )
    open_owned = by_id[_item_id(4)]
    assert open_owned.kind is BriefItemKind.FOLLOWUP_OPEN and open_owned.reasons == (
        BriefReasonCode.OPEN_OWNED,
    )
    assert open_owned.reason_text == "Open since 2026-09-15 and owned by you."
    assert open_owned.sort_key == ("2026-09-15T09:00:00Z", _item_id(4))
    # A due date still in the future is "no due date today or past": the item stays open.
    assert by_id[_item_id(5)].kind is BriefItemKind.FOLLOWUP_OPEN
    new_evidence = by_id[_item_id(6)]
    assert new_evidence.kind is BriefItemKind.FOLLOWUP_NEW_EVIDENCE
    assert (
        new_evidence.reason_text
        == "New evidence recorded 2026-10-23T11:00:00Z after the last decision."
    )
    assert new_evidence.sort_key == (
        -int(datetime(2026, 10, 23, 11, tzinfo=UTC).timestamp()),
        _item_id(6),
    )
    # Without add_evidence rows after the decision the item's updated_at orders it.
    assert (
        by_id[_item_id(8)].reason_text
        == "New evidence recorded 2026-10-22T12:00:00Z after the last decision."
    )
    assert result.scanned == 8 and result.truncated is False
    for candidate in result.candidates:
        assert "Synthetic" not in candidate.reason_text and "summary" not in candidate.reason_text


def test_followup_provider_failure_and_unreadable_engine() -> None:
    failed = followup_candidates(_FakeFollowups(list_error=RuntimeError("boom")), WORK, today=TODAY)
    assert failed.status == "failed" and failed.candidates == () and failed.scanned == 0
    with pytest.raises(BriefError) as pending:
        followup_candidates(
            _FakeFollowups(list_error=sqlite3.OperationalError("unable to open database file")),
            WORK,
            today=TODAY,
        )
    assert pending.value.code == "operation_pending"
    with pytest.raises(BriefError) as pending_t03:
        followup_candidates(
            _FakeFollowups(list_error=T03Error("operation_pending")), WORK, today=TODAY
        )
    assert pending_t03.value.code == "operation_pending"
    hidden = _FakeFollowups(
        items=(_Item(_item_id(6), FollowupState.DISMISSED, new_evidence_since_decision=True),),
        hidden=frozenset({_item_id(6)}),
    )
    assert followup_candidates(hidden, WORK, today=TODAY).candidates == ()


def test_owner_space_filter_keeps_items_citing_a_selected_capture() -> None:
    fake = _FakeFollowups(
        items=(
            _Item(_item_id(1), FollowupState.CONFIRMED, created_at="2026-09-01T09:00:00Z"),
            _Item(_item_id(2), FollowupState.CONFIRMED, created_at="2026-09-02T09:00:00Z"),
        ),
        views={
            _item_id(1): (
                _View(_evidence_id(1), "capture_in"),
                _View(_evidence_id(2), "capture_out"),
            ),
            _item_id(2): (_View(_evidence_id(3), "capture_out"),),
        },
    )
    result = followup_candidates(fake, OWNER, today=TODAY, capture_filter=frozenset({"capture_in"}))
    assert [candidate.item_id for candidate in result.candidates] == [_item_id(1)]
    assert result.scanned == 2


def test_family_provider_classifies_and_honours_acks() -> None:
    families = (
        _Family("consumer:ns-a:space_1", "space_1", "2026-10-01T09:00:00Z", 2, True),
        _Family("consumer:ns-b:space_1", "space_1", "2026-10-23T09:00:00Z", 1, False),
        _Family("markdown:space_2", "space_2", "2026-09-01T09:00:00Z", 0, True),
        _Family("consumer:ns-c:space_2", "space_2", "2026-10-24T08:00:00Z", 0, False),
        _Family("consumer:acked:space_2", "space_2", "2026-08-01T09:00:00Z", 0, True),
        _Family("consumer:reacked:space_2", "space_2", "2026-08-02T09:00:00Z", 1, True),
    )
    acks = {
        "family:consumer:acked:space_2": FamilyFingerprint("2026-08-01T09:00:00Z", 0),
        "family:consumer:reacked:space_2": FamilyFingerprint("2026-08-02T09:00:00Z", 0),
    }
    fake = _FakeFreshness(families)
    result = family_candidates(fake, WORK, space_ids=None, stale_days=7, acks=acks)
    assert fake.calls == [(WORK, None, 7)]
    assert result.status == "read"
    assert (result.families_checked, result.families_stale, result.captures_unavailable) == (
        6,
        4,
        4,
    )
    by_id = {candidate.family_id: candidate for candidate in result.candidates}
    assert set(by_id) == {
        "consumer:ns-a:space_1",
        "consumer:ns-b:space_1",
        "markdown:space_2",
        "consumer:reacked:space_2",
    }
    both = by_id["consumer:ns-a:space_1"]
    assert both.kind is BriefItemKind.SOURCE_UNAVAILABLE
    assert both.reasons == (BriefReasonCode.CAPTURES_UNAVAILABLE, BriefReasonCode.NO_CAPTURE_SINCE)
    assert (
        both.reason_text
        == "2 captures missing or inaccessible; no capture since 2026-10-01T09:00:00Z."
    )
    assert both.sort_key == (-2, "consumer:ns-a:space_1")
    assert both.fingerprint == FamilyFingerprint("2026-10-01T09:00:00Z", 2)
    only_unavailable = by_id["consumer:ns-b:space_1"]
    assert only_unavailable.reasons == (BriefReasonCode.CAPTURES_UNAVAILABLE,)
    assert only_unavailable.reason_text == "1 capture missing or inaccessible."
    stale = by_id["markdown:space_2"]
    assert stale.kind is BriefItemKind.SOURCE_STALE and stale.reasons == (
        BriefReasonCode.NO_CAPTURE_SINCE,
    )
    assert stale.reason_text == "No capture since 2026-09-01T09:00:00Z."
    assert stale.sort_key == ("2026-09-01T09:00:00Z", "markdown:space_2")
    # The re-acked family's unavailable count changed, so the stale ack no longer hides it.
    assert by_id["consumer:reacked:space_2"].kind is BriefItemKind.SOURCE_UNAVAILABLE
    scoped = family_candidates(
        _FakeFreshness(families), WORK, space_ids=frozenset({"space_1"}), stale_days=30, acks={}
    )
    assert scoped.families_checked == 2 and {c.family_id for c in scoped.candidates} == {
        "consumer:ns-a:space_1",
        "consumer:ns-b:space_1",
    }


def test_family_provider_failure_and_unreadable_engine() -> None:
    failed = family_candidates(
        _FakeFreshness(error=RuntimeError("boom")), WORK, space_ids=None, stale_days=7, acks={}
    )
    assert failed.status == "failed" and (
        failed.families_checked,
        failed.families_stale,
        failed.captures_unavailable,
    ) == (0, 0, 0)
    with pytest.raises(BriefError) as pending:
        family_candidates(
            _FakeFreshness(error=sqlite3.OperationalError("locked")),
            WORK,
            space_ids=None,
            stale_days=7,
            acks={},
        )
    assert pending.value.code == "operation_pending"
    with pytest.raises(BriefError) as refused:
        family_candidates(
            _FakeFreshness(error=T03Error("unsupported_capability")),
            WORK,
            space_ids=None,
            stale_days=7,
            acks={},
        )
    assert refused.value.code == "unsupported_capability"


def _followup(kind: BriefItemKind, index: int, *key: object) -> FollowupCandidate:
    return FollowupCandidate(
        kind, _item_id(index), (BriefReasonCode.OPEN_OWNED,), "x.", tuple(key) or (str(index),)
    )


def _family(kind: BriefItemKind, name: str, *key: object) -> FamilyCandidate:
    return FamilyCandidate(
        kind,
        name,
        (BriefReasonCode.NO_CAPTURE_SINCE,),
        "x.",
        tuple(key) or (name,),
        FamilyFingerprint("2026-10-01T09:00:00Z", 0),
    )


def test_ranking_follows_the_fixed_levels_and_new_before_still_open() -> None:
    previous = {"followup:" + _item_id(1), "family:f-stale-old"}
    ordered = rank_candidates(
        (
            _followup(BriefItemKind.FOLLOWUP_OPEN, 10, "2026-09-10"),
            _followup(
                BriefItemKind.FOLLOWUP_DUE, 1, "2026-10-01", "2026-09-01T09:00:00Z"
            ),  # still open
            _followup(
                BriefItemKind.FOLLOWUP_DUE, 2, "2026-10-05", "2026-09-01T09:00:00Z"
            ),  # new, later due
            _followup(BriefItemKind.FOLLOWUP_NEW_EVIDENCE, 3, -100),
            _followup(BriefItemKind.FOLLOWUP_NEW_EVIDENCE, 4, -200),  # newer evidence sorts first
            _followup(BriefItemKind.FOLLOWUP_OPEN, 11, "2026-09-09"),
        ),
        (
            _family(BriefItemKind.SOURCE_STALE, "f-stale-old", "2026-08-01T09:00:00Z"),
            _family(BriefItemKind.SOURCE_STALE, "f-stale-new", "2026-09-01T09:00:00Z"),
            _family(BriefItemKind.SOURCE_UNAVAILABLE, "f-one", -1),
            _family(BriefItemKind.SOURCE_UNAVAILABLE, "f-three", -3),
        ),
        previous_refs=previous,
    )
    assert [candidate.ref for candidate in ordered] == [
        "followup:" + _item_id(2),  # due: new before still-open even though due later
        "followup:" + _item_id(1),
        "family:f-three",  # unavailable: most unavailable first
        "family:f-one",
        "followup:" + _item_id(4),  # new evidence: newest first
        "followup:" + _item_id(3),
        "followup:" + _item_id(11),  # open: oldest first
        "followup:" + _item_id(10),
        "family:f-stale-new",  # stale: new ref before the still-open one, then longest silence
        "family:f-stale-old",
    ]


def test_selection_caps_items_and_families_and_counts_hidden_candidates() -> None:
    ordered = (
        _followup(BriefItemKind.FOLLOWUP_DUE, 1),
        _family(BriefItemKind.SOURCE_UNAVAILABLE, "markdown:f-a"),
        _family(BriefItemKind.SOURCE_UNAVAILABLE, "markdown:f-b"),
        _followup(BriefItemKind.FOLLOWUP_NEW_EVIDENCE, 2),
        _followup(BriefItemKind.FOLLOWUP_OPEN, 3),
        _followup(BriefItemKind.FOLLOWUP_OPEN, 4),
        _followup(BriefItemKind.FOLLOWUP_OPEN, 5),
        _family(BriefItemKind.SOURCE_STALE, "markdown:f-c"),
        _followup(BriefItemKind.FOLLOWUP_OPEN, 6),
    )
    hidden = {_item_id(2), _item_id(4)}

    def reread(candidate: FollowupCandidate) -> tuple[str, ...] | None:
        return None if candidate.item_id in hidden else (_evidence_id(1),)

    previous = {
        "followup:" + _item_id(1): {
            "kind": "followup_due",
            "reasons": ["open_owned"],
            "evidence": [_evidence_id(1)],
        },
        "followup:" + _item_id(3): {
            "kind": "followup_open",
            "reasons": ["open_owned"],
            "evidence": [_evidence_id(9)],
        },
        "family:markdown:f-a": {
            "kind": "source_unavailable",
            "reasons": ["no_capture_since"],
            "evidence": [],
        },
    }
    items, dropped = select_items(ordered, previous=previous, reread=reread)
    assert dropped == 2
    assert len(items) == BRIEF_MAX_ITEMS
    assert [item.ref for item in items] == [
        "followup:" + _item_id(1),
        "family:markdown:f-a",
        "family:markdown:f-b",
        "followup:" + _item_id(3),
        "followup:" + _item_id(5),
    ]
    assert (
        sum(
            item.kind in {BriefItemKind.SOURCE_UNAVAILABLE, BriefItemKind.SOURCE_STALE}
            for item in items
        )
        == BRIEF_MAX_FAMILY_ITEMS
    )
    assert [item.rank for item in items] == [1, 2, 3, 4, 5]
    assert [item.changed for item in items] == [
        BriefChange.STILL_OPEN,  # same kind, reasons and evidence
        BriefChange.STILL_OPEN,
        BriefChange.NEW,
        BriefChange.CHANGED,  # evidence differs
        BriefChange.NEW,
    ]
    assert items[1].evidence == () and items[0].evidence == (_evidence_id(1),)
    assert items[0].reason_text == "x." and items[0].reasons == (BriefReasonCode.OPEN_OWNED,)
