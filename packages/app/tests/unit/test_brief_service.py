from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.followup_contracts import (
    MAX_EVIDENCE_PER_ITEM,
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
    BriefServiceError,
    FamilyCandidate,
    FollowupCandidate,
    _fit_response,
    bind_brief_operations,
    brief_ack,
    brief_generate,
    brief_last,
    brief_state_dir,
    family_candidates,
    followup_candidates,
    rank_candidates,
    select_items,
    validate_brief_arguments,
)
from open_brain.services.brief_authority import BriefSessionAuthority, visibility_key
from open_brain.services.brief_contracts import (
    BRIEF_FOLLOWUP_SCAN_LIMIT,
    BRIEF_MAX_FAMILY_ITEMS,
    BRIEF_MAX_ITEMS,
    BRIEF_RESPONSE_MAX_BYTES,
    Brief,
    BriefChange,
    BriefCoverage,
    BriefError,
    BriefItem,
    BriefItemKind,
    BriefLogCoverage,
    BriefReasonCode,
    BriefScope,
)
from open_brain.services.brief_log import (
    BriefLogStore,
    BriefLogWriteError,
    FamilyFingerprint,
)
from open_brain.services.followups import FOLLOWUP_GRANTS

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


OWNER_SESSION = BriefSessionAuthority(
    authority=OWNER, brief_grants=BRIEF_GRANTS, followup_grants=FOLLOWUP_GRANTS
)
WORK_SESSION = BriefSessionAuthority(
    authority=WORK,
    brief_grants=frozenset({"brief-read"}),
    followup_grants=frozenset({"followup-read"}),
)
FAMILY_ONLY_SESSION = BriefSessionAuthority(
    authority=replace(WORK, capabilities=frozenset({"brief-read"})),
    brief_grants=frozenset({"brief-read"}),
    followup_grants=frozenset(),
)
SPACE_1 = "space_3e6e8e2c-e638-47c6-8195-4bd6f3000001"
SPACE_2 = "space_3e6e8e2c-e638-47c6-8195-4bd6f3000002"


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def tick(self, **delta: int) -> None:
        self.now += timedelta(**delta)


@dataclass
class _FakeSpaces:
    captures: dict[str, tuple[str, ...]] = field(default_factory=dict)
    truncated: bool = False

    def space_capture_ids(self, space_id: str, *, limit: int) -> tuple[tuple[str, ...], bool]:
        assert 1 <= limit <= 10_000
        if space_id not in self.captures:
            raise T03Error("not_found")
        return self.captures[space_id], self.truncated


def _tasks(
    tmp_path: Path,
    *,
    followups: _FakeFollowups | None = None,
    freshness: _FakeFreshness | None = None,
    spaces: _FakeSpaces | None = None,
) -> Any:
    root = tmp_path / "brain"
    (root / ".open-brain" / "state").mkdir(parents=True, exist_ok=True)
    profile = SimpleNamespace(root=root, root_identity=(0, 0), owner_actor_id="synthetic-owner")
    return SimpleNamespace(
        profile=profile,
        followups=followups or _FakeFollowups(),
        source_freshness=freshness or _FakeFreshness(),
        spaces=spaces or _FakeSpaces(),
    )


def _log(tasks: Any, clock: _Clock | None = None) -> BriefLogStore:
    return BriefLogStore(brief_state_dir(tasks.profile), clock=clock or _Clock())


def _log_path(
    tasks: Any, session: BriefSessionAuthority, selected: tuple[str, ...] | None = None
) -> Path:
    key = visibility_key(session, selected_space_ids=selected)
    return brief_state_dir(tasks.profile) / "briefs" / f"{key}.json"


DUE_ITEM = _Item(
    _item_id(1), FollowupState.CONFIRMED, due=date(2026, 10, 20), created_at="2026-10-02T09:00:00Z"
)
STALE_FAMILY = _Family("consumer:ns-a:" + SPACE_1, SPACE_1, "2026-10-01T09:00:00Z", 0, True)


def test_generate_with_nothing_to_report_logs_an_entry_and_reports_since_next_time(
    tmp_path: Path,
) -> None:
    tasks = _tasks(tmp_path)
    clock = _Clock()
    log = _log(tasks, clock)
    brief = brief_generate(tasks, OWNER_SESSION, log=log, clock=clock)
    assert brief.items == () and brief.nothing_needs_attention is True
    assert brief.since is None and brief.saved is True and brief.model == "none"
    assert brief.generated_at == "2026-10-24T09:00:00Z"
    assert brief.scope.space_ids is None and brief.scope.tiers == ("personal", "public", "work")
    coverage = brief.coverage
    assert (coverage.followups, coverage.freshness, coverage.collector) == (
        "read",
        "read",
        "not_read",
    )
    assert (coverage.followups_scanned, coverage.followups_truncated, coverage.hidden_dropped) == (
        0,
        False,
        0,
    )
    assert (coverage.log.entries, coverage.log.bytes, coverage.log.unavailable) == (0, 0, False)
    path = _log_path(tasks, OWNER_SESSION)
    assert path.exists()
    wire = brief.to_wire()
    assert wire["model"] == "none" and wire["saved"] is True and wire["items"] == []

    size_after_first = path.stat().st_size
    clock.tick(days=1)
    again = brief_generate(tasks, OWNER_SESSION, log=log, clock=clock)
    assert again.since == "2026-10-24T09:00:00Z" and again.generated_at == "2026-10-25T09:00:00Z"
    assert (again.coverage.log.entries, again.coverage.log.bytes) == (1, size_after_first)
    assert log.read(visibility_key(OWNER_SESSION, selected_space_ids=None)).entries == 2


def test_generate_marks_changed_against_the_last_brief_and_logs_pointers_only(
    tmp_path: Path,
) -> None:
    followups = _FakeFollowups(items=(DUE_ITEM,))
    tasks = _tasks(tmp_path, followups=followups, freshness=_FakeFreshness((STALE_FAMILY,)))
    clock = _Clock()
    log = _log(tasks, clock)
    first = brief_generate(tasks, OWNER_SESSION, log=log, clock=clock)
    assert [(item.ref, item.changed, item.rank) for item in first.items] == [
        ("followup:" + _item_id(1), BriefChange.NEW, 1),
        ("family:" + STALE_FAMILY.family_id, BriefChange.NEW, 2),
    ]
    assert first.items[0].evidence == (_evidence_id(1),) and first.items[1].evidence == ()
    clock.tick(hours=1)
    second = brief_generate(tasks, OWNER_SESSION, log=log, clock=clock)
    assert [item.changed for item in second.items] == [
        BriefChange.STILL_OPEN,
        BriefChange.STILL_OPEN,
    ]
    followups.views[_item_id(1)] = (
        _View(_evidence_id(1), "capture_a"),
        _View(_evidence_id(2), "capture_a"),
    )
    clock.tick(hours=1)
    third = brief_generate(tasks, OWNER_SESSION, log=log, clock=clock)
    assert [item.changed for item in third.items] == [BriefChange.CHANGED, BriefChange.STILL_OPEN]
    assert third.since == second.generated_at
    raw = _log_path(tasks, OWNER_SESSION).read_bytes()
    stored = json.loads(raw)
    assert len(stored["briefs"]) == 3
    for entry in stored["briefs"]:
        for item in entry["items"]:
            assert set(item) == {"changed", "evidence", "kind", "rank", "ref", "reasons"}
    assert b"Due 2026" not in raw and b"reason_text" not in raw and b"overdue." not in raw
    assert b"capture_a" not in raw, "capture IDs are not logged either; only evidence IDs"


def test_without_followup_read_the_brief_holds_family_items_only(tmp_path: Path) -> None:
    followups = _FakeFollowups(items=(DUE_ITEM,))
    tasks = _tasks(tmp_path, followups=followups, freshness=_FakeFreshness((STALE_FAMILY,)))
    brief = brief_generate(tasks, FAMILY_ONLY_SESSION, log=_log(tasks), clock=_Clock())
    assert brief.coverage.followups == "not_granted" and brief.coverage.followups_scanned == 0
    assert [item.kind for item in brief.items] == [BriefItemKind.SOURCE_STALE]
    assert followups.calls == []
    assert brief.scope.tiers == ("work",)


def test_ack_hides_a_family_until_a_component_changes_and_ignores_space_selection(
    tmp_path: Path,
) -> None:
    freshness = _FakeFreshness((STALE_FAMILY,))
    tasks = _tasks(tmp_path, freshness=freshness, spaces=_FakeSpaces({SPACE_1: ()}))
    log = _log(tasks)
    assert (
        brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock()).items[0].kind
        is BriefItemKind.SOURCE_STALE
    )
    acked = brief_ack(tasks, OWNER_SESSION, log=log, family_ref="family:" + STALE_FAMILY.family_id)
    assert acked == {
        "family_ref": "family:" + STALE_FAMILY.family_id,
        "fingerprint": {"newest_recorded_at": "2026-10-01T09:00:00Z", "unavailable_count": 0},
    }
    assert brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock()).items == ()
    scoped = brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock(), space_ids=(SPACE_1,))
    assert scoped.items == () and scoped.coverage.families_checked == 1, (
        "the ack applies to every selection"
    )
    assert _log_path(tasks, OWNER_SESSION, (SPACE_1,)).exists()
    freshness.families = (replace(STALE_FAMILY, unavailable_count=1),)
    returned = brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock())
    assert [item.kind for item in returned.items] == [BriefItemKind.SOURCE_UNAVAILABLE]
    # The ack is keyed by visibility: a different session does not inherit it.
    assert brief_generate(tasks, FAMILY_ONLY_SESSION, log=log, clock=_Clock()).items != ()


def test_ack_refusals(tmp_path: Path) -> None:
    tasks = _tasks(tmp_path, freshness=_FakeFreshness((STALE_FAMILY,)))
    log = _log(tasks)
    for ref, code in (
        ("family:consumer:unknown:" + SPACE_1, "not_found"),
        ("followup:" + _item_id(1), "invalid_request"),
        ("family:", "invalid_request"),
    ):
        with pytest.raises(BriefError) as refused:
            brief_ack(tasks, OWNER_SESSION, log=log, family_ref=ref)
        assert refused.value.code == code, ref

    class _Conflicting(BriefLogStore):
        def ack(self, *args: Any, **kwargs: Any) -> bool:
            return False

    with pytest.raises(BriefError) as conflict:
        brief_ack(
            tasks,
            OWNER_SESSION,
            log=_Conflicting(brief_state_dir(tasks.profile), clock=_Clock()),
            family_ref="family:" + STALE_FAMILY.family_id,
        )
    assert conflict.value.code == "version_conflict"
    tasks.source_freshness.error = RuntimeError("boom")
    with pytest.raises(BriefError) as pending:
        brief_ack(tasks, OWNER_SESSION, log=log, family_ref="family:" + STALE_FAMILY.family_id)
    assert pending.value.code == "operation_pending"
    tasks.source_freshness.error = T03Error("unsupported_capability")
    with pytest.raises(BriefError) as unsupported:
        brief_ack(tasks, OWNER_SESSION, log=log, family_ref="family:" + STALE_FAMILY.family_id)
    assert unsupported.value.code == "unsupported_capability"


def test_log_conflict_or_write_failure_gives_saved_false_after_one_retry(tmp_path: Path) -> None:
    tasks = _tasks(tmp_path, followups=_FakeFollowups(items=(DUE_ITEM,)))

    class _Losing(BriefLogStore):
        attempts = 0

        def append(self, *args: Any, **kwargs: Any) -> bool:
            type(self).attempts += 1
            return False

    losing = _Losing(brief_state_dir(tasks.profile), clock=_Clock())
    brief = brief_generate(tasks, OWNER_SESSION, log=losing, clock=_Clock())
    assert brief.saved is False and _Losing.attempts == 2
    assert [item.ref for item in brief.items] == ["followup:" + _item_id(1)], (
        "the brief is still complete"
    )
    assert brief.to_wire()["saved"] is False

    class _Broken(BriefLogStore):
        attempts = 0

        def append(self, *args: Any, **kwargs: Any) -> bool:
            type(self).attempts += 1
            raise BriefLogWriteError("disk")

    broken = _Broken(brief_state_dir(tasks.profile), clock=_Clock())
    assert brief_generate(tasks, OWNER_SESSION, log=broken, clock=_Clock()).saved is False
    assert _Broken.attempts == 2


def test_corrupt_log_file_gives_since_none_and_an_aside_copy_without_touching_other_keys(
    tmp_path: Path,
) -> None:
    tasks = _tasks(tmp_path)
    log = _log(tasks)
    brief_generate(tasks, FAMILY_ONLY_SESSION, log=log, clock=_Clock())
    other = _log_path(tasks, FAMILY_ONLY_SESSION)
    other_bytes = other.read_bytes()
    first = brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock())
    path = _log_path(tasks, OWNER_SESSION)
    path.write_bytes(b"{corrupt")
    clock = _Clock()
    clock.tick(days=1)
    brief = brief_generate(tasks, OWNER_SESSION, log=log, clock=clock)
    assert brief.since is None and brief.coverage.log.unavailable is True
    assert (brief.coverage.log.entries, brief.coverage.log.bytes) == (0, 0)
    assert brief.saved is True
    assert path.with_name(path.name + ".aside").read_bytes() == b"{corrupt"
    assert json.loads(path.read_bytes())["briefs"][0]["generated_at"] == "2026-10-25T09:00:00Z"
    assert other.read_bytes() == other_bytes
    assert first.since is None


def test_engine_unreadable_is_operation_pending_and_logs_nothing(tmp_path: Path) -> None:
    tasks = _tasks(
        tmp_path,
        followups=_FakeFollowups(
            list_error=sqlite3.OperationalError("unable to open database file")
        ),
    )
    log = _log(tasks)
    with pytest.raises(BriefError) as pending:
        brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock())
    assert pending.value.code == "operation_pending"
    assert not _log_path(tasks, OWNER_SESSION).exists()
    tasks = _tasks(tmp_path, freshness=_FakeFreshness(error=T03Error("operation_pending")))
    with pytest.raises(BriefError):
        brief_generate(tasks, OWNER_SESSION, log=_log(tasks), clock=_Clock())


def test_partial_provider_failure_appears_in_coverage_and_the_rest_still_appears(
    tmp_path: Path,
) -> None:
    tasks = _tasks(
        tmp_path,
        followups=_FakeFollowups(items=(DUE_ITEM,)),
        freshness=_FakeFreshness(error=RuntimeError("boom")),
    )
    brief = brief_generate(tasks, OWNER_SESSION, log=_log(tasks), clock=_Clock())
    assert brief.coverage.freshness == "failed" and brief.coverage.followups == "read"
    assert [item.kind for item in brief.items] == [BriefItemKind.FOLLOWUP_DUE]
    assert brief.saved is True
    tasks = _tasks(
        tmp_path,
        followups=_FakeFollowups(list_error=RuntimeError("boom")),
        freshness=_FakeFreshness((STALE_FAMILY,)),
    )
    brief = brief_generate(tasks, OWNER_SESSION, log=_log(tasks), clock=_Clock())
    assert brief.coverage.followups == "failed" and brief.coverage.freshness == "read"
    assert [item.kind for item in brief.items] == [BriefItemKind.SOURCE_STALE]


def test_non_owner_candidates_that_no_longer_read_are_dropped_and_counted(tmp_path: Path) -> None:
    items = (
        DUE_ITEM,
        _Item(
            _item_id(2),
            FollowupState.CONFIRMED,
            due=date(2026, 10, 21),
            created_at="2026-10-03T09:00:00Z",
        ),
    )
    followups = _FakeFollowups(items=items, hidden=frozenset({_item_id(2)}))
    tasks = _tasks(tmp_path, followups=followups)
    brief = brief_generate(tasks, WORK_SESSION, log=_log(tasks), clock=_Clock())
    assert [item.ref for item in brief.items] == ["followup:" + _item_id(1)]
    assert brief.coverage.hidden_dropped == 1
    encoded = json.dumps(brief.to_wire())
    assert "withheld" not in encoded and _item_id(2) not in encoded
    assert all(call[2] is WORK for call in followups.calls)


def test_owner_space_selection_filters_by_evidence_captures_and_validates_spaces(
    tmp_path: Path,
) -> None:
    followups = _FakeFollowups(
        items=(
            DUE_ITEM,
            _Item(
                _item_id(2),
                FollowupState.CONFIRMED,
                due=date(2026, 10, 21),
                created_at="2026-10-03T09:00:00Z",
            ),
        ),
        views={
            _item_id(1): (_View(_evidence_id(1), "capture_one"),),
            _item_id(2): (_View(_evidence_id(2), "capture_two"),),
        },
    )
    freshness = _FakeFreshness(
        (STALE_FAMILY, _Family("markdown:" + SPACE_2, SPACE_2, "2026-09-01T09:00:00Z", 0, True))
    )
    spaces = _FakeSpaces({SPACE_1: ("capture_one",), SPACE_2: ("capture_two",)})
    tasks = _tasks(tmp_path, followups=followups, freshness=freshness, spaces=spaces)
    log = _log(tasks)
    brief = brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock(), space_ids=(SPACE_2,))
    assert [item.ref for item in brief.items] == [
        "followup:" + _item_id(2),
        "family:markdown:" + SPACE_2,
    ]
    assert brief.scope.space_ids == (SPACE_2,) and brief.coverage.families_checked == 1
    assert freshness.calls[-1] == (OWNER, frozenset({SPACE_2}), 7)
    assert all(call[2] is OWNER for call in followups.calls), "owner authority is never narrowed"
    everything = brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock())
    assert len(everything.items) == 4 and everything.since is None, (
        "a different selection is a different key"
    )
    with pytest.raises(BriefError) as unknown:
        brief_generate(
            tasks,
            OWNER_SESSION,
            log=log,
            clock=_Clock(),
            space_ids=("space_3e6e8e2c-e638-47c6-8195-4bd6f3000009",),
        )
    assert unknown.value.code == "invalid_request"
    spaces.truncated = True
    assert (
        brief_generate(
            tasks, OWNER_SESSION, log=log, clock=_Clock(), space_ids=(SPACE_1,)
        ).coverage.followups_truncated
        is True
    )
    with pytest.raises(BriefError):
        brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock(), stale_days=0)
    with pytest.raises(BriefError):
        brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock(), space_ids=(SPACE_1, SPACE_1))


def test_client_space_selection_must_stay_within_the_session_authority(tmp_path: Path) -> None:
    narrowed = replace(WORK, space_ids=frozenset({SPACE_1}))
    session = BriefSessionAuthority(
        authority=narrowed,
        brief_grants=frozenset({"brief-read"}),
        followup_grants=frozenset({"followup-read"}),
    )
    followups = _FakeFollowups(items=(DUE_ITEM,))
    freshness = _FakeFreshness((STALE_FAMILY,))
    tasks = _tasks(tmp_path, followups=followups, freshness=freshness)
    with pytest.raises(BriefError) as outside:
        brief_generate(tasks, session, log=_log(tasks), clock=_Clock(), space_ids=(SPACE_2,))
    assert outside.value.code == "invalid_request"
    brief = brief_generate(tasks, session, log=_log(tasks), clock=_Clock(), space_ids=(SPACE_1,))
    assert len(brief.items) == 2
    authority, space_ids, _days = freshness.calls[-1]
    assert (
        authority.space_ids == frozenset({SPACE_1})
        and authority.owner is False
        and space_ids == frozenset({SPACE_1})
    )
    assert all(call[2].space_ids == frozenset({SPACE_1}) for call in followups.calls)
    unrestricted = brief_generate(
        tasks, WORK_SESSION, log=_log(tasks), clock=_Clock(), space_ids=(SPACE_2,)
    )
    assert freshness.calls[-1][0].space_ids == frozenset({SPACE_2}), (
        "an unrestricted client is narrowed to its selection"
    )
    assert unrestricted.coverage.families_checked == 0


def test_brief_last_is_owner_only_and_marks_unreadable_followups_withheld(tmp_path: Path) -> None:
    followups = _FakeFollowups(items=(DUE_ITEM,))
    tasks = _tasks(tmp_path, followups=followups, freshness=_FakeFreshness((STALE_FAMILY,)))
    log = _log(tasks)
    with pytest.raises(BriefError) as refused:
        brief_last(tasks, WORK_SESSION, log=log)
    assert refused.value.code == "unsupported_capability"
    assert brief_last(tasks, OWNER_SESSION, log=log) is None
    clock = _Clock()
    first = brief_generate(tasks, OWNER_SESSION, log=log, clock=clock)
    clock.tick(days=1)
    second = brief_generate(tasks, OWNER_SESSION, log=log, clock=clock)
    last: Any = brief_last(tasks, OWNER_SESSION, log=log)
    assert last is not None
    assert last["generated_at"] == second.generated_at and last["since"] == first.generated_at
    assert last["model"] == "none" and last["log"] == {
        "entries": 2,
        "bytes": _log_path(tasks, OWNER_SESSION).stat().st_size,
        "unavailable": False,
    }
    assert [(item["ref"], item["status"]) for item in last["items"]] == [
        ("followup:" + _item_id(1), "current"),
        ("family:" + STALE_FAMILY.family_id, "logged"),
    ]
    assert "reason_text" not in last["items"][0] and last["items"][0]["reasons"] == ["overdue"]
    followups.items = (replace(DUE_ITEM, evidence_retracted=True),)
    assert brief_last(tasks, OWNER_SESSION, log=log)["items"][0]["status"] == "withheld"  # type: ignore[index]
    followups.hidden = frozenset({_item_id(1)})
    assert brief_last(tasks, OWNER_SESSION, log=log)["items"][0]["status"] == "withheld"  # type: ignore[index]

    def failing_show(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("locked")

    # The dated brief stays readable when follow-up reads fail, busy engine included.
    tasks.followups = SimpleNamespace(show=failing_show)
    assert brief_last(tasks, OWNER_SESSION, log=log)["items"][0]["status"] == "withheld"  # type: ignore[index]
    assert brief_last(tasks, OWNER_SESSION, log=log, space_ids=(SPACE_1,)) is None


def test_evidence_is_capped_per_item_and_the_wire_fits_the_response_cap(tmp_path: Path) -> None:
    views = tuple(
        _View(_evidence_id(index), "capture_a") for index in range(MAX_EVIDENCE_PER_ITEM + 20)
    )
    tasks = _tasks(
        tmp_path, followups=_FakeFollowups(items=(DUE_ITEM,), views={_item_id(1): views})
    )
    brief = brief_generate(tasks, OWNER_SESSION, log=_log(tasks), clock=_Clock())
    assert len(brief.items[0].evidence) == MAX_EVIDENCE_PER_ITEM and brief.saved is True
    huge = Brief(
        generated_at="2026-10-24T09:00:00Z",
        scope=BriefScope(space_ids=None, tiers=("work",)),
        items=tuple(
            BriefItem(
                kind=BriefItemKind.FOLLOWUP_DUE,
                ref="followup:" + _item_id(rank),
                reasons=(BriefReasonCode.OVERDUE,),
                reason_text="x.",
                evidence=tuple(_evidence_id(index) for index in range(400)),
                changed=BriefChange.NEW,
                rank=rank,
            )
            for rank in range(1, 6)
        ),
        nothing_needs_attention=False,
        since=None,
        coverage=BriefCoverage(
            families_checked=0,
            families_stale=0,
            captures_unavailable=0,
            followups_scanned=5,
            followups_truncated=False,
            followups="read",
            freshness="read",
            collector="not_read",
            hidden_dropped=0,
            log=BriefLogCoverage(entries=0, bytes=0, unavailable=False),
        ),
        model="none",
        saved=False,
    )
    assert len(json.dumps(huge.to_wire())) > BRIEF_RESPONSE_MAX_BYTES
    fitted = _fit_response(huge)
    assert len(json.dumps(fitted.to_wire(), separators=(",", ":"))) <= BRIEF_RESPONSE_MAX_BYTES
    assert len(fitted.items) == 5 and all(item.evidence for item in fitted.items)
    assert (
        max(len(item.evidence) for item in fitted.items)
        - min(len(item.evidence) for item in fitted.items)
        <= 1
    )


def test_bound_operations_validate_shapes_and_map_refusals(tmp_path: Path) -> None:
    tasks = _tasks(tmp_path, freshness=_FakeFreshness((STALE_FAMILY,)))
    bound = bind_brief_operations(tasks, WORK_SESSION, log=_log(tasks), clock=_Clock())
    assert set(bound) == {"brief", "ack"}
    result = bound["brief"]({})
    assert result["outcome"] == "ok" and result["code"] is None
    data: Any = result["data"]
    assert data["model"] == "none" and data["items"][0]["ref"] == "family:" + STALE_FAMILY.family_id
    assert data["coverage"]["followups"] == "read" and data["scope"]["tiers"] == ["work"]
    scoped: Any = bound["brief"]({"space_ids": [SPACE_2], "stale_days": 30})["data"]
    assert scoped["coverage"]["families_checked"] == 0
    unknown = "family:consumer:synthetic/unknown:" + SPACE_1
    assert bound["ack"]({"family_ref": unknown}) == {
        "outcome": "refused",
        "data": None,
        "code": "not_found",
    }
    acked: Any = bound["ack"]({"family_ref": "family:" + STALE_FAMILY.family_id})
    assert acked["outcome"] == "ok" and acked["data"]["fingerprint"]["unavailable_count"] == 0
    after: Any = bound["brief"]({})["data"]
    assert after["nothing_needs_attention"] is True
    for operation, arguments in (
        ("brief", {"unknown": 1}),
        ("brief", {"space_ids": []}),
        ("brief", {"space_ids": ["not-a-space"]}),
        ("brief", {"space_ids": [SPACE_1, SPACE_1]}),
        ("brief", {"space_ids": [SPACE_1] * 33}),
        ("brief", {"stale_days": 0}),
        ("brief", {"stale_days": 366}),
        ("brief", {"stale_days": True}),
        ("brief", {"stale_days": "7"}),
        ("ack", {}),
        ("ack", {"family_ref": "followup:x"}),
        ("ack", {"family_ref": "family:"}),
        ("ack", {"family_ref": "family: leading-space"}),
        ("ack", {"family_ref": "family:" + "x" * 2201}),
        ("ack", {"family_ref": "family:x", "extra": 1}),
        ("unknown", {}),
    ):
        with pytest.raises(BriefServiceError, match="^invalid_arguments$"):
            validate_brief_arguments(operation, arguments)
        if operation in bound:
            with pytest.raises(BriefServiceError):
                bound[operation](arguments)


def test_family_ids_up_to_the_contract_bound_are_logged_and_acked(tmp_path: Path) -> None:
    # Connector names and connection IDs are any non-control text up to 1024 characters each,
    # so a family ID can be long and non-ASCII; the contract admits it, so the log must too.
    family = _Family(
        "consumer:Équipe " + "n" * 300 + "/conn:" + SPACE_1,
        SPACE_1,
        "2026-10-01T09:00:00Z",
        0,
        True,
    )
    tasks = _tasks(tmp_path, freshness=_FakeFreshness((family,)))
    log = _log(tasks)
    brief = brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock())
    assert [item.ref for item in brief.items] == ["family:" + family.family_id]
    assert brief.saved is True
    validate_brief_arguments("ack", {"family_ref": "family:" + family.family_id})
    acked = brief_ack(tasks, OWNER_SESSION, log=log, family_ref="family:" + family.family_id)
    assert acked["family_ref"] == "family:" + family.family_id
    assert brief_generate(tasks, OWNER_SESSION, log=log, clock=_Clock()).items == ()
